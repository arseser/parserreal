"""
Парсер OLX.pl — извлекает данные объявлений, обогащает их информацией
о продавце и применяет фильтры.
"""
import json
import random
import re
import time
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

import config


class OlxParserError(Exception):
    """Ошибка парсера (напр. OLX вернул 404 или капчу)."""
    pass


DETAIL_CIRCUIT_BREAKER = 5  # после N неудач подряд — перестаём дозапрашивать детали


def create_session():
    """Создаёт сессию requests с заголовками для имитации реального браузера."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        ),
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    return s


def _normalize_url(url: str) -> str:
    """Приводит URL к стандартному виду (без utm-меток, с правильной схемой)."""
    parsed = urlparse(url)
    if not parsed.scheme:
        parsed = parsed._replace(scheme="https")
    query_dict = parse_qs(parsed.query)
    clean_query = {k: v for k, v in query_dict.items() if not k.startswith("utm_")}
    new_query = urlencode(clean_query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def parse_olx(url: str, session: requests.Session, limit: int = 50):
    """
    Парсит страницу поиска OLX и возвращает список объявлений.
    """
    normalized_url = _normalize_url(url)
    try:
        resp = session.get(normalized_url, timeout=15)
    except requests.RequestException as e:
        raise OlxParserError(f"Ошибка подключения: {e}")

    if resp.status_code != 200:
        if "captcha" in resp.text.lower() or resp.status_code in (403, 429):
            raise OlxParserError("OLX вернул капчу или заблокировал запрос. Попробуйте позже.")
        raise OlxParserError(f"Страница не доступна: {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")
    ads = []
    seen_urls = set()

    for item in soup.select('div[data-cy="l-card"]'):
        link_tag = item.select_one('a[href*="/oferta/"]')
        if not link_tag:
            continue
        href = link_tag.get("href")
        full_url = _normalize_url(href) if href.startswith("/") else href
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        title_tag = item.select_one('[data-cy="subject"]')
        title = title_tag.get_text(strip=True) if title_tag else "Без названия"

        price_tag = item.select_one('span[aria-label]')
        price = price_tag.get_text(strip=True) if price_tag else "Цена не указана"

        location_tag = item.select_one('span[aria-label="Miejscowość"]')
        location = location_tag.get_text(strip=True) if location_tag else ""

        photo_tag = item.select_one('img[src*="olximg"]')
        photo = photo_tag.get("src") if photo_tag else ""

        # Извлекаем дату создания из текста элемента
        date_tag = item.find("p", string=re.compile(r"\d{1,2} \w+|\w+ \d{4}|dzisiaj|wczoraj"))
        created_raw = date_tag.get_text(strip=True) if date_tag else ""

        # Преобразуем дату в нужный формат
        created_display = _convert_date(created_raw)

        ad = {
            "title": title,
            "price": price,
            "location": location,
            "ad_url": full_url,
            "photo": photo,
            "created_raw": created_raw,
            "created_display": created_display,
            "seller": "",
            "seller_registered": "",
            "seller_last_seen": "",
            "seller_ads_count": None,
            "delivery": None,
            "seller_rating": None,
            "chat_url": "",
        }
        ads.append(ad)

        if len(ads) >= limit:
            break

    return ads


def _convert_date(date_str: str) -> str:
    """Преобразует дату из формата OLX в формат DD.MM.YYYY HH:MM."""
    from datetime import datetime, timedelta
    now = datetime.now()

    if not date_str:
        return ""

    # Проверяем формат "час назад", "2 godziny temu" и т.п.
    match = re.match(r"(\d+)\s*(\w+)", date_str)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        if "godz" in unit or "hour" in unit or "h" in unit:
            past_time = now - timedelta(hours=num)
            return f"{past_time.strftime('%d.%m.%Y %H:%M')} ({num} {'час' if num==1 else 'часа' if 2<=num<=4 else 'часов'} назад)"
        elif "min" in unit or "minute" in unit:
            past_time = now - timedelta(minutes=num)
            mins_back = (now - past_time).seconds // 60
            return f"{past_time.strftime('%d.%m.%Y %H:%M')} ({mins_back} {'минута' if mins_back==1 else 'минуты' if 2<=mins_back<=4 else 'минут'} назад)"
        elif "dzień" in unit or "day" in unit or "dni" in unit or "days" in unit:
            past_time = now - timedelta(days=num)
            days_back = (now - past_time).days
            return f"{past_time.strftime('%d.%m.%Y %H:%M')} ({days_back} {'день' if days_back==1 else 'дня' if 2<=days_back<=4 else 'дней'} назад)"

    # Проверяем "dzisiaj", "wczoraj"
    if "dzisiaj" in date_str.lower():
        return f"{now.strftime('%d.%m.%Y')} (сегодня)"
    if "wczoraj" in date_str.lower():
        yesterday = now - timedelta(days=1)
        return f"{yesterday.strftime('%d.%m.%Y')} (вчера)"

    # Проверяем формат "2 paź 2024"
    month_map = {
        "sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6,
        "lip": 7, "sie": 8, "wrz": 9, "paź": 10, "lis": 11, "gru": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    match = re.match(r"(\d+)\s*(\w+)\s*(\d{4})?", date_str)
    if match:
        day = int(match.group(1))
        month_str = match.group(2)[:3].lower()
        year = int(match.group(3)) if match.group(3) else now.year
        month = month_map.get(month_str)
        if month:
            dt = datetime(year, month, day)
            hours_ago = int((now - dt).total_seconds() // 3600)
            return f"{dt.strftime('%d.%m.%Y %H:%M')} ({hours_ago} {'час' if hours_ago==1 else 'часа' if 2<=hours_ago<=4 else 'часов'} назад)"

    # Если не удалось распознать, возвращаем как есть
    return date_str


def enrich_with_detail(ad: dict, session: requests.Session):
    """
    Добавляет к объявлению данные о продавце, доставке, рейтинге и т.д.
    Возвращает (обогащенное объявление, успех).
    """
    url = ad.get("ad_url")
    if not url:
        return ad, False

    try:
        resp = session.get(url, timeout=10)
        if resp.status_code != 200:
            return ad, False
    except Exception:
        return ad, False

    soup = BeautifulSoup(resp.text, "html.parser")

    # Имя продавца
    seller_tag = soup.find("a", {"data-testid": "user-box-link"})
    if seller_tag:
        ad["seller"] = seller_tag.get_text(strip=True)

    # Ссылка на чат
    chat_link_tag = soup.find("a", string=re.compile("Napisz wiadomość"))
    if chat_link_tag:
        chat_href = chat_link_tag.get("href")
        if chat_href:
            ad["chat_url"] = "https://www.olx.pl" + chat_href if chat_href.startswith("/") else chat_href

    # Данные о продавце (через JSON-LD или JS)
    scripts = soup.find_all("script")
    for script in scripts:
        if script.string and "window.__APP_STATE__" in script.string:
            try:
                start = script.string.find("{")
                end = script.string.rfind("}") + 1
                app_state = json.loads(script.string[start:end])

                # Извлечение информации о продавце из состояния приложения
                offer_data = app_state.get("offer", {})
                seller_info = offer_data.get("seller", {})

                reg_date = seller_info.get("joinedDate")
                if reg_date:
                    joined_dt = datetime.fromisoformat(reg_date.replace("Z", "+00:00"))
                    ad["seller_registered"] = joined_dt.strftime("%d.%m.%Y")

                last_seen = seller_info.get("lastSeenDate")
                if last_seen:
                    seen_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                    ad["seller_last_seen"] = seen_dt.strftime("%d.%m.%Y %H:%M")

                ads_count = seller_info.get("offersCount")
                if ads_count is not None:
                    ad["seller_ads_count"] = ads_count

                rating_info = seller_info.get("opinions", {})
                avg_rating = rating_info.get("averageRating")
                if avg_rating is not None:
                    ad["seller_rating"] = round(avg_rating, 2)

            except Exception:
                pass
            break

    # Альтернативный способ получения информации о продавце
    if not ad["seller"]:
        seller_alt = soup.find("a", class_=re.compile("userBox"))
        if seller_alt:
            ad["seller"] = seller_alt.get_text(strip=True)

    # Проверка доставки
    delivery_tag = soup.find(string=re.compile("Opcje dostawy", re.IGNORECASE))
    if delivery_tag:
        ad["delivery"] = True
    else:
        ad["delivery"] = False

    # Рейтинг продавца (если не получен выше)
    if ad.get("seller_rating") is None:
        rating_tags = soup.find_all(class_=re.compile("rate-"))
        for tag in rating_tags:
            txt = tag.get_text(strip=True)
            match = re.search(r"(\d+[.,]?\d*)", txt)
            if match:
                try:
                    ad["seller_rating"] = float(match.group(1).replace(",", "."))
                    break
                except ValueError:
                    pass

    return ad, True


def search_ads(query: str, limit: int, filters: dict = None, progress_cb=None):
    """
    Выполняет поиск объявлений по запросу или URL.
    """
    if not filters:
        filters = {}

    session = create_session()
    ads = []

    # Если это URL, используем его напрямую
    if query.startswith("http"):
        base_url = _normalize_url(query)
    else:
        # Иначе формируем URL поиска
        encoded_query = requests.utils.quote(query.encode('utf-8'))
        base_url = f"https://www.olx.pl/oferty/q-{encoded_query}/"

    # Применяем фильтры к URL
    parsed = urlparse(base_url)
    qdict = parse_qs(parsed.query)
    
    # Период
    if filters.get("period_days"):
        qdict["search%5Bfilter_enum_dostepne%5D"] = ["true"]  # например, доступно сейчас
        # Для простоты, можно добавить фильтр по дате, но OLX не всегда позволяет это через GET параметры
        # Поэтому будем фильтровать вручную ниже
    
    # Доставка
    if filters.get("delivery") is True:
        qdict["search%5Bfilter_enum_dostawa%5D"] = ["tak"]
    elif filters.get("delivery") is False:
        qdict["search%5Bfilter_enum_dostawa%5D"] = ["nie"]

    # Категория
    if filters.get("category_slug"):
        new_path = f"/{filters['category_slug']}/q-{parsed.path.split('/')[-1].replace('q-', '') if 'q-' in parsed.path else query}/"
        parsed = parsed._replace(path=new_path)

    new_query = urlencode(qdict, doseq=True)
    search_url = urlunparse(parsed._replace(query=new_query))

    page = 1
    while len(ads) < limit:
        page_url = f"{search_url}&page={page}" if "?" in search_url else f"{search_url}?&page={page}"

        try:
            page_ads = parse_olx(page_url, session, limit=limit)
        except OlxParserError:
            break

        if not page_ads:
            break

        # Применяем фильтры по дате вручную
        filtered_ads = []
        for ad in page_ads:
            if filters.get("period_days"):
                created_disp = ad.get("created_display", "")
                # Извлекаем дату из строки вида "DD.MM.YYYY HH:MM (X часов назад)"
                match = re.search(r"(\d{2}\.\d{2}\.\d{4})", created_disp)
                if match:
                    try:
                        ad_date = datetime.strptime(match.group(1), "%d.%m.%Y")
                        delta = (datetime.now() - ad_date).days
                        if delta <= filters["period_days"]:
                            filtered_ads.append(ad)
                    except ValueError:
                        filtered_ads.append(ad)  # если не смогли распарсить дату — пропускаем проверку
                else:
                    filtered_ads.append(ad)  # если нет даты — пропускаем проверку
            else:
                filtered_ads.append(ad)

        ads.extend(filtered_ads)

        if progress_cb:
            progress_cb("pages", page, len(ads))

        page += 1

        # Задержка между страницами
        time.sleep(random.uniform(*config.DELAY_BETWEEN_PAGES))

        if len(ads) >= limit:
            ads = ads[:limit]
            break

    return ads
