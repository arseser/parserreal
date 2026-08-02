"""
Telegram-бот для парсинга OLX.pl
"""
import logging
import os
import re
import threading
import time
import traceback

import telebot
from telebot.types import InputFile
from flask import Flask

import config
import filters as filters_module
from parser import (
    parse_olx,
    search_ads,
    enrich_with_detail,
    create_session,
    OlxParserError,
    DETAIL_CIRCUIT_BREAKER,
)
from sent_ads_storage import get_seen_urls, add_seen_urls

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("olx_bot")

bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN, parse_mode="HTML")

# --- мини веб-сервер для Render ---
web_app = Flask(__name__)

@web_app.route("/")
def health_check():
    return "OLX Telegram bot is running.", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

HELP_TEXT = (
    "👋 Привет! Я парсер объявлений OLX.pl.\n\n"
    "Отправь мне ссылку на поиск/категорию OLX.pl или просто ключевое слово.\n\n"
    "<b>Команды:</b>\n"
    "<code>/parse ЗАПРОС [лимит]</code> — разовый поиск\n"
    "<code>/filters</code> — настроить фильтры\n"
    "<code>/monitor ЗАПРОС</code> — автослежение\n"
    "<code>/stopmonitor</code> — выключить слежение\n\n"
    "После списка объявлений я пришлю файл olx.txt со всеми данными."
)

_AWAITING: dict[int, str] = {}
_MONITORS: dict[int, dict] = {}
_MONITORS_LOCK = threading.Lock()

def _get_initial_seen_urls(chat_id: int) -> set:
    return get_seen_urls(chat_id)

@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    bot.reply_to(message, HELP_TEXT)

@bot.message_handler(commands=["filters"])
def handle_filters(message):
    chat_id = message.chat.id
    _AWAITING.pop(chat_id, None)
    bot.send_message(
        chat_id,
        filters_module.summary_text(chat_id),
        reply_markup=filters_module.main_menu_keyboard(chat_id),
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("flt:"))
def handle_filter_callback(call):
    chat_id = call.message.chat.id
    data = call.data.split(":")
    action = data[1] if len(data) > 1 else ""
    f = filters_module.get_filters(chat_id)

    if action == "close":
        bot.answer_callback_query(call.id, "Фильтры сохранены")
        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return

    if action == "reset":
        filters_module.reset_filters(chat_id)
        bot.answer_callback_query(call.id, "Фильтры сброшены")
        bot.edit_message_text(
            filters_module.summary_text(chat_id),
            chat_id=chat_id, message_id=call.message.message_id,
            reply_markup=filters_module.main_menu_keyboard(chat_id),
        )
        return

    if action == "back":
        bot.edit_message_text(
            filters_module.summary_text(chat_id),
            chat_id=chat_id, message_id=call.message.message_id,
            reply_markup=filters_module.main_menu_keyboard(chat_id),
        )
        bot.answer_callback_query(call.id)
        return

    if action == "cat" and len(data) == 2:
        bot.edit_message_text("📂 Выберите категорию:", chat_id=chat_id, message_id=call.message.message_id,
                               reply_markup=filters_module.categories_keyboard())
        bot.answer_callback_query(call.id)
        return
    if action == "period" and len(data) == 2:
        bot.edit_message_text("🕒 За какой период показывать объявления?", chat_id=chat_id, message_id=call.message.message_id,
                               reply_markup=filters_module.period_keyboard())
        bot.answer_callback_query(call.id)
        return
    if action == "delivery" and len(data) == 2:
        bot.edit_message_text("🚚 Фильтр по доставке:", chat_id=chat_id, message_id=call.message.message_id,
                               reply_markup=filters_module.delivery_keyboard())
        bot.answer_callback_query(call.id)
        return
    if action == "seller" and len(data) == 2:
        bot.edit_message_text("👤 Фильтры продавца:", chat_id=chat_id, message_id=call.message.message_id,
                               reply_markup=filters_module.seller_menu_keyboard(chat_id))
        bot.answer_callback_query(call.id)
        return
    if action == "seller_age" and len(data) == 2:
        bot.edit_message_text("👤 Минимальный возраст аккаунта продавца:", chat_id=chat_id, message_id=call.message.message_id,
                               reply_markup=filters_module.seller_age_keyboard())
        bot.answer_callback_query(call.id)
        return

    if action == "cat" and len(data) == 3:
        idx = int(data[2])
        cat = config.CATEGORIES[idx]
        f["category_label"], f["category_slug"] = cat["label"], cat["slug"]
        bot.answer_callback_query(call.id, f"Категория: {cat['label']}")
        bot.edit_message_text(filters_module.summary_text(chat_id), chat_id=chat_id, message_id=call.message.message_id,
                               reply_markup=filters_module.main_menu_keyboard(chat_id))
        return

    if action == "period" and len(data) == 3:
        idx = int(data[2])
        p = config.PERIODS[idx]
        f["period_label"], f["period_days"] = p["label"], p["days"]
        bot.answer_callback_query(call.id, f"Период: {p['label']}")
        bot.edit_message_text(filters_module.summary_text(chat_id), chat_id=chat_id, message_id=call.message.message_id,
                               reply_markup=filters_module.main_menu_keyboard(chat_id))
        return

    if action == "delivery" and len(data) == 3:
        mapping = {"any": None, "yes": True, "no": False}
        f["delivery"] = mapping.get(data[2])
        bot.answer_callback_query(call.id, "Фильтр доставки обновлён")
        bot.edit_message_text(filters_module.summary_text(chat_id), chat_id=chat_id, message_id=call.message.message_id,
                               reply_markup=filters_module.main_menu_keyboard(chat_id))
        return

    if action == "seller_age" and len(data) == 3:
        idx = int(data[2])
        opt = config.SELLER_AGE_OPTIONS[idx]
        f["seller_age_label"], f["seller_age_days"] = opt["label"], opt["days"]
        bot.answer_callback_query(call.id, f"Регистрация: {opt['label']}")
        bot.edit_message_text("👤 Фильтры продавца:", chat_id=chat_id, message_id=call.message.message_id,
                               reply_markup=filters_module.seller_menu_keyboard(chat_id))
        return

    prompts = {
        "price": "Введите цену в формате <code>мин макс</code> (например <code>100 500</code>), только <code>мин</code> или <code>-</code> для сброса.",
        "banwords": "Отправьте слова через запятую для блокировки или <code>-</code>, чтобы очистить.",
        "seller_ads": "Введите мин. число объявлений продавца или <code>-</code> для сброса.",
        "seller_rating": "Введите мин. рейтинг (0-5) или <code>-</code> для сброса.",
    }
    if action in prompts:
        _AWAITING[chat_id] = action
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, prompts[action])
        return

    bot.answer_callback_query(call.id)

