"""
Telegram-бот для парсинга OLX.pl — расширенные данные, настраиваемые
фильтры (/filters) и автомониторинг новых объявлений (/monitor).

Запуск: python bot.py
Требуется переменная окружения TELEGRAM_BOT_TOKEN.

ВАЖНО про Render free plan:
Бесплатный тариф Render даёт только "Web Service" (Background Worker —
платный), а Web Service ожидает, что приложение слушает HTTP-порт.
Поэтому здесь поднят маленький Flask-сервер (отвечает "OK" на любой
GET-запрос) в отдельном потоке — он нужен только чтобы Render видел
открытый порт, вся логика бота как была на long polling, так и осталась.
"""
import logging
import os
import threading
import time
import traceback

import telebot
from telebot.types import InputFile
from flask import Flask

import config
import filters as filters_module
from parser import parse_olx, OlxParserError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("olx_bot")

bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN, parse_mode="HTML")

# --- мини веб-сервер только для открытого порта (требование Render Web Service) ---
web_app = Flask(__name__)


@web_app.route("/")
def health_check():
    return "OLX Telegram bot is running.", 200


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


HELP_TEXT = (
    "👋 Привет! Я парсер объявлений OLX.pl.\n\n"
    "Отправь мне ссылку на поиск/категорию OLX.pl или просто ключевое слово — "
    "и я пришлю подходящие объявления с полными данными о продавце.\n\n"
    "<b>Команды:</b>\n"
    "<code>/parse ЗАПРОС_ИЛИ_ССЫЛКА [лимит]</code> — разовый поиск\n"
    "<code>/filters</code> — настроить категорию, цену, доставку, банворды и т.д.\n"
    "<code>/monitor ЗАПРОС_ИЛИ_ССЫЛКА</code> — включить автослежение за новыми "
    "объявлениями (проверка каждые "
    f"{config.MONITOR_INTERVAL_SECONDS // 60} мин.)\n"
    "<code>/stopmonitor</code> — выключить автослежение в этом чате\n\n"
    f"Лимит по умолчанию: {config.DEFAULT_LIMIT}, максимум: {config.MAX_LIMIT}.\n"
    "После списка объявлений я пришлю файл olx.txt со всеми найденными данными."
)

# chat_id -> что бот ждёт следующим текстовым сообщением ("price", "banwords",
# "seller_ads", "seller_rating") или None
_AWAITING: dict[int, str] = {}

# --- автомониторинг: chat_id -> {"stop_event", "thread", "query", "filters", "seen"} ---
_MONITORS: dict[int, dict] = {}
_MONITORS_LOCK = threading.Lock()


@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    bot.reply_to(message, HELP_TEXT)


# ---------------------------------------------------------------------------
# /filters — меню настройки фильтров
# ---------------------------------------------------------------------------

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

    # открытие подменю
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

    # выбор конкретного значения
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

    # поля, требующие ввода текста
    prompts = {
        "price": "Введите цену в формате <code>мин макс</code> (например <code>100 500</code>), "
                 "только <code>мин</code> (например <code>100</code>) или <code>-</code>, чтобы сбросить.",
        "banwords": "Отправьте слова через запятую, объявления с ними в названии будут скрыты. "
                    "Например: <code>сломан, битый, донор</code>. Отправьте <code>-</code>, чтобы очистить список.",
        "seller_ads": "Введите минимальное число объявлений продавца (например <code>3</code>) "
                      "или <code>-</code>, чтобы сбросить.",
        "seller_rating": "Введите минимальный рейтинг продавца от 0 до 5 (например <code>4</code>) "
                          "или <code>-</code>, чтобы сбросить.",
    }
    if action in prompts:
        _AWAITING[chat_id] = action
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, prompts[action])
        return

    bot.answer_callback_query(call.id)


def _handle_awaited_input(message) -> bool:
    """Если бот ждал текстовый ввод для фильтра — обрабатывает его и
    возвращает True. Иначе возвращает False (сообщение обычное)."""
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
        bot.reply_to(message, "Не понял значение. Попробуйте ещё раз или отправьте <code>-</code>, чтобы сбросить.")
        return True

    bot.send_message(chat_id, filters_module.summary_text(chat_id), reply_markup=filters_module.main_menu_keyboard(chat_id))
    return True


