"""
История уже отправленных объявлений — чтобы бот не присылал одно и то же
объявление повторно при повторном /parse по тому же запросу (или тексту),
а также чтобы /monitor не дублировал то, что уже было показано вручную.

Хранение — В ПАМЯТИ ПРОЦЕССА, по тому же принципу, что и filters.py:
просто, не требует базы данных, но список слетает при перезапуске сервиса
(передеплой, засыпание/пробуждение на free-хостинге). Если это станет
проблемой — см. README, раздел "Что доделать в первую очередь" (замена на
SQLite/Redis, там же хранить и фильтры).
"""
import threading

_LOCK = threading.Lock()

# chat_id -> set(ad_url)
_SENT: dict[int, set] = {}

# Чтобы список не рос бесконечно при активном использовании — как только
# для чата накопилось больше MAX_STORED_PER_CHAT ссылок, обрезаем до
# TRIM_TO самых недавних (порядок в set не гарантирован, поэтому это
# приблизительная, а не строгая LRU-обрезка — для целей дедупликации
# этого достаточно).
MAX_STORED_PER_CHAT = 3000
TRIM_TO = 1500


def already_sent(chat_id: int, ad_url: str) -> bool:
    """Показывали ли уже это объявление в данном чате."""
    if not ad_url:
        return False
    with _LOCK:
        return ad_url in _SENT.get(chat_id, set())


def mark_sent(chat_id: int, ad_url: str) -> None:
    """Отметить объявление как отправленное в данном чате."""
    if not ad_url:
        return
    with _LOCK:
        seen = _SENT.setdefault(chat_id, set())
        seen.add(ad_url)
        if len(seen) > MAX_STORED_PER_CHAT:
            _SENT[chat_id] = set(list(seen)[-TRIM_TO:])


def get_seen(chat_id: int) -> set:
    """Копия множества уже отправленных ссылок для данного чата — передаётся
    в parser.search_ads(exclude_urls=...), чтобы такие объявления не попадали
    в результат поиска вовсе (и не тратились дозапросы деталей на них)."""
    with _LOCK:
        return set(_SENT.get(chat_id, set()))


def reset(chat_id: int) -> int:
    """Очистить историю для чата (команда /resetseen). Возвращает, сколько
    ссылок было очищено."""
    with _LOCK:
        seen = _SENT.pop(chat_id, set())
        return len(seen)
