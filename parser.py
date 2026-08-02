import json
import random
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

import config

class OlxParserError(Exception):
    pass

DETAIL_CIRCUIT_BREAKER = 5

def create_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    })
    return s

def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme: parsed = parsed._replace(scheme="https")
    query_dict = parse_qs(parsed.query)
    clean_query = {k: v for k, v in query_dict.items() if not k.startswith("utm_")}
    return urlunparse(parsed._replace(query=urlencode(clean_query, doseq=True)))

def _convert_date(date_str: str) -> str:
    """Преобразует дату OLX в красивый формат: DD.MM.YYYY HH:MM (X назад)"""
    if not date_str: return ""
    now = datetime.now()
    
    # Сегодня/Вчера
    if "dzisiaj" in date_str.lower():
        return f"{now.strftime('%d.%m.%Y %H:%M')} (Сегодня)"
    if "wczoraj" in date_str.lower():
        y = now - timedelta(days=1)
        return f"{y.strftime('%d.%m.%Y %H:%M')} (Вчера)"

    # Часы/Минуты/Дни назад (польский/английский)
    match = re.match(r"(\d+)\s*(min|godz|h|day|dni|dzień)", date_str, re.I)
    if match:
        val = int(match.group(1))
        unit = match.group(2).lower()
        if "min" in unit:
            dt = now - timedelta(minutes=val)
            return f"{dt.strftime('%d.%m.%Y %H:%M')} ({val} мин назад)"
        elif "godz" in unit or "h" in unit:
            dt = now - timedelta(hours=val)
            return f"{dt.strftime('%d.%m.%Y %H:%M')} ({val} ч назад)"
        elif "day" in unit or "dni" in unit or "dzień" in unit:
            dt = now - timedelta(days=val)
            return f"{dt.strftime('%d.%m.%Y %H:%M')} ({val} дн назад)"

    # Дата вида "2 paź" или "2 paź 2024"
    months = {
        "sty":1,"lut":2,"mar":3,"kwi":4,"maj":5,"cze":6,"lip":7,"sie":8,"wrz":9,"paź":10,"lis":11,"gru":12,
        "jan":1,"feb":2,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12
    }
    match = re.match(r"(\d{1,2})\s+(\w{3})(?:\s+(\d{4}))?", date_str)
    if match:
        d = int(match.group(1))
        m_str = match.group(2).lower()[:3]
        y = int(match.group(3)) if match.group(3) else now.year
        m = months.get(m_str)
        if m:
            dt = datetime(y, m, d)
            # Пытаемся угадать время (ставим noon если неизвестно)
            return f"{dt.strftime('%d.%m.%Y')} (Около {dt.strftime('%H:%M')})"

    return date_str

def parse_olx(url: str, session: requests.Session, limit: int = 50):
    normalized_url = _normalize_url(url)
    try:
        resp = session.get(normalized_url, timeout=15)
    except requests.RequestException as e:
        raise OlxParserError(f"Ошибка подключения: {e}")

    if resp.status_code != 200:
        raise OlxParserError(f"Страница недоступна: {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")
    ads = []
    seen_urls = set()

    for item in soup.select('div[data-cy="l-card"]'):
        link_tag = item.select_one('a[href*="/oferta/"]')
        if not link_tag: continue
        
        href = link_tag.get("href")
        full_url = _normalize_url(href) if href.startswith("/") else href
        if full_url in seen_urls: continue
        seen_urls.add(full_url)

        title_tag = item.select_one('[data-cy="subject"]')
        title = title_tag.get_text(strip=True) if title_tag else "Без названия"

        price_tag = item.select_one('span[aria-label]')
        price = price_tag.get_text(strip=True) if price_tag else "Цена не указана"

        location_tag = item.select_one('span[aria-label="Miejscowość"]')
        location = location_tag.get_text(strip=True) if location_tag else ""

        photo_tag = item.select_one('img[src*="olximg"]')
        photo = photo_tag.get("src") if photo_tag else ""

        # Ищем дату (обычно в параграфе под ценой или рядом)
        date_container = item.find("p", string=re.compile(r"\d|dzisiaj|wczoraj|min|godz"))
        created_raw = date_container.get_text(strip=True) if date_container else ""
        created_display = _convert_date(created_raw)

        ad = {
            "title": title, "price": price, "location": location,
            "ad_url": full_url, "photo": photo,
            "created_display": created_display, # Красивая дата сразу тут
            "seller": "", "seller_registered": "", "seller_last_seen": "",
            "seller_ads_count": None, "delivery": None, "seller_rating": None, "chat_url": "",
        }
        ads.append(ad)
        if len(ads) >= limit: break

    return ads