# ---------------------------------------------------------------------------
# /parse — разовый поиск
# ---------------------------------------------------------------------------

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
    status_msg = bot.reply_to(message, f"🔎 Ищу объявления по запросу: <b>{query}</b> (лимит {limit})...")

    try:
        ads = parse_olx(query, limit, filters=chat_filters)
    except OlxParserError as e:
        bot.edit_message_text(
            f"❌ Не удалось получить объявления.\n\nПричина: {e}\n\n"
            "Попробуй другой запрос/ссылку, ослабь фильтры (/filters) или повтори попытку позже.",
            chat_id=status_msg.chat.id, message_id=status_msg.message_id,
        )
        return
    except Exception as e:
        log.error("Непредвиденная ошибка парсинга: %s\n%s", e, traceback.format_exc())
        bot.edit_message_text("❌ Произошла непредвиденная ошибка при парсинге. Попробуй ещё раз позже.",
                               chat_id=status_msg.chat.id, message_id=status_msg.message_id)
        return

    ads = filters_module.apply_client_filters(ads, chat_filters)
    if not ads:
        bot.edit_message_text(
            "😕 После применения фильтров (период/банворды/продавец) ничего не осталось. "
            "Попробуйте ослабить фильтры через /filters.",
            chat_id=status_msg.chat.id, message_id=status_msg.message_id,
        )
        return

    bot.edit_message_text(
        f"✅ Найдено объявлений: {len(ads)}. Отправляю первые {min(limit, len(ads))}...",
        chat_id=status_msg.chat.id, message_id=status_msg.message_id,
    )

    _send_ads(message.chat.id, ads[:limit])
    _send_txt_file(message.chat.id, ads)


