"""
Конфигурационный файл для OLX Telegram-бота.
"""
import os


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# --- Настройки парсера ---
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
OUTPUT_FILE = "olx.txt"

# Задержки для парсера (снижено для ускорения)
DELAY_BETWEEN_PAGES = (0.5, 1.5)      # между страницами
DELAY_BETWEEN_REQUESTS = (0.3, 0.8)   # между запросами объявлений

# --- Настройки автомониторинга ---
MONITOR_INTERVAL_SECONDS = 10 * 60  # 10 минут
MONITOR_MAX_SUBSCRIPTIONS = 10
MONITOR_MAX_ADS_PER_CHECK = 50

# --- Категории (для фильтрации) ---
CATEGORIES = [
    {"label": "Все категории", "slug": ""},
    {"label": "Электроника", "slug": "elektronika"},
    {"label": "Для дома и сада", "slug": "dom-i-ogrodek"},
    {"label": "Мода", "slug": "moda"},
    {"label": "Детям", "slug": "dla-dzieci"},
    {"label": "Досуг", "slug": "rozrywka"},
    {"label": "Авто", "slug": "motoryzacja"},
    {"label": "Недвижимость", "slug": "nieruchomosci"},
]

# --- Периоды (для фильтрации) ---
PERIODS = [
    {"label": "За все время", "days": None},
    {"label": "За 1 день", "days": 1},
    {"label": "За 3 дня", "days": 3},
    {"label": "За неделю", "days": 7},
    {"label": "За 2 недели", "days": 14},
]

# --- Возраст аккаунта продавца ---
SELLER_AGE_OPTIONS = [
    {"label": "Без фильтра", "days": None},
    {"label": "Не менее 30 дней", "days": 30},
    {"label": "Не менее 90 дней", "days": 90},
    {"label": "Не менее 180 дней", "days": 180},
    {"label": "Не менее года", "days": 365},
]
