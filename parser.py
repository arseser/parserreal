"""
Парсер объявлений OLX.pl.

Стратегия (устойчивость к смене вёрстки):
  1) Пытаемся вытащить встроенный JSON с данными объявлений
     (Next.js __NEXT_DATA__ / __PRERENDERED_STATE__ и т.п.) — самый надёжный путь.
  2) Если не вышло — разбираем HTML через BeautifulSoup, перебирая несколько
     наборов CSS-селекторов (на случай, если OLX поменял классы/атрибуты).
  3) Для КАЖДОГО объявления дозапрашивается страница объявления — там же
     достаём расширенные данные о продавце (регистрация, был в сети,
     кол-во объявлений, рейтинг), которых нет в карточке списка. Эти
     дозапросы идут ПАРАЛЛЕЛЬНО пачками (см. enrich_many/config.DETAIL_CONCURRENCY),
     а не строго по одному — это основной источник ускорения парсинга.

ВАЖНО: OLX регулярно меняет вёрстку и защиту от ботов. Если через какое-то
время парсер перестанет находить объявления или данные о продавце — в первую
очередь проверьте актуальные селекторы через "Просмотр кода страницы" в
браузере и обновите CARD_SELECTORS / SELLER_TEXT_PATTERNS ниже.
"""
import concurrent.futures
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlencode, parse_qs

try:
    from zoneinfo import ZoneInfo
    WARSAW_TZ = ZoneInfo("Europe/Warsaw")
except Exception:  # на случай, если в системе нет базы часовых поясов
    WARSAW_TZ = timezone.utc

import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger("olx_bot.parser")

# Сколько дозапросов детальной страницы подряд должны провалиться, чтобы
# парсер решил "OLX сейчас блокирует запросы" и перестал долбиться дальше
# по остальным объявлениям (вместо того чтобы зависать на десятки минут).
DETAIL_CIRCUIT_BREAKER = 5


class OlxParserError(Exception):
    """Ошибка парсинга/получения данных с OLX."""


# ---------------------------------------------------------------------------
# Служебные функции: сессия, заголовки, задержки, HTTP-запрос с ретраями
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_random_headers())
    return s


def create_session() -> requests.Session:
    """Публичная обёртка над _session() — используется в bot.py, когда
    нужно вручную управлять сессией (например, для потоковой отправки
    объявлений по одному, без ожидания полного парсинга)."""
    return _session()


