import os
import random
import asyncio
import logging
import time
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import anthropic
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict as TelegramConflict
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    MessageHandler, filters, ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
YOUR_PERSONAL_ID = int(os.environ["YOUR_PERSONAL_ID"])
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
CHANNEL_USERNAME = "@zetta_uzbekistan"
APPROVAL_GROUP_ID = -5160536788
GROUP_APPROVALS_NEEDED = 2

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# pending_posts[post_id] = {
#   "text": str | None,
#   "photo_url": str | None,
#   "stage": "type_select" | "owner" | "group",
#   "post_type": "news" | "lifehack" | "deepdive" | None,
#   "group_approvals": set[int],   # user_ids of group members who approved
#   "group_message_id": int | None,
# }
pending_posts: dict[str, dict] = {}

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
SCRAPE_TIMEOUT = 5

# ---------------------------------------------------------------------------
# Topic-matched photo pools (Unsplash CDN — no auth needed)
# ---------------------------------------------------------------------------
PHOTOS_NEWS = [
    "https://images.unsplash.com/photo-1414235077428-338989a2e8c0?w=900&q=85",
    "https://images.unsplash.com/photo-1555396273-367ea4eb4db5?w=900&q=85",
    "https://images.unsplash.com/photo-1466978913421-dad2ebd01d17?w=900&q=85",
    "https://images.unsplash.com/photo-1537047902294-62a40c20a6ae?w=900&q=85",
    "https://images.unsplash.com/photo-1600891964599-f61ba0e24092?w=900&q=85",
    "https://images.unsplash.com/photo-1565299624946-b28f40a0ae38?w=900&q=85",
    "https://images.unsplash.com/photo-1568901346375-23c9450c58cd?w=900&q=85",
    "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?w=900&q=85",
    "https://images.unsplash.com/photo-1476224203421-9ac39bcb3df1?w=900&q=85",
    "https://images.unsplash.com/photo-1543353071-873f17a7a088?w=900&q=85",
]

PHOTOS_LIFEHACK = [
    "https://images.unsplash.com/photo-1563013544-824ae1b704d3?w=900&q=85",
    "https://images.unsplash.com/photo-1556742049-0cfed4f6a45d?w=900&q=85",
    "https://images.unsplash.com/photo-1556742393-d75f468bfcb0?w=900&q=85",
    "https://images.unsplash.com/photo-1587614382346-4ec70e388b28?w=900&q=85",
    "https://images.unsplash.com/photo-1611532736597-de2d4265fba3?w=900&q=85",
    "https://images.unsplash.com/photo-1460925895917-afdab827c52f?w=900&q=85",
    "https://images.unsplash.com/photo-1499028344343-cd173ffc68a9?w=900&q=85",
    "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?w=900&q=85",
    "https://images.unsplash.com/photo-1552566626-52f8b828add9?w=900&q=85",
    "https://images.unsplash.com/photo-1572116469696-31de0f17cc34?w=900&q=85",
]

PHOTOS_DEEPDIVE = [
    "https://images.unsplash.com/photo-1556909114-f6e7ad7d3136?w=900&q=85",
    "https://images.unsplash.com/photo-1581349485608-9469926a8e5e?w=900&q=85",
    "https://images.unsplash.com/photo-1607631568010-a87245c0daf8?w=900&q=85",
    "https://images.unsplash.com/photo-1600565193348-f74bd3960996?w=900&q=85",
    "https://images.unsplash.com/photo-1593759608142-e976bdf5d2b7?w=900&q=85",
    "https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=900&q=85",
    "https://images.unsplash.com/photo-1490645935967-10de6ba17061?w=900&q=85",
    "https://images.unsplash.com/photo-1484723091739-30a097e8f929?w=900&q=85",
    "https://images.unsplash.com/photo-1455619452474-d2be8b1e70cd?w=900&q=85",
    "https://images.unsplash.com/photo-1559329007-40df8a9345d8?w=900&q=85",
]

# Topic keyword map: topic substring → search query for photo APIs
TOPIC_PHOTO_KEYWORDS: list[tuple[str, str]] = [
    ("food cost",     "restaurant kitchen cost"),
    ("себестоимост",  "restaurant kitchen cost"),
    ("техкарт",       "restaurant kitchen recipe"),
    ("ABC",           "restaurant analytics data"),
    ("аналитик",      "restaurant analytics data"),
    ("инвентариза",   "warehouse food inventory"),
    ("склад",         "warehouse food storage"),
    ("KDS",           "restaurant kitchen display screen"),
    ("кухн",          "restaurant kitchen chef"),
    ("официант",      "restaurant waiter service"),
    ("стоп-лист",     "restaurant menu tablet"),
    ("модификатор",   "restaurant pos tablet"),
    ("доставк",       "food delivery courier"),
    ("лояльност",     "restaurant loyalty customer"),
    ("смен",          "restaurant manager shift"),
    ("персонал",      "restaurant team staff"),
    ("меню",          "restaurant menu design"),
    ("стол",          "restaurant table dining"),
    ("банкет",        "restaurant banquet event"),
    ("автоматиза",    "restaurant automation technology"),
]

TYPE_DEFAULT_QUERY = {
    "news":     "restaurant industry news",
    "lifehack": "restaurant pos system tablet",
    "deepdive": "restaurant kitchen management",
}


def _topic_to_query(topic: str, post_type: str) -> str:
    topic_lower = topic.lower()
    for keyword, query in TOPIC_PHOTO_KEYWORDS:
        if keyword.lower() in topic_lower:
            return query
    return TYPE_DEFAULT_QUERY.get(post_type, "restaurant")


