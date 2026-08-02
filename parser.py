"""
Парсер объявлений OLX.pl.

Стратегия (устойчивость к смене вёрстки):
  1) Пытаемся вытащить встроенный JSON с данными объявлений
     (Next.js __NEXT_DATA__ / __PRERENDERED_STATE__ и т.п.) — самый надёжный путь.
  2) Если не вышло — разбираем HTML через BeautifulSoup, перебирая несколько
     наборов CSS-селекторов (на случай, если OLX поменял классы/атрибуты).
  3) Если в списке результатов не было продавца/доставки — дозапрашиваем
     страницу конкретного объявления (с задержкой и запасными селекторами).

ВАЖНО: OLX регулярно меняет вёрстку и защиту от ботов. Если через какое-то
время парсер перестанет находить объявления — в первую очередь проверьте
актуальные селекторы через "Просмотр кода страницы" в браузере и обновите
CARD_SELECTORS / DETAIL_*_SELECTORS ниже.
"""
import json
import random
import re
import time
from urllib.parse import urljoin, urlparse, urlencode, parse_qs

import requests
from bs4 import BeautifulSoup

import config


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
                return resp.text
            if resp.status_code in (403, 429):
                # похоже на бан/рейт-лимит — ждём дольше и меняем User-Agent
                last_err = OlxParserError(
                    f"OLX вернул статус {resp.status_code} (похоже на временную блокировку запросов)."
                )
                _sleep(4 + attempt * 2, 7 + attempt * 3)
                continue
            last_err = OlxParserError(f"OLX вернул неожиданный статус {resp.status_code} для {url}.")
        except requests.RequestException as e:
            last_err = OlxParserError(f"Сетевая ошибка при запросе {url}: {e}")
        _sleep(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)
    raise last_err or OlxParserError(f"Не удалось загрузить {url}")


def build_search_url(query_or_url: str, page: int = 1) -> str:
    """
    Принимает либо готовую ссылку на поиск/категорию OLX.pl, либо ключевое слово.
    Возвращает URL с подставленным номером страницы.
    """
    query_or_url = query_or_url.strip()
    if query_or_url.startswith("http://") or query_or_url.startswith("https://"):
        base = query_or_url
    else:
        safe_q = re.sub(r"\s+", "-", query_or_url.strip())
        base = f"{config.OLX_BASE}/oferty/q-{safe_q}/"

    parsed = urlparse(base)
    qs = parse_qs(parsed.query)
    if page > 1:
        qs["page"] = [str(page)]
    else:
        qs.pop("page", None)
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
        # __PRERENDERED_STATE__ обычно приходит как экранированная JSON-строка
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

    seller = ad.get("sellerName") or (ad.get("user") or {}).get("name")
    delivery_flag = ad.get("delivery") or ad.get("safety_trade") or ad.get("shipping")

    return {
        "title": (ad.get("title") or "").strip() or "Без названия",
        "price": _format_price(price, ad.get("currency", "zł")),
        "ad_url": url or "",
        "seller": seller or None,
        "delivery": ("Да" if delivery_flag else "Нет") if delivery_flag is not None else None,
        "date": ad.get("createdAt") or ad.get("created_at") or "Не указана",
        "photo": photo,
    }


def _format_price(value, currency="zł") -> str:
    if value in (None, "", "Do negocjacji"):
        return "Цена не указана"
    try:
        value = float(str(value).replace(" ", "").replace(",", "."))
        return f"{value:,.0f} {currency}".replace(",", " ")
    except (ValueError, TypeError):
        return f"{value} {currency}"


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
            date_text = _text_or_none(date_tag) or "Не указана"

            results.append({
                "title": title,
                "price": price_text,
                "ad_url": ad_url,
                "photo": photo,
                "date": date_text,
                "seller": None,    # добираем с детальной страницы
                "delivery": None,  # добираем с детальной страницы
            })
        if results:
            return results
    return []


# ---------------------------------------------------------------------------
# Детальная страница объявления — продавец / доставка / фото, если их не было
# ---------------------------------------------------------------------------

DETAIL_SELLER_SELECTORS = [
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


def enrich_with_detail(ad: dict, session: requests.Session) -> dict:
    if ad.get("seller") and ad.get("delivery"):
        return ad
    try:
        html = _get(ad["ad_url"], session)
    except OlxParserError:
        ad.setdefault("seller", "Не удалось определить")
        ad.setdefault("delivery", "Нет")
        return ad

    soup = BeautifulSoup(html, "html.parser")

    if not ad.get("seller"):
        seller = None
        for sel in DETAIL_SELLER_SELECTORS:
            tag = soup.select_one(sel)
            if tag:
                seller = _text_or_none(tag)
                if seller:
                    break
        ad["seller"] = seller or "Не указан"

    if not ad.get("delivery"):
        delivery = "Нет"
        for sel in DETAIL_DELIVERY_SELECTORS:
            if soup.select_one(sel):
                delivery = "Да"
                break
        ad["delivery"] = delivery

    if not ad.get("photo"):
        og_img = soup.select_one('meta[property="og:image"]')
        if og_img:
            ad["photo"] = og_img.get("content")

    _sleep(config.DETAIL_DELAY_MIN, config.DETAIL_DELAY_MAX)
    return ad


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def parse_olx(query_or_url: str, limit: int, fetch_details: bool = True, progress_cb=None):
    """
    Возвращает список словарей объявлений. Может вернуть больше, чем `limit` —
    ограничение на число сообщений в чат применяется отдельно в bot.py,
    а в файл olx.txt пишутся ВСЕ найденные объявления.
    """
    session = _session()
    all_ads = []
    seen_urls = set()

    for page in range(1, config.MAX_PAGES + 1):
        url = build_search_url(query_or_url, page)
        try:
            html = _get(url, session)
        except OlxParserError:
            if page == 1:
                raise
            break  # временная ошибка на дальней странице — просто останавливаемся

        json_ads = _try_extract_embedded_json(html)
        if json_ads:
            page_ads = [_normalize_json_ad(a) for a in json_ads]
        else:
            page_ads = _parse_cards_html(html)

        if not page_ads:
            break  # объявления закончились или вёрстка страницы не распознана

        new_on_page = 0
        for ad in page_ads:
            if not ad.get("ad_url") or ad["ad_url"] in seen_urls:
                continue
            seen_urls.add(ad["ad_url"])
            all_ads.append(ad)
            new_on_page += 1

        if progress_cb:
            progress_cb(page, len(all_ads))

        if new_on_page == 0:
            break

        # собираем с запасом (больше limit), чтобы в файл попало больше данных,
        # но не уходим в бесконечный обход всех страниц выдачи
        if len(all_ads) >= max(limit * 3, limit + 20):
            break

        _sleep(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)

    if not all_ads:
        raise OlxParserError(
            "Объявления не найдены. Либо по запросу пусто, либо OLX изменил вёрстку "
            "страницы и парсер не смог распознать карточки объявлений."
        )

    if fetch_details:
        for ad in all_ads:
            if not ad.get("seller") or not ad.get("delivery"):
                enrich_with_detail(ad, session)

    return all_ads