def _random_headers() -> dict:
    return {
        "User-Agent": random.choice(config.USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
    }


def _sleep(a: float, b: float) -> None:
    time.sleep(random.uniform(a, b))


# ---------------------------------------------------------------------------
# Разбор дат на польском ("Dodane 14 lipca 2026", "wczoraj o 21:14",
# "Na OLX od grudnia 2023") — используется и для даты публикации объявления
# (когда JSON недоступен и приходится брать текст из HTML-селектора), и для
# даты регистрации/последней активности продавца.
# ---------------------------------------------------------------------------

_PL_MONTHS = {
    "styczeń": 1, "stycznia": 1, "styczniu": 1,
    "luty": 2, "lutego": 2, "lutym": 2,
    "marzec": 3, "marca": 3, "marcu": 3,
    "kwiecień": 4, "kwietnia": 4, "kwietniu": 4,
    "maj": 5, "maja": 5, "maju": 5,
    "czerwiec": 6, "czerwca": 6, "czerwcu": 6,
    "lipiec": 7, "lipca": 7, "lipcu": 7,
    "sierpień": 8, "sierpnia": 8, "sierpniu": 8,
    "wrzesień": 9, "września": 9, "wrześniu": 9,
    "październik": 10, "października": 10, "październiku": 10,
    "listopad": 11, "listopada": 11, "listopadzie": 11,
    "grudzień": 12, "grudnia": 12, "grudniu": 12,
}
# длинные варианты (typu "październiku") должны идти раньше коротких
# префиксов той же основы, чтобы regex-alternation не обрывался раньше времени
_PL_MONTH_ALT = "|".join(sorted(_PL_MONTHS.keys(), key=len, reverse=True))

_RE_PL_RELATIVE_DATE = re.compile(r"\b(dzisiaj|wczoraj)\b(?:[^\d]{0,6}(\d{1,2}):(\d{2}))?", re.I)
_RE_PL_FULL_DATE = re.compile(
    r"(\d{1,2})\s+(" + _PL_MONTH_ALT + r")\s+(\d{4})(?:[^\d]{0,6}(\d{1,2}):(\d{2}))?", re.I
)


def _parse_polish_date_text(text: str):
    """Пытается распознать дату в тексте на польском ('Dodane 14 lipca 2026',
    'wczoraj o 21:14', 'dzisiaj') и вернуть datetime в UTC. None, если текст
    не подошёл ни под один из известных форматов — тогда используется
    исходный текст как есть (лучше показать сырой текст, чем ошибиться)."""
    if not text:
        return None

    m = _RE_PL_FULL_DATE.search(text)
    if m:
        day, month_word, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month = _PL_MONTHS.get(month_word)
        if month:
            hour = int(m.group(4)) if m.group(4) else 0
            minute = int(m.group(5)) if m.group(5) else 0
            try:
                dt = datetime(year, month, day, hour, minute, tzinfo=WARSAW_TZ)
                return dt.astimezone(timezone.utc)
            except ValueError:
                return None

    m = _RE_PL_RELATIVE_DATE.search(text)
    if m:
        word = m.group(1).lower()
        hour = int(m.group(2)) if m.group(2) else 0
        minute = int(m.group(3)) if m.group(3) else 0
        now_warsaw = datetime.now(WARSAW_TZ)
        base = now_warsaw.date() if word == "dzisiaj" else (now_warsaw - timedelta(days=1)).date()
        try:
            dt = datetime(base.year, base.month, base.day, hour, minute, tzinfo=WARSAW_TZ)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    return None


def _apply_parsed_date(ad: dict, raw_text: str) -> None:
    """Пытается превратить сырой текст с датой ('Dodane 14 lipca 2026' и
    т.п.) в нормальный datetime + красиво отформатированную строку
    (см. format_created_display). Если не получилось — используем сырой
    текст как есть, ничего не теряя."""
    dt = _parse_polish_date_text(raw_text)
    if dt:
        ad["created_dt"] = dt
        ad["created_display"] = format_created_display(dt)
    else:
        ad["created_display"] = raw_text


def _format_pl_month_year(day, month_word: str, year: str) -> str:
    """'grudnia 2023' (+опционально день) -> '12.2023' или '08.12.2023'."""
    month = _PL_MONTHS.get(month_word.lower())
    if not month:
        return None
    if day:
        return f"{int(day):02d}.{month:02d}.{year}"
    return f"{month:02d}.{year}"


def _normalize_currency(text: str) -> str:
    """Приводит обозначение валюты в тексте к единому виду 'ZL' (злотые) —
    OLX.pl показывает валюту то как 'zł', то как 'PLN', то как 'zl' в
    зависимости от места. По просьбе — везде показываем 'ZL'."""
    if not text:
        return text
    return re.sub(r"z[łl]\.?|PLN", "ZL", text, flags=re.I)


def _get(url: str, session: requests.Session) -> str:
    last_err = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            session.headers.update(_random_headers())
            resp = session.get(url, timeout=config.REQUEST_TIMEOUT)
            if resp.status_code == 200:
                log.info("GET %s -> 200 (попытка %s/%s)", url, attempt, config.MAX_RETRIES)
                return resp.text
            if resp.status_code in (403, 429):
                log.warning("GET %s -> %s (бан/лимит), попытка %s/%s", url, resp.status_code, attempt, config.MAX_RETRIES)
                last_err = OlxParserError(
                    f"OLX вернул статус {resp.status_code} (похоже на временную блокировку запросов)."
                )
                _sleep(4 + attempt * 2, 7 + attempt * 3)
                continue
            log.warning("GET %s -> неожиданный статус %s, попытка %s/%s", url, resp.status_code, attempt, config.MAX_RETRIES)
            last_err = OlxParserError(f"OLX вернул неожиданный статус {resp.status_code} для {url}.")
        except requests.RequestException as e:
            log.warning("GET %s -> сетевая ошибка (%s), попытка %s/%s", url, e, attempt, config.MAX_RETRIES)
            last_err = OlxParserError(f"Сетевая ошибка при запросе {url}: {e}")
        _sleep(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)
    log.error("GET %s -> все %s попыток провалились: %s", url, config.MAX_RETRIES, last_err)
    raise last_err or OlxParserError(f"Не удалось загрузить {url}")


def build_search_url(query_or_url: str, page: int = 1, filters: dict = None) -> str:
    """
    Принимает либо готовую ссылку на поиск/категорию OLX.pl, либо ключевое
    слово, плюс словарь фильтров (см. filters.DEFAULT_FILTERS). Возвращает
    URL с подставленной категорией, страницей, ценой и доставкой.

    Фильтры, которые OLX не умеет принимать в URL (период публикации,
    банворды, параметры продавца), применяются отдельно — см.
    filters.apply_client_filters().
    """
    filters = filters or {}
    query_or_url = query_or_url.strip()

    if query_or_url.startswith("http://") or query_or_url.startswith("https://"):
        base = query_or_url
    else:
        safe_q = re.sub(r"\s+", "-", query_or_url.strip())
        category_slug = (filters.get("category_slug") or "").strip("/")
        prefix = f"{category_slug}/" if category_slug else "oferty/"
        base = f"{config.OLX_BASE}/{prefix}q-{safe_q}/"

    parsed = urlparse(base)
    qs = parse_qs(parsed.query)

    if page > 1:
        qs["page"] = [str(page)]
    else:
        qs.pop("page", None)

    price_min = filters.get("price_min")
    price_max = filters.get("price_max")
    if price_min is not None:
        qs["search[filter_float_price:from]"] = [str(price_min)]
    if price_max is not None:
        qs["search[filter_float_price:to]"] = [str(price_max)]

    delivery = filters.get("delivery")
    if delivery is True:
        qs["search[filter_enum_delivery][0]"] = ["1"]

    # сортировка "сначала новые" — важно для режима мониторинга
    qs["search[order]"] = ["created_at:desc"]

    new_query = urlencode(qs, doseq=True)
    rebuilt = parsed._replace(query=new_query)
    return rebuilt.geturl()


# ---------------------------------------------------------------------------
# Путь №1: встроенный JSON на странице поиска
# ---------------------------------------------------------------------------

_JSON_SCRIPT_PATTERNS = [
    re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S),
    re.compile(r'window\.__PRERENDERED_STATE__\s*=\s*"(.*?)";', re.S),
    re.compile(r'<script[^>]+type="application/json"[^>]*data-cy="[^"]*listing[^"]*"[^>]*>(.*?)</script>', re.S),
]