def _fetch_pexels_photo(query: str) -> str | None:
    if not PEXELS_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "orientation": "landscape", "per_page": 15},
            headers={"Authorization": PEXELS_API_KEY},
            timeout=5,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if photos:
            url = random.choice(photos)["src"]["large"]
            logger.info(f"Pexels photo fetched for '{query}'")
            return url
    except Exception as e:
        logger.warning(f"Pexels failed for '{query}': {e}")
    return None


def pick_photo(post_type: str = "lifehack", topic: str = "") -> str | None:
    """Return a Pexels photo URL matched to topic, or None if unavailable."""
    query = _topic_to_query(topic, post_type)
    return _fetch_pexels_photo(query)


# ---------------------------------------------------------------------------
# Topics — 60 specific Russian iiko topics
# ---------------------------------------------------------------------------
# Topics grouped by feature — 15 equal categories, rotated so the same
# feature is never used twice in a row.
TOPICS_BY_FEATURE: dict[str, list[str]] = {
    "kds": [
        "KDS в iiko: как кухонный экран сокращает время отдачи блюд",
        "Маршрутизация заказов на KDS: какое блюдо на какой экран",
        "Цветовая индикация на KDS: как повара видят приоритет без слов",
        "Время приготовления в KDS: как iiko измеряет и зачем это менеджеру",
    ],
    "stoplist": [
        "Стоп-лист в iiko: три способа поставить позицию и чем они отличаются",
        "Автоматический стоп-лист: как склад сам блокирует продажи при нуле остатка",
        "Стоп-лист и официант: как это предотвращает конфликты с гостями",
        "Частичный стоп-лист в iiko: ограничить продажи, но не убирать из меню",
        "Синхронизация стоп-листа с агрегаторами доставки в iiko",
    ],
    "abc_analysis": [
        "ABC-анализ меню в iiko: какие блюда тянут прибыль вниз незаметно",
        "Матрица меню iiko: звёзды, рабочие лошадки, загадки и балласт",
        "ABC по выручке vs ABC по прибыли — почему результаты разные",
        "Как часто делать ABC-анализ и когда менять меню на основе данных",
        "Инжиниринг меню через iiko: убрать «собак» и усилить «звёзд»",
    ],
    "food_cost": [
        "Food cost в iiko: как система считает себестоимость блюда в реальном времени",
        "Плановый vs фактический food cost: что значит расхождение больше 3%",
        "Потери при обработке в iiko: почему 1 кг говядины даёт 650 г готового продукта",
        "Полуфабрикаты в iiko: как составной ингредиент снижает ошибки в техкартах",
        "Себестоимость с учётом модификаторов: iiko считает каждый вариант блюда",
    ],
    "modifiers": [
        "Модификаторы в iiko: разница между обязательными и опциональными группами",
        "Как настроить модификаторы так, чтобы официант не пропустил выбор соуса",
        "Модификаторы и себестоимость: почему каждый топпинг должен быть в техкарте",
        "Платные vs бесплатные модификаторы: как iiko считает финальную цену блюда",
        "Комбо и сеты в iiko: настройка и влияние на средний чек",
    ],
    "loyalty": [
        "Программа лояльности в iiko: бонусы, скидки и кешбэк — что выгоднее ресторану",
        "Гостевая база iiko: какие данные собирать и как использовать для возврата гостей",
        "Персональные акции в iiko: настройка скидки на день рождения без ручного труда",
        "RFM-анализ гостей в iiko: как найти «засыпающих» клиентов до ухода",
        "Бонусные баллы в iiko: срок сгорания, минимальное списание и психология удержания",
    ],
    "delivery": [
        "Доставка в iiko: как заказ с агрегатора попадает прямо на кухонный экран",
        "iiko и внешние агрегаторы: синхронизация стоп-листа в реальном времени",
        "Зоны доставки в iiko: привязка к адресу и расчёт стоимости",
        "Статусы доставки в iiko: как повар, упаковщик и курьер видят один заказ",
        "Интеграция iiko с Yandex Go и local-агрегаторами в Узбекистане",
    ],
    "inventory": [
        "Инвентаризация в iiko за 20 минут: правильный порядок зон и сотрудников",
        "Акт списания в iiko: когда списывать и что будет без документа",
        "Пересорт на складе iiko: почему возникает и как ручная коррекция ломает аналитику",
        "Минимальный остаток в iiko: автоуведомления до того, как кончился продукт",
        "Инвентаризация без остановки зала в iiko: лайфхак для занятых ресторанов",
    ],
    "staff": [
        "Рейтинг официантов в iiko: средний чек, количество гостей, скорость",
        "Контроль скидок в iiko: отчёт, который ловит злоупотребления персонала",
        "Права доступа в iiko: почему кассир не должен видеть склад",
        "Табель учёта рабочего времени в iiko: автоматический расчёт по открытию смены",
        "Нарушения кассовой дисциплины: какие действия сотрудника iiko логирует всегда",
        "График персонала в iiko: планирование смен и контроль выхода",
    ],
    "table_management": [
        "Схема зала в iiko: как правильно нарисовать и зачем это влияет на оборот стола",
        "Резервирование столов в iiko: привязка к гостевой базе и история визитов",
        "Перенос заказа между столами в iiko: 30 секунд вместо пересоздания",
        "Банкет в iiko: предзаказ, депозит и разбивка счёта на несколько гостей",
        "Время ожидания у стола в iiko: когда менеджер получает алерт",
    ],
    "financial_reports": [
        "P&L отчёт в iiko: как владелец видит прибыль ресторана за день в реальном времени",
        "Отчёт по выручке в iiko: разбивка по официантам, столам, категориям блюд",
        "Сравнение периодов в iiko: как найти причину падения выручки за неделю",
        "Финансовая аналитика iiko: какие 5 цифр должен смотреть владелец каждое утро",
        "Отчёт по скидкам и промо в iiko: считаем реальную стоимость акций",
    ],
    "waste_tracking": [
        "Учёт списаний в iiko: как фиксировать потери и не терять деньги дважды",
        "Списание по причинам в iiko: порча, проба, брак — и что каждая категория говорит о кухне",
        "Норма потерь в iiko: как установить лимит и получать алерт при превышении",
        "Waste tracking в iiko: связь между списаниями и реальным food cost",
        "Акт переработки в iiko: когда продукт меняет форму и как это учесть",
    ],
    "recipe_costing": [
        "Технологическая карта в iiko: пошаговое создание и привязка к складу",
        "Техкарта с несколькими единицами измерения в iiko: граммы, штуки, порции",
        "Версионность техкарт в iiko: как менять рецептуру без потери истории",
        "Себестоимость сезонного блюда в iiko: как менять цену ингредиента и пересчитывать",
        "Техкарта для заготовок в iiko: полуфабрикаты и многоуровневые рецепты",
    ],
    "shift_reports": [
        "Отчёт по закрытию смены в iiko: 7 показателей, которые менеджер обязан проверить",
        "Кассовые расхождения в iiko: как система фиксирует и почему нельзя игнорировать",
        "X-отчёт и Z-отчёт в iiko: в чём разница и когда использовать каждый",
        "Почасовой отчёт продаж в iiko: как найти провальные и пиковые часы",
        "Смена без закрытия в iiko: что происходит с данными и как исправить",
    ],
    "api_integrations": [
        "iiko и Payme/Click: как автоматизировать приём безналичных платежей в Ташкенте",
        "API iiko: какие интеграции уже доступны на рынке Узбекистана",
        "iiko и системы видеоаналитики: как камера считает гостей и передаёт данные в систему",
        "Интеграция iiko с 1С: что синхронизируется и что остаётся ручным",
        "iiko и телеграм-боты: автоматические отчёты владельцу без открытия системы",
    ],
}