def _format_caption(ad: dict) -> str:
    delivery_line = "📦 Товар можно взять с доставкой" if ad.get("delivery") else "🚫 Доставки нет / не указана"
    rating = ad.get("seller_rating")
    rating_str = f"{rating}/5" if rating not in (None, "") else "0/5"
    return (
        f"Товар: <code>{ad.get('title', 'Без названия')}</code>\n"
        f"Цена: <code>{ad.get('price', 'не указана')}</code>\n"
        f"Местоположение товара: {ad.get('location') or 'не указано'}\n"
        f"Имя продавца: <code>{ad.get('seller', 'не указан')}</code>\n\n"
        f"Объявление: <a href=\"{ad.get('ad_url', '')}\">Клик</a>\n"
        f"Фото: <a href=\"{ad.get('photo') or ad.get('ad_url', '')}\">Клик</a>\n"
        f"Чат с продавцом: <a href=\"{ad.get('chat_url') or ad.get('ad_url', '')}\">Клик</a>\n\n"
        f"Дата создания объявления: {ad.get('created_display', 'не указана')}\n"
        f"Дата регистрации продавца: {ad.get('seller_registered') or 'не указана'}\n"
        f"Был в сети: {ad.get('seller_last_seen') or 'не указано'}\n"
        f"Кол-во объявлений продавца: {ad.get('seller_ads_count') if ad.get('seller_ads_count') is not None else 'не указано'}\n"
        f"{delivery_line}\n"
        f"Рейтинг продавца: {rating_str}"
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
            log.warning("Не удалось отправить фото для %s: %s", ad.get("ad_url"), e)
            try:
                bot.send_message(chat_id, caption)
            except Exception:
                pass


def _send_txt_file(chat_id: int, ads: list):
    path = os.path.join(os.getcwd(), config.OUTPUT_FILE)
    with open(path, "w", encoding="utf-8") as f:
        for ad in ads:
            f.write(f"Товар: {ad.get('title', '')}\n")
            f.write(f"Цена: {ad.get('price', '')}\n")
            f.write(f"Местоположение: {ad.get('location') or ''}\n")
            f.write(f"Продавец: {ad.get('seller', '')}\n")
            f.write(f"Ссылка: {ad.get('ad_url', '')}\n")
            f.write(f"Чат: {ad.get('chat_url', '')}\n")
            f.write(f"Фото: {ad.get('photo') or '-'}\n")
            f.write(f"Дата создания: {ad.get('created_display', '')}\n")
            f.write(f"Регистрация продавца: {ad.get('seller_registered') or ''}\n")
            f.write(f"Был в сети: {ad.get('seller_last_seen') or ''}\n")
            f.write(f"Объявлений продавца: {ad.get('seller_ads_count') if ad.get('seller_ads_count') is not None else ''}\n")
            f.write(f"Доставка: {'Да' if ad.get('delivery') else 'Нет'}\n")
            f.write(f"Рейтинг: {ad.get('seller_rating') if ad.get('seller_rating') is not None else ''}\n")
            f.write("===\n")
    with open(path, "rb") as fh:
        bot.send_document(chat_id, InputFile(fh, filename=config.OUTPUT_FILE), caption=f"📄 Все найденные объявления: {len(ads)} шт.")


# ---------------------------------------------------------------------------
# /monitor, /stopmonitor — автомониторинг новых объявлений
# ---------------------------------------------------------------------------

def _monitor_loop(chat_id: int, query: str, snapshot_filters: dict, stop_event: threading.Event):
    log.info("Мониторинг запущен: chat=%s query=%s", chat_id, query)
    while not stop_event.is_set():
        try:
            ads = parse_olx(query, config.MONITOR_CHECK_LIMIT, filters=snapshot_filters, fetch_details=True)
            ads = filters_module.apply_client_filters(ads, snapshot_filters)
        except OlxParserError as e:
            log.warning("Мониторинг: ошибка парсинга (chat=%s): %s", chat_id, e)
            ads = []
        except Exception as e:
            log.error("Мониторинг: непредвиденная ошибка (chat=%s): %s\n%s", chat_id, e, traceback.format_exc())
            ads = []

        with _MONITORS_LOCK:
            entry = _MONITORS.get(chat_id)
            if entry is None or entry["stop_event"] is not stop_event:
                return  # мониторинг уже остановлен/заменён
            seen = entry["seen"]
            new_ads = [a for a in ads if a.get("ad_url") and a["ad_url"] not in seen]
            for a in new_ads:
                seen.add(a["ad_url"])
            # ограничиваем размер множества, чтобы не расти бесконечно
            if len(seen) > 500:
                entry["seen"] = set(list(seen)[-300:])

        if new_ads:
            try:
                bot.send_message(chat_id, f"🔔 Новых объявлений: {len(new_ads)}")
            except Exception:
                pass
            _send_ads(chat_id, new_ads)

        stop_event.wait(config.MONITOR_INTERVAL_SECONDS)
    log.info("Мониторинг остановлен: chat=%s query=%s", chat_id, query)


@bot.message_handler(commands=["monitor"])
def handle_monitor(message):
    chat_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Укажи запрос или ссылку. Пример:\n<code>/monitor iphone 15</code>")
        return
    query = parts[1].strip()

    with _MONITORS_LOCK:
        if chat_id in _MONITORS:
            bot.reply_to(message, "У вас уже включён мониторинг. Сначала остановите его: /stopmonitor")
            return
        if len(_MONITORS) >= config.MONITOR_MAX_SUBSCRIPTIONS:
            bot.reply_to(message, "Достигнут лимит одновременных подписок на мониторинг. Попробуйте позже.")
            return

        snapshot_filters = dict(filters_module.get_filters(chat_id))
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_monitor_loop, args=(chat_id, query, snapshot_filters, stop_event), daemon=True
        )
        _MONITORS[chat_id] = {"stop_event": stop_event, "thread": thread, "query": query, "seen": set()}
        thread.start()

    bot.reply_to(
        message,
        f"🟢 Мониторинг включён для запроса: <b>{query}</b>\n"
        f"Проверка каждые {config.MONITOR_INTERVAL_SECONDS // 60} мин. с текущими фильтрами (/filters).\n"
        "Чтобы остановить: /stopmonitor",
    )


@bot.message_handler(commands=["stopmonitor"])
def handle_stopmonitor(message):
    chat_id = message.chat.id
    with _MONITORS_LOCK:
        entry = _MONITORS.pop(chat_id, None)
    if not entry:
        bot.reply_to(message, "Мониторинг в этом чате и так не запущен.")
        return
    entry["stop_event"].set()
    bot.reply_to(message, "🔴 Мониторинг остановлен.")


# ---------------------------------------------------------------------------
# Прочий текст
# ---------------------------------------------------------------------------

@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_any_text(message):
    if message.text.startswith("/"):
        bot.reply_to(message, "Неизвестная команда. Напиши /help.")
        return
    if _handle_awaited_input(message):
        return
    message.text = f"/parse {message.text}"
    handle_parse(message)


def run_bot_with_restart():
    while True:
        try:
            bot.remove_webhook()
            time.sleep(1)
            log.info("Бот запускается (long polling)...")
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            log.error("Polling упал: %s\n%s", e, traceback.format_exc())
            time.sleep(5)


if __name__ == "__main__":
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    run_bot_with_restart()
