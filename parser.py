"""
Парсер объявлений OLX.pl.

Стратегия (устойчивость к смене вёрстки):
  1) Пытаемся вытащить встроенный JSON с данными объявлений
     (Next.js __NEXT_DATA__ / __PRERENDERED_STATE__ и т.п.) — самый надёжный путь.
  2) Если не вышло — разбираем HTML через BeautifulSoup, перебирая несколько
     наборов CSS-селекторов (на случай, если OLX поменял классы/атрибуты).
  3) Для КАЖДОГО объявления дозапрашивается страница объявления — там же
     достаём расширенные данные о продавце (регистрация, был в сети,
     кол-во объявлений, рейтинг), которых нет в карточке списка.

ВАЖНО: OLX регулярно меняет вёрстку и защиту от ботов. Если через какое-то
время парсер перестанет находить объявления или данные о продавце — в первую
очередь проверьте актуальные селекторы через "Просмотр кода страницы" в
браузере и обновите CARD_SELECTORS / SELLER_TEXT_PATTERNS ниже.
"""
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlencode, parse_qs

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
        "created_display": format_relative_ru(created_dt) if created_dt else (created_raw or "не указана"),
        "photo": photo,
        # поля продавца — дозаполняются на детальной странице
        "seller_registered": None,
        "seller_last_seen": None,
        "seller_ads_count": None,
        "seller_rating": None,
    }