def _try_extract_embedded_json(html: str):
    for pattern in _JSON_SCRIPT_PATTERNS:
        m = pattern.search(html)
        if not m:
            continue
        raw = m.group(1)
        data = None
        for candidate in (raw, raw.encode("utf-8").decode("unicode_escape", errors="ignore") if "\\u" in raw or '\\"' in raw else raw):
            try:
                data = json.loads(candidate)
                break
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        if data is None:
            continue
        listings = _find_listings_in_json(data)
        if listings:
            return listings
    return None


def _find_listings_in_json(data):
    """Рекурсивно ищем в JSON массивы объектов, похожих на объявления OLX."""
    found = []

    def looks_like_ad(obj):
        return isinstance(obj, dict) and "id" in obj and ("title" in obj or "url" in obj) and "price" in obj

    def walk(node):
        if isinstance(node, dict):
            for key in ("listing", "listings", "ads", "items", "data"):
                val = node.get(key)
                if isinstance(val, list):
                    candidates = [x for x in val if looks_like_ad(x)]
                    if candidates:
                        found.extend(candidates)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                if looks_like_ad(item):
                    found.append(item)
                else:
                    walk(item)

    walk(data)

    seen, unique = set(), []
    for ad in found:
        ad_id = ad.get("id")
        if ad_id in seen:
            continue
        seen.add(ad_id)
        unique.append(ad)
    return unique


def _extract_location(ad: dict):
    loc = ad.get("location") or {}
    if not isinstance(loc, dict):
        return None
    parts = []
    for key in ("district", "city", "region"):
        node = loc.get(key)
        name = node.get("name") if isinstance(node, dict) else node
        if name and name not in parts:
            parts.append(name)
    return ", ".join(parts) if parts else None


def _normalize_json_ad(ad: dict) -> dict:
    photo = None
    photos = ad.get("photos") or ad.get("images") or []
    if photos:
        first = photos[0]
        photo = (first.get("link") or first.get("url")) if isinstance(first, dict) else first
        if photo:
            photo = photo.replace("{width}", "800").replace("{height}", "600")

    price = ad.get("price")
    if isinstance(price, dict):
        inner = price.get("value")
        price = inner.get("value") if isinstance(inner, dict) else inner

    url = ad.get("url") or ad.get("ad_url")
    if url and not url.startswith("http"):
        url = urljoin(config.OLX_BASE, url)

    user = ad.get("user") if isinstance(ad.get("user"), dict) else {}
    seller = ad.get("sellerName") or user.get("name")
    delivery_flag = ad.get("delivery") or ad.get("safety_trade") or ad.get("shipping")

    created_raw = ad.get("createdAt") or ad.get("created_at")
    created_dt = _parse_iso_datetime(created_raw)

    return {
        "title": (ad.get("title") or "").strip() or "Без названия",
        "price": _format_price(price, ad.get("currency", "zł")),
        "ad_url": url or "",
        "chat_url": f"{url}?chat=1&isPreviewActive=0" if url else "",
        "location": _extract_location(ad),
        "seller": seller or None,
        "delivery": bool(delivery_flag) if delivery_flag is not None else None,
        "created_dt": created_dt,
        "created_display": format_created_display(created_dt) if created_dt else (created_raw or "не указана"),
        "photo": photo,
        # поля продавца — дозаполняются на детальной странице
        "seller_registered": None,
        "seller_last_seen": None,
        "seller_ads_count": None,
        "seller_rating": None,
        "seller_reviews_count": None,
    }


def _format_price(value, currency="ZL") -> str:
    if value in (None, "", "Do negocjacji"):
        return "Цена не указана"
    try:
        value = float(str(value).replace(" ", "").replace(",", "."))
        return f"{value:,.0f} ZL".replace(",", " ")
    except (ValueError, TypeError):
        return _normalize_currency(f"{value} {currency}")


