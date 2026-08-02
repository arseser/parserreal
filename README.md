# OLX.pl Telegram-бот

## Структура

```
olx_telegram_bot/
├── bot.py          # Главный файл (long polling)
├── parser.py       # Парсер OLX (JSON + HTML fallback + детали объявления)
├── config.py       # Настройки (токен, паузы, лимиты)
├── requirements.txt
├── Procfile        # для Render/Railway (worker: python bot.py)
└── olx.txt         # создаётся автоматически при первом парсинге
```

## Как это работает

1. Пользователь пишет боту `/parse <ссылка или ключевое слово> [лимит]`.
2. Парсер обходит страницы выдачи OLX.pl (пагинация), сначала пытаясь
   вытащить встроенный JSON с данными объявлений, а если не получилось —
   разбирает HTML через BeautifulSoup (несколько наборов селекторов).
3. Если в карточке не было продавца/доставки — бот дозапрашивает страницу
   конкретного объявления (с задержкой).
4. В чат уходят первые `limit` объявлений (фото + текст), а следом —
   файл `olx.txt` со **всеми** найденными объявлениями.

## Локальный запуск

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="ваш_токен_от_BotFather"   # Windows: set TELEGRAM_BOT_TOKEN=...
python bot.py
```

## Деплой на Render

1. Залейте проект в GitHub-репозиторий.
2. Render → **New** → **Background Worker** (не Web Service — боту на polling
   не нужен открытый HTTP-порт).
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python bot.py`
5. В Environment → добавьте переменную `TELEGRAM_BOT_TOKEN`.
6. Deploy.

## Деплой на Railway

1. Залейте проект в GitHub, импортируйте репозиторий в Railway.
2. Railway сам подхватит `Procfile` (`worker: python bot.py`).
   Если создаст Web-сервис по умолчанию — вручную укажите Start Command
   `python bot.py` и уберите привязку к порту (боту порт не нужен).
3. В Variables → добавьте `TELEGRAM_BOT_TOKEN`.
4. Deploy.

## Важные оговорки

- **OLX регулярно меняет вёрстку и защиту от ботов.** Я заложил запасные
  селекторы и попытку читать встроенный JSON, но раз в какое-то время
  парсер всё равно может потребовать обновления селекторов в `parser.py`
  (см. `CARD_SELECTORS`, `DETAIL_SELLER_SELECTORS`, `DETAIL_DELIVERY_SELECTORS`).
- **Бесплатные хостинги (Render/Railway free tier)** могут "усыплять"
  сервис при простое или ограничивать исходящий трафик — если бот перестал
  отвечать после долгого бездействия, проверьте логи сервиса.
- **Без прокси.** Если OLX начнёт стабильно возвращать 403/429 с IP вашего
  хостинга, потребуется добавить ротацию прокси — в текущей версии её нет,
  только ротация User-Agent и задержки между запросами.
- Используйте парсинг ответственно: не завышайте лимиты и не убирайте
  задержки — это увеличивает риск блокировки IP хостинга.