# Flat list preserving order within each feature for sequential fallback.
TOPICS = [t for topics in TOPICS_BY_FEATURE.values() for t in topics]

_feature_queue: list[str] = []          # shuffled rotation queue
_last_feature: str | None = None        # never repeat back-to-back
_feature_topic_indices: dict[str, int] = {f: 0 for f in TOPICS_BY_FEATURE}


def _refill_feature_queue() -> None:
    """Shuffle all 15 features into the queue, ensuring no back-to-back repeat."""
    global _feature_queue
    keys = list(TOPICS_BY_FEATURE.keys())
    random.shuffle(keys)
    # If the first item in the new shuffle matches the last used, rotate it to end
    if keys and keys[0] == _last_feature:
        keys.append(keys.pop(0))
    _feature_queue = keys


def get_next_topic() -> str:
    """
    Pick the next topic using a shuffled round-robin across all 15 feature categories.
    Every feature appears once before any repeats. Never the same feature twice in a row.
    """
    global _last_feature, _feature_queue
    if not _feature_queue:
        _refill_feature_queue()
    chosen_feature = _feature_queue.pop(0)
    idx = _feature_topic_indices[chosen_feature]
    topics = TOPICS_BY_FEATURE[chosen_feature]
    topic = topics[idx % len(topics)]
    _feature_topic_indices[chosen_feature] = idx + 1
    _last_feature = chosen_feature
    logger.info(f"Topic feature: [{chosen_feature}] → {topic[:60]}")
    return topic


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def _scrape_headlines(url: str) -> list[str]:
    resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=SCRAPE_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    seen: set[str] = set()
    results: list[str] = []
    for tag in soup.find_all(["h1", "h2", "h3"]) + soup.find_all("a", href=True):
        text = tag.get_text(separator=" ", strip=True)
        if 15 < len(text) < 200 and text not in seen:
            seen.add(text)
            results.append(text)
        if len(results) >= 8:
            break
    return results


def _scrape_rss(url: str) -> list[str]:
    import xml.etree.ElementTree as ET
    resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=SCRAPE_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    results: list[str] = []
    for item in root.iter("item"):
        title = item.findtext("title", "").strip()
        if title and 15 < len(title) < 200:
            results.append(title)
        if len(results) >= 10:
            break
    return results


