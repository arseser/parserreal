"""
Хранилище уже отправленных объявлений для предотвращения дублей.
Использует SQLite базу данных.
"""
import sqlite3
import threading
from pathlib import Path


DB_PATH = Path("sent_ads.db")
_DB_LOCK = threading.Lock()


def init_db():
    """Инициализирует таблицу в базе данных."""
    with _DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_ads (
                chat_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                PRIMARY KEY (chat_id, url)
            )
        """)
        conn.commit()
        conn.close()


def get_seen_urls(chat_id: int) -> set:
    """
    Возвращает множество URL уже отправленных объявлений для данного чата.
    """
    with _DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT url FROM sent_ads WHERE chat_id = ?",
            (chat_id,)
        )
        urls = {row[0] for row in cursor.fetchall()}
        conn.close()
        return urls


def add_seen_urls(chat_id: int, urls: list):
    """
    Добавляет URL отправленных объявлений в базу данных.
    """
    if not urls:
        return
    with _DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        data = [(chat_id, url) for url in urls]
        cursor.executemany(
            "INSERT OR IGNORE INTO sent_ads (chat_id, url) VALUES (?, ?)",
            data
        )
        conn.commit()
        conn.close()


init_db()
