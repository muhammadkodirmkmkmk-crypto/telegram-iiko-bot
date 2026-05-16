"""
Telegram Group Monitor Bot

Мониторит группу, анализирует текст и фото через Claude AI,
при необходимости ищет контекст в документации iiko,
и отправляет предложенный ответ всем владельцам на одобрение.
"""

import asyncio
import base64
import logging
import os
import re
import threading
import requests
from flask import Flask
from bs4 import BeautifulSoup
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]

_ids_raw = os.environ.get("OWNER_TELEGRAM_IDS", os.environ.get("OWNER_TELEGRAM_ID", ""))
OWNER_IDS: list[int] = [int(x.strip()) for x in _ids_raw.split(",") if x.strip()]

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-5"

WAITING_FOR_CUSTOM_REPLY = 1


KEEP_ALIVE_PORT = 8082

_flask_app = Flask(__name__)
_flask_app.logger.disabled = True
logging.getLogger("werkzeug").setLevel(logging.ERROR)


@_flask_app.route("/")
def _index():
    return "Bot is running"


@_flask_app.route("/ping")
def _ping():
    return "OK"


def start_keep_alive():
    t = threading.Thread(
        target=lambda: _flask_app.run(host="0.0.0.0", port=KEEP_ALIVE_PORT),
        daemon=True,
    )
    t.start()
    logger.info("Keep-alive Flask server started on port %s", KEEP_ALIVE_PORT)

pending: dict[str, dict] = {}
last_bot_answers: dict[str, str] = {}