def _parse_iso_datetime(raw):
    if not raw or not isinstance(raw, str):
        return None
    try:
        cleaned = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def format_relative_ru(dt) -> str:
    """'20 минут назад' / '3 дня назад' и т.п. Принимает datetime с таймзоной."""
    if dt is None:
        return "не указана"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (now - dt).total_seconds()
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "только что"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} {_plural_ru(minutes, 'минуту', 'минуты', 'минут')} назад"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} {_plural_ru(hours, 'час', 'часа', 'часов')} назад"
    days = int(hours // 24)
    if days < 30:
        return f"{days} {_plural_ru(days, 'день', 'дня', 'дней')} назад"
    months = int(days // 30)
    if months < 12:
        return f"{months} {_plural_ru(months, 'месяц', 'месяца', 'месяцев')} назад"
    years = int(days // 365)
    return f"{years} {_plural_ru(years, 'год', 'года', 'лет')} назад"


def format_created_display(dt) -> str:
    """'01.08.2026 21:14 (вчера)' — точная дата+время публикации (по
    варшавскому времени, т.к. OLX.pl — польский сайт) плюс относительное
    "N дней назад" в скобках, для наглядности."""
    if dt is None:
        return "не указана"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local_dt = dt.astimezone(WARSAW_TZ)
    return f"{local_dt.strftime('%d.%m.%Y %H:%M')} ({format_relative_ru(dt)})"


# ---------------------------------------------------------------------------
# Путь №2: разбор HTML напрямую (запасной вариант, несколько наборов селекторов)
# ---------------------------------------------------------------------------

CARD_SELECTORS = [
    {
        "container": '[data-cy="l-card"]',
        "title": 'h4, h6, [data-cy="ad-card-title"] h4',
        "price": '[data-testid="ad-price"]',
        "link": "a",
        "img": "img",
        "date": '[data-testid="location-date"]',
    },
    {
        "container": "div.css-1sw7q4x",
        "title": "h6",
        "price": "p.css-10b0gli",
        "link": "a.css-1tqlkj0",
        "img": "img",
        "date": "p.css-veheph",
    },
    {
        "container": "article",
        "title": "h4, h6",
        "price": '[data-testid="ad-price"], .price',
        "link": "a",
        "img": "img",
        "date": ".css-veheph, time",
    },
]


def _text_or_none(tag):
    if tag is None:
        return None
    # separator=" " — иначе bs4 склеивает соседние текстовые узлы без
    # пробела (например, надпись "Dodane" и дата в соседних тегах давали
    # "Dodane14 lipca 2026" вместо "Dodane 14 lipca 2026").
    txt = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
    return txt or None


def _parse_cards_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for sel in CARD_SELECTORS:
        cards = soup.select(sel["container"])
        if not cards:
            continue
        results = []
        for card in cards:
            link_tag = card.select_one(sel["link"])
            href = link_tag.get("href") if link_tag else None
            if not href:
                continue
            ad_url = href if href.startswith("http") else urljoin(config.OLX_BASE, href)

            title_tag = card.select_one(sel["title"])
            title = _text_or_none(title_tag) or _text_or_none(link_tag) or "Без названия"

            price_tag = card.select_one(sel["price"])
            price_text = _text_or_none(price_tag) or "Цена не указана"
            if price_text != "Цена не указана":
                price_text = _normalize_currency(price_text)

            img_tag = card.select_one(sel["img"])
            photo = None
            if img_tag:
                photo = img_tag.get("src") or img_tag.get("data-src")

            date_tag = card.select_one(sel["date"])
            date_text = _text_or_none(date_tag) or "не указана"
            # пробуем распознать польскую дату с карточки ("Dodane 14 lipca
            # 2026", "wczoraj o 21:14" и т.п.) — если получилось, у нас
            # появляется настоящий created_dt (важно для фильтра "период
            # публикации" в filters.py, который без created_dt не работает)
            parsed_dt = _parse_polish_date_text(date_text)
            created_display = format_created_display(parsed_dt) if parsed_dt else date_text

            results.append({
                "title": title,
                "price": price_text,
                "ad_url": ad_url,
                "chat_url": f"{ad_url}?chat=1&isPreviewActive=0",
                "location": None,      # добираем с детальной страницы
                "photo": photo,
                "created_dt": parsed_dt,
                "created_display": created_display,
                "seller": None,        # добираем с детальной страницы
                "delivery": None,      # добираем с детальной страницы
                "seller_registered": None,
                "seller_last_seen": None,
                "seller_ads_count": None,
                "seller_rating": None,
                "seller_reviews_count": None,
            })
        if results:
            return results
    return []


# ---------------------------------------------------------------------------
# Детальная страница объявления — продавец, доставка, фото, локация
# ---------------------------------------------------------------------------

DETAIL_SELLER_NAME_SELECTORS = [
    '[data-testid="seller-name"]',
    '[data-cy="seller_card"] h4',
    '.css-1lcz6o7 h4',
    'a[href*="/oferty/uzytkownik/"]',
]

DETAIL_DELIVERY_SELECTORS = [
    '[data-testid="courier-delivery"]',
    '[data-cy="delivery-icon"]',
    '.css-19zqekf',
]

# Текстовые регулярки-эвристики для блока продавца — OLX не всегда отдаёт эти
# данные через понятные data-атрибуты, поэтому ищем по характерным фразам
# в тексте страницы. Если после смены вёрстки OLX данные перестанут
# находиться — обновите шаблоны ниже, посмотрев актуальный текст на странице
# объявления (Ctrl+F по словам "OLX od", "Aktywność", "ogłosze").
## ВАЖНО: старые версии этих регулярок брали "до 30/40 любых символов
## подряд" ([^\n<]{3,30}) в качестве значения. Т.к. текст со страницы
## извлекается одной строкой без переносов (get_text(" ")), это давало
## наложение полей друг на друга — например, "grudzień 2023 Ostatnio
## online" вместо просто "grudzień 2023" (регулярка "съедала" начало
## следующего поля). Теперь каждая регулярка ограничена ЗАВЕДОМО ИЗВЕСТНЫМ
## форматом значения и не может "убежать" в соседний текст.

# "Na OLX od grudnia 2023" / "Na OLX.pl od 8 grudnia 2023" / "Dołączył w grudniu 2023"
_RE_SELLER_SINCE = re.compile(
    r"(?:Na OLX(?:\.pl)? od|Dołączył[a]? w)\s+(?:(\d{1,2})\s+)?(" + _PL_MONTH_ALT + r")\s+(\d{4})",
    re.I,
)

# "Ostatnio widziany dzisiaj o 21:14" / "...wczoraj" / "...3 dni temu" / "...online" (без деталей)
_RE_SELLER_LAST_SEEN = re.compile(
    r"Ostatnio (?:widzian[ay]|aktywn[ay]|online)\b\s*[:\-]?\s*"
    r"(dzisiaj(?:[^\d]{0,6}\d{1,2}:\d{2})?|wczoraj(?:[^\d]{0,6}\d{1,2}:\d{2})?|"
    r"\d+\s+(?:minut[ęy]?|minuta|godzin[ęy]?|godzina|dni|dzień|tydzień|tygodni(?:e)?|miesi[ąę]c\w*)\s+temu)?",
    re.I,
)

# Количество объявлений продавца — на OLX встречается в нескольких формах:
# "12 ogłoszeń", "12 aktywne ogłoszenia", "Ogłoszenia (12)", "ogłoszenia: 12"
_RE_SELLER_ADS_COUNT = re.compile(
    r"(?:(\d+)\s*(?:aktywn\w*\s+)?ogłoszeni?[ae]\b)|"
    r"(?:ogłoszeni[ae]\w*\s*(?:użytkownika)?\s*[:\(]\s*(\d+)\s*\)?)",
    re.I,
)

_RE_SELLER_RATING = re.compile(r"(\d(?:[.,]\d)?)\s*/\s*5")

# Количество отзывов/оценок — "12 ocen", "(12 opinii)", "na podstawie 5 recenzji"
_RE_SELLER_REVIEWS_COUNT = re.compile(
    r"(\d+)\s*(?:ocen[a-ząćęłńóśźż]*|opini[a-ząćęłńóśźż]*|recenzj[a-ząćęłńóśźż]*)", re.I
)


def _extract_seller_from_json(html: str) -> dict:
    """Пытаемся найти объект продавца во встроенном JSON детальной страницы."""
    for pattern in _JSON_SCRIPT_PATTERNS:
        m = pattern.search(html)
        if not m:
            continue
        raw = m.group(1)
        data = None
        for candidate in (raw, raw.encode("utf-8").decode("unicode_escape", errors="ignore") if "\\u" in raw or '\\"' in raw else raw):
            try:
                data = json.loads(candidate)
                break
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        if data is None:
            continue

        result = {}

        def looks_like_seller(obj):
            if not isinstance(obj, dict) or "id" not in obj:
                return False
            has_name = "name" in obj or "firstName" in obj
            has_seller_field = any(k in obj for k in ("createdAt", "lastSeenAt", "lastSeen", "adsCount", "banType"))
            return has_name and has_seller_field

        def walk(node):
            if result:
                return
            if isinstance(node, dict):
                for key in ("user", "author", "seller"):
                    val = node.get(key)
                    if looks_like_seller(val):
                        result.update(val)
                        return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        if result:
            return result
    return {}


def _extract_seller_details(html: str) -> dict:
    """Возвращает словарь с ключами seller_registered/seller_last_seen/
    seller_ads_count/seller_rating/seller_reviews_count, используя JSON,
    если получилось, иначе — текстовые эвристики по всей странице.

    seller_registered всегда приводится к виду ДД.ММ.ГГГГ (если известен
    день) или ММ.ГГГГ (если OLX отдал только месяц и год, как это обычно
    бывает в текстовом варианте "Na OLX od grudnia 2023")."""
    out = {
        "seller_registered": None,
        "seller_last_seen": None,
        "seller_ads_count": None,
        "seller_rating": None,
        "seller_reviews_count": None,
    }

    seller_json = _extract_seller_from_json(html)
    if seller_json:
        created_dt = _parse_iso_datetime(seller_json.get("createdAt"))
        if created_dt:
            out["seller_registered"] = created_dt.astimezone(WARSAW_TZ).strftime("%d.%m.%Y")
        last_seen_raw = seller_json.get("lastSeenAt") or seller_json.get("lastSeen")
        last_seen_dt = _parse_iso_datetime(last_seen_raw) if isinstance(last_seen_raw, str) else None
        if last_seen_dt:
            out["seller_last_seen"] = format_relative_ru(last_seen_dt)
        elif last_seen_raw:
            out["seller_last_seen"] = str(last_seen_raw)
        if seller_json.get("adsCount") is not None:
            out["seller_ads_count"] = seller_json.get("adsCount")
        rating_block = seller_json.get("userRatingV2") or {}
        rating = seller_json.get("rating") or rating_block.get("rating")
        if rating is not None:
            out["seller_rating"] = rating
        reviews_count = (
            seller_json.get("reviewsCount")
            or seller_json.get("ratingsCount")
            or rating_block.get("reviewsCount")
            or rating_block.get("count")
        )
        if reviews_count is not None:
            out["seller_reviews_count"] = reviews_count

    # что не нашли через JSON — добираем текстовыми эвристиками
    if any(v is None for v in out.values()):
        text = re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" "))

        if out["seller_registered"] is None:
            m = _RE_SELLER_SINCE.search(text)
            if m:
                day, month_word, year = m.group(1), m.group(2), m.group(3)
                out["seller_registered"] = _format_pl_month_year(day, month_word, year)

        if out["seller_last_seen"] is None:
            m = _RE_SELLER_LAST_SEEN.search(text)
            if m:
                raw = m.group(1).strip() if m.group(1) else None
                if raw:
                    dt = _parse_polish_date_text(raw)
                    out["seller_last_seen"] = format_relative_ru(dt) if dt else raw
                else:
                    # "Ostatnio online" без уточнения времени — считаем,
                    # что продавец в сети прямо сейчас
                    out["seller_last_seen"] = "сейчас в сети"

        if out["seller_ads_count"] is None:
            m = _RE_SELLER_ADS_COUNT.search(text)
            if m:
                count_str = m.group(1) or m.group(2)
                if count_str:
                    out["seller_ads_count"] = int(count_str)

        if out["seller_rating"] is None:
            m = _RE_SELLER_RATING.search(text)
            if m:
                out["seller_rating"] = m.group(1).replace(",", ".")

        if out["seller_reviews_count"] is None:
            m = _RE_SELLER_REVIEWS_COUNT.search(text)
            if m:
                out["seller_reviews_count"] = int(m.group(1))

    return out


# ---------------------------------------------------------------------------
# Добор цены и даты публикации С ДЕТАЛЬНОЙ СТРАНИЦЫ (если карточка поиска
# их не дала — например, OLX поменял вёрстку страницы выдачи, или объявление
# попало через HTML fallback-парсер, у которого могли не сработать
# селекторы). Раз мы всё равно заходим на страницу объявления за данными
# продавца — пробуем добрать здесь же и цену/дату, несколькими способами по
# очереди (первый сработавший — используется):
#   1) встроенный JSON конкретного объявления (самый надёжный)
#   2) JSON-LD разметка (<script type="application/ld+json">)
#   3) meta-теги (og:price:amount / product:price:amount)
#   4) CSS-селекторы на детальной странице
#   5) запасной текстовый поиск "N zł" в начале страницы
# ---------------------------------------------------------------------------

_LD_JSON_PATTERN = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S)