def _handle_awaited_input(message) -> bool:
    chat_id = message.chat.id
    awaiting = _AWAITING.get(chat_id)
    if not awaiting:
        return False

    _AWAITING.pop(chat_id, None)
    f = filters_module.get_filters(chat_id)
    text = message.text.strip()

    try:
        if awaiting == "price":
            if text == "-":
                f["price_min"], f["price_max"] = None, None
            else:
                parts = text.split()
                nums = [int(p) for p in parts if p.isdigit()]
                if len(nums) == 2:
                    f["price_min"], f["price_max"] = min(nums), max(nums)
                elif len(nums) == 1:
                    f["price_min"], f["price_max"] = nums[0], None
                else:
                    bot.reply_to(message, "Не понял формат. Пример: <code>100 500</code>")
                    return True
        elif awaiting == "banwords":
            f["banwords"] = [] if text == "-" else [w.strip() for w in text.split(",") if w.strip()]
        elif awaiting == "seller_ads":
            f["seller_min_ads"] = None if text == "-" else max(0, int(text))
        elif awaiting == "seller_rating":
            f["seller_min_rating"] = None if text == "-" else max(0.0, min(5.0, float(text.replace(",", "."))))
    except ValueError:
        bot.reply_to(message, "Не понял значение. Попробуйте ещё раз.")
        return True

    bot.send_message(chat_id, filters_module.summary_text(chat_id), reply_markup=filters_module.main_menu_keyboard(chat_id))
    return True

def _parse_args(text: str):
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None, config.DEFAULT_LIMIT
    rest = parts[1].strip()
    tokens = rest.rsplit(maxsplit=1)
    if len(tokens) == 2 and tokens[1].isdigit():
        query = tokens[0]
        limit = min(int(tokens[1]), config.MAX_LIMIT)
    else:
        query = rest
        limit = config.DEFAULT_LIMIT
    return query, max(1, limit)

