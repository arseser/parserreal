--- sent_ads_storage.py (原始)


+++ sent_ads_storage.py (修改后)
"""
Хранилище уже отправленных объявлений (по URL).
Используется в режиме мониторинга, чтобы не дублировать объявления.

Хранение — в файле sent_ads.json в рабочей директории.
Формат: { chat_id: [url1, url2, ...], ... }
"""
import json
import os
from threading import Lock

FILE_PATH = os.path.join(os.getcwd(), "sent_ads.json")
_lock = Lock()


def _load() -> dict:
    if not os.path.exists(FILE_PATH):
        return {}
    try:
        with open(FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            # конвертируем списки обратно в множества для быстрого поиска
            return {int(k): set(v) for k, v in data.items()}
    except (json.JSONDecodeError, IOError):
        return {}


def _save(data: dict) -> None:
    # конвертируем множества обратно в списки для JSON
    serializable = {str(k): list(v) for k, v in data.items()}
    with open(FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def get_seen_urls(chat_id: int) -> set:
    """Вернуть множество URL объявлений, которые уже были отправлены в этот чат."""
    with _lock:
        data = _load()
        return data.get(chat_id, set())


def add_seen_urls(chat_id: int, urls: list) -> None:
    """Добавить URL объявлений в список уже отправленных для этого чата."""
    with _lock:
        data = _load()
        if chat_id not in data:
            data[chat_id] = set()
        data[chat_id].update(urls)
        # ограничиваем размер множества, чтобы файл не рос бесконечно
        if len(data[chat_id]) > 500:
            data[chat_id] = set(list(data[chat_id])[-300:])
        _save(data)


def clear_seen_urls(chat_id: int) -> None:
    """Очистить список отправленных объявлений для чата."""
    with _lock:
        data = _load()
        if chat_id in data:
            del data[chat_id]
            _save(data)