def fetch_news_context() -> str | None:
    # Static HTML pages — scraping works because content is server-rendered.
    # Search result pages are JS-rendered and return no useful headlines.
    html_sources = [
        ("gazeta.uz",   "https://www.gazeta.uz/ru/"),
        ("kun.uz",      "https://kun.uz/ru/news"),
        ("daryo.uz",    "https://daryo.uz/ru"),
        ("nuz.uz",      "https://nuz.uz/ekonomika-i-finansy/"),
    ]
    # RSS feeds — most reliable, return targeted Russian restaurant news.
    rss_sources = [
        ("Google: ресторан Узбекистан",
         "https://news.google.com/rss/search?q=%D1%80%D0%B5%D1%81%D1%82%D0%BE%D1%80%D0%B0%D0%BD+%D0%A3%D0%B7%D0%B1%D0%B5%D0%BA%D0%B8%D1%81%D1%82%D0%B0%D0%BD&hl=ru&gl=UZ&ceid=UZ:ru"),
        ("Google: кафе Ташкент общепит",
         "https://news.google.com/rss/search?q=%D0%BA%D0%B0%D1%84%D0%B5+%D0%A2%D0%B0%D1%88%D0%BA%D0%B5%D0%BD%D1%82+%D0%BE%D0%B1%D1%89%D0%B5%D0%BF%D0%B8%D1%82&hl=ru&gl=UZ&ceid=UZ:ru"),
        ("Google: HoReCa автоматизация",
         "https://news.google.com/rss/search?q=HoReCa+%D0%B0%D0%B2%D1%82%D0%BE%D0%BC%D0%B0%D1%82%D0%B8%D0%B7%D0%B0%D1%86%D0%B8%D1%8F+%D1%80%D0%B5%D1%81%D1%82%D0%BE%D1%80%D0%B0%D0%BD&hl=ru&gl=UZ&ceid=UZ:ru"),
        ("Google: ресторанный бизнес ЦА",
         "https://news.google.com/rss/search?q=%D1%80%D0%B5%D1%81%D1%82%D0%BE%D1%80%D0%B0%D0%BD%D0%BD%D1%8B%D0%B9+%D0%B1%D0%B8%D0%B7%D0%BD%D0%B5%D1%81+%D0%A6%D0%B5%D0%BD%D1%82%D1%80%D0%B0%D0%BB%D1%8C%D0%BD%D0%B0%D1%8F+%D0%90%D0%B7%D0%B8%D1%8F&hl=ru&gl=UZ&ceid=UZ:ru"),
    ]
    blocks: list[str] = []

    for name, url in html_sources:
        try:
            headlines = _scrape_headlines(url)
            if headlines:
                blocks.append(f"[{name}]\n" + "\n".join(f"- {h}" for h in headlines))
                logger.info(f"Scraped {len(headlines)} headlines from {name}")
            else:
                logger.warning(f"No headlines found on {name}")
        except Exception as e:
            logger.warning(f"Scraping failed for {name}: {e}")

    for name, url in rss_sources:
        try:
            headlines = _scrape_rss(url)
            if headlines:
                blocks.append(f"[{name}]\n" + "\n".join(f"- {h}" for h in headlines))
                logger.info(f"Fetched {len(headlines)} RSS items from {name}")
            else:
                logger.warning(f"No RSS items from {name}")
        except Exception as e:
            logger.warning(f"RSS fetch failed for {name}: {e}")

    return "\n\n".join(blocks) if blocks else None


# ---------------------------------------------------------------------------
# Post generation — 3 distinct prompts for 3 content types
# ---------------------------------------------------------------------------

TYPE_LABELS = {
    "news":     "🔥 Свежая новость",
    "lifehack": "💡 Лайфхак iiko",
    "deepdive": "📊 Полезный разбор",
}

# Anonymous restaurant descriptors for case examples — never use real brand names.
ANON_RESTAURANT_EXAMPLES = (
    "один ташкентский ресторан, "
    "сеть кафе в Узбекистане, "
    "владелец 3 заведений в Ташкенте, "
    "популярная плов-хона в центре города, "
    "ресторан узбекской кухни, "
    "семейное кафе на Чиланзаре, "
    "банкетный ресторан в Мирабадском районе, "
    "сеть фастфуда в Ташкенте"
)

