"""
Настраиваемые фильтры поиска (меню /filters) + их применение к результатам.

Хранение состояния — В ПАМЯТИ ПРОЦЕССА (словарь на chat_id). Это осознанное
упрощение для прототипа: просто, не требует базы данных, но фильтры и
подписки на мониторинг слетают при перезапуске сервиса (передеплой,
засыпание/пробуждение на free-хостинге). Если это станет проблемой —
см. README, раздел "Что доделать в первую очередь" (замена на SQLite/Redis).
"""
from datetime import datetime, timezone

from telebot import types

import config

# chat_id -> filters dict
_STATE: dict[int, dict] = {}


def default_filters() -> dict:
    return {
        "category_label": config.CATEGORIES[0]["label"],
        "category_slug": config.CATEGORIES[0]["slug"],
        "price_min": None,
        "price_max": None,
        "period_label": config.PERIODS[0]["label"],
        "period_days": config.PERIODS[0]["days"],
        "delivery": None,               # None = не важно, True/False
        "banwords": [],                 # список слов, объявления с ними скрываются
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
        return f"{lo}–{hi} zł"
    if lo is not None:
        return f"от {lo} zł"
    return f"до {hi} zł"


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
        "Нажмите на раздел, чтобы изменить его, или отправьте запрос "
        "командой /parse (либо просто текстом) — фильтры применятся "
        "автоматически."
    )


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def main_menu_keyboard(chat_id: int) -> types.InlineKeyboardMarkup:
    f = get_filters(chat_id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(f"📂 Категория: {f['category_label']}", callback_data="flt:cat"))
    kb.add(types.InlineKeyboardButton(f"💰 Цена: {_price_label(f)}", callback_data="flt:price"))
    kb.add(types.InlineKeyboardButton(f"🕒 Период: {f['period_label']}", callback_data="flt:period"))
    kb.add(types.InlineKeyboardButton(f"🚚 Доставка: {_delivery_label(f['delivery'])}", callback_data="flt:delivery"))
    kb.add(types.InlineKeyboardButton(f"🚫 Банворды: {_banwords_label(f)}", callback_data="flt:banwords"))
    kb.add(types.InlineKeyboardButton("👤 Фильтры продавца ›", callback_data="flt:seller"))
    kb.add(types.InlineKeyboardButton("♻️ Сбросить всё", callback_data="flt:reset"))
    kb.add(types.InlineKeyboardButton("✅ Готово", callback_data="flt:close"))
    return kb


def categories_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, cat in enumerate(config.CATEGORIES):
        kb.add(types.InlineKeyboardButton(cat["label"], callback_data=f"flt:cat:{i}"))
    kb.add(types.InlineKeyboardButton("‹ Назад", callback_data="flt:back"))
    return kb


def period_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, p in enumerate(config.PERIODS):
        kb.add(types.InlineKeyboardButton(p["label"], callback_data=f"flt:period:{i}"))
    kb.add(types.InlineKeyboardButton("‹ Назад", callback_data="flt:back"))
    return kb


def delivery_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("Не важно", callback_data="flt:delivery:any"))
    kb.add(types.InlineKeyboardButton("Только с доставкой", callback_data="flt:delivery:yes"))
    kb.add(types.InlineKeyboardButton("Без доставки", callback_data="flt:delivery:no"))
    kb.add(types.InlineKeyboardButton("‹ Назад", callback_data="flt:back"))
    return kb


def seller_menu_keyboard(chat_id: int) -> types.InlineKeyboardMarkup:
    f = get_filters(chat_id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(f"Объявления: {_seller_ads_label(f)}", callback_data="flt:seller_ads"))
    kb.add(types.InlineKeyboardButton(f"Отзывы/рейтинг: {_seller_rating_label(f)}", callback_data="flt:seller_rating"))
    kb.add(types.InlineKeyboardButton(f"Регистрация: {f['seller_age_label']}", callback_data="flt:seller_age"))
    kb.add(types.InlineKeyboardButton("‹ Назад", callback_data="flt:back"))
    return kb


def seller_age_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, opt in enumerate(config.SELLER_AGE_OPTIONS):
        kb.add(types.InlineKeyboardButton(opt["label"], callback_data=f"flt:seller_age:{i}"))
    kb.add(types.InlineKeyboardButton("‹ Назад", callback_data="flt:seller"))
    return kb


# ---------------------------------------------------------------------------
# Постфильтрация (то, что нельзя передать в URL поиска OLX)
# ---------------------------------------------------------------------------

def apply_client_filters(ads: list, filters: dict) -> list:
    banwords = [w.strip().lower() for w in (filters.get("banwords") or []) if w.strip()]
    period_days = filters.get("period_days")
    min_ads = filters.get("seller_min_ads")
    min_rating = filters.get("seller_min_rating")
    seller_age_days = filters.get("seller_age_days")
    delivery_filter = filters.get("delivery")  # None/True/False

    now = datetime.now(timezone.utc)
    result = []
    for ad in ads:
        title_lower = (ad.get("title") or "").lower()
        if banwords and any(w in title_lower for w in banwords):
            continue

        # "Без доставки": раньше этот выбор нигде не проверялся и не
        # отсеивал объявления с доставкой. OLX в URL умеет фильтровать
        # только "только с доставкой" (delivery=True), поэтому вариант
        # "без доставки" отсеиваем здесь, на стороне бота.
        if delivery_filter is False and ad.get("delivery"):
            continue

        if period_days is not None and ad.get("created_dt") is not None:
            age_days = (now - ad["created_dt"]).days
            if age_days > period_days:
                continue

        if min_ads is not None:
            count = ad.get("seller_ads_count")
            if isinstance(count, (int, float)) and count < min_ads:
                continue

        if min_rating is not None:
            rating = ad.get("seller_rating")
            try:
                if rating is not None and float(rating) < min_rating:
                    continue
            except (TypeError, ValueError):
                pass

        if seller_age_days is not None:
            reg = ad.get("seller_registered")
            reg_dt = None
            if isinstance(reg, str):
                try:
                    reg_dt = datetime.fromisoformat(reg).replace(tzinfo=timezone.utc)
                except ValueError:
                    reg_dt = None
            # если дату регистрации не удалось распознать — не отсеиваем
            # объявление (лучше показать лишнее, чем скрыть нужное)
            if reg_dt is not None and (now - reg_dt).days < seller_age_days:
                continue

        result.append(ad)
    return result
