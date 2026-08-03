"""
Парсер объявлений OLX.pl и OLX.pt.
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
except Exception:
    WARSAW_TZ = timezone.utc

import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger("olx_bot.parser")

DETAIL_CIRCUIT_BREAKER = 5

# --- Множитель сбора (чтобы после фильтрации осталось достаточно) ---
COLLECT_MULTIPLIER = 3

class OlxParserError(Exception):
    pass

# ---------------------------------------------------------------------------
# Словари месяцев для разных локалей
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

_PT_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    # варианты с "de" (ex: "janeiro de 2025")
}

_LOCALE_MONTHS = {
    'pl': _PL_MONTHS,
    'pt': _PT_MONTHS,
}

# ---------------------------------------------------------------------------
# Регулярки для продавца (зависят от языка)
# ---------------------------------------------------------------------------
def _build_seller_patterns(locale: str):
    if locale == 'pl':
        return {
            'since': re.compile(
                r"(?:Na OLX(?:\.pl)? od|Dołączył[a]? w)\s+(?:(\d{1,2})\s+)?(" + '|'.join(sorted(_PL_MONTHS.keys(), key=len, reverse=True)) + r")\s+(\d{4})",
                re.I
            ),
            'last_seen': re.compile(
                r"Ostatnio (?:widzian[ay]|aktywn[ay]|online)\b\s*[:\-]?\s*"
                r"(dzisiaj(?:[^\d]{0,6}\d{1,2}:\d{2})?|wczoraj(?:[^\d]{0,6}\d{1,2}:\d{2})?|"
                r"\d+\s+(?:minut[ęy]?|minuta|godzin[ęy]?|godzina|dni|dzień|tydzień|tygodni(?:e)?|miesi[ąę]c\w*)\s+temu)?",
                re.I
            ),
            'ads_count': re.compile(
                r"(?:(\d+)\s*(?:aktywn\w*\s+)?ogłoszeni?[ae]\b)|"
                r"(?:ogłoszeni[ae]\w*\s*(?:użytkownika)?\s*[:\(]\s*(\d+)\s*\)?)",
                re.I
            ),
            'rating': re.compile(r"(\d(?:[.,]\d)?)\s*/\s*5"),
            'reviews': re.compile(r"(\d+)\s*(?:ocen[a-ząćęłńóśźż]*|opini[a-ząćęłńóśźż]*|recenzj[a-ząćęłńóśźż]*)", re.I),
        }
    elif locale == 'pt':
        return {
            'since': re.compile(
                r"(?:Registado em|Anunciado em|Membro desde)\s+(?:(\d{1,2})\s+)?(" + '|'.join(sorted(_PT_MONTHS.keys(), key=len, reverse=True)) + r")\s+(?:de\s+)?(\d{4})",
                re.I
            ),
            'last_seen': re.compile(
                r"(?:Última (?:vez online|atividade)|Online)\s*[:\-]?\s*"
                r"(?:Hoje(?:[^\d]{0,6}\d{1,2}:\d{2})?|Ontem(?:[^\d]{0,6}\d{1,2}:\d{2})?|"
                r"\d+\s+(?:minuto|minutos|hora|horas|dia|dias|semana|semanas|mês|meses)\s+atrás)?",
                re.I
            ),
            'ads_count': re.compile(
                r"(?:(\d+)\s*(?:anúncios?|anúncios ativos?))|"
                r"(?:anúncios?[:\s]+(\d+))",
                re.I
            ),
            'rating': re.compile(r"(\d(?:[.,]\d)?)\s*/\s*5"),
            'reviews': re.compile(r"(\d+)\s*(?:avaliaç[ão]es?|comentários?|opiniões?)", re.I),
        }
    else:
        raise ValueError(f"Unsupported locale: {locale}")

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def detect_locale(url_or_query: str) -> str:
    """Определяет локаль по ссылке (olx.pl или olx.pt). Если не ссылка – default."""
    if url_or_query.startswith(("http://", "https://")):
        if "olx.pt" in url_or_query:
            return 'pt'
    return config.DEFAULT_DOMAIN

def _get_domain_config(locale: str):
    return config.OLX_DOMAINS.get(locale, config.OLX_DOMAINS[config.DEFAULT_DOMAIN])

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_random_headers())
    return s

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
                last_err = OlxParserError(f"OLX вернул статус {resp.status_code} (похоже на временную блокировку).")
                _sleep(4 + attempt * 2, 7 + attempt * 3)
                continue
            log.warning("GET %s -> неожиданный статус %s", url, resp.status_code)
            last_err = OlxParserError(f"OLX вернул статус {resp.status_code} для {url}.")
        except requests.RequestException as e:
            log.warning("GET %s -> сетевая ошибка: %s", url, e)
            last_err = OlxParserError(f"Сетевая ошибка при запросе {url}: {e}")
        _sleep(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)
    raise last_err or OlxParserError(f"Не удалось загрузить {url}")

def build_search_url(query_or_url: str, page: int = 1, filters: dict = None, locale: str = None) -> str:
    """Строит URL с учётом домена и фильтров."""
    locale = locale or detect_locale(query_or_url)
    domain_cfg = _get_domain_config(locale)
    base_domain = domain_cfg['base_url']

    if query_or_url.startswith(("http://", "https://")):
        base = query_or_url
    else:
        safe_q = re.sub(r"\s+", "-", query_or_url.strip())
        category_slug = (filters or {}).get("category_slug", "").strip("/")
        prefix = f"{category_slug}/" if category_slug else "oferty/"
        base = f"{base_domain}/{prefix}q-{safe_q}/"

    parsed = urlparse(base)
    qs = parse_qs(parsed.query)
    if page > 1:
        qs["page"] = [str(page)]
    else:
        qs.pop("page", None)

    # Цена
    price_min = (filters or {}).get("price_min")
    price_max = (filters or {}).get("price_max")
    if price_min is not None:
        qs["search[filter_float_price:from]"] = [str(price_min)]
    if price_max is not None:
        qs["search[filter_float_price:to]"] = [str(price_max)]

    # Доставка (только True передаём в URL)
    if (filters or {}).get("delivery") is True:
        qs["search[filter_enum_delivery][0]"] = ["1"]

    # Сортировка – сначала новые
    qs["search[order]"] = ["created_at:desc"]

    new_query = urlencode(qs, doseq=True)
    rebuilt = parsed._replace(query=new_query)
    return rebuilt.geturl()

# ---------------------------------------------------------------------------
# Парсинг дат на разных языках
# ---------------------------------------------------------------------------
def _parse_date_text(text: str, locale: str) -> datetime:
    """Пытается распарсить дату из текста (польский или португальский)."""
    if not text:
        return None
    months = _LOCALE_MONTHS.get(locale, {})
    if not months:
        return None

    # Собираем регулярку для полной даты с месяцами
    month_alt = '|'.join(sorted(months.keys(), key=len, reverse=True))
    full_pattern = re.compile(
        r"(\d{1,2})\s+(" + month_alt + r")\s+(?:de\s+)?(\d{4})(?:[^\d]{0,6}(\d{1,2}):(\d{2}))?",
        re.I
    )
    rel_pattern = re.compile(r"\b(hoje|dzisiaj|wczoraj|ontem)\b(?:[^\d]{0,6}(\d{1,2}):(\d{2}))?", re.I)

    m = full_pattern.search(text)
    if m:
        day, month_word, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month = months.get(month_word)
        if month:
            hour = int(m.group(4)) if m.group(4) else 0
            minute = int(m.group(5)) if m.group(5) else 0
            try:
                dt = datetime(year, month, day, hour, minute, tzinfo=WARSAW_TZ)
                return dt.astimezone(timezone.utc)
            except ValueError:
                return None

    m = rel_pattern.search(text)
    if m:
        word = m.group(1).lower()
        hour = int(m.group(2)) if m.group(2) else 0
        minute = int(m.group(3)) if m.group(3) else 0
        now_warsaw = datetime.now(WARSAW_TZ)
        if word in ("dzisiaj", "hoje"):
            base = now_warsaw.date()
        elif word in ("wczoraj", "ontem"):
            base = (now_warsaw - timedelta(days=1)).date()
        else:
            return None
        try:
            dt = datetime(base.year, base.month, base.day, hour, minute, tzinfo=WARSAW_TZ)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None

def _format_pl_pt_month(day, month_word: str, year: str, locale: str) -> str:
    months = _LOCALE_MONTHS.get(locale, {})
    month = months.get(month_word.lower())
    if not month:
        return None
    if day:
        return f"{int(day):02d}.{month:02d}.{year}"
    return f"{month:02d}.{year}"

def _normalize_currency(text: str, locale: str = 'pl') -> str:
    """Приводит валюту к единому виду (ZL или EUR)."""
    if not text:
        return text
    domain_cfg = _get_domain_config(locale)
    currency = domain_cfg['currency']
    # заменяем любые обозначения на валюту локали
    if locale == 'pl':
        return re.sub(r"z[łl]\.?|PLN", currency, text, flags=re.I)
    elif locale == 'pt':
        return re.sub(r"€|EUR", currency, text, flags=re.I)
    return text

# ---------------------------------------------------------------------------
# Встроенный JSON (не зависит от локали)
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

    seen = set()
    unique = []
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

def _format_price(value, currency="ZL") -> str:
    if value in (None, "", "Do negocjacji"):
        return "Цена не указана"
    try:
        value = float(str(value).replace(" ", "").replace(",", "."))
        return f"{value:,.0f} {currency}".replace(",", " ")
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
    if dt is None:
        return "не указана"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local_dt = dt.astimezone(WARSAW_TZ)
    return f"{local_dt.strftime('%d.%m.%Y %H:%M')} ({format_relative_ru(dt)})"

def _apply_parsed_date(ad: dict, raw_text: str, locale: str) -> None:
    dt = _parse_date_text(raw_text, locale)
    if dt:
        ad["created_dt"] = dt
        ad["created_display"] = format_created_display(dt)
    else:
        ad["created_display"] = raw_text

# ---------------------------------------------------------------------------
# HTML-парсинг карточек (с учётом локали)
# ---------------------------------------------------------------------------
CARD_SELECTORS = [
    {"container": '[data-cy="l-card"]', "title": 'h4, h6, [data-cy="ad-card-title"] h4', "price": '[data-testid="ad-price"]', "link": "a", "img": "img", "date": '[data-testid="location-date"]'},
    {"container": "div.css-1sw7q4x", "title": "h6", "price": "p.css-10b0gli", "link": "a.css-1tqlkj0", "img": "img", "date": "p.css-veheph"},
    {"container": "article", "title": "h4, h6", "price": '[data-testid="ad-price"], .price', "link": "a", "img": "img", "date": ".css-veheph, time"},
]

def _text_or_none(tag):
    if tag is None:
        return None
    txt = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
    return txt or None

def _parse_cards_html(html: str, locale: str):
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
            ad_url = href if href.startswith("http") else urljoin(_get_domain_config(locale)['base_url'], href)

            title_tag = card.select_one(sel["title"])
            title = _text_or_none(title_tag) or _text_or_none(link_tag) or "Без названия"

            price_tag = card.select_one(sel["price"])
            price_text = _text_or_none(price_tag) or "Цена не указана"
            if price_text != "Цена не указана":
                price_text = _normalize_currency(price_text, locale)

            img_tag = card.select_one(sel["img"])
            photo = None
            if img_tag:
                photo = img_tag.get("src") or img_tag.get("data-src")

            date_tag = card.select_one(sel["date"])
            date_text = _text_or_none(date_tag) or "не указана"

            parsed_dt = _parse_date_text(date_text, locale)
            created_display = format_created_display(parsed_dt) if parsed_dt else date_text

            results.append({
                "title": title,
                "price": price_text,
                "ad_url": ad_url,
                "chat_url": f"{ad_url}?chat=1&isPreviewActive=0",
                "location": None,
                "photo": photo,
                "created_dt": parsed_dt,
                "created_display": created_display,
                "seller": None,
                "delivery": None,
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
# Детальная страница – данные продавца с учётом локали
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

def _extract_seller_from_json(html: str) -> dict:
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

def _extract_seller_details(html: str, locale: str) -> dict:
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

    # Текстовые эвристики (с учётом локали)
    if any(v is None for v in out.values()):
        soup = BeautifulSoup(html, "html.parser")
        text = re.sub(r"\s+", " ", soup.get_text(" "))
        patterns = _build_seller_patterns(locale)

        if out["seller_registered"] is None:
            m = patterns['since'].search(text)
            if m:
                day, month_word, year = m.group(1), m.group(2), m.group(3)
                out["seller_registered"] = _format_pl_pt_month(day, month_word, year, locale)

        if out["seller_last_seen"] is None:
            m = patterns['last_seen'].search(text)
            if m:
                raw = m.group(1).strip() if m.group(1) else None
                if raw:
                    dt = _parse_date_text(raw, locale)
                    out["seller_last_seen"] = format_relative_ru(dt) if dt else raw
                else:
                    out["seller_last_seen"] = "сейчас в сети"

        if out["seller_ads_count"] is None:
            m = patterns['ads_count'].search(text)
            if m:
                count_str = m.group(1) or m.group(2)
                if count_str:
                    out["seller_ads_count"] = int(count_str)

        if out["seller_rating"] is None:
            m = patterns['rating'].search(text)
            if m:
                out["seller_rating"] = m.group(1).replace(",", ".")

        if out["seller_reviews_count"] is None:
            m = patterns['reviews'].search(text)
            if m:
                out["seller_reviews_count"] = int(m.group(1))

    return out

# ---------------------------------------------------------------------------
# Добор цены и даты с детальной страницы (не зависит от локали)
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
_RE_PRICE_TEXT = re.compile(r'(\d[\d\s]{0,9})\s*(zł|PLN|€|EUR)\b', re.I)

def _extract_ad_object_from_json(html: str) -> dict:
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

def _fill_price_and_date(ad: dict, html: str, soup, locale: str) -> None:
    need_price = ad.get("price") in (None, "", "Цена не указана")
    need_date = ad.get("created_dt") is None and ad.get("created_display") in (None, "", "не указана")

    if not need_price and not need_date:
        return

    # способ 1: встроенный JSON
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

    # способ 3: meta
    if need_price:
        price, currency = _extract_price_from_meta(soup)
        if price is not None:
            ad["price"] = _format_price(price, currency or "zł")
            need_price = False

    # способ 4: CSS
    if need_price:
        text = _extract_price_from_selectors(soup)
        if text:
            ad["price"] = _normalize_currency(text, locale)
            need_price = False
    if need_date:
        text = _extract_date_from_selectors(soup)
        if text:
            _apply_parsed_date(ad, text, locale)
            need_date = False

    # способ 5: текст
    if need_price:
        text = _extract_price_from_text(html)
        if text:
            ad["price"] = _normalize_currency(text, locale)
            need_price = False

    if need_price:
        log.warning("Не удалось определить цену для %s", ad.get("ad_url"))
    if need_date:
        log.warning("Не удалось определить дату для %s", ad.get("ad_url"))

# ---------------------------------------------------------------------------
# Обогащение деталями
# ---------------------------------------------------------------------------
def enrich_with_detail(ad: dict, session: requests.Session, locale: str) -> tuple:
    try:
        html = _get(ad["ad_url"], session)
    except OlxParserError as e:
        log.warning("Не удалось дозапросить детали %s: %s", ad.get("ad_url"), e)
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

    seller_details = _extract_seller_details(html, locale)
    for key, value in seller_details.items():
        if value is not None:
            ad[key] = value

    _fill_price_and_date(ad, html, soup, locale)

    _sleep(config.DETAIL_DELAY_MIN, config.DETAIL_DELAY_MAX)
    return ad, True

def enrich_many(ads: list, max_workers: int = None, locale: str = 'pl', circuit_breaker: int = DETAIL_CIRCUIT_BREAKER):
    max_workers = max_workers or config.DETAIL_CONCURRENCY
    n = len(ads)
    idx = 0
    consecutive_failures = 0

    while idx < n:
        batch = ads[idx: idx + max_workers]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as executor:
            future_to_pos = {
                executor.submit(enrich_with_detail, ad, _session(), locale): pos
                for pos, ad in enumerate(batch)
            }
            batch_results = [None] * len(batch)
            for future in concurrent.futures.as_completed(future_to_pos):
                pos = future_to_pos[future]
                try:
                    batch_results[pos] = future.result()
                except Exception as e:
                    log.error("Ошибка обогащения %s: %s", batch[pos].get("ad_url"), e)
                    batch_results[pos] = (batch[pos], False)

        for pos, (ad, ok) in enumerate(batch_results):
            consecutive_failures = 0 if ok else consecutive_failures + 1
            yield ad, ok
            if consecutive_failures >= circuit_breaker:
                stopped_from = idx + pos + 1
                log.error("Сработал circuit breaker, останавливаю дозапрос.")
                for j in range(stopped_from, n):
                    yield ads[j], False
                return
        idx += len(batch)

# ---------------------------------------------------------------------------
# Основной поиск (с учётом локали и буфера)
# ---------------------------------------------------------------------------
def search_ads(query_or_url: str, limit: int, filters: dict = None,
               progress_cb=None, session=None, exclude_urls: set = None,
               locale: str = None) -> list:
    """
    Возвращает СПИСОК объявлений (без деталей продавца) размером до limit * COLLECT_MULTIPLIER.
    """
    locale = locale or detect_locale(query_or_url)
    filters = filters or {}
    session = session or _session()
    exclude_urls = exclude_urls or set()

    all_ads = []
    seen_urls = set()
    target_collect = limit * COLLECT_MULTIPLIER

    log.info("Поиск: %s, цель сбора: %s", query_or_url, target_collect)

    for page in range(1, config.MAX_PAGES + 1):
        url = build_search_url(query_or_url, page, filters, locale)
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
            page_ads = _parse_cards_html(html, locale)

        log.info("Страница %s: карточек %s (JSON=%s)", page, len(page_ads), bool(json_ads))

        if not page_ads:
            break

        new_on_page = 0
        for ad in page_ads:
            ad_url = ad.get("ad_url")
            if not ad_url or ad_url in seen_urls:
                continue
            seen_urls.add(ad_url)
            new_on_page += 1
            if ad_url in exclude_urls:
                continue
            # Если объявление уже есть в all_ads по ссылке – пропускаем (защита от дублей)
            if ad_url in [a.get("ad_url") for a in all_ads]:
                continue
            all_ads.append(ad)

        if progress_cb:
            progress_cb("page", page, len(all_ads))

        if new_on_page == 0:
            break

        if len(all_ads) >= target_collect:
            break

        _sleep(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)

    if not all_ads:
        raise OlxParserError(
            "Новых объявлений не найдено. Возможно, все уже показаны, или фильтры слишком строгие."
        )

    return all_ads

def parse_olx(query_or_url: str, limit: int, filters: dict = None,
              fetch_details: bool = True, progress_cb=None,
              exclude_urls: set = None, locale: str = None) -> list:
    locale = locale or detect_locale(query_or_url)
    session = _session()
    ads = search_ads(query_or_url, limit, filters, progress_cb, session, exclude_urls, locale)

    if fetch_details:
        result = []
        total = len(ads)
        for i, (ad, ok) in enumerate(enrich_many(ads, locale=locale), start=1):
            result.append(ad)
            if progress_cb:
                progress_cb("detail", i, total)
        return result

    return ads