PROMPTS = {
    "news": (
        "Ты — старший аналитик HoReCa-рынка Узбекистана, бывший операционный директор сети из 12 "
        "ресторанов в Ташкенте. Пишешь в стиле журналиста-расследователя с MBA: каждый тезис — "
        "цифра, каждый вывод — механика, ни одного слова-паразита.\n\n"
        "ЗАДАЧА: Написать новостной пост-разбор для рестораторов Ташкента. Тема задана ниже.\n\n"
        "СТРУКТУРА — строго в этом порядке, без отклонений:\n\n"
        "1. СТОП-СТРОКА (1 предложение): Факт или цифра, которая заставляет остановить скролл. "
        "Конкретно, парадоксально, без вступлений. Пример уровня: «Один ташкентский ресторан сократил "
        "food cost с 38% до 27% за 3 месяца — не за счёт смены поставщиков, а за счёт одной "
        "настройки в iiko».\n\n"
        "2. ПРОБЛЕМА (2-3 предложения): Что происходит на рынке Ташкента прямо сейчас. "
        "Цифры: количество заведений (3 200+ в Ташкенте), средний чек (90–180 тыс. сум), "
        "маржинальность (12–18% у большинства). Конкретные потери в сумах в месяц.\n\n"
        "3. МЕХАНИКА (2-3 предложения): Почему это происходит — операционная цепочка, "
        "где именно рвётся процесс. Не «плохой менеджмент», а конкретный сбой: какой шаг, "
        "чья зона ответственности, какой результат.\n\n"
        "4. РЕШЕНИЕ (2-3 предложения): Конкретные функции iiko с правильными техническими "
        "названиями — KDS (кухонный дисплей), стоп-лист, ABC-анализ меню, food cost "
        "(плановый vs фактический), модификаторы, отчёт по кассовым сменам, инвентаризация, "
        "iiko.net (лояльность), модуль доставки. Объясни механизм: как именно функция решает проблему.\n\n"
        "5. КЕЙС + ИТОГ (2 предложения): Пример из ташкентской практики — используй анонимное "
        "описание: «один ташкентский ресторан», «сеть кафе в Узбекистане», «банкетный ресторан "
        "в Мирабадском районе», «семейное кафе на Чиланзаре» и т.п. "
        "Никогда не упоминай реальные названия брендов. "
        "Придумай реалистичные (не фантастические) цифры: food cost снизился с X% до Y%, "
        "время отдачи сократилось на Z минут, экономия составила N млн сум в месяц.\n\n"
        "СТИЛЬ: Журналист + операционный директор. Короткие предложения. Никаких клише "
        "(«в современном мире», «как известно», «всё больше»). Тон — коллега, который знает "
        "больше тебя и уважает твоё время."
    ),

    "lifehack": (
        "Ты — сертифицированный специалист по внедрению iiko. За 8 лет настроил систему в 160+ "
        "ресторанах Ташкента и Узбекистана. Знаешь каждую кнопку, каждый отчёт, каждую ловушку. "
        "Говоришь как практик: точно, конкретно, без академической воды.\n\n"
        "ЗАДАЧА: Написать пост-лайфхак — передача конкретного рабочего знания по заданной теме.\n\n"
        "СТРУКТУРА — строго в этом порядке:\n\n"
        "1. СТОП-СТРОКА (1 предложение): Конкретный факт с числом, который останавливает скролл. "
        "Привязан к типичной ташкентской ситуации — без названий конкретных заведений. "
        "Пример уровня: «Одно кафе в Юнусабаде теряло 1,8 млн сум ежемесячно из-за одной "
        "некорректно настроенной техкарты в iiko — и не знало об этом 4 месяца».\n\n"
        "2. ПРОБЛЕМА (2 предложения): Что именно ломается и сколько это стоит. "
        "Цифры в сумах или процентах, частота возникновения, кто в зоне риска "
        "(средний ресторан Ташкента с выручкой 60–120 млн сум/месяц).\n\n"
        "3. МЕХАНИКА СБОЯ (2 предложения): Почему большинство рестораторов этого не замечают — "
        "где именно в операционной цепочке происходит потеря, какой процесс её маскирует.\n\n"
        "4. РЕШЕНИЕ В iiko (3-4 предложения): Пошагово — что открыть, что настроить, что проверить. "
        "Точные названия функций: KDS, стоп-лист, ABC-анализ, food cost (техкарты), модификаторы "
        "блюд, отчёт по кассовым сменам, инвентаризация, банкетное меню, iiko.net, доставка. "
        "Формат: «Шаг 1: ... → Шаг 2: ...» или описание конкретных действий.\n\n"
        "5. РЕЗУЛЬТАТ (1-2 предложения): Измеримый итог — время, деньги, проценты. "
        "Используй анонимный пример: «один ташкентский ресторан», «сеть кафе в Узбекистане», "
        "«владелец 3 заведений в Ташкенте», «популярная плов-хона в центре» и т.п. "
        "Никогда не называй реальные бренды. Придумай реалистичные цифры.\n\n"
        "СТИЛЬ: Умный практик, не учитель. Читатель — управляющий или владелец ресторана в "
        "Ташкенте. Ноль воды. Максимум ценности на каждое слово."
    ),

    "deepdive": (
        "Ты — управляющий партнёр ресторанной группы в Ташкенте (4 заведения, выручка 800 млн сум "
        "в год), сертифицированный эксперт iiko, MBA Варшавской школы бизнеса. Пишешь аналитику "
        "уровня Harvard Business Review для владельцев и инвесторов HoReCa Узбекистана.\n\n"
        "ЗАДАЧА: Написать глубокий разбор по заданной теме — не обзор, а настоящая экспертиза "
        "с механикой, цифрами и практическим выводом.\n\n"
        "СТРУКТУРА — строго в этом порядке:\n\n"
        "1. СТОП-СТРОКА (1 предложение): Контринтуитивный факт или парадокс с цифрой — "
        "то, что противоречит интуиции опытного ресторатора. Без реальных названий заведений. "
        "Пример уровня: «Один ресторан узбекской кухни в Ташкенте увеличил выручку на 23% "
        "без открытия новых точек — только за счёт переработки структуры модификаторов в iiko».\n\n"
        "2. МЕХАНИКА (3 предложения): Как это реально работает в ресторане Ташкента — "
        "операционная цепочка, где деньги утекают или создаются незаметно. "
        "Почему стандартный Excel или 1С этого не видит.\n\n"
        "3. ОШИБКИ БОЛЬШИНСТВА (2 предложения): Что делают 70-80% рестораторов Ташкента "
        "и чем это заканчивается. Конкретные потери: сумы в месяц, проценты маржи, "
        "время. Без морализаторства.\n\n"
        "4. ПРАВИЛЬНЫЙ ПОДХОД (3 предложения): Методология с точными функциями iiko — "
        "ABC-анализ (разделение меню на группы A/B/C по марже и частоте заказов), "
        "food cost (настройка техкарт, контроль плановый vs фактический, норма отклонения "
        "не более 2%), KDS (контроль времени отдачи по станциям), модификаторы "
        "(управление средним чеком), отчёты по сменам, инвентаризация.\n\n"
        "5. КЕЙС (2 предложения): Конкретный кейс с цифрами. Используй анонимное описание: "
        "«один ташкентский ресторан», «сеть кафе в Узбекистане», «владелец 3 заведений», "
        "«популярная плов-хона в центре города», «ресторан узбекской кухни» и т.п. "
        "Никогда не называй реальные бренды или названия заведений. "
        "Придумай реалистичные (не фантастические) результаты: food cost с X% до Y%, "
        "экономия N млн сум в месяц, рост среднего чека на Z%.\n\n"
        "6. KPI-ФИНАЛ (1-2 предложения): 2 метрики, которые покажут что всё настроено "
        "правильно. Конкретные целевые значения для ташкентского рынка.\n\n"
        "СТИЛЬ: HBR + операционная конкретика. Структура чувствуется, но текст читается "
        "как единое целое. Читатель должен закончить пост с ощущением, что получил "
        "конкретный инструмент, а не очередной совет."
    ),
}


