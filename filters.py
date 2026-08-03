"""
Настраиваемые фильтры поиска + их применение.
"""
from datetime import datetime, timezone
import re

from telebot import types
import config

_STATE: dict[int, dict] = {}

def default_filters() -> dict:
    return {
        "category_label": config.CATEGORIES[0]["label"],
        "category_slug": config.CATEGORIES[0]["slug"],
        "price_min": None,
        "price_max": None,
        "period_label": config.PERIODS[0]["label"],
        "period_days": config.PERIODS[0]["days"],
        "delivery": None,
        "banwords": [],
        "seller_min_ads": None,
        "seller_min_rating": None,
        "seller_age_label": config.SELLER_AGE_OPTIONS[0]["label"],
        "seller_age_days": config.SELLER_AGE_OPTIONS[0]["days"],
    }

def get_filters(chat_id: int) -> dict:
    if chat_id not in _STATE:
        _STATE[chat_id] = default_filters()
    return _STATE[chat_id]

def reset_filters(chat_id: int) -> dict:
    _STATE[chat_id] = default_filters()
    return _STATE[chat_id]

def _delivery_label(value) -> str:
    return {True: "Только с доставкой", False: "Без доставки", None: "Не важно"}[value]

def _price_label(f: dict) -> str:
    lo, hi = f.get("price_min"), f.get("price_max")
    if lo is None and hi is None:
        return "Любая"
    if lo is not None and hi is not None:
        return f"{lo}–{hi} ZL"
    if lo is not None:
        return f"от {lo} ZL"
    return f"до {hi} ZL"

def _banwords_label(f: dict) -> str:
    words = f.get("banwords") or []
    return ", ".join(words) if words else "нет"

def _seller_ads_label(f: dict) -> str:
    v = f.get("seller_min_ads")
    return f"от {v}" if v is not None else "не важно"

def _seller_rating_label(f: dict) -> str:
    v = f.get("seller_min_rating")
    return f"от {v}" if v is not None else "не важно"

def summary_text(chat_id: int) -> str:
    f = get_filters(chat_id)
    return (
        "⚙️ <b>Текущие фильтры</b>\n\n"
        f"Категория\n└ {f['category_label']}\n\n"
        f"Цена\n└ {_price_label(f)}\n\n"
        f"Период публикации\n└ {f['period_label']}\n\n"
        f"Доставка\n└ {_delivery_label(f['delivery'])}\n\n"
        f"Банворды\n└ {_banwords_label(f)}\n\n"
        f"Фильтры продавца\n"
        f"┌ Объявления: {_seller_ads_label(f)}\n"
        f"├ Отзывы/рейтинг: {_seller_rating_label(f)}\n"
        f"└ Регистрация: {f['seller_age_label']}\n\n"
        "Нажмите на раздел, чтобы изменить его, или отправьте запрос."
    )

# ---------------------------------------------------------------------------
# Клавиатуры (без изменений, опущены для краткости)
# ---------------------------------------------------------------------------
# ... (оставлены как в исходном коде)

# ---------------------------------------------------------------------------
# Применение фильтров (исправлено)
# ---------------------------------------------------------------------------
def apply_client_filters(ads: list, filters: dict) -> list:
    banwords = [w.strip().lower() for w in (filters.get("banwords") or []) if w.strip()]
    period_days = filters.get("period_days")
    min_ads = filters.get("seller_min_ads")
    min_rating = filters.get("seller_min_rating")
    seller_age_days = filters.get("seller_age_days")
    delivery_filter = filters.get("delivery")

    now = datetime.now(timezone.utc)
    result = []

    for ad in ads:
        title_lower = (ad.get("title") or "").lower()
        if banwords and any(w in title_lower for w in banwords):
            continue

        if delivery_filter is False and ad.get("delivery"):
            continue

        # Фильтр по периоду – если задан, объявление без даты отсеивается
        if period_days is not None:
            created_dt = ad.get("created_dt")
            if created_dt is None:
                continue
            if (now - created_dt).days > period_days:
                continue

        # Фильтр по количеству объявлений продавца
        if min_ads is not None:
            count = ad.get("seller_ads_count")
            if isinstance(count, (int, float)) and count < min_ads:
                continue

        # Фильтр по рейтингу
        if min_rating is not None:
            rating = ad.get("seller_rating")
            try:
                if rating is not None and float(rating) < min_rating:
                    continue
            except (TypeError, ValueError):
                pass

        # Фильтр по возрасту продавца
        if seller_age_days is not None:
            reg = ad.get("seller_registered")
            if reg is None:
                continue
            # Пытаемся распарсить строку вида "dd.mm.yyyy" или "mm.yyyy"
            reg_dt = None
            m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", reg)
            if m:
                day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
                try:
                    reg_dt = datetime(year, month, day, tzinfo=timezone.utc)
                except ValueError:
                    reg_dt = None
            else:
                m = re.match(r"(\d{2})\.(\d{4})", reg)
                if m:
                    month, year = int(m.group(1)), int(m.group(2))
                    try:
                        reg_dt = datetime(year, month, 1, tzinfo=timezone.utc)
                    except ValueError:
                        reg_dt = None
            if reg_dt is None:
                continue
            if (now - reg_dt).days < seller_age_days:
                continue

        result.append(ad)

    return result