DETAIL_PRICE_SELECTORS = [
    '[data-testid="ad-price-container"]',
    'h3[data-testid="ad-price-container"]',
    '[data-cy="ad-price-container"]',
    '[data-testid="ad-price"]',
]

DETAIL_DATE_SELECTORS = [
    '[data-testid="ad-posted-at"]',
    '[data-cy="ad-posted-at"]',
    '[data-testid="location-date"]',
]

_RE_PRICE_TEXT = re.compile(r'(\d[\d\s]{0,9})\s*(zł|PLN)\b', re.I)


def _extract_ad_object_from_json(html: str) -> dict:
    """Ищем ОДИН объект объявления (не список) во встроенном JSON детальной
    страницы — на детальной странице объявление обычно лежит прямо в объекте
    props/pageProps (или похожем месте), а не в массиве listings/ads, как на
    странице поиска. Используем те же паттерны скриптов, что и для поиска."""
    for pattern in _JSON_SCRIPT_PATTERNS:
        m = pattern.search(html)
        if not m:
            continue
        raw = m.group(1)
        data = None
        for candidate in (raw, raw.encode("utf-8").decode("unicode_escape", errors="ignore") if "\\u" in raw or '\\"' in raw else raw):
            try:
                data = json.loads(candidate)
                break
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        if data is None:
            continue

        result = {}

        def looks_like_ad(obj):
            return isinstance(obj, dict) and "id" in obj and ("title" in obj or "url" in obj) and "price" in obj

        def walk(node):
            if result:
                return
            if isinstance(node, dict):
                if looks_like_ad(node):
                    result.update(node)
                    return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        if result:
            return result
    return {}


