"""
Telegram-бот для парсинга OLX.pl и OLX.pt.
"""
import logging
import os
import re
import threading
import time
import traceback

import telebot
from flask import Flask

import config
import filters as filters_module
import history
from parser import (
    parse_olx,
    search_ads,
    enrich_many,
    OlxParserError,
    detect_locale,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("olx_bot")

bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN, parse_mode="HTML")

# --- Flask web server (для Render) ---
web_app = Flask(__name__)

@web_app.route("/")
def health_check():
    return "OLX Telegram bot is running.", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

HELP_TEXT = (
    "👋 Привет! Я парсер объявлений OLX.pl и OLX.pt.\n\n"
    "Отправь мне ссылку на поиск/категорию или ключевое слово – я пришлю объявления.\n\n"
    "<b>Команды:</b>\n"
    "<code>/parse ЗАПРОС_ИЛИ_ССЫЛКА [лимит]</code> – разовый поиск\n"
    "<code>/filters</code> – настроить категорию, цену, доставку, банворды\n"
    "<code>/monitor ЗАПРОС_ИЛИ_ССЫЛКА</code> – автослежение\n"
    "<code>/stopmonitor</code> – отключить автослежение\n"
    "<code>/resetseen</code> – забыть отправленные объявления\n\n"
    f"Лимит по умолчанию: {config.DEFAULT_LIMIT}, максимум: {config.MAX_LIMIT}.\n"
    "Объявления, которые уже присылались, повторно не приходят."
)

_AWAITING: dict[int, str] = {}
_MONITORS: dict[int, dict] = {}
_MONITORS_LOCK = threading.Lock()

# --- Обработчики команд (start, help, resetseen, filters) ---
# (оставлены как в исходном коде, без изменений)
# ...

@bot.message_handler(commands=["parse"])
def handle_parse(message):
    query, limit = _parse_args(message.text)
    if not query:
        bot.reply_to(message, "Укажи запрос или ссылку. Пример:\n<code>/parse iphone 15 20</code>")
        return

    chat_filters = filters_module.get_filters(message.chat.id)
    locale = detect_locale(query)
    status_msg = bot.reply_to(message, f"🔎 Ищу объявления по запросу: <b>{query}</b> (лимит {limit})...")

    last_edit_ts = [0.0]
    def _progress(stage, current, total):
        now = time.time()
        if now - last_edit_ts[0] < 4:
            return
        last_edit_ts[0] = now
        text = f"🔎 Ищу объявления... страница {current}, найдено {total}"
        try:
            bot.edit_message_text(text, chat_id=status_msg.chat.id, message_id=status_msg.message_id)
        except Exception:
            pass

    already_seen = history.get_seen(message.chat.id)
    try:
        # Собираем с запасом (COLLECT_MULTIPLIER)
        ads = search_ads(query, limit, filters=chat_filters, progress_cb=_progress,
                         exclude_urls=already_seen, locale=locale)
    except OlxParserError as e:
        bot.edit_message_text(
            f"❌ Ошибка: {e}\n\nПопробуй другой запрос, ослабь фильтры (/filters) или повтори позже.",
            chat_id=status_msg.chat.id, message_id=status_msg.message_id,
        )
        return
    except Exception as e:
        log.error("Ошибка поиска: %s\n%s", e, traceback.format_exc())
        bot.edit_message_text("❌ Непредвиденная ошибка. Попробуй позже.", chat_id=status_msg.chat.id, message_id=status_msg.message_id)
        return

    try:
        bot.edit_message_text(
            f"✅ Найдено {len(ads)} объявлений. Собираю данные о продавцах...",
            chat_id=status_msg.chat.id, message_id=status_msg.message_id,
        )
    except Exception:
        pass

    sent_ads = []
    sent_count = 0
    try:
        for ad, ok in enrich_many(ads, locale=locale):
            # Применяем клиентские фильтры
            filtered = filters_module.apply_client_filters([ad], chat_filters)
            if not filtered:
                continue
            good_ad = filtered[0]
            ad_url = good_ad.get("ad_url")
            if history.already_sent(message.chat.id, ad_url):
                continue
            # Отправляем не более limit штук
            if sent_count >= limit:
                break
            _send_ads(message.chat.id, [good_ad])
            history.mark_sent(message.chat.id, ad_url)
            sent_ads.append(good_ad)
            sent_count += 1
    finally:
        if sent_ads:
            try:
                _send_txt_file(message.chat.id, sent_ads)
            except Exception as e:
                log.error("Ошибка отправки файла: %s", e)
                bot.send_message(message.chat.id, "⚠️ Не удалось отправить файл с результатами.")
        else:
            bot.send_message(
                message.chat.id,
                "😕 После фильтров ничего не осталось, либо все объявления уже были показаны. "
                "Попробуйте ослабить фильтры (/filters) или сбросить историю (/resetseen)."
            )

# --- Остальные функции (_format_caption, _send_ads, _send_txt_file, мониторинг) ---
# (оставлены как в исходном коде, но _format_caption уже выводит seller_reviews_count)
# ...

# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    while True:
        try:
            bot.remove_webhook()
            time.sleep(1)
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            log.error("Polling упал: %s\n%s", e, traceback.format_exc())
            time.sleep(5)