@bot.message_handler(commands=["parse"])
def handle_parse(message):
    query, limit = _parse_args(message.text)
    if not query:
        bot.reply_to(message, "Укажи запрос или ссылку. Пример:\n<code>/parse iphone 15 20</code>")
        return

    chat_filters = filters_module.get_filters(message.chat.id)
    status_msg = bot.reply_to(message, f"🔎 Ищу объявления по запросу: <b>{query}</b>...")

    last_edit_ts = [0.0]
    def _progress(stage, current, total):
        now = time.time()
        if now - last_edit_ts[0] < 4: return
        last_edit_ts[0] = now
        try:
            bot.edit_message_text(f"🔎 Ищу... страница {current}, найдено {total}", chat_id=status_msg.chat.id, message_id=status_msg.message_id)
        except Exception: pass

    try:
        # Ищем объявления (без внутренней фильтрации дат, чтобы не ломать выдачу)
        ads = search_ads(query, limit, filters=chat_filters, progress_cb=_progress)
    except OlxParserError as e:
        bot.edit_message_text(f"❌ Ошибка парсинга: {e}", chat_id=status_msg.chat.id, message_id=status_msg.message_id)
        return
    except Exception as e:
        log.error("Ошибка поиска: %s", e)
        bot.edit_message_text("❌ Произошла ошибка.", chat_id=status_msg.chat.id, message_id=status_msg.message_id)
        return

    try:
        bot.edit_message_text(f"✅ Найдено: {len(ads)}. Обрабатываю детали...", chat_id=status_msg.chat.id, message_id=status_msg.message_id)
    except Exception: pass

    session = create_session()
    sent_ads = []
    already_seen = get_seen_urls(message.chat.id)
    
    try:
        consecutive_failures = 0
        for i, ad in enumerate(ads, start=1):
            # Пропуск дублей
            if ad.get("ad_url") and ad["ad_url"] in already_seen:
                continue
            
            try:
                ad, ok = enrich_with_detail(ad, session)
            except Exception:
                ok = False
            consecutive_failures = 0 if ok else consecutive_failures + 1

            # Применяем фильтры ТОЛЬКО здесь (как в оригинале)
            for good_ad in filters_module.apply_client_filters([ad], chat_filters):
                _send_ads(message.chat.id, [good_ad])
                sent_ads.append(good_ad)
                if good_ad.get("ad_url"):
                    add_seen_urls(message.chat.id, [good_ad["ad_url"]])
                    already_seen.add(good_ad["ad_url"])

            if consecutive_failures >= DETAIL_CIRCUIT_BREAKER:
                break
    finally:
        if sent_ads:
            try:
                _send_txt_file(message.chat.id, sent_ads)
            except Exception as e:
                log.error("Ошибка отправки файла: %s", e)
                bot.send_message(message.chat.id, "⚠️ Не удалось отправить файл, но объявления выше сохранены.")
        else:
            # Если ничего не отправлено, возможно фильтры слишком строгие
            # Но мы не пишем "ничего не осталось" если ads были пустыми изначально
            if len(ads) == 0:
                 bot.send_message(message.chat.id, "😕 Объявления не найдены. Попробуйте другой запрос.")
            else:
                 bot.send_message(message.chat.id, "😕 После применения фильтров ничего не подошло. Проверьте /filters.")

def _format_caption(ad: dict) -> str:
    delivery_line = "📦 Есть доставка" if ad.get("delivery") else "🚫 Доставки нет"
    rating = ad.get("seller_rating")
    rating_str = f"{rating}/5" if rating not in (None, "") else "Нет рейтинга"
    
    # Форматируем дату красиво
    created_info = ad.get('created_display', '')
    if not created_info:
        created_info = "Дата не указана"

    return (
        f"📦 <b>{ad.get('title', 'Без названия')}</b>\n"
        f"💰 Цена: <code>{ad.get('price', 'не указана')}</code>\n"
        f"📍 {ad.get('location') or 'Город не указан'}\n"
        f"👤 Продавец: <code>{ad.get('seller', 'не указан')}</code>\n\n"
        f"📅 <b>{created_info}</b>\n"
        f"🏢 Аккаунт создан: {ad.get('seller_registered') or 'неизвестно'}\n"
        f"🌐 Был в сети: {ad.get('seller_last_seen') or 'давно'}\n"
        f"📢 Объявлений у продавца: {ad.get('seller_ads_count') if ad.get('seller_ads_count') is not None else 'не указано'}\n"
        f"⭐ Рейтинг: {rating_str}\n{delivery_line}\n\n"
        f"🔗 <a href=\"{ad.get('ad_url', '')}\">Открыть объявление</a> | "
        f"📸 <a href=\"{ad.get('photo') or ad.get('ad_url', '')}\">Фото</a> | "
        f"💬 <a href=\"{ad.get('chat_url') or ad.get('ad_url', '')}\">Написать продавцу</a>"
    )

def _send_ads(chat_id: int, ads: list):
    for ad in ads:
        caption = _format_caption(ad)
        try:
            if ad.get("photo"):
                bot.send_photo(chat_id, ad["photo"], caption=caption)
            else:
                bot.send_message(chat_id, caption)
        except Exception as e:
            log.warning("Не удалось отправить фото: %s", e)
            try:
                bot.send_message(chat_id, caption)
            except Exception: pass

