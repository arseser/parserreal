"""
Telegram-бот для парсинга OLX.pl.

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
    "и я пришлю подходящие объявления.\n\n"
    "Формат команды:\n"
    "<code>/parse ЗАПРОС_ИЛИ_ССЫЛКА [лимит]</code>\n\n"
    "Примеры:\n"
    "<code>/parse iphone 15</code>\n"
    "<code>/parse https://www.olx.pl/elektronika/telefony/ 30</code>\n\n"
    f"Лимит по умолчанию: {config.DEFAULT_LIMIT}, максимум: {config.MAX_LIMIT}.\n"
    "После списка объявлений я пришлю файл olx.txt со всеми найденными данными "
    "(их может быть больше, чем прислано сообщений в чат)."
)


@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    bot.reply_to(message, HELP_TEXT)


def _parse_args(text: str):
    """Разбирает '/parse <запрос или ссылка> [лимит]'."""
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
        bot.reply_to(
            message,
            "Укажи запрос или ссылку. Пример:\n<code>/parse iphone 15 20</code>",
        )
        return

    status_msg = bot.reply_to(
        message, f"🔎 Ищу объявления по запросу: <b>{query}</b> (лимит {limit})..."
    )

    try:
        ads = parse_olx(query, limit)
    except OlxParserError as e:
        bot.edit_message_text(
            f"❌ Не удалось получить объявления.\n\nПричина: {e}\n\n"
            "Попробуй другой запрос/ссылку или повтори попытку позже — "
            "возможно, OLX временно ограничил запросы с этого сервера.",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
        )
        return
    except Exception as e:
        log.error("Непредвиденная ошибка парсинга: %s\n%s", e, traceback.format_exc())
        bot.edit_message_text(
            "❌ Произошла непредвиденная ошибка при парсинге. Попробуй ещё раз позже.",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
        )
        return

    bot.edit_message_text(
        f"✅ Найдено объявлений: {len(ads)}. Отправляю первые {min(limit, len(ads))}...",
        chat_id=status_msg.chat.id,
        message_id=status_msg.message_id,
    )

    to_send = ads[:limit]
    for ad in to_send:
        caption = _format_caption(ad)
        try:
            if ad.get("photo"):
                bot.send_photo(message.chat.id, ad["photo"], caption=caption)
            else:
                bot.send_message(message.chat.id, caption)
        except Exception as e:
            # если фото не загрузилось (битая ссылка, не то расширение и т.п.) —
            # всё равно шлём текстовые данные объявления
            log.warning("Не удалось отправить фото для %s: %s", ad.get("ad_url"), e)
            try:
                bot.send_message(message.chat.id, caption)
            except Exception:
                pass

    file_path = _write_txt_file(ads)
    with open(file_path, "rb") as f:
        bot.send_document(
            message.chat.id,
            InputFile(f, filename=config.OUTPUT_FILE),
            caption=f"📄 Все найденные объявления: {len(ads)} шт.",
        )


def _format_caption(ad: dict) -> str:
    return (
        f"🏷 <b>{ad.get('title', 'Без названия')}</b>\n"
        f"💰 Цена: {ad.get('price', 'не указана')}\n"
        f"🔗 Ссылка: {ad.get('ad_url', '-')}\n"
        f"👤 Продавец: {ad.get('seller', 'не указан')}\n"
        f"🚚 Доставка: {ad.get('delivery', 'Нет')}\n"
        f"📅 Дата публикации: {ad.get('date', 'не указана')}"
    )


def _write_txt_file(ads) -> str:
    path = os.path.join(os.getcwd(), config.OUTPUT_FILE)
    with open(path, "w", encoding="utf-8") as f:
        for ad in ads:
            f.write(f"Название: {ad.get('title', '')}\n")
            f.write(f"Цена: {ad.get('price', '')}\n")
            f.write(f"Ссылка: {ad.get('ad_url', '')}\n")
            f.write(f"Продавец: {ad.get('seller', '')}\n")
            f.write(f"Доставка: {ad.get('delivery', '')}\n")
            f.write(f"Дата публикации: {ad.get('date', '')}\n")
            f.write(f"Фото: {ad.get('photo') or '-'}\n")
            f.write("===\n")
    return path


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_any_text(message):
    if message.text.startswith("/"):
        bot.reply_to(message, "Неизвестная команда. Напиши /help.")
        return
    # обычный текст трактуем как запрос с лимитом по умолчанию
    message.text = f"/parse {message.text}"
    handle_parse(message)


def run_bot_with_restart():
    """infinity_polling иногда падает на сетевых сбоях — перезапускаем в цикле."""
    while True:
        try:
            # на случай, если для этого токена где-то остался webhook или
            # зависшая сессия getUpdates — сбрасываем перед стартом polling
            bot.remove_webhook()
            time.sleep(1)
            log.info("Бот запускается (long polling)...")
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            log.error("Polling упал: %s\n%s", e, traceback.format_exc())
            time.sleep(5)


if __name__ == "__main__":
    # веб-сервер — в фоновом потоке (нужен только для открытого порта на Render)
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # сам бот — в основном потоке
    run_bot_with_restart()