def enrich_with_detail(ad: dict, session: requests.Session):
    url = ad.get("ad_url")
    if not url: return ad, False
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code != 200: return ad, False
    except Exception: return ad, False

    soup = BeautifulSoup(resp.text, "html.parser")

    # Продавец
    seller_tag = soup.find("a", {"data-testid": "user-box-link"})
    if seller_tag: ad["seller"] = seller_tag.get_text(strip=True)

    # Чат
    chat_link = soup.find("a", string=re.compile("Napisz wiadomość"))
    if chat_link:
        href = chat_link.get("href")
        if href: ad["chat_url"] = "https://www.olx.pl" + href if href.startswith("/") else href

    # JSON данные
    scripts = soup.find_all("script")
    for script in scripts:
        if script.string and "window.__APP_STATE__" in script.string:
            try:
                start = script.string.find("{")
                end = script.string.rfind("}") + 1
                data = json.loads(script.string[start:end])
                seller = data.get("offer", {}).get("seller", {})
                
                if "joinedDate" in seller:
                    dt = datetime.fromisoformat(seller["joinedDate"].replace("Z", "+00:00"))
                    ad["seller_registered"] = dt.strftime("%d.%m.%Y")
                
                if "lastSeenDate" in seller:
                    dt = datetime.fromisoformat(seller["lastSeenDate"].replace("Z", "+00:00"))
                    ad["seller_last_seen"] = dt.strftime("%d.%m.%Y %H:%M")
                
                if "offersCount" in seller: ad["seller_ads_count"] = seller["offersCount"]
                
                rating = seller.get("opinions", {}).get("averageRating")
                if rating is not None: ad["seller_rating"] = round(rating, 2)
            except: pass
            break

    # Доставка
    ad["delivery"] = bool(soup.find(string=re.compile("Opcje dostawy", re.I)))
    return ad, True

def search_ads(query: str, limit: int, filters: dict = None, progress_cb=None):
    """
    Просто ищет объявления. НЕ фильтрует даты внутри себя, 
    чтобы не ломать логику (фильтрация делается в bot.py после получения).
    """
    if not filters: filters = {}
    session = create_session()
    ads = []

    if query.startswith("http"):
        base_url = _normalize_url(query)
    else:
        encoded = requests.utils.quote(query.encode('utf-8'))
        base_url = f"https://www.olx.pl/oferty/q-{encoded}/"

    # Базовые фильтры URL (категория, доставка)
    parsed = urlparse(base_url)
    qdict = parse_qs(parsed.query)
    
    if filters.get("delivery") is True: qdict["search%5Bfilter_enum_dostawa%5D"] = ["tak"]
    elif filters.get("delivery") is False: qdict["search%5Bfilter_enum_dostawa%5D"] = ["nie"]
    
    if filters.get("category_slug"):
        # Простая замена пути для категории
        pass 

    new_query = urlencode(qdict, doseq=True)
    search_url = urlunparse(parsed._replace(query=new_query))

    page = 1
    while len(ads) < limit:
        page_url = f"{search_url}&page={page}" if "?" in search_url else f"{search_url}?page={page}"
        try:
            page_ads = parse_olx(page_url, session, limit=limit)
        except OlxParserError: break
        
        if not page_ads: break
        ads.extend(page_ads)
        
        if progress_cb: progress_cb("pages", page, len(ads))
        page += 1
        
        # Ускоренная задержка
        time.sleep(random.uniform(*config.DELAY_BETWEEN_PAGES))
        
        if len(ads) >= limit:
            ads = ads[:limit]
            break

    return ads