def generate_post(post_type: str, topic: str | None = None) -> str:
    if topic is None:
        topic = get_next_topic()

    news_context = fetch_news_context()
    type_prompt = PROMPTS[post_type]

    if news_context and post_type == "news":
        news_block = (
            f"\n\nСВЕЖИЕ ЗАГОЛОВКИ из отраслевых изданий (используй как источник вдохновения, "
            f"выбери самое интересное):\n\n{news_context}\n\n"
        )
    elif news_context:
        news_block = (
            f"\n\nКОНТЕКСТ из отраслевых новостей (можешь использовать для актуальных примеров):\n\n"
            f"{news_context}\n\n"
        )
    else:
        news_block = ""

    content = (
        f"{type_prompt}\n\n"
        f"ТЕМА ПОСТА: {topic}\n"
        f"{news_block}"
        f"ЖЁСТКИЕ ПРАВИЛА:\n"
        f"— Контекст ТОЛЬКО Узбекистан/Ташкент: цифры, районы, реалии местного рынка\n"
        f"— Фокус поста — ТОЛЬКО та функция iiko, которая указана в теме. Не смешивай с другими.\n"
        f"— Называй точное техническое название функции iiko из темы: "
        f"KDS / стоп-лист / ABC-анализ / food cost / модификаторы / программа лояльности / "
        f"доставка / инвентаризация / персонал / управление столами / финансовые отчёты / "
        f"учёт списаний / технологические карты / отчёты по сменам / API-интеграции\n"
        f"— Цифры обязательны в каждом блоке: суммы в сумах, проценты, временные показатели\n"
        f"— НЕ реклама: никогда «купи iiko», «Zetta Group», «обратитесь к нам»\n"
        f"— Строго 200-250 слов, только русский язык\n"
        f"— КРИТИЧНО: статья должна быть завершена полностью — никогда не обрывай на полуслове "
        f"или в середине мысли. Последний абзац должен быть закончен.\n"
        f"— Эмодзи: 3-5 штук, уместно по контексту\n"
        f"— Последняя строка без изменений: «Связаться: @iikoman»\n"
        f"— Только текст поста, без заголовков типа «Пост:» и без пояснений"
    )

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1400,
        messages=[{"role": "user", "content": content}],
    )
    post = msg.content[0].text.strip()

    if "Связаться: @iikoman" not in post:
        post = f"{post}\n\nСвязаться: @iikoman"

    return post


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def build_type_keyboard(post_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Свежая новость",  callback_data=f"type_news:{post_id}")],
        [InlineKeyboardButton("💡 Лайфхак iiko",    callback_data=f"type_lifehack:{post_id}")],
        [InlineKeyboardButton("📊 Полезный разбор", callback_data=f"type_deepdive:{post_id}")],
        [InlineKeyboardButton("🎲 Случайный тип",   callback_data=f"type_random:{post_id}")],
    ])


def build_owner_keyboard(post_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить → группа", callback_data=f"owner_approve:{post_id}"),
            InlineKeyboardButton("❌ Отклонить",          callback_data=f"reject:{post_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Переделать",   callback_data=f"regen:{post_id}"),
            InlineKeyboardButton("📸 Другое фото",  callback_data=f"change_photo:{post_id}"),
        ],
        [InlineKeyboardButton("📝 Другой текст",    callback_data=f"change_text:{post_id}")],
    ])


def build_group_keyboard(post_id: str, count: int) -> InlineKeyboardMarkup:
    label = f"✅ Опубликовать ({count}/{GROUP_APPROVALS_NEEDED})"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(label,         callback_data=f"group_approve:{post_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"group_reject:{post_id}"),
        ],
    ])


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _new_pending(stage: str = "type_select") -> dict:
    return {
        "text": None,
        "photo_url": None,
        "stage": stage,
        "post_type": None,
        "group_approvals": set(),
        "group_message_id": None,
    }


async def send_type_selection(app: Application) -> None:
    import uuid
    post_id = str(uuid.uuid4())[:8]
    pending_posts[post_id] = _new_pending("type_select")
    await app.bot.send_message(
        chat_id=YOUR_PERSONAL_ID,
        text="📝 Выберите тип поста:",
        reply_markup=build_type_keyboard(post_id),
    )
    logger.info(f"Type-selection menu sent for post {post_id}.")


async def _generate_and_send_to_owner(bot, post_id: str, post_type: str) -> None:
    topic = get_next_topic()
    post = await asyncio.get_event_loop().run_in_executor(
        None, lambda: generate_post(post_type, topic)
    )
    photo_url = pick_photo(post_type, topic)

    entry = pending_posts.get(post_id)
    if entry is None:
        return
    entry.update({"text": post, "photo_url": photo_url, "stage": "owner", "post_type": post_type, "topic": topic})

    label = TYPE_LABELS[post_type]
    keyboard = build_owner_keyboard(post_id)
    if photo_url:
        try:
            await bot.send_photo(chat_id=YOUR_PERSONAL_ID, photo=photo_url)
        except Exception as e:
            logger.warning(f"Owner photo send failed ({e}), skipping photo.")
    await bot.send_message(
        chat_id=YOUR_PERSONAL_ID,
        text=f"[{label}]\n\n{post}",
        reply_markup=keyboard,
    )
    logger.info(f"Post {post_id} ({post_type}) sent to owner.")


async def _forward_to_group(bot, post_id: str) -> None:
    entry = pending_posts[post_id]
    post = entry["text"]
    photo_url = entry["photo_url"]
    label = TYPE_LABELS.get(entry.get("post_type", "lifehack"), "Пост")
    count = len(entry["group_approvals"])
    keyboard = build_group_keyboard(post_id, count)

    # Send photo separately — Telegram captions are limited to 1024 chars which
    # post text easily exceeds, causing send_photo to fail silently and the
    # fallback text message to have no photo at all.
    if photo_url:
        try:
            await bot.send_photo(
                chat_id=APPROVAL_GROUP_ID,
                photo=photo_url,
            )
        except Exception as e:
            logger.warning(f"Group photo send failed ({e}), continuing without photo.")

    # Send the post text + approval buttons as a plain text message.
    msg = await bot.send_message(
        chat_id=APPROVAL_GROUP_ID,
        text=f"📋 [{label}] На проверку:\n\n{post}",
        reply_markup=keyboard,
    )
    entry["stage"] = "group"
    entry["group_message_id"] = msg.message_id
    logger.info(f"Post {post_id} forwarded to group for vote.")