def _extract_price_date_from_ld_json(html: str):
    """JSON-LD (<script type="application/ld+json">) — стандартная SEO-разметка,
    которую многие площадки (в т.ч. OLX) добавляют на страницу товара с ценой
    и датой публикации, независимо от вёрстки/фреймворка."""
    price, currency, date_raw = None, None, None
    for m in _LD_JSON_PATTERN.finditer(html):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            offers = obj.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict) and offers.get("price") is not None:
                price = offers.get("price")
                currency = offers.get("priceCurrency")
            if obj.get("datePublished"):
                date_raw = obj.get("datePublished")
            elif obj.get("dateCreated"):
                date_raw = obj.get("dateCreated")
    return price, currency, date_raw


def _extract_price_from_meta(soup) -> tuple:
    for prop in ("product:price:amount", "og:price:amount"):
        tag = soup.select_one(f'meta[property="{prop}"]')
        if tag and tag.get("content"):
            currency_tag = soup.select_one(
                'meta[property="product:price:currency"], meta[property="og:price:currency"]'
            )
            return tag.get("content"), (currency_tag.get("content") if currency_tag else None)
    return None, None


def _extract_price_from_selectors(soup) -> str:
    for sel in DETAIL_PRICE_SELECTORS:
        tag = soup.select_one(sel)
        text = _text_or_none(tag)
        if text and re.search(r"\d", text):
            return text
    return None


def _extract_price_from_text(html: str) -> str:
    # ограничиваем поиск первыми ~20000 символами страницы, чтобы не
    # зацепить случайную цену из блока "Podobne ogłoszenia" (похожие
    # объявления), который на OLX обычно идёт в конце HTML
    head = html[:20000] if html else ""
    m = _RE_PRICE_TEXT.search(head)
    if m:
        return f"{m.group(1).strip()} {m.group(2)}"
    return None


def _extract_date_from_selectors(soup) -> str:
    for sel in DETAIL_DATE_SELECTORS:
        tag = soup.select_one(sel)
        text = _text_or_none(tag)
        if text:
            return text
    return None


