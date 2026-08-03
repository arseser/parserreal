"""
Настройки бота и парсера.
Все чувствительные данные (токен) берутся из переменных окружения.
"""
import os

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Переменная окружения TELEGRAM_BOT_TOKEN не задана.")

# --- Паузы между запросами ---
REQUEST_DELAY_MIN = 1.5
REQUEST_DELAY_MAX = 3.5

DETAIL_DELAY_MIN = 1.0
DETAIL_DELAY_MAX = 2.5

# --- Лимиты ---
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_PAGES = 25
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3

# Параллельность дозапроса деталей
DETAIL_CONCURRENCY = int(os.environ.get("DETAIL_CONCURRENCY", 4))

# Файл для выгрузки
OUTPUT_FILE = "olx.txt"

# ---------------------------------------------------------------------------
# Поддержка нескольких доменов OLX (pl, pt)
# ---------------------------------------------------------------------------
OLX_DOMAINS = {
    'pl': {
        'base_url': 'https://www.olx.pl',
        'currency': 'ZL',
        'locale': 'pl',
    },
    'pt': {
        'base_url': 'https://www.olx.pt',
        'currency': 'EUR',
        'locale': 'pt',
    }
}
DEFAULT_DOMAIN = 'pl'

# ---------------------------------------------------------------------------
# Категории (общие для всех доменов)
# ---------------------------------------------------------------------------
CATEGORIES = [
    {"label": "📁 Все категории", "slug": ""},
    {"label": "📱 Электроника", "slug": "elektronika"},
    {"label": "🚗 Транспорт", "slug": "motoryzacja"},
    {"label": "🏠 Недвижимость", "slug": "nieruchomosci"},
    {"label": "💼 Работа", "slug": "praca"},
    {"label": "🛋 Дом и сад", "slug": "dom-ogrod-remont"},
    {"label": "👗 Мода", "slug": "moda"},
    {"label": "🐾 Животные", "slug": "zwierzeta"},
    {"label": "👶 Для детей", "slug": "dla-dzieci"},
    {"label": "⚽ Спорт и хобби", "slug": "sport-hobby"},
    {"label": "🚜 Сельское хозяйство", "slug": "rolnictwo"},
    {"label": "🔧 Услуги", "slug": "uslugi"},
]

# ---------------------------------------------------------------------------
# Период публикации (фильтруется на стороне бота)
# ---------------------------------------------------------------------------
PERIODS = [
    {"label": "Любой", "days": None},
    {"label": "За сутки", "days": 1},
    {"label": "За 3 дня", "days": 3},
    {"label": "За неделю", "days": 7},
    {"label": "За месяц", "days": 30},
]

# ---------------------------------------------------------------------------
# Давность регистрации продавца
# ---------------------------------------------------------------------------
SELLER_AGE_OPTIONS = [
    {"label": "Не важно", "days": None},
    {"label": "Старше 30 дней", "days": 30},
    {"label": "Старше 6 месяцев", "days": 182},
    {"label": "Старше 1 года", "days": 365},
]

# ---------------------------------------------------------------------------
# Автомониторинг
# ---------------------------------------------------------------------------
MONITOR_INTERVAL_SECONDS = int(os.environ.get("MONITOR_INTERVAL_SECONDS", 300))
MONITOR_CHECK_LIMIT = 20
MONITOR_MAX_SUBSCRIPTIONS = 20
