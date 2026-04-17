import sqlite3
import threading
from datetime import datetime


class Storage:
    """SQLite tabanlı veri kalıcılığı modülü."""

    def __init__(self, db_path="data.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._ram_cache = set()  # Double-Lock Check için RAM cache
        self._init_db()
        self._load_cache()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.executescript("""
                CREATE TABLE IF NOT EXISTS sent_links (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    link      TEXT UNIQUE NOT NULL,
                    timestamp TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_progress (
                    channel_id      TEXT PRIMARY KEY,
                    last_message_id TEXT,
                    completed       INTEGER NOT NULL DEFAULT 0,
                    timestamp       TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_mapping (
                    pair_index         INTEGER NOT NULL,
                    source_channel_id  TEXT PRIMARY KEY,
                    target_channel_id  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            # Eski channel_mapping tablosunda pair_index kolonu yoksa ekle (migration)
            try:
                cursor.execute("ALTER TABLE channel_mapping ADD COLUMN pair_index INTEGER NOT NULL DEFAULT 0")
                conn.commit()
            except Exception:
                pass
            # Eski channel_progress tablosunda completed kolonu yoksa ekle (migration)
            try:
                cursor.execute("ALTER TABLE channel_progress ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
                conn.commit()
            except Exception:
                pass
            conn.commit()
            conn.close()

    def _load_cache(self):
        """Gönderilen linkleri RAM'e yükle (hız için)."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT link FROM sent_links")
            rows = cursor.fetchall()
            conn.close()
            self._ram_cache = {row[0] for row in rows}

    # ─── Sent Links ───────────────────────────────────────────────────────────

    def is_link_sent(self, link: str) -> bool:
        """Double-Lock Check: önce RAM, sonra SQLite."""
        if link in self._ram_cache:
            return True
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM sent_links WHERE link = ?", (link,))
            result = cursor.fetchone()
            conn.close()
            if result:
                self._ram_cache.add(link)
                return True
            return False

    def save_sent_link(self, link: str):
        """Gönderilen linki hem RAM hem SQLite'a kaydet."""
        self._ram_cache.add(link)
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT OR IGNORE INTO sent_links (link, timestamp) VALUES (?, ?)",
                    (link, datetime.now().isoformat())
                )
                conn.commit()
            except sqlite3.Error:
                pass
            finally:
                conn.close()

    def get_sent_count(self) -> int:
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sent_links")
            count = cursor.fetchone()[0]
            conn.close()
            return count

    # ─── Channel Progress ─────────────────────────────────────────────────────

    def get_channel_progress(self, channel_id: str) -> dict:
        """
        Kanal için progress bilgisi döndürür.
        Returns: {last_message_id: str|None, completed: bool}
        """
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT last_message_id, completed FROM channel_progress WHERE channel_id = ?",
                (str(channel_id),)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                return {"last_message_id": row[0], "completed": bool(row[1])}
            return {"last_message_id": None, "completed": False}

    def save_channel_progress(self, channel_id: str, last_message_id: str, completed: bool = False):
        """Kanal progress'ini kaydet. completed=True → kanal tamamen tarandı."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                """INSERT OR REPLACE INTO channel_progress
                   (channel_id, last_message_id, completed, timestamp)
                   VALUES (?, ?, ?, ?)""",
                (str(channel_id), str(last_message_id), int(completed), datetime.now().isoformat())
            )
            conn.commit()
            conn.close()

    def mark_channel_completed(self, channel_id: str):
        """Kanalı tamamlandı olarak işaretle."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                """INSERT OR REPLACE INTO channel_progress
                   (channel_id, last_message_id, completed, timestamp)
                   VALUES (?, COALESCE((SELECT last_message_id FROM channel_progress WHERE channel_id=?), '0'), 1, ?)""",
                (str(channel_id), str(channel_id), datetime.now().isoformat())
            )
            conn.commit()
            conn.close()

    def is_channel_completed(self, channel_id: str) -> bool:
        progress = self.get_channel_progress(channel_id)
        return progress["completed"]

    # ─── Channel Mapping ──────────────────────────────────────────────────────

    def get_channel_mapping(self) -> dict:
        """pair_index sırasına göre sıralı {src_id: tgt_id} döndürür."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT source_channel_id, target_channel_id FROM channel_mapping ORDER BY pair_index ASC"
            )
            rows = cursor.fetchall()
            conn.close()
            return {row[0]: row[1] for row in rows}

    def save_channel_mapping(self, mapping: dict):
        """Mapping'i pair_index ile birlikte kaydet (sıra korunur)."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM channel_mapping")
            for idx, (src, tgt) in enumerate(mapping.items()):
                cursor.execute(
                    "INSERT INTO channel_mapping (pair_index, source_channel_id, target_channel_id) VALUES (?, ?, ?)",
                    (idx, str(src), str(tgt))
                )
            conn.commit()
            conn.close()

    # ─── Session State ────────────────────────────────────────────────────────

    def save_session_value(self, key: str, value: str):
        """Genel amaçlı session değeri kaydet (örn: aktif kanal index'i)."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO session_state (key, value) VALUES (?, ?)",
                (key, str(value))
            )
            conn.commit()
            conn.close()

    def get_session_value(self, key: str, default: str = None) -> str | None:
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM session_state WHERE key = ?", (key,))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else default

    def clear_session_value(self, key: str):
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM session_state WHERE key = ?", (key,))
            conn.commit()
            conn.close()

    # ─── Reset ────────────────────────────────────────────────────────────────

    def reset_database(self):
        """Tüm verileri siler (mapping dahil)."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.executescript("""
                DELETE FROM sent_links;
                DELETE FROM channel_progress;
                DELETE FROM channel_mapping;
                DELETE FROM session_state;
            """)
            conn.commit()
            conn.close()
            self._ram_cache.clear()

    def reset_progress_only(self):
        """Sadece progress ve session'ı sıfırla, gönderilen linkler korunur."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.executescript("""
                DELETE FROM channel_progress;
                DELETE FROM session_state;
            """)
            conn.commit()
            conn.close()