async def _safe_edit_caption(query, text: str) -> None:
    try:
        await query.edit_message_caption(text)
    except Exception:
        try:
            await query.edit_message_text(text)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    action, post_id = query.data.split(":", 1)
    entry = pending_posts.get(post_id)

    if entry is None:
        await _safe_edit_caption(query, "⚠️ Пост уже обработан или не найден.")
        return

    stage = entry["stage"]

    # ── TYPE SELECTION ──────────────────────────────────────────────────────
    if action.startswith("type_") and stage == "type_select":
        chosen = action[5:]
        if chosen == "random":
            chosen = random.choice(["news", "lifehack", "deepdive"])
        try:
            await query.edit_message_text(f"⏳ Генерирую: {TYPE_LABELS[chosen]}...")
        except Exception:
            pass
        await _generate_and_send_to_owner(context.bot, post_id, chosen)

    # ── OWNER APPROVE ───────────────────────────────────────────────────────
    elif action == "owner_approve" and stage == "owner":
        await _safe_edit_caption(
            query, f"✅ Одобрено. Отправлено в группу.\n\n{entry['text']}"
        )
        await _forward_to_group(context.bot, post_id)

    # ── GROUP APPROVE (counted) ─────────────────────────────────────────────
    elif action == "group_approve" and stage == "group":
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name or "Участник"
        approvals: set = entry["group_approvals"]

        if user_id in approvals:
            await query.answer("Вы уже проголосовали ✅", show_alert=True)
            return

        approvals.add(user_id)
        count = len(approvals)
        logger.info(f"Post {post_id}: {count}/{GROUP_APPROVALS_NEEDED} approvals ({user_name})")

        if count >= GROUP_APPROVALS_NEEDED:
            post = entry["text"]
            photo_url = entry["photo_url"]
            if photo_url:
                try:
                    await context.bot.send_photo(chat_id=CHANNEL_USERNAME, photo=photo_url)
                except Exception as e:
                    logger.warning(f"Channel photo failed ({e}), skipping photo.")
            await context.bot.send_message(chat_id=CHANNEL_USERNAME, text=post)

            del pending_posts[post_id]
            await _safe_edit_caption(query, f"✅ Опубликовано ({count}/{GROUP_APPROVALS_NEEDED})!\n\n{post}")
            await context.bot.send_message(
                chat_id=YOUR_PERSONAL_ID,
                text=f"✅ Пост опубликован в {CHANNEL_USERNAME}.",
            )
            logger.info(f"Post {post_id} published to channel.")
        else:
            try:
                await query.edit_message_reply_markup(
                    reply_markup=build_group_keyboard(post_id, count)
                )
            except Exception:
                pass
            await query.answer(f"Голос принят ({count}/{GROUP_APPROVALS_NEEDED})", show_alert=True)

    # ── GROUP REJECT ────────────────────────────────────────────────────────
    elif action == "group_reject" and stage == "group":
        rejecter = update.effective_user.first_name or "Участник группы"
        post = entry["text"]
        del pending_posts[post_id]
        await _safe_edit_caption(query, f"❌ Пост отклонён ({rejecter}).")
        await context.bot.send_message(
            chat_id=YOUR_PERSONAL_ID,
            text=f"❌ Пост отклонён группой ({rejecter}):\n\n{post}",
        )
        logger.info(f"Post {post_id} rejected by group ({rejecter}).")

    # ── OWNER REJECT ────────────────────────────────────────────────────────
    elif action == "reject" and stage == "owner":
        del pending_posts[post_id]
        await _safe_edit_caption(query, "❌ Пост отклонён.")
        logger.info(f"Post {post_id} rejected by owner.")

    # ── CHANGE PHOTO ────────────────────────────────────────────────────────
    elif action == "change_photo" and stage == "owner":
        post_type = entry.get("post_type", "lifehack")
        current_topic = entry.get("topic", "")
        new_photo = pick_photo(post_type, current_topic)
        entry["photo_url"] = new_photo
        keyboard = build_owner_keyboard(post_id)
        try:
            await context.bot.send_photo(
                chat_id=YOUR_PERSONAL_ID,
                photo=new_photo,
                caption=entry["text"],
                reply_markup=keyboard,
            )
            await _safe_edit_caption(query, "📸 Новое фото — смотрите сообщение выше.")
        except Exception as e:
            logger.warning(f"change_photo failed: {e}")
            await _safe_edit_caption(query, "Ошибка при смене фото.")

    # ── CHANGE TEXT ─────────────────────────────────────────────────────────
    elif action == "change_text" and stage == "owner":
        post_type = entry.get("post_type", "lifehack")
        photo_url = entry["photo_url"]
        await _safe_edit_caption(query, "📝 Генерирую новый текст, фото остаётся...")
        new_post = await asyncio.get_event_loop().run_in_executor(
            None, lambda: generate_post(post_type)
        )
        entry["text"] = new_post
        keyboard = build_owner_keyboard(post_id)
        try:
            await context.bot.send_photo(
                chat_id=YOUR_PERSONAL_ID,
                photo=photo_url,
                caption=new_post,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.warning(f"change_text photo failed: {e}")
            await context.bot.send_message(
                chat_id=YOUR_PERSONAL_ID, text=new_post, reply_markup=keyboard
            )

    # ── REGEN ───────────────────────────────────────────────────────────────
    elif action == "regen" and stage == "owner":
        import uuid
        post_type = entry.get("post_type", "lifehack")
        del pending_posts[post_id]
        await _safe_edit_caption(query, "🔄 Генерирую новый пост...")
        new_id = str(uuid.uuid4())[:8]
        pending_posts[new_id] = _new_pending("owner")
        await _generate_and_send_to_owner(context.bot, new_id, post_type)


async def handle_edited_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post_id = context.user_data.get("editing_post_id")
    if not post_id:
        return
    new_text = update.message.text
    entry = pending_posts.get(post_id)
    if entry:
        entry["text"] = new_text
    context.user_data.pop("editing_post_id", None)
    photo_url = (entry or {}).get("photo_url") or pick_photo("lifehack", "")
    keyboard = build_owner_keyboard(post_id)
    try:
        await update.message.reply_photo(
            photo=photo_url,
            caption=f"Отредактированный пост:\n\n{new_text}",
            reply_markup=keyboard,
        )
    except Exception:
        await update.message.reply_text(
            f"Отредактированный пост:\n\n{new_text}", reply_markup=keyboard
        )


async def handle_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != YOUR_PERSONAL_ID:
        return
    await send_type_selection(context.application)
    logger.info("Type selection sent via /test.")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

# Default posting times — interpreted in Asia/Tashkent timezone.
posting_times: list[str] = ["09:00", "13:00", "19:00"]

_main_loop = None
_scheduler = BackgroundScheduler(timezone=TASHKENT_TZ)


def run_scheduled_job(app: Application) -> None:
    async def job():
        logger.info("Scheduled post triggered.")
        await send_type_selection(app)

    if _main_loop is not None and _main_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(job(), _main_loop)
        try:
            future.result(timeout=30)
        except Exception as e:
            logger.error(f"Scheduled job failed: {e}")
    else:
        logger.warning("Main event loop not available; skipping scheduled job.")


def apply_schedule(app: Application) -> None:
    """Rebuild all cron jobs from posting_times (Asia/Tashkent)."""
    _scheduler.remove_all_jobs()
    for t in posting_times:
        hour, minute = t.split(":")
        _scheduler.add_job(
            run_scheduled_job,
            trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone=TASHKENT_TZ),
            args=[app],
            id=f"post_{t}",
            replace_existing=True,
        )
    logger.info(f"Schedule updated (Tashkent): {', '.join(posting_times)}")