def _fill_price_and_date(ad: dict, html: str, soup) -> None:
    """Добирает цену/дату публикации с детальной страницы объявления, если
    их не удалось получить со страницы выдачи (см. заголовок секции выше)."""
    need_price = ad.get("price") in (None, "", "Цена не указана")
    need_date = ad.get("created_dt") is None and ad.get("created_display") in (None, "", "не указана")

    if not need_price and not need_date:
        return

    # способ 1: встроенный JSON конкретного объявления
    if need_price or need_date:
        ad_json = _extract_ad_object_from_json(html)
        if ad_json:
            if need_price:
                price = ad_json.get("price")
                if isinstance(price, dict):
                    inner = price.get("value")
                    price = inner.get("value") if isinstance(inner, dict) else inner
                if price is not None:
                    ad["price"] = _format_price(price, ad_json.get("currency", "zł"))
                    need_price = False
            if need_date:
                created_raw = ad_json.get("createdAt") or ad_json.get("created_at")
                created_dt = _parse_iso_datetime(created_raw)
                if created_dt:
                    ad["created_dt"] = created_dt
                    ad["created_display"] = format_created_display(created_dt)
                    need_date = False

    # способ 2: JSON-LD
    if need_price or need_date:
        price, currency, date_raw = _extract_price_date_from_ld_json(html)
        if need_price and price is not None:
            ad["price"] = _format_price(price, currency or "zł")
            need_price = False
        if need_date and date_raw:
            created_dt = _parse_iso_datetime(date_raw)
            if created_dt:
                ad["created_dt"] = created_dt
                ad["created_display"] = format_created_display(created_dt)
                need_date = False

    # способ 3: meta-теги
    if need_price:
        price, currency = _extract_price_from_meta(soup)
        if price is not None:
            ad["price"] = _format_price(price, currency or "zł")
            need_price = False

    # способ 4: CSS-селекторы на детальной странице
    if need_price:
        text = _extract_price_from_selectors(soup)
        if text:
            ad["price"] = _normalize_currency(text)
            need_price = False

    if need_date:
        text = _extract_date_from_selectors(soup)
        if text:
            _apply_parsed_date(ad, text)
            need_date = False

    # способ 5: запасной текстовый поиск цены по странице
    if need_price:
        text = _extract_price_from_text(html)
        if text:
            ad["price"] = _normalize_currency(text)
            need_price = False

    if need_price:
        log.warning("Не удалось определить цену для %s ни одним из способов.", ad.get("ad_url"))
    if need_date:
        log.warning("Не удалось определить дату публикации для %s ни одним из способов.", ad.get("ad_url"))


def enrich_with_detail(ad: dict, session: requests.Session) -> tuple:
    """Дозапрашивает страницу объявления и дополняет продавца/доставку/
    локацию/фото. Вызывается для КАЖДОГО объявления, т.к. данные о
    регистрации/рейтинге продавца есть только на детальной странице.

    Возвращает (ad, success) — success=False означает, что дозапрос не
    удался (например, OLX заблокировал запрос), и в ad остались только
    те данные, что были в карточке списка."""
    try:
        html = _get(ad["ad_url"], session)
    except OlxParserError as e:
        log.warning("Не удалось дозапросить детали объявления %s: %s", ad.get("ad_url"), e)
        ad.setdefault("seller", ad.get("seller") or "Не удалось определить")
        ad.setdefault("delivery", ad.get("delivery") if ad.get("delivery") is not None else False)
        _sleep(config.DETAIL_DELAY_MIN, config.DETAIL_DELAY_MAX)
        return ad, False

    soup = BeautifulSoup(html, "html.parser")

    if not ad.get("seller"):
        seller = None
        for sel in DETAIL_SELLER_NAME_SELECTORS:
            tag = soup.select_one(sel)
            if tag:
                seller = _text_or_none(tag)
                if seller:
                    break
        ad["seller"] = seller or "Не указан"

    if ad.get("delivery") is None:
        delivery = False
        for sel in DETAIL_DELIVERY_SELECTORS:
            if soup.select_one(sel):
                delivery = True
                break
        ad["delivery"] = delivery

    if not ad.get("photo"):
        og_img = soup.select_one('meta[property="og:image"]')
        if og_img:
            ad["photo"] = og_img.get("content")

    if not ad.get("location"):
        og_loc = soup.select_one('[data-testid="map-aside-section"], [data-testid="location-date"]')
        if og_loc:
            ad["location"] = _text_or_none(og_loc)
        ad["location"] = ad.get("location") or "Не указано"

    seller_details = _extract_seller_details(html)
    for key, value in seller_details.items():
        if value is not None:
            ad[key] = value

    # Цена и дата публикации: если карточка поиска их не дала (не нашли в
    # JSON выдачи / не сработали селекторы HTML-фолбэка) — добираем прямо
    # со страницы объявления, которую мы всё равно уже загрузили.
    _fill_price_and_date(ad, html, soup)

    _sleep(config.DETAIL_DELAY_MIN, config.DETAIL_DELAY_MAX)
    return ad, True


# ---------------------------------------------------------------------------
# Параллельный дозапрос деталей (ускорение)
# ---------------------------------------------------------------------------

