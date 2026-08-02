"""
Настройки бота и парсера.
Все чувствительные данные (токен) берутся из переменных окружения —
не хардкодьте токен в коде при деплое на Render/Railway.
"""
import os

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "Переменная окружения TELEGRAM_BOT_TOKEN не задана. "
        "Укажите её в настройках сервиса на Render/Railway (Environment Variables)."
    )

# --- Паузы между запросами, чтобы не получить бан от OLX ---
REQUEST_DELAY_MIN = 1.5   # сек, между запросами страниц поиска
REQUEST_DELAY_MAX = 3.5

DETAIL_DELAY_MIN = 1.0    # сек, между запросами детальных страниц объявлений
DETAIL_DELAY_MAX = 2.5

# --- Лимиты ---
DEFAULT_LIMIT = 20        # сколько объявлений отправлять в чат, если лимит не указан
MAX_LIMIT = 100            # верхняя граница, чтобы не положить бесплатный хостинг
MAX_PAGES = 25             # максимум страниц пагинации за один запрос (защита от зацикливания)

REQUEST_TIMEOUT = 15       # сек, таймаут HTTP-запроса
MAX_RETRIES = 3            # число повторных попыток при ошибке/бане (403/429)

OLX_BASE = "https://www.olx.pl"

# Ротация User-Agent между запросами
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]

OUTPUT_FILE = "olx.txt"