def _send_txt_file(chat_id: int, ads: list):
    path = os.path.join(os.getcwd(), config.OUTPUT_FILE)
    with open(path, "w", encoding="utf-8") as f:
        for ad in ads:
            f.write(f"Товар: {ad.get('title', '')}\n")
            f.write(f"Цена: {ad.get('price', '')}\n")
            f.write(f"Дата: {ad.get('created_display', '')}\n")
            f.write(f"Ссылка: {ad.get('ad_url', '')}\n")
            f.write("===\n")

    last_err = None
    for attempt in range(1, 6):
        try:
            with open(path, "rb") as fh:
                bot.send_document(
                    chat_id, InputFile(fh, filename=config.OUTPUT_FILE),
                    caption=f"📄 Файл с {len(ads)} объявлениями.",
                )
            return
        except Exception as e:
            last_err = e
            m = re.search(r"retry after (\d+)", str(e), re.I)
            wait = int(m.group(1)) + 2 if m else 5 * attempt
            time.sleep(wait)
    
    bot.send_message(chat_id, f"⚠️ Файл не отправлен (лимит Telegram), но все {len(ads)} объявлений выше.")

@bot.message_handler(content_types=["text"])
def handle_text(message):
    if _handle_awaited_input(message): return
    # Копия логики /parse для обычных сообщений
    query = message.text.strip()
    if not query: return
    
    chat_filters = filters_module.get_filters(message.chat.id)
    status_msg = bot.reply_to(message, f"🔎 Ищу: <b>{query}</b>...")
    
    try:
        ads = search_ads(query, config.DEFAULT_LIMIT, filters=chat_filters)
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка: {e}", chat_id=status_msg.chat.id, message_id=status_msg.message_id)
        return

    session = create_session()
    sent_ads = []
    already_seen = get_seen_urls(message.chat.id)
    
    for ad in ads:
        if ad.get("ad_url") and ad["ad_url"] in already_seen: continue
        try: ad, _ = enrich_with_detail(ad, session)
        except: pass
        
        for good_ad in filters_module.apply_client_filters([ad], chat_filters):
            _send_ads(message.chat.id, [good_ad])
            sent_ads.append(good_ad)
            if good_ad.get("ad_url"):
                add_seen_urls(message.chat.id, [good_ad["ad_url"]])
                already_seen.add(good_ad["ad_url"])
    
    if sent_ads:
        _send_txt_file(message.chat.id, sent_ads)
    else:
        bot.send_message(message.chat.id, "😕 Ничего не найдено или всё отфильтровано.")

@bot.message_handler(commands=["monitor"])
def handle_monitor(message):
    chat_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Пример: <code>/monitor iphone 15</code>")
        return
    query = parts[1].strip()
    chat_filters = filters_module.get_filters(chat_id)

    with _MONITORS_LOCK:
        if chat_id in _MONITORS:
            bot.reply_to(message, "Мониторинг уже запущен.")
            return
        stop_event = threading.Event()
        thread = threading.Thread(target=_monitor_loop, args=(chat_id, query, chat_filters, stop_event), daemon=True)
        _MONITORS[chat_id] = {"stop_event": stop_event, "thread": thread, "seen": _get_initial_seen_urls(chat_id)}
        thread.start()
    
    bot.reply_to(message, f"✅ Мониторинг '{query}' запущен. /stopmonitor для остановки.")

def _monitor_loop(chat_id: int, query: str, filters: dict, stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            new_ads = search_ads(query, config.MONITOR_MAX_ADS_PER_CHECK, filters=filters)
            session = create_session()
            already_seen = get_seen_urls(chat_id)
            count = 0
            for ad in new_ads:
                url = ad.get("ad_url")
                if not url or url in already_seen: continue
                try: ad, _ = enrich_with_detail(ad, session)
                except: pass
                for filtered_ad in filters_module.apply_client_filters([ad], filters):
                    _send_ads(chat_id, [filtered_ad])
                    add_seen_urls(chat_id, [url])
                    count += 1
            if count > 0: log.info("Отправлено %d новых", count)
        except Exception as e:
            log.error("Ошибка мониторинга: %s", e)
        if stop_event.wait(config.MONITOR_INTERVAL_SECONDS): break

@bot.message_handler(commands=["stopmonitor"])
def handle_stop_monitor(message):
    chat_id = message.chat.id
    with _MONITORS_LOCK:
        monitor = _MONITORS.pop(chat_id, None)
    if monitor:
        monitor["stop_event"].set()
        bot.reply_to(message, "⏹️ Остановлено.")
    else:
        bot.reply_to(message, "Мониторинг не запущен.")

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    log.info("Запуск бота...")
    bot.infinity_polling(timeout=10, long_polling_timeout=60)