def enrich_many(ads: list, max_workers: int = None, circuit_breaker: int = DETAIL_CIRCUIT_BREAKER):
    """
    Дозапрашивает детальные страницы для СПИСКА объявлений параллельно —
    пачками по `max_workers` штук одновременно (разные потоки, разные
    HTTP-сессии), вместо строго последовательного "один за другим".

    Это главный источник ускорения парсинга: раньше дозапрос N объявлений
    занимал ~N * (DETAIL_DELAY + время запроса) секунд строго
    последовательно; теперь — примерно в max_workers раз быстрее, т.к.
    запросы внутри пачки идут одновременно. Пауза DETAIL_DELAY между
    запросами и ротация User-Agent сохраняются как и раньше — просто
    теперь несколько таких "потоков ожидания" работают параллельно.

    Это generator: отдаёт (ad, ok) по мере готовности, пачками, в ТОМ ЖЕ
    порядке, что и на входе — вызывающий код (bot.py) может отправлять
    объявления в чат сразу, не дожидаясь всего списка целиком.

    Схема защиты от бана (circuit breaker) сохранена: если подряд
    провалилось >= circuit_breaker дозапросов — дальнейший дозапрос
    полностью останавливается, а оставшиеся объявления отдаются как есть
    (ok=False, без данных о продавце), без новых запросов к OLX.
    """
    max_workers = max_workers or config.DETAIL_CONCURRENCY
    n = len(ads)
    idx = 0
    consecutive_failures = 0

    while idx < n:
        batch = ads[idx: idx + max_workers]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as executor:
            future_to_pos = {
                executor.submit(enrich_with_detail, ad, _session()): pos
                for pos, ad in enumerate(batch)
            }
            batch_results = [None] * len(batch)
            for future in concurrent.futures.as_completed(future_to_pos):
                pos = future_to_pos[future]
                try:
                    batch_results[pos] = future.result()
                except Exception as e:
                    log.error("Ошибка обогащения объявления %s: %s", batch[pos].get("ad_url"), e)
                    batch_results[pos] = (batch[pos], False)

        for pos, (ad, ok) in enumerate(batch_results):
            consecutive_failures = 0 if ok else consecutive_failures + 1
            yield ad, ok
            if consecutive_failures >= circuit_breaker:
                stopped_from = idx + pos + 1
                log.error(
                    "%s дозапросов деталей подряд провалились — похоже, OLX "
                    "блокирует запросы. Останавливаю дозапрос деталей, "
                    "оставшиеся %s объявлений уйдут с неполными данными.",
                    consecutive_failures, n - stopped_from,
                )
                for j in range(stopped_from, n):
                    yield ads[j], False
                return

        idx += len(batch)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def search_ads(query_or_url: str, limit: int, filters: dict = None, progress_cb=None, session=None,
               exclude_urls: set = None):
    """
    Ищет объявления по страницам выдачи OLX и возвращает РОВНО до `limit`
    штук (без искусственного раздувания x3/x5 — раньше при limit=20 парсер
    мог насобирать и дозапросить детали для ~100 объявлений, из-за чего
    первые же 20 не успевали получить данные о продавце до срабатывания
    защиты от блокировки). Детальные данные о продавце сюда НЕ входят —
    для них отдельно вызывается enrich_with_detail()/enrich_many().

    exclude_urls: множество ссылок объявлений, которые уже показывались
    этому пользователю раньше (см. history.py) — такие объявления
    пропускаются, а парсер продолжает листать страницы дальше, пока не
    наберёт `limit` действительно НОВЫХ объявлений (или не упрётся в
    MAX_PAGES / конец выдачи). Это и есть защита от повторной отправки
    одних и тех же объявлений при повторном /parse по тому же запросу.
    """
    filters = filters or {}
    session = session or _session()
    exclude_urls = exclude_urls or set()
    all_ads = []
    seen_urls = set()

    log.info("Начинаю поиск: query=%r limit=%s exclude=%s", query_or_url, limit, len(exclude_urls))

    for page in range(1, config.MAX_PAGES + 1):
        url = build_search_url(query_or_url, page, filters)
        try:
            html = _get(url, session)
        except OlxParserError as e:
            log.error("Страница %s не загрузилась: %s", page, e)
            if page == 1:
                raise
            break

        json_ads = _try_extract_embedded_json(html)
        if json_ads:
            page_ads = [_normalize_json_ad(a) for a in json_ads]
        else:
            page_ads = _parse_cards_html(html)

        log.info("Страница %s: карточек найдено %s (JSON=%s)", page, len(page_ads), bool(json_ads))

        if not page_ads:
            break

        # new_on_page считает ЛЮБЫЕ карточки, которых мы ещё не видели в
        # этом прогоне (даже уже отправленные раньше) — так пагинация не
        # останавливается раньше времени только из-за того, что вся первая
        # страница состоит из старых объявлений.
        new_on_page = 0
        for ad in page_ads:
            ad_url = ad.get("ad_url")
            if not ad_url or ad_url in seen_urls:
                continue
            seen_urls.add(ad_url)
            new_on_page += 1
            if ad_url in exclude_urls:
                continue
            all_ads.append(ad)

        if progress_cb:
            progress_cb("page", page, len(all_ads))

        if new_on_page == 0:
            break

        # раньше тут было max(limit*3, limit+20) — набирали в разы больше,
        # чем просили, и потом дозапрашивали детали для всех лишних.
        if len(all_ads) >= limit:
            break

        _sleep(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)

    if not all_ads:
        raise OlxParserError(
            "Новых объявлений не найдено. Либо по запросу/фильтрам пусто, "
            "либо все найденные объявления уже были показаны раньше "
            "(см. /resetseen), либо OLX изменил вёрстку страницы или "
            "заблокировал запрос."
        )

    return all_ads[:limit]


def parse_olx(query_or_url: str, limit: int, filters: dict = None, fetch_details: bool = True, progress_cb=None,
              exclude_urls: set = None):
    """
    Обёртка над search_ads() + enrich_many() "всё за один вызов" —
    используется там, где стриминг по одному объявлению не нужен
    (сейчас — только /monitor). Для /parse в bot.py используется прямой
    вызов search_ads() + enrich_many() в цикле, чтобы отправлять
    объявления в чат по мере готовности.
    """
    session = _session()
    all_ads = search_ads(query_or_url, limit, filters=filters, progress_cb=progress_cb, session=session,
                          exclude_urls=exclude_urls)

    log.info("Собрано объявлений: %s. Дозапрос деталей: %s", len(all_ads), fetch_details)

    if fetch_details:
        result = []
        total = len(all_ads)
        for i, (ad, ok) in enumerate(enrich_many(all_ads), start=1):
            result.append(ad)
            if progress_cb:
                progress_cb("detail", i, total)
        return result

    return all_ads