async def handle_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != YOUR_PERSONAL_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            f"🕐 Текущее расписание:\n\n{'  •  '.join(posting_times)}\n\n"
            f"Изменить: /schedule 08:00 12:00 18:00"
        )
        return
    import re
    time_pattern = re.compile(r"^\d{2}:\d{2}$")
    new_times = list(args)
    invalid = [t for t in new_times if not time_pattern.match(t)]
    if invalid:
        await update.message.reply_text(f"Неверный формат: {', '.join(invalid)} (нужно ЧЧ:ММ)")
        return
    bad = [t for t in new_times if not (0 <= int(t[:2]) <= 23 and 0 <= int(t[3:]) <= 59)]
    if bad:
        await update.message.reply_text(f"Некорректные значения: {', '.join(bad)}")
        return
    posting_times.clear()
    posting_times.extend(sorted(set(new_times)))
    apply_schedule(context.application)
    await update.message.reply_text(
        f"✅ Расписание обновлено!\n\n{'  •  '.join(posting_times)}"
    )
    logger.info(f"Schedule changed to: {posting_times}")


# ---------------------------------------------------------------------------
# Error handler — reclaim polling session on 409 Conflict
# ---------------------------------------------------------------------------

async def handle_telegram_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    PTB error handler. On 409 Conflict (competing bot instance), immediately call
    getUpdates?timeout=0 to terminate the competitor's long-poll and steal the
    session back. PTB will then retry its own getUpdates and win the slot.
    """
    if isinstance(context.error, TelegramConflict):
        logger.warning("409 Conflict — reclaiming polling session from competing instance...")
        try:
            await context.bot.get_updates(timeout=0, limit=1)
            logger.info("Polling session reclaimed via error handler.")
        except Exception as e:
            logger.warning(f"Session reclaim in error handler failed: {e}")
    else:
        logger.error(f"Unhandled bot error: {context.error}", exc_info=context.error)


# ---------------------------------------------------------------------------
# Startup: force-claim the Telegram polling session
# ---------------------------------------------------------------------------

def _force_claim_polling_session() -> None:
    """
    Telegram only allows one active getUpdates connection per bot token.
    If another instance (e.g. Railway deployment) is polling, our calls
    return 409 Conflict indefinitely.

    Fix: call getUpdates with timeout=0 in a tight loop. Each call causes
    Telegram to immediately terminate the previous long-poll, so the competing
    instance gets a 409 on its next request while we can start fresh.
    We retry until we receive HTTP 200, then hand off to PTB's run_polling().
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    # First, delete any webhook (PTB does this too, but do it early)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
            json={"drop_pending_updates": True},
            timeout=10,
        )
        logger.info("Webhook deleted.")
    except Exception as e:
        logger.warning(f"deleteWebhook error: {e}")

    logger.info("Claiming polling session (terminating competing instances)...")
    for attempt in range(40):
        try:
            resp = requests.post(
                url,
                json={"timeout": 0, "limit": 1},
                timeout=10,
            )
            logger.info(f"  Session claim attempt {attempt + 1}: HTTP {resp.status_code}")
            if resp.status_code == 200:
                logger.info("Polling session claimed successfully.")
                return
            # 409 = another instance is active; keep hammering to steal the slot
        except Exception as e:
            logger.warning(f"  Claim attempt {attempt + 1} error: {e}")
        time.sleep(2)

    logger.warning("Could not claim polling session after 40 attempts — starting anyway.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _main_loop

    # Steal the polling session before PTB starts its own polling loop.
    _force_claim_polling_session()

    _main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_main_loop)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("test", handle_test_command))
    app.add_handler(CommandHandler("schedule", handle_schedule_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.User(YOUR_PERSONAL_ID),
            handle_edited_post,
        )
    )
    app.add_error_handler(handle_telegram_error)

    apply_schedule(app)
    _scheduler.start()
    logger.info("zetta_agent bot is running...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