async def send_to_group(bot, chat_id: int, reply_to_id: int, text: str) -> None:
    """Send answer to group without buttons. Falls back to plain text if HTML fails."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_id,
            parse_mode="HTML",
        )
        logger.info("Ответ отправлен в группу %s (HTML, reply_to=%s)", chat_id, reply_to_id)
    except Exception as html_err:
        logger.warning("HTML-ошибка при отправке в группу, пробую plain text: %s", html_err)
        plain = re.sub(r"<[^>]+>", "", text)
        await bot.send_message(
            chat_id=chat_id,
            text=plain,
            reply_to_message_id=reply_to_id,
        )
        logger.info("Ответ отправлен в группу %s (plain text, reply_to=%s)", chat_id, reply_to_id)


def generate_deeper_answer(previous_answer: str) -> str:
    system_prompt = (
        "Ты — умный универсальный помощник в Telegram-группе, связанной с ресторанным бизнесом и ПО iiko. "
        "Отвечай на русском языке. "
        "Расширь, углуби и детализируй предоставленный ответ: добавь примеры, подробности, нюансы, "
        "практические советы. Сделай ответ более полным и информативным.\n\n"
        "ВАЖНО: Форматируй ответ используя HTML-теги Telegram: "
        "<b>жирный</b>, <i>курсив</i>, <code>код</code>, <pre>блок кода</pre>. "
        "НЕ используй Markdown. Только HTML-теги или обычный текст."
    )
    user_prompt = (
        f"Вот предыдущий ответ:\n\n{previous_answer}\n\n"
        "Расширь и углуби его — добавь примеры, подробности, нюансы и практические советы."
    )
    return call_claude([{"role": "user", "content": user_prompt}], system_prompt)


def make_pending_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def is_iiko_related(text: str) -> bool:
    iiko_keywords = [
        "iiko", "iiко", "ико", "касса", "кассир", "официант", "меню", "стол",
        "заказ", "чек", "оплата", "скидка", "депозит", "бонус", "лояльность",
        "склад", "накладная", "инвентаризация", "поставщик", "блюдо", "рецептура",
        "модификатор", "стоп-лист", "банкет", "доставка", "возврат", "смена",
        "отчёт", "выручка", "ресторан", "кафе", "бар", "кухня", "терминал",
        "фронт", "бэк", "офис", "сервер", "лицензия", "техподдержка",
    ]
    text_lower = text.lower()
    return any(kw in text_lower for kw in iiko_keywords)


def search_iiko_help(query: str) -> list[dict]:
    try:
        search_url = "https://ru.iiko.help/search"
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }
        resp = requests.get(search_url, params={"query": query}, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        results = []
        for item in soup.select("a.search-result, .search-results li a, article a, .article-list a, h3 a, h2 a")[:5]:
            href = item.get("href", "")
            title = item.get_text(strip=True)
            if not href or not title or len(title) < 5:
                continue
            if not href.startswith("http"):
                href = "https://ru.iiko.help" + href
            results.append({"title": title, "url": href, "snippet": ""})

        if not results:
            for link in soup.find_all("a", href=True)[:20]:
                href = link["href"]
                title = link.get_text(strip=True)
                if (
                    "/articles/" in href or "/help/" in href or
                    any(word.lower() in title.lower() for word in query.split() if len(word) > 3)
                ) and len(title) > 5:
                    if not href.startswith("http"):
                        href = "https://ru.iiko.help" + href
                    results.append({"title": title, "url": href, "snippet": ""})
                    if len(results) >= 3:
                        break

        for item in results[:3]:
            try:
                page = requests.get(item["url"], headers=headers, timeout=8)
                page.raise_for_status()
                page_soup = BeautifulSoup(page.text, "lxml")
                for tag in page_soup(["script", "style", "nav", "header", "footer"]):
                    tag.decompose()
                main = page_soup.select_one("article, .article-body, main, .content, #content")
                text = (main or page_soup).get_text(separator=" ", strip=True)
                text = re.sub(r"\s+", " ", text)
                item["snippet"] = text[:2000]
            except Exception as e:
                logger.warning("Не удалось получить страницу %s: %s", item["url"], e)

        return results
    except Exception as e:
        logger.error("Ошибка поиска на iiko.help: %s", e)
        return []


def download_image_as_base64(file_url: str) -> tuple[str, str] | None:
    try:
        resp = requests.get(file_url, timeout=15)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if content_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            content_type = "image/jpeg"
        b64 = base64.standard_b64encode(resp.content).decode("utf-8")
        return b64, content_type
    except Exception as e:
        logger.error("Ошибка загрузки изображения: %s", e)
        return None


def call_claude(messages_payload: list, system_prompt: str) -> str:
    try:
        headers = {
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": messages_payload,
        }
        resp = requests.post(CLAUDE_API_URL, headers=headers, json=payload, timeout=40)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()
    except Exception as e:
        logger.error("Ошибка Claude API: %s — %s", type(e).__name__, e)
        return "Не удалось сгенерировать ответ через AI."


def generate_text_answer(question: str, iiko_results: list[dict]) -> str:
    system_prompt = (
        "Ты — умный универсальный помощник в Telegram-группе, связанной с ресторанным бизнесом и ПО iiko. "
        "Отвечай на русском языке, кратко, понятно и по делу. "
        "Ты можешь отвечать на ЛЮБЫЕ вопросы: про iiko, ресторанный бизнес, технические проблемы, "
        "общие вопросы, советы и всё остальное. "
        "Если есть документация iiko — используй её как дополнительный контекст. "
        "Если вопрос не связан с iiko — просто дай полезный ответ. "
        "Не придумывай несуществующие функции iiko. Будь дружелюбным и профессиональным.\n\n"
        "ВАЖНО: Форматируй ответ используя HTML-теги Telegram: "
        "<b>жирный</b>, <i>курсив</i>, <code>код</code>, <pre>блок кода</pre>. "
        "НЕ используй Markdown (*звёздочки*, _подчёркивания_, `backticks`). "
        "Только HTML-теги или обычный текст без разметки."
    )
    context_block = ""
    if iiko_results:
        parts = [
            f"Статья {i}: {r['title']}\nURL: {r['url']}\n{r['snippet'][:1500]}"
            for i, r in enumerate(iiko_results, 1) if r.get("snippet")
        ]
        if parts:
            context_block = "\n\nКонтекст из документации iiko:\n" + "\n\n---\n\n".join(parts)
    user_prompt = f"Вопрос: {question}{context_block}\n\nДай чёткий и полезный ответ."
    return call_claude([{"role": "user", "content": user_prompt}], system_prompt)


def generate_photo_answer(caption: str, image_b64: str, media_type: str, iiko_results: list[dict]) -> str:
    system_prompt = (
        "Ты — умный универсальный помощник в Telegram-группе, связанной с ресторанным бизнесом и ПО iiko. "
        "Отвечай на русском языке, кратко и по делу. "
        "Пользователь прислал фотографию. Внимательно проанализируй её: "
        "это может быть скриншот ошибки, интерфейса программы, чека, оборудования или чего угодно другого. "
        "Определи проблему или содержание и дай конкретное решение или объяснение. "
        "Если есть текстовый комментарий к фото — учти его. "
        "Будь дружелюбным и профессиональным.\n\n"
        "ВАЖНО: Форматируй ответ используя HTML-теги Telegram: "
        "<b>жирный</b>, <i>курсив</i>, <code>код</code>, <pre>блок кода</pre>. "
        "НЕ используй Markdown (*звёздочки*, _подчёркивания_, `backticks`). "
        "Только HTML-теги или обычный текст без разметки."
    )
    context_block = ""
    if iiko_results:
        parts = [f"Статья {i}: {r['title']}\n{r['snippet'][:1000]}" for i, r in enumerate(iiko_results, 1) if r.get("snippet")]
        if parts:
            context_block = "\n\nКонтекст из документации iiko:\n" + "\n\n---\n\n".join(parts)

    text_part = caption or "Пользователь прислал фото без подписи. Проанализируй, что на нём изображено, и дай полезный комментарий."
    if context_block:
        text_part += context_block

    return call_claude(
        [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": text_part},
        ]}],
        system_prompt,
    )


async def notify_other_owners(context: ContextTypes.DEFAULT_TYPE, key: str, acting_owner_id: int, status_text: str) -> None:
    """Обновляет сообщение у других владельцев, что вопрос уже обработан."""
    info = pending.get(key)
    if not info:
        return
    owner_msg_ids: dict[int, int] = info.get("owner_msg_ids", {})
    for owner_id, msg_id in owner_msg_ids.items():
        if owner_id == acting_owner_id:
            continue
        try:
            if info.get("has_photo"):
                await context.bot.edit_message_caption(
                    chat_id=owner_id,
                    message_id=msg_id,
                    caption=status_text,
                    parse_mode="HTML",
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=owner_id,
                    message_id=msg_id,
                    text=status_text,
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.warning("Не удалось обновить сообщение у владельца %s: %s", owner_id, e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id in OWNER_IDS:
        await update.message.reply_text(
            "✅ Бот запущен и работает.\n\n"
            f"Владельцы: {len(OWNER_IDS)} чел.\n"
            "Я буду пересылать сюда все сообщения из групп с AI-ответом.\n"
            "• Текстовые вопросы — отвечаю на любые\n"
            "• Фотографии — анализирую и объясняю проблему\n\n"
            "Нажмите «Отправить ответ», «Редактировать» или «Отклонить».\n"
            "Если другой владелец уже ответил — кнопки станут неактивными."
        )
    else:
        await update.message.reply_text(
            "Этот бот помогает модерировать вопросы в группе. Обратитесь к администратору."
        )


async def forward_to_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return
    if chat.type not in ("group", "supergroup"):
        return
    if user.id in OWNER_IDS:
        return

    chat_id = chat.id
    message_id = message.message_id
    key = make_pending_key(chat_id, message_id)

    question_text = message.text or message.caption or ""
    has_photo = bool(message.photo)

    if not question_text.strip() and not has_photo:
        return

    # Отвечаем только на вопросы и iiko-тематику, игнорируем номера и прочее
    if not has_photo:
        text = question_text.strip()
        is_question = "?" in text
        is_relevant = is_iiko_related(text)
        # Минимальная длина — не реагируем на очень короткие сообщения (номера, «да», «нет» и т.д.)
        is_long_enough = len(text) >= 15
        if not (is_question or is_relevant) or not is_long_enough:
            logger.debug("Пропускаю нерелевантное сообщение: %r", text[:60])
            return

    pending[key] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "chat_title": chat.title or str(chat_id),
        "user_name": user.full_name,
        "username": f"@{user.username}" if user.username else "",
        "text": question_text,
        "has_photo": has_photo,
        "ai_answer": "",
        "iiko_urls": [],
        "handled": False,
        "handled_by": None,
        "owner_msg_ids": {},
    }

    sender = pending[key]["user_name"]
    if pending[key]["username"]:
        sender += f" ({pending[key]['username']})"

    msg_type = "фото" if has_photo else "вопрос"
    for owner_id in OWNER_IDS:
        try:
            await context.bot.send_message(
                chat_id=owner_id,
                text=(
                    f"⏳ Получен {msg_type} от {sender} в <b>{pending[key]['chat_title']}</b>.\n"
                    "Анализирую и генерирую AI-ответ…"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Не удалось уведомить владельца %s: %s", owner_id, e)

    iiko_results = []
    if question_text.strip() and is_iiko_related(question_text):
        iiko_results = search_iiko_help(question_text)
        pending[key]["iiko_urls"] = [r["url"] for r in iiko_results if r.get("url")]

    if has_photo:
        photo = message.photo[-1]
        try:
            photo_file = await context.bot.get_file(photo.file_id)
            img_data = download_image_as_base64(photo_file.file_path)
            if img_data:
                image_b64, media_type = img_data
                ai_answer = generate_photo_answer(question_text, image_b64, media_type, iiko_results)
            else:
                ai_answer = "Не удалось загрузить изображение для анализа."
        except Exception as e:
            logger.error("Ошибка при получении фото: %s", e)
            ai_answer = "Не удалось обработать изображение."
    else:
        ai_answer = generate_text_answer(question_text, iiko_results)

    pending[key]["ai_answer"] = ai_answer

    preview = question_text[:300] + ("…" if len(question_text) > 300 else "")
    _plain_answer = re.sub(r"<[^>]+>", "", ai_answer)
    answer_preview = _plain_answer[:500] + ("…" if len(_plain_answer) > 500 else "")

    sources_block = ""
    if iiko_results:
        links = "\n".join(
            f'• <a href="{r["url"]}">{r["title"][:60]}</a>'
            for r in iiko_results[:3] if r.get("url") and r.get("title")
        )
        if links:
            sources_block = f"\n\n📚 <b>Источники iiko:</b>\n{links}"

    photo_icon = "🖼 " if has_photo else ""
    question_label = "Подпись к фото" if (has_photo and question_text) else ("Фото без подписи" if has_photo else "Вопрос")
    question_block = f"\n\n❓ <b>{question_label}:</b>\n<blockquote>{preview}</blockquote>" if preview else ""

    caption = (
        f"📨 <b>Новое сообщение</b> {photo_icon}в <b>{pending[key]['chat_title']}</b>\n"
        f"👤 От: {sender}"
        f"{question_block}\n\n"
        f"🤖 <b>Предложенный ответ AI:</b>\n<blockquote>{answer_preview}</blockquote>"
        f"{sources_block}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Отправить ответ", callback_data=f"send:{key}"),
            InlineKeyboardButton("🔍 Углубить ответ", callback_data=f"deepen:{key}"),
        ],
        [
            InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit:{key}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{key}"),
        ],
    ])

    for owner_id in OWNER_IDS:
        try:
            if has_photo:
                sent = await context.bot.send_photo(
                    chat_id=owner_id,
                    photo=message.photo[-1].file_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            else:
                sent = await context.bot.send_message(
                    chat_id=owner_id,
                    text=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            pending[key]["owner_msg_ids"][owner_id] = sent.message_id
        except Exception as e:
            logger.warning("Не удалось отправить сообщение владельцу %s: %s", owner_id, e)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not query.data:
        return ConversationHandler.END

    parts = query.data.split(":", 1)
    if len(parts) != 2:
        return ConversationHandler.END
    action, key = parts

    acting_owner = update.effective_user.id
    info = pending.get(key)

    if not info:
        await query.edit_message_text("⚠️ Это сообщение уже недоступно.")
        return ConversationHandler.END

    if info.get("handled") and action != "deepen":
        handler_id = info.get("handled_by")
        handler_note = f" (владельцем {handler_id})" if handler_id and handler_id != acting_owner else ""
        await query.answer(f"⚠️ Уже обработано{handler_note}.", show_alert=True)
        return ConversationHandler.END

    if action == "reject":
        info["handled"] = True
        info["handled_by"] = acting_owner
        done_text = f"❌ Сообщение отклонено."
        if info.get("has_photo"):
            await query.edit_message_caption(caption=done_text)
        else:
            await query.edit_message_text(done_text)
        await notify_other_owners(context, key, acting_owner, f"❌ Сообщение отклонено другим владельцем.")
        pending.pop(key, None)
        return ConversationHandler.END

    if action == "send":
        info["handled"] = True
        info["handled_by"] = acting_owner
        try:
            ans_key = f"{info['chat_id']}:{info['message_id']}"
            last_bot_answers[ans_key] = info["ai_answer"]
            await send_to_group(context.bot, info["chat_id"], info["message_id"], info["ai_answer"])
            done_text = f"✅ AI-ответ отправлен в <b>{info['chat_title']}</b>."
            if info.get("has_photo"):
                await query.edit_message_caption(caption=done_text, parse_mode="HTML")
            else:
                await query.edit_message_text(done_text, parse_mode="HTML")
            await notify_other_owners(context, key, acting_owner, f"✅ Ответ уже отправлен другим владельцем в <b>{info['chat_title']}</b>.")
        except Exception as e:
            logger.error("Ошибка отправки ответа: %s", e)
            await query.edit_message_text(f"⚠️ Не удалось отправить ответ: {e}")
        pending.pop(key, None)
        return ConversationHandler.END

    if action == "deepen":
        await query.answer("🔍 Генерирую углублённый ответ…")
        # Показываем статус у всех владельцев пока генерируем
        owner_msg_ids: dict = info.get("owner_msg_ids", {})
        for oid, mid in owner_msg_ids.items():
            try:
                if info.get("has_photo"):
                    await context.bot.edit_message_caption(
                        chat_id=oid, message_id=mid, caption="🔍 Генерирую углублённый ответ…"
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=oid, message_id=mid, text="🔍 Генерирую углублённый ответ…"
                    )
            except Exception:
                pass

        deep_answer = await asyncio.get_event_loop().run_in_executor(
            None, generate_deeper_answer, info["ai_answer"]
        )
        info["ai_answer"] = deep_answer

        # Пересобираем превью и клавиатуру с обновлённым ответом
        _plain = re.sub(r"<[^>]+>", "", deep_answer)
        answer_preview = _plain[:500] + ("…" if len(_plain) > 500 else "")
        sender = info["user_name"]
        if info.get("username"):
            sender += f" ({info['username']})"
        preview = info["text"][:300] + ("…" if len(info["text"]) > 300 else "")
        question_block = f"\n\n❓ <b>Вопрос:</b>\n<blockquote>{preview}</blockquote>" if preview else ""
        iiko_results = [{"url": u, "title": u} for u in info.get("iiko_urls", [])]
        sources_block = ""
        if iiko_results:
            links = "\n".join(f'• <a href="{r["url"]}">{r["url"][:60]}</a>' for r in iiko_results[:3])
            sources_block = f"\n\n📚 <b>Источники iiko:</b>\n{links}"
        photo_icon = "🖼 " if info.get("has_photo") else ""
        new_caption = (
            f"📨 <b>Новое сообщение</b> {photo_icon}в <b>{info['chat_title']}</b>\n"
            f"👤 От: {sender}"
            f"{question_block}\n\n"
            f"🤖 <b>Углублённый ответ AI:</b>\n<blockquote>{answer_preview}</blockquote>"
            f"{sources_block}"
        )
        new_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Отправить ответ", callback_data=f"send:{key}"),
                InlineKeyboardButton("🔍 Углубить ответ", callback_data=f"deepen:{key}"),
            ],
            [
                InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit:{key}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{key}"),
            ],
        ])
        for oid, mid in owner_msg_ids.items():
            try:
                if info.get("has_photo"):
                    await context.bot.edit_message_caption(
                        chat_id=oid, message_id=mid,
                        caption=new_caption, parse_mode="HTML", reply_markup=new_keyboard,
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=oid, message_id=mid,
                        text=new_caption, parse_mode="HTML",
                        reply_markup=new_keyboard, disable_web_page_preview=True,
                    )
            except Exception as e:
                logger.warning("Не удалось обновить углублённый ответ у владельца %s: %s", oid, e)
        return ConversationHandler.END

    if action == "edit":
        context.user_data["pending_key"] = key
        sender = info["user_name"]
        if info.get("username"):
            sender += f" ({info['username']})"
        ai_preview = re.sub(r"<[^>]+>", "", info["ai_answer"])
        prompt_text = (
            f"✏️ <b>Редактирование ответа</b> для {sender} в <b>{info['chat_title']}</b>.\n\n"
            f"Текущий текст (скопируйте и измените):\n<blockquote>{ai_preview[:800]}</blockquote>\n\n"
            "Напишите свой вариант ответа:"
        )
        try:
            await context.bot.send_message(
                chat_id=acting_owner,
                text=prompt_text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Ошибка отправки запроса на редактирование: %s", e)
            await query.answer("⚠️ Не удалось открыть редактор.", show_alert=True)
            return ConversationHandler.END
        return WAITING_FOR_CUSTOM_REPLY

    return ConversationHandler.END


async def receive_custom_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reply_text = update.message.text
    acting_owner = update.effective_user.id
    key = context.user_data.pop("pending_key", None)

    if not key:
        await update.message.reply_text("⚠️ Нет активного вопроса для редактирования.")
        return ConversationHandler.END

    info = pending.get(key)
    if not info:
        await update.message.reply_text("⚠️ Исходное сообщение больше недоступно.")
        return ConversationHandler.END

    if info.get("handled"):
        await update.message.reply_text("⚠️ Это сообщение уже было обработано другим владельцем.")
        return ConversationHandler.END

    # Обновляем текст ответа и показываем всем владельцам с теми же 4 кнопками
    info["ai_answer"] = reply_text

    _plain = re.sub(r"<[^>]+>", "", reply_text)
    answer_preview = _plain[:500] + ("…" if len(_plain) > 500 else "")
    sender = info["user_name"]
    if info.get("username"):
        sender += f" ({info['username']})"
    preview = info["text"][:300] + ("…" if len(info["text"]) > 300 else "")
    question_block = f"\n\n❓ <b>Вопрос:</b>\n<blockquote>{preview}</blockquote>" if preview else ""
    iiko_urls = info.get("iiko_urls", [])
    sources_block = ""
    if iiko_urls:
        links = "\n".join(f'• <a href="{u}">{u[:60]}</a>' for u in iiko_urls[:3])
        sources_block = f"\n\n📚 <b>Источники iiko:</b>\n{links}"
    photo_icon = "🖼 " if info.get("has_photo") else ""
    new_caption = (
        f"📨 <b>Новое сообщение</b> {photo_icon}в <b>{info['chat_title']}</b>\n"
        f"👤 От: {sender}"
        f"{question_block}\n\n"
        f"✏️ <b>Отредактированный ответ:</b>\n<blockquote>{answer_preview}</blockquote>"
        f"{sources_block}"
    )
    new_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Отправить ответ", callback_data=f"send:{key}"),
            InlineKeyboardButton("🔍 Углубить ответ", callback_data=f"deepen:{key}"),
        ],
        [
            InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit:{key}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{key}"),
        ],
    ])
    owner_msg_ids: dict = info.get("owner_msg_ids", {})
    for oid, mid in owner_msg_ids.items():
        try:
            if info.get("has_photo"):
                await context.bot.edit_message_caption(
                    chat_id=oid, message_id=mid,
                    caption=new_caption, parse_mode="HTML", reply_markup=new_keyboard,
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=oid, message_id=mid,
                    text=new_caption, parse_mode="HTML",
                    reply_markup=new_keyboard, disable_web_page_preview=True,
                )
        except Exception as e:
            logger.warning("Не удалось обновить отредактированный ответ у владельца %s: %s", oid, e)

    await update.message.reply_text("✅ Ответ обновлён. Теперь вы можете отправить его, углубить или отклонить.")
    return ConversationHandler.END


async def group_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not query.data:
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return

    action, chat_id_str, message_id_str = parts
    ans_key = f"{chat_id_str}:{message_id_str}"
    answer_text = last_bot_answers.get(ans_key)

    if not answer_text:
        await query.answer("⚠️ Ответ больше недоступен.", show_alert=True)
        return

    chat_id = int(chat_id_str)
    message_id = int(message_id_str)

    if action == "grp_edit":
        plain = re.sub(r"<[^>]+>", "", answer_text)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📋 <b>Текст ответа для редактирования:</b>\n\n{plain}",
            reply_to_message_id=message_id,
            parse_mode="HTML",
        )

    elif action == "grp_deep":
        placeholder = await context.bot.send_message(
            chat_id=chat_id,
            text="🔍 Генерирую углублённый ответ…",
            reply_to_message_id=message_id,
        )
        deep_answer = generate_deeper_answer(answer_text)
        new_ans_key = f"{chat_id}:{placeholder.message_id}"
        last_bot_answers[new_ans_key] = deep_answer
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=placeholder.message_id,
            text=deep_answer,
            parse_mode="HTML",
            reply_markup=make_group_keyboard(chat_id, placeholder.message_id),
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("pending_key", None)
    await update.message.reply_text("❌ Действие отменено.")
    return ConversationHandler.END


def main() -> None:
    if not OWNER_IDS:
        raise RuntimeError("Не задан ни один OWNER_TELEGRAM_IDS или OWNER_TELEGRAM_ID")

    start_keep_alive()
    logger.info("Владельцы бота: %s", OWNER_IDS)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    approval_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_callback, pattern=r"^(send|deepen|edit|reject):")],
        states={
            WAITING_FOR_CUSTOM_REPLY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.User(OWNER_IDS),
                    receive_custom_reply,
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=False,
        per_message=False,
    )
    app.add_handler(approval_conv)

    app.add_handler(CallbackQueryHandler(group_button_callback, pattern=r"^grp_(edit|deep):"))

    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & (filters.TEXT | filters.PHOTO | filters.CAPTION),
            forward_to_owner,
        )
    )

    logger.info("Бот запущен. Ожидаю сообщения и фото из групп…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