def _format_price(value, currency="zł") -> str:
    if value in (None, "", "Do negocjacji"):
        return "Цена не указана"
    try:
        value = float(str(value).replace(" ", "").replace(",", "."))
        return f"{value:,.0f} {currency}".replace(",", " ")
    except (ValueError, TypeError):
        return f"{value} {currency}"


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
    txt = re.sub(r"\s+", " ", tag.get_text(strip=True))
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

            img_tag = card.select_one(sel["img"])
            photo = None
            if img_tag:
                photo = img_tag.get("src") or img_tag.get("data-src")

            date_tag = card.select_one(sel["date"])
            date_text = _text_or_none(date_tag) or "не указана"

            results.append({
                "title": title,
                "price": price_text,
                "ad_url": ad_url,
                "chat_url": f"{ad_url}?chat=1&isPreviewActive=0",
                "location": None,      # добираем с детальной страницы
                "photo": photo,
                "created_dt": None,    # текст с карточки не всегда парсится в дату
                "created_display": date_text,
                "seller": None,        # добираем с детальной страницы
                "delivery": None,      # добираем с детальной страницы
                "seller_registered": None,
                "seller_last_seen": None,
                "seller_ads_count": None,
                "seller_rating": None,
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
_RE_SELLER_SINCE = re.compile(r"(?:Na OLX od|Na OLX\.pl od|Dołączył[a]? w)\s*[:\-]?\s*([^\n<]{3,30})", re.I)
_RE_SELLER_LAST_SEEN = re.compile(r"(?:Aktywność|Ostatnio (?:widziany|aktywny|online))\s*[:\-]?\s*([^\n<]{3,40})", re.I)
_RE_SELLER_ADS_COUNT = re.compile(r"(\d+)\s*(?:ogłoszeni?[ae]|ogłosze[ń])", re.I)
_RE_SELLER_RATING = re.compile(r"(\d(?:[.,]\d)?)\s*/\s*5")


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
    seller_ads_count/seller_rating, используя JSON, если получилось, иначе —
    текстовые эвристики по всей странице."""
    out = {
        "seller_registered": None,
        "seller_last_seen": None,
        "seller_ads_count": None,
        "seller_rating": None,
    }

    seller_json = _extract_seller_from_json(html)
    if seller_json:
        created_dt = _parse_iso_datetime(seller_json.get("createdAt"))
        if created_dt:
            out["seller_registered"] = created_dt.strftime("%Y-%m-%d")
        last_seen_raw = seller_json.get("lastSeenAt") or seller_json.get("lastSeen")
        last_seen_dt = _parse_iso_datetime(last_seen_raw) if isinstance(last_seen_raw, str) else None
        if last_seen_dt:
            out["seller_last_seen"] = format_relative_ru(last_seen_dt)
        elif last_seen_raw:
            out["seller_last_seen"] = str(last_seen_raw)
        if seller_json.get("adsCount") is not None:
            out["seller_ads_count"] = seller_json.get("adsCount")
        rating = seller_json.get("rating") or (seller_json.get("userRatingV2") or {}).get("rating")
        if rating is not None:
            out["seller_rating"] = rating

    # что не нашли через JSON — добираем текстовыми эвристиками
    if any(v is None for v in out.values()):
        text = re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" "))
        if out["seller_registered"] is None:
            m = _RE_SELLER_SINCE.search(text)
            if m:
                out["seller_registered"] = m.group(1).strip()
        if out["seller_last_seen"] is None:
            m = _RE_SELLER_LAST_SEEN.search(text)
            if m:
                out["seller_last_seen"] = m.group(1).strip()
        if out["seller_ads_count"] is None:
            m = _RE_SELLER_ADS_COUNT.search(text)
            if m:
                out["seller_ads_count"] = int(m.group(1))
        if out["seller_rating"] is None:
            m = _RE_SELLER_RATING.search(text)
            if m:
                out["seller_rating"] = m.group(1).replace(",", ".")

    return out


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

    _sleep(config.DETAIL_DELAY_MIN, config.DETAIL_DELAY_MAX)
    return ad, True


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def parse_olx(query_or_url: str, limit: int, filters: dict = None, fetch_details: bool = True, progress_cb=None):
    """
    Возвращает список словарей объявлений (после применения серверных
    фильтров цены/категории/доставки; клиентские фильтры — период/банворды/
    продавец — применяются отдельно через filters.apply_client_filters,
    т.к. для них нужны уже дозапрошенные детальные данные).

    Может вернуть больше, чем `limit` — ограничение на число сообщений в
    чат применяется отдельно в bot.py, а в файл olx.txt пишутся ВСЕ
    найденные объявления.
    """
    filters = filters or {}
    session = _session()
    all_ads = []
    seen_urls = set()

    log.info("Начинаю парсинг: query=%r limit=%s", query_or_url, limit)

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

        new_on_page = 0
        for ad in page_ads:
            if not ad.get("ad_url") or ad["ad_url"] in seen_urls:
                continue
            seen_urls.add(ad["ad_url"])
            all_ads.append(ad)
            new_on_page += 1

        if progress_cb:
            progress_cb("page", page, len(all_ads))

        if new_on_page == 0:
            break

        if len(all_ads) >= max(limit * 3, limit + 20):
            break

        _sleep(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)

    if not all_ads:
        raise OlxParserError(
            "Объявления не найдены. Либо по запросу/фильтрам пусто, либо OLX "
            "изменил вёрстку страницы (или заблокировал запрос) и парсер не "
            "смог распознать карточки объявлений."
        )

    log.info("Собрано объявлений: %s. Дозапрос деталей: %s", len(all_ads), fetch_details)

    if fetch_details:
        consecutive_failures = 0
        total = len(all_ads)
        for i, ad in enumerate(all_ads, start=1):
            ad, ok = enrich_with_detail(ad, session)
            consecutive_failures = 0 if ok else consecutive_failures + 1

            if progress_cb:
                progress_cb("detail", i, total)

            if consecutive_failures >= DETAIL_CIRCUIT_BREAKER:
                log.error(
                    "%s дозапросов деталей подряд провалились — похоже, OLX "
                    "блокирует запросы. Останавливаю дозапрос деталей, "
                    "оставшиеся %s объявлений уйдут с неполными данными.",
                    consecutive_failures, total - i,
                )
                break

    return all_ads
