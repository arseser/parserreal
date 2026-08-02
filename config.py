import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
OUTPUT_FILE = "olx.txt"

# Ускоренные задержки
DELAY_BETWEEN_PAGES = (0.5, 1.0)      
DELAY_BETWEEN_REQUESTS = (0.3, 0.6)   

MONITOR_INTERVAL_SECONDS = 10 * 60
MONITOR_MAX_SUBSCRIPTIONS = 10
MONITOR_MAX_ADS_PER_CHECK = 50

CATEGORIES = [
    {"label": "Все категории", "slug": ""},
    {"label": "Электроника", "slug": "elektronika"},
    {"label": "Для дома", "slug": "dom-i-ogrodek"},
    {"label": "Мода", "slug": "moda"},
    {"label": "Детям", "slug": "dla-dzieci"},
    {"label": "Авто", "slug": "motoryzacja"},
    {"label": "Недвижимость", "slug": "nieruchomosci"},
]

PERIODS = [
    {"label": "За все время", "days": None},
    {"label": "За 1 день", "days": 1},
    {"label": "За 3 дня", "days": 3},
    {"label": "За неделю", "days": 7},
]

SELLER_AGE_OPTIONS = [
    {"label": "Без фильтра", "days": None},
    {"label": "Месяц", "days": 30},
    {"label": "Полгода", "days": 180},
]
