"""
Модуль управления фильтрами для Telegram-бота.
"""
import json
from pathlib import Path
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import config


FILTERS_FILE = Path("filters.json")


def load_filters(chat_id: int) -> dict:
    """Загружает фильтры для конкретного чата."""
    if not FILTERS_FILE.exists():
        return {}
    try:
        with open(FILTERS_FILE, "r", encoding="utf-8") as f:
            all_filters = json.load(f)
        return all_filters.get(str(chat_id), {})
    except Exception:
        return {}


def save_filters(chat_id: int, filters: dict):
    """Сохраняет фильтры для конкретного чата."""
    all_filters = {}
    if FILTERS_FILE.exists():
        try:
            with open(FILTERS_FILE, "r", encoding="utf-8") as f:
                all_filters = json.load(f)
        except Exception:
            pass
    all_filters[str(chat_id)] = filters
    with open(FILTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(all_filters, f, ensure_ascii=False, indent=2)


def get_filters(chat_id: int) -> dict:
    """Возвращает фильтры для чата, создавая дефолтные при необходимости."""
    f = load_filters(chat_id)
    defaults = {
        "category_label": "Все",
        "category_slug": "",
        "price_min": None,
        "price_max": None,
        "period_label": "Все время",
        "period_days": None,
        "delivery": None,
        "banwords": [],
        "seller_age_label": "Без фильтра",
        "seller_age_days": None,
        "seller_min_ads": None,
        "seller_min_rating": None,
    }
    for k, v in defaults.items():
        if k not in f:
            f[k] = v
    return f


def reset_filters(chat_id: int):
    """Сбрасывает фильтры для чата."""
    save_filters(chat_id, get_filters(chat_id))


def summary_text(chat_id: int) -> str:
    """Возвращает текстовое описание текущих фильтров."""
    f = get_filters(chat_id)
    banwords_str = ", ".join(f["banwords"]) if f["banwords"] else "нет"
    price_str = f"{f['price_min'] or ''} — {f['price_max'] or ''}".strip().replace(" — ", "—") or "любая"
    delivery_str = {
        True: "только с доставкой",
        False: "без доставки",
        None: "любые"
    }[f["delivery"]]
    seller_age_str = f["seller_age_label"]
    seller_ads_str = f["seller_min_ads"] or "любое"
    seller_rating_str = f["seller_min_rating"] or "любой"

    return (
        f"📋 <b>Текущие фильтры:</b>\n\n"
        f"🏷️ Категория: {f['category_label']}\n"
        f"💰 Цена: {price_str}\n"
        f"🕒 Период: {f['period_label']}\n"
        f"📦 Доставка: {delivery_str}\n"
        f"🚫 Банворды: {banwords_str}\n"
        f"👤 Регистрация продавца: {seller_age_str}\n"
        f"📊 Объявлений у продавца: {seller_ads_str}\n"
        f"⭐ Рейтинг продавца: {seller_rating_str}"
    )


def main_menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Основное меню фильтров."""
    markup = InlineKeyboardMarkup()
    f = get_filters(chat_id)
    markup.row_width = 1
    markup.add(
        InlineKeyboardButton(f"🏷️ Категория: {f['category_label']}", callback_data="flt:cat"),
        InlineKeyboardButton(f"💰 Цена: {f.get('price_min') or ''} — {f.get('price_max') or ''}".strip().replace(" — ", "—") or "любая", callback_data="flt:price"),
        InlineKeyboardButton(f"🕒 Период: {f['period_label']}", callback_data="flt:period"),
        InlineKeyboardButton(f"📦 Доставка: {'только с доставкой' if f['delivery'] else 'без доставки' if f['delivery'] is False else 'любые'}", callback_data="flt:delivery"),
        InlineKeyboardButton("🚫 Банворды", callback_data="flt:banwords"),
        InlineKeyboardButton("👤 Фильтры продавца", callback_data="flt:seller"),
    )
    markup.add(
        InlineKeyboardButton("🔄 Сбросить", callback_data="flt:reset"),
        InlineKeyboardButton("✅ Готово", callback_data="flt:close"),
    )
    return markup


def categories_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора категории."""
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    for i, cat in enumerate(config.CATEGORIES):
        markup.add(InlineKeyboardButton(cat["label"], callback_data=f"flt:cat:{i}"))
    markup.add(InlineKeyboardButton("⬅️ Назад", callback_data="flt:back"))
    return markup


def period_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора периода."""
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    for i, per in enumerate(config.PERIODS):
        markup.add(InlineKeyboardButton(per["label"], callback_data=f"flt:period:{i}"))
    markup.add(InlineKeyboardButton("⬅️ Назад", callback_data="flt:back"))
    return markup


def delivery_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора доставки."""
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    markup.add(
        InlineKeyboardButton("🚚 Любые", callback_data="flt:delivery:any"),
        InlineKeyboardButton("📦 Только с доставкой", callback_data="flt:delivery:yes"),
        InlineKeyboardButton("🚫 Без доставки", callback_data="flt:delivery:no"),
    )
    markup.add(InlineKeyboardButton("⬅️ Назад", callback_data="flt:back"))
    return markup


def seller_menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Меню фильтров продавца."""
    f = get_filters(chat_id)
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    markup.add(
        InlineKeyboardButton(f"👤 Регистрация: {f['seller_age_label']}", callback_data="flt:seller_age"),
        InlineKeyboardButton(f"📊 Мин. объявлений: {f['seller_min_ads'] or 'любое'}", callback_data="flt:seller_ads"),
        InlineKeyboardButton(f"⭐ Мин. рейтинг: {f['seller_min_rating'] or 'любой'}", callback_data="flt:seller_rating"),
    )
    markup.add(
        InlineKeyboardButton("⬅️ Назад", callback_data="flt:back"),
    )
    return markup


def seller_age_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора возраста аккаунта продавца."""
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    for i, opt in enumerate(config.SELLER_AGE_OPTIONS):
        markup.add(InlineKeyboardButton(opt["label"], callback_data=f"flt:seller_age:{i}"))
    markup.add(InlineKeyboardButton("⬅️ Назад", callback_data="flt:back"))
    return markup


def apply_client_filters(ads: list, filters: dict) -> list:
    """Применяет фильтры к списку объявлений."""
    result = []
    for ad in ads:
        # Цена
        price_str = ad.get("price", "").replace(" ", "").replace("PLN", "")
        match = re.search(r"(\d+)", price_str)
        if match:
            try:
                price = int(match.group(1))
                if filters.get("price_min") and price < filters["price_min"]:
                    continue
                if filters.get("price_max") and price > filters["price_max"]:
                    continue
            except ValueError:
                pass

        # Банворды
        title = ad.get("title", "").lower()
        if any(word.lower() in title for word in filters.get("banwords", [])):
            continue

        # Период (по дате создания)
        if filters.get("period_days"):
            created_disp = ad.get("created_display", "")
            match = re.search(r"(\d{2}\.\d{2}\.\d{4})", created_disp)
            if match:
                try:
                    ad_date = datetime.strptime(match.group(1), "%d.%m.%Y")
                    delta = (datetime.now() - ad_date).days
                    if delta > filters["period_days"]:
                        continue
                except ValueError:
                    pass  # если не распознали дату — пропускаем проверку

        # Фильтры продавца
        if filters.get("seller_age_days"):
            reg_str = ad.get("seller_registered", "")
            if reg_str:
                try:
                    reg_date = datetime.strptime(reg_str, "%d.%m.%Y")
                    delta = (datetime.now() - reg_date).days
                    if delta < filters["seller_age_days"]:
                        continue
                except ValueError:
                    pass

        if filters.get("seller_min_ads") is not None:
            ads_count = ad.get("seller_ads_count")
            if ads_count is not None and ads_count < filters["seller_min_ads"]:
                continue

        if filters.get("seller_min_rating") is not None:
            rating = ad.get("seller_rating")
            if rating is not None and rating < filters["seller_min_rating"]:
                continue

        result.append(ad)

    return result


# Импортируем re и datetime для внутреннего использования
import re
from datetime import datetime
