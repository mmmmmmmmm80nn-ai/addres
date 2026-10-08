import copy
import functools
import re
import sqlite3
import threading
import time
import config

_lock = threading.Lock()

# ---- كاش قراءة قصير (3 ثواني) للدوال الكتير الاستخدام: بيقلل فتح اتصالات SQLite في كل رسالة ----
_RC = {}
_RC_TTL = 3.0

def _rc_clear():
    _RC.clear()

def _ttl_cache(fn):
    name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            key = (name, args, tuple(sorted(kwargs.items())))
            hash(key)
        except TypeError:
            return fn(*args, **kwargs)
        now = time.time()
        hit = _RC.get(key)
        if hit is not None and now - hit[0] < _RC_TTL:
            value = hit[1]
        else:
            value = fn(*args, **kwargs)
            if len(_RC) > 5000:
                _RC.clear()
            _RC[key] = (now, value)
        return copy.deepcopy(value) if isinstance(value, (dict, list)) else value
    return wrapper

def _invalidate_after(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            _rc_clear()
    return wrapper

def _connect():
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-8000")
    except sqlite3.Error:
        pass
    return conn

def init_db():
    with _lock:
        conn = _connect()
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS business_connections (
            business_connection_id TEXT PRIMARY KEY,
            owner_user_id INTEGER,
            owner_chat_id INTEGER,
            owner_name TEXT,
            owner_username TEXT,
            is_enabled INTEGER DEFAULT 1,
            created_at INTEGER
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_connection_id TEXT,
            chat_id INTEGER,
            message_id INTEGER,
            sender_id INTEGER,
            sender_name TEXT,
            sender_username TEXT,
            content TEXT,
            content_type TEXT,
            date INTEGER
        )
        """)
        cur.execute("PRAGMA table_info(messages)")
        message_cols = {row[1] for row in cur.fetchall()}
        if "media_file_id" not in message_cols:
            cur.execute("ALTER TABLE messages ADD COLUMN media_file_id TEXT")
        if "media_kind" not in message_cols:
            cur.execute("ALTER TABLE messages ADD COLUMN media_kind TEXT")

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_msg_lookup
        ON messages (business_connection_id, chat_id, message_id)
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_msg_sender
        ON messages (business_connection_id, chat_id, sender_id, date)
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS auto_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER,
            keyword TEXT,
            content_type TEXT,
            text_content TEXT,
            photo_file_id TEXT,
            has_button INTEGER DEFAULT 0,
            button_text TEXT,
            button_color TEXT,
            button_type TEXT,
            button_value TEXT,
            created_at INTEGER
        )
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_replies_owner
        ON auto_replies (owner_user_id)
        """)

        # صفحات الرد التلقائي: كل صفحة = زرار بيفتح نص الصفحة ومعاه رجوع
        cur.execute("""
        CREATE TABLE IF NOT EXISTS reply_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reply_id INTEGER,
            owner_user_id INTEGER,
            title TEXT,
            text_content TEXT,
            color TEXT,
            position INTEGER DEFAULT 0
        )
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_reply_pages_reply
        ON reply_pages (reply_id)
        """)

        # صلاحية الرد: all (للجميع) / owner (للمدير) / client (للعميل)
        cur.execute("PRAGMA table_info(auto_replies)")
        _ar_cols = {row[1] for row in cur.fetchall()}
        if "audience" not in _ar_cols:
            cur.execute("ALTER TABLE auto_replies ADD COLUMN audience TEXT DEFAULT 'all'")
        if "reply_kind" not in _ar_cols:
            cur.execute("ALTER TABLE auto_replies ADD COLUMN reply_kind TEXT DEFAULT 'normal'")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS online_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER UNIQUE,
            keyword TEXT,
            content_type TEXT,
            text_content TEXT,
            photo_file_id TEXT,
            has_button INTEGER DEFAULT 0,
            button_text TEXT,
            button_color TEXT,
            button_type TEXT,
            button_value TEXT,
            created_at INTEGER
        )
        """)

        cur.execute("PRAGMA table_info(online_replies)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "keyword" not in existing_cols:
            cur.execute("ALTER TABLE online_replies ADD COLUMN keyword TEXT")
        if "audience" not in existing_cols:
            cur.execute("ALTER TABLE online_replies ADD COLUMN audience TEXT DEFAULT 'all'")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS force_sub_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER,
            channel TEXT,
            created_at INTEGER
        )
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_fsub_channels_owner
        ON force_sub_channels (owner_user_id)
        """)

        cur.execute(
            "SELECT key, value FROM settings WHERE key = 'force_sub_channel' "
            "OR key LIKE 'force_sub_channel\\_%' ESCAPE '\\'"
        )
        legacy_rows = cur.fetchall()
        for key, value in legacy_rows:
            value = (value or "").strip()
            if value:
                if key == "force_sub_channel":
                    owner_user_id = None
                else:
                    suffix = key[len("force_sub_channel_"):]
                    try:
                        owner_user_id = int(suffix)
                    except ValueError:
                        owner_user_id = None
                        continue
                if owner_user_id is None:
                    exists = cur.execute(
                        "SELECT 1 FROM force_sub_channels WHERE owner_user_id IS NULL AND channel=?",
                        (value,),
                    ).fetchone()
                else:
                    exists = cur.execute(
                        "SELECT 1 FROM force_sub_channels WHERE owner_user_id=? AND channel=?",
                        (owner_user_id, value),
                    ).fetchone()
                if not exists:
                    cur.execute(
                        "INSERT INTO force_sub_channels (owner_user_id, channel, created_at) "
                        "VALUES (?, ?, strftime('%s','now'))",
                        (owner_user_id, value),
                    )
            cur.execute("DELETE FROM settings WHERE key=?", (key,))

        cur.execute("""
        CREATE TABLE IF NOT EXISTS banned_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER,
            word TEXT,
            created_at INTEGER
        )
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_banned_owner
        ON banned_words (owner_user_id)
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS banned_word_replies (
            owner_user_id INTEGER PRIMARY KEY,
            reply_text TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS muted_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER,
            muted_user_id INTEGER,
            muted_name TEXT,
            muted_username TEXT,
            created_at INTEGER,
            UNIQUE(owner_user_id, muted_user_id)
        )
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_muted_owner
        ON muted_users (owner_user_id)
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS force_sub_exempt (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER,
            user_id INTEGER,
            name TEXT,
            username TEXT,
            created_at INTEGER,
            UNIQUE(owner_user_id, user_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS blocked_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER,
            blocked_user_id INTEGER,
            blocked_name TEXT,
            blocked_username TEXT,
            created_at INTEGER,
            UNIQUE(owner_user_id, blocked_user_id)
        )
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_blocked_owner
        ON blocked_users (owner_user_id)
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS business_clients (
            owner_user_id INTEGER,
            chat_id INTEGER,
            business_connection_id TEXT,
            client_user_id INTEGER,
            client_name TEXT,
            client_username TEXT,
            last_message_at INTEGER,
            PRIMARY KEY (owner_user_id, chat_id)
        )
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_clients_owner
        ON business_clients (owner_user_id)
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            first_seen INTEGER,
            last_seen INTEGER
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            actor_id INTEGER,
            action TEXT,
            target_id INTEGER,
            details TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS supervisors (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            added_at INTEGER
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS banned_bot_users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            created_at INTEGER
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS premium_users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            granted_at INTEGER,
            expires_at INTEGER
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS hosted_bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER,
            display_name TEXT,
            filename TEXT,
            dir_path TEXT,
            python_version TEXT,
            status TEXT DEFAULT 'stopped',
            pid INTEGER,
            last_error TEXT,
            created_at INTEGER,
            updated_at INTEGER
        )
        """)

        cur.execute("PRAGMA table_info(hosted_bots)")
        hosted_cols = {row[1] for row in cur.fetchall()}
        if "bot_username" not in hosted_cols:
            cur.execute("ALTER TABLE hosted_bots ADD COLUMN bot_username TEXT")

        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_hosted_bots_owner
        ON hosted_bots (owner_id)
        """)

        conn.commit()
        conn.close()

def get_setting(key, default=None):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else default

def set_setting(key, value):
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()
        conn.close()

def list_settings(prefix):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ? ORDER BY key",
            (str(prefix) + "%",),
        ).fetchall()
        conn.close()
        return [(row["key"], row["value"]) for row in rows]

def add_force_sub_channel(owner_user_id, channel):
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO force_sub_channels (owner_user_id, channel, created_at) "
            "VALUES (?, ?, strftime('%s','now'))",
            (owner_user_id, channel),
        )
        conn.commit()
        conn.close()

def list_force_sub_channels(owner_user_id):
    with _lock:
        conn = _connect()
        if owner_user_id is None:
            rows = conn.execute(
                "SELECT * FROM force_sub_channels WHERE owner_user_id IS NULL ORDER BY id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM force_sub_channels WHERE owner_user_id=? ORDER BY id ASC",
                (owner_user_id,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def channel_exists(owner_user_id, channel):
    with _lock:
        conn = _connect()
        if owner_user_id is None:
            row = conn.execute(
                "SELECT id FROM force_sub_channels WHERE owner_user_id IS NULL AND lower(channel)=lower(?)",
                (channel,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM force_sub_channels WHERE owner_user_id=? AND lower(channel)=lower(?)",
                (owner_user_id, channel),
            ).fetchone()
        conn.close()
        return row is not None

def remove_force_sub_channel(channel_id, owner_user_id):
    with _lock:
        conn = _connect()
        if owner_user_id is None:
            conn.execute(
                "DELETE FROM force_sub_channels WHERE id=? AND owner_user_id IS NULL",
                (channel_id,),
            )
        else:
            conn.execute(
                "DELETE FROM force_sub_channels WHERE id=? AND owner_user_id=?",
                (channel_id, owner_user_id),
            )
        conn.commit()
        conn.close()

def clear_force_sub_channels(owner_user_id):
    with _lock:
        conn = _connect()
        if owner_user_id is None:
            conn.execute("DELETE FROM force_sub_channels WHERE owner_user_id IS NULL")
        else:
            conn.execute("DELETE FROM force_sub_channels WHERE owner_user_id=?", (owner_user_id,))
        conn.commit()
        conn.close()

def upsert_business_connection(bc_id, owner_user_id, owner_chat_id, owner_name, owner_username, is_enabled, created_at):
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO business_connections
                (business_connection_id, owner_user_id, owner_chat_id, owner_name, owner_username, is_enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(business_connection_id) DO UPDATE SET
                owner_user_id=excluded.owner_user_id,
                owner_chat_id=excluded.owner_chat_id,
                owner_name=excluded.owner_name,
                owner_username=excluded.owner_username,
                is_enabled=excluded.is_enabled,
                created_at=excluded.created_at
        """, (bc_id, owner_user_id, owner_chat_id, owner_name, owner_username, is_enabled, created_at))
        conn.commit()
        conn.close()

def get_business_connection(bc_id):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM business_connections WHERE business_connection_id=?", (bc_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

def set_connection_enabled(bc_id, enabled: bool):
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE business_connections SET is_enabled=? WHERE business_connection_id=?",
            (1 if enabled else 0, bc_id),
        )
        conn.commit()
        conn.close()

def list_known_bc_ids_for_owner(owner_user_id):
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT business_connection_id AS bc FROM business_connections WHERE owner_user_id=?
            UNION
            SELECT business_connection_id AS bc FROM business_clients WHERE owner_user_id=?
        """, (owner_user_id, owner_user_id)).fetchall()
        conn.close()
        return [r["bc"] for r in rows if r["bc"]]

def list_all_connections():
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT * FROM business_connections").fetchall()
        conn.close()
        return [dict(r) for r in rows]

def list_orphan_bc_ids(limit=30):
    """business_connection_id موجودة في الرسائل المحفوظة بس مالهاش سجل في business_connections."""
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT m.business_connection_id AS bc, MAX(m.date) AS last_date
            FROM messages m
            LEFT JOIN business_connections bc ON bc.business_connection_id = m.business_connection_id
            WHERE m.business_connection_id IS NOT NULL AND bc.business_connection_id IS NULL
            GROUP BY m.business_connection_id
            ORDER BY last_date DESC
            LIMIT ?
        """, (int(limit),)).fetchall()
        conn.close()
        return [r["bc"] for r in rows if r["bc"]]

def list_connections_for_owner(owner_user_id):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM business_connections WHERE owner_user_id=?", (owner_user_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def save_message(bc_id, chat_id, message_id, sender_id, sender_name, sender_username, content, content_type, date, media_file_id=None, media_kind=None):
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO messages
                (business_connection_id, chat_id, message_id, sender_id, sender_name, sender_username, content, content_type, date, media_file_id, media_kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (bc_id, chat_id, message_id, sender_id, sender_name, sender_username, content, content_type, date, media_file_id, media_kind))
        conn.commit()
        conn.close()

def get_message(bc_id, chat_id, message_id):
    with _lock:
        conn = _connect()
        row = conn.execute("""
            SELECT * FROM messages
            WHERE business_connection_id=? AND chat_id=? AND message_id=?
        """, (bc_id, chat_id, message_id)).fetchone()
        conn.close()
        return dict(row) if row else None

def get_last_message_from_sender(bc_id, chat_id, sender_id, exclude_message_id):
    with _lock:
        conn = _connect()
        row = conn.execute("""
            SELECT * FROM messages
            WHERE business_connection_id=? AND chat_id=? AND sender_id=? AND message_id!=?
            ORDER BY date DESC, id DESC
            LIMIT 1
        """, (bc_id, chat_id, sender_id, exclude_message_id)).fetchone()
        conn.close()
        return dict(row) if row else None

def get_recent_message_ids(bc_id, chat_id, limit, exclude_message_id=None):
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT message_id FROM messages
            WHERE business_connection_id=? AND chat_id=?
            ORDER BY date DESC, id DESC
            LIMIT ?
        """, (bc_id, chat_id, int(limit) + (1 if exclude_message_id else 0))).fetchall()
        conn.close()
        ids = [r["message_id"] for r in rows]
        if exclude_message_id is not None:
            ids = [i for i in ids if i != exclude_message_id]
        return ids[:limit]

def get_all_message_ids(bc_id, chat_id, exclude_message_id=None):
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT message_id FROM messages
            WHERE business_connection_id=? AND chat_id=?
            ORDER BY date DESC, id DESC
        """, (bc_id, chat_id)).fetchall()
        conn.close()
        ids = [r["message_id"] for r in rows]
        if exclude_message_id is not None:
            ids = [i for i in ids if i != exclude_message_id]
        return ids

def get_recent_messages(bc_id, chat_id, limit=12):
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT sender_id, content, content_type, date FROM messages
            WHERE business_connection_id=? AND chat_id=?
            ORDER BY date DESC, id DESC
            LIMIT ?
        """, (bc_id, chat_id, int(limit))).fetchall()
        conn.close()
        return list(reversed([dict(r) for r in rows]))

def add_auto_reply(owner_user_id, keyword, content_type, text_content, photo_file_id,
                    has_button, button_text, button_color, button_type, button_value,
                    reply_kind="normal"):
    with _lock:
        conn = _connect()
        cur = conn.execute("""
            INSERT INTO auto_replies
            (owner_user_id, keyword, content_type, text_content, photo_file_id,
             has_button, button_text, button_color, button_type, button_value, reply_kind, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
        """, (owner_user_id, keyword, content_type, text_content, photo_file_id,
              1 if has_button else 0, button_text, button_color, button_type, button_value,
              reply_kind if reply_kind in ("normal_ad", "promotional_ad") else "normal"))
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        return new_id

def get_auto_replies(owner_user_id):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM auto_replies WHERE owner_user_id=? ORDER BY id DESC", (owner_user_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def get_auto_reply(reply_id):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM auto_replies WHERE id=?", (reply_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

_REPLY_AUDIENCES = ("all", "owner", "client")

def set_auto_reply_audience(reply_id, owner_user_id, audience):
    if audience not in _REPLY_AUDIENCES:
        return
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE auto_replies SET audience=? WHERE id=? AND owner_user_id=?",
            (audience, reply_id, owner_user_id),
        )
        conn.commit()
        conn.close()

def set_online_reply_audience(owner_user_id, audience):
    if audience not in _REPLY_AUDIENCES:
        return
    with _lock:
        conn = _connect()
        conn.execute("UPDATE online_replies SET audience=? WHERE owner_user_id=?", (audience, owner_user_id))
        conn.commit()
        conn.close()

def set_all_replies_audience(owner_user_id, audience):
    """يطبّق الصلاحية على كل الردود (العادية + الانلاين) بتاعة الأونر."""
    if audience not in _REPLY_AUDIENCES:
        return
    with _lock:
        conn = _connect()
        conn.execute("UPDATE auto_replies SET audience=? WHERE owner_user_id=?", (audience, owner_user_id))
        conn.execute("UPDATE online_replies SET audience=? WHERE owner_user_id=?", (audience, owner_user_id))
        conn.commit()
        conn.close()

def delete_auto_reply(reply_id):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM auto_replies WHERE id=?", (reply_id,))
        conn.execute("DELETE FROM reply_pages WHERE reply_id=?", (reply_id,))
        conn.commit()
        conn.close()

def add_reply_page(reply_id, owner_user_id, title, text_content, color, position=0):
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO reply_pages (reply_id, owner_user_id, title, text_content, color, position) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (reply_id, owner_user_id, title, text_content, color, position),
        )
        conn.commit()
        conn.close()

def get_reply_pages(reply_id):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM reply_pages WHERE reply_id=? ORDER BY position ASC, id ASC", (reply_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def get_reply_page(page_id):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM reply_pages WHERE id=?", (page_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

_INVISIBLE_CHARS_RE = re.compile("[\u200b\u200c\u200d\u200e\u200f\ufeff]")

def normalize_reply_text(text):
    text = _INVISIBLE_CHARS_RE.sub("", text or "")
    return " ".join(text.split()).strip().lower()

def find_matching_reply(owner_user_id, text):
    if not text:
        return None
    text_l = normalize_reply_text(text)
    if not text_l:
        return None
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM auto_replies WHERE owner_user_id=? ORDER BY id ASC", (owner_user_id,)
        ).fetchall()
        conn.close()
    replies = [dict(r) for r in rows]
    for r in replies:
        if normalize_reply_text(r["keyword"]) == text_l:
            return r
    return None

def set_online_reply(owner_user_id, keyword, content_type, text_content, photo_file_id,
                      has_button, button_text, button_color, button_type, button_value):
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO online_replies
            (owner_user_id, keyword, content_type, text_content, photo_file_id,
             has_button, button_text, button_color, button_type, button_value, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
            ON CONFLICT(owner_user_id) DO UPDATE SET
                keyword=excluded.keyword,
                content_type=excluded.content_type,
                text_content=excluded.text_content,
                photo_file_id=excluded.photo_file_id,
                has_button=excluded.has_button,
                button_text=excluded.button_text,
                button_color=excluded.button_color,
                button_type=excluded.button_type,
                button_value=excluded.button_value,
                created_at=excluded.created_at
        """, (owner_user_id, keyword, content_type, text_content, photo_file_id,
              1 if has_button else 0, button_text, button_color, button_type, button_value))
        conn.commit()
        conn.close()

def get_online_reply(owner_user_id):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM online_replies WHERE owner_user_id=?", (owner_user_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

def get_online_reply_by_id(reply_id):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM online_replies WHERE id=?", (reply_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

def delete_online_reply(owner_user_id):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM online_replies WHERE owner_user_id=?", (owner_user_id,))
        conn.commit()
        conn.close()

def add_banned_word(owner_user_id, word):
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO banned_words (owner_user_id, word, created_at) VALUES (?, ?, strftime('%s','now'))",
            (owner_user_id, word),
        )
        conn.commit()
        conn.close()

def get_banned_words(owner_user_id):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM banned_words WHERE owner_user_id=? ORDER BY id DESC", (owner_user_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def get_banned_word(word_id):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM banned_words WHERE id=?", (word_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

def delete_banned_word(word_id):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM banned_words WHERE id=?", (word_id,))
        conn.commit()
        conn.close()

def delete_all_banned_words(owner_user_id):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM banned_words WHERE owner_user_id=?", (owner_user_id,))
        conn.commit()
        conn.close()

def find_banned_word_in_text(owner_user_id, text):
    if not text:
        return None
    text_l = text.lower()
    for w in get_banned_words(owner_user_id):
        if w["word"].strip().lower() in text_l:
            return w
    return None

def set_banned_reply(owner_user_id, text):
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO banned_word_replies (owner_user_id, reply_text) VALUES (?, ?)
            ON CONFLICT(owner_user_id) DO UPDATE SET reply_text=excluded.reply_text
        """, (owner_user_id, text))
        conn.commit()
        conn.close()

def get_banned_reply(owner_user_id):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT reply_text FROM banned_word_replies WHERE owner_user_id=?", (owner_user_id,)
        ).fetchone()
        conn.close()
        return row["reply_text"] if row else None

def clear_banned_reply(owner_user_id):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM banned_word_replies WHERE owner_user_id=?", (owner_user_id,))
        conn.commit()
        conn.close()

def mute_user(owner_user_id, muted_user_id, muted_name="", muted_username=""):
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO muted_users (owner_user_id, muted_user_id, muted_name, muted_username, created_at)
            VALUES (?, ?, ?, ?, strftime('%s','now'))
            ON CONFLICT(owner_user_id, muted_user_id) DO UPDATE SET
                muted_name=excluded.muted_name,
                muted_username=excluded.muted_username
        """, (owner_user_id, muted_user_id, muted_name, muted_username))
        conn.commit()
        conn.close()

def add_force_sub_exempt(owner_user_id, user_id, name="", username=""):
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO force_sub_exempt (owner_user_id, user_id, name, username, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_user_id, user_id) DO UPDATE SET name=excluded.name, username=excluded.username",
            (int(owner_user_id), int(user_id), name or "", username or "", int(time.time())),
        )
        conn.commit()
        conn.close()

def remove_force_sub_exempt(owner_user_id, user_id):
    with _lock:
        conn = _connect()
        conn.execute(
            "DELETE FROM force_sub_exempt WHERE owner_user_id=? AND user_id=?",
            (int(owner_user_id), int(user_id)),
        )
        conn.commit()
        conn.close()

def is_force_sub_exempt(owner_user_id, user_id):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT 1 FROM force_sub_exempt WHERE owner_user_id=? AND user_id=?",
            (int(owner_user_id), int(user_id)),
        ).fetchone()
        conn.close()
        return row is not None

def list_force_sub_exempt(owner_user_id):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM force_sub_exempt WHERE owner_user_id=? ORDER BY id DESC",
            (int(owner_user_id),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def unmute_user(owner_user_id, muted_user_id):
    with _lock:
        conn = _connect()
        conn.execute(
            "DELETE FROM muted_users WHERE owner_user_id=? AND muted_user_id=?",
            (owner_user_id, muted_user_id),
        )
        conn.commit()
        conn.close()

def is_muted(owner_user_id, user_id):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT 1 FROM muted_users WHERE owner_user_id=? AND muted_user_id=?",
            (owner_user_id, user_id),
        ).fetchone()
        conn.close()
        return row is not None

def get_muted_users(owner_user_id):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM muted_users WHERE owner_user_id=? ORDER BY id DESC", (owner_user_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def block_user(owner_user_id, blocked_user_id, blocked_name="", blocked_username=""):
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO blocked_users (owner_user_id, blocked_user_id, blocked_name, blocked_username, created_at)
            VALUES (?, ?, ?, ?, strftime('%s','now'))
            ON CONFLICT(owner_user_id, blocked_user_id) DO UPDATE SET
                blocked_name=excluded.blocked_name,
                blocked_username=excluded.blocked_username
        """, (owner_user_id, blocked_user_id, blocked_name, blocked_username))
        conn.commit()
        conn.close()

def unblock_user(owner_user_id, blocked_user_id):
    with _lock:
        conn = _connect()
        conn.execute(
            "DELETE FROM blocked_users WHERE owner_user_id=? AND blocked_user_id=?",
            (owner_user_id, blocked_user_id),
        )
        conn.commit()
        conn.close()

def unblock_all_users(owner_user_id):
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "DELETE FROM blocked_users WHERE owner_user_id=?", (owner_user_id,)
        )
        conn.commit()
        count = cur.rowcount
        conn.close()
        return count

def is_blocked(owner_user_id, user_id):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT 1 FROM blocked_users WHERE owner_user_id=? AND blocked_user_id=?",
            (owner_user_id, user_id),
        ).fetchone()
        conn.close()
        return row is not None

def get_blocked_users(owner_user_id):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM blocked_users WHERE owner_user_id=? ORDER BY id DESC", (owner_user_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def find_sender_info(owner_user_id, sender_id):
    with _lock:
        conn = _connect()
        row = conn.execute("""
            SELECT m.sender_name, m.sender_username FROM messages m
            JOIN business_connections bc ON bc.business_connection_id = m.business_connection_id
            WHERE bc.owner_user_id=? AND m.sender_id=?
            ORDER BY m.id DESC LIMIT 1
        """, (owner_user_id, sender_id)).fetchone()
        conn.close()
        return dict(row) if row else None

def upsert_client(owner_user_id, chat_id, bc_id, client_user_id, client_name, client_username, last_message_at):
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO business_clients
                (owner_user_id, chat_id, business_connection_id, client_user_id,
                 client_name, client_username, last_message_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_user_id, chat_id) DO UPDATE SET
                business_connection_id=excluded.business_connection_id,
                client_user_id=excluded.client_user_id,
                client_name=excluded.client_name,
                client_username=excluded.client_username,
                last_message_at=excluded.last_message_at
        """, (owner_user_id, chat_id, bc_id, client_user_id, client_name, client_username, last_message_at))
        conn.commit()
        conn.close()

def count_clients(owner_user_id):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM business_clients WHERE owner_user_id=?", (owner_user_id,)
        ).fetchone()
        conn.close()
        return row["c"] if row else 0

def list_clients(owner_user_id):
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT chat_id, business_connection_id FROM business_clients WHERE owner_user_id=?",
            (owner_user_id,),
        ).fetchall()
        conn.close()
        return [(r["chat_id"], r["business_connection_id"]) for r in rows]

def cleanup_old_messages(keep_last=20000):
    with _lock:
        conn = _connect()
        conn.execute("""
            DELETE FROM messages WHERE id NOT IN (
                SELECT id FROM messages ORDER BY id DESC LIMIT ?
            )
        """, (keep_last,))
        conn.commit()
        conn.close()

def upsert_bot_user(user_id, name="", username=""):
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO bot_users (user_id, name, username, first_seen, last_seen)
            VALUES (?, ?, ?, strftime('%s','now'), strftime('%s','now'))
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                username=excluded.username,
                last_seen=excluded.last_seen
        """, (int(user_id), name or "", username or ""))
        conn.commit()
        conn.close()

def bot_user_exists(user_id):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT 1 FROM bot_users WHERE user_id=?", (int(user_id),)).fetchone()
        conn.close()
        return row is not None

PERMANENT_TS = 4102444800  # 2100-01-01

def log_admin_action(actor_id, action, target_id=None, details=""):
    import time as _time
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO admin_log (ts, actor_id, action, target_id, details) VALUES (?,?,?,?,?)",
            (int(_time.time()), int(actor_id), action, int(target_id) if target_id is not None else None, details or ""),
        )
        conn.execute("DELETE FROM admin_log WHERE id NOT IN (SELECT id FROM admin_log ORDER BY id DESC LIMIT 1000)")
        conn.commit()
        conn.close()

def list_admin_log(limit=20):
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT * FROM admin_log ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def get_bot_user_row(user_id):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM bot_users WHERE user_id=?", (int(user_id),)).fetchone()
        conn.close()
        return dict(row) if row else None

def add_premium_permanent(user_id, name="", username=""):
    import time as _time
    now = int(_time.time())
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT expires_at FROM premium_users WHERE user_id=?", (int(user_id),)
        ).fetchone()
        extended = bool(row and int(row["expires_at"] or 0) > now)
        conn.execute("""
            INSERT INTO premium_users (user_id, name, username, granted_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                username=excluded.username,
                expires_at=excluded.expires_at
        """, (int(user_id), name or "", username or "", now, PERMANENT_TS))
        conn.commit()
        conn.close()
        return {"expires_at": PERMANENT_TS, "extended": extended}

def add_supervisor(user_id, name="", username=""):
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO supervisors (user_id, name, username, added_at)
            VALUES (?, ?, ?, strftime('%s','now'))
            ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username
        """, (int(user_id), name or "", username or ""))
        conn.commit()
        conn.close()

def remove_supervisor(user_id):
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM supervisors WHERE user_id=?", (int(user_id),))
        conn.commit()
        n = cur.rowcount
        conn.close()
        return n > 0

def list_supervisors():
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT * FROM supervisors ORDER BY added_at ASC").fetchall()
        conn.close()
        return [dict(r) for r in rows]

def list_supervisor_ids():
    return [int(r["user_id"]) for r in list_supervisors()]

def count_bot_users():
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT COUNT(*) AS c FROM bot_users").fetchone()
        conn.close()
        return row["c"] if row else 0

def ban_bot_user(user_id, name="", username=""):
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO banned_bot_users (user_id, name, username, created_at)
            VALUES (?, ?, ?, strftime('%s','now'))
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                username=excluded.username
        """, (int(user_id), name or "", username or ""))
        conn.commit()
        conn.close()

def unban_bot_user(user_id):
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM banned_bot_users WHERE user_id=?", (int(user_id),))
        conn.commit()
        removed = cur.rowcount
        conn.close()
        return removed > 0

def list_banned_bot_users():
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM banned_bot_users ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def add_premium_user(user_id, name="", username="", days=30):
    import time as _time
    now = int(_time.time())
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT expires_at, granted_at FROM premium_users WHERE user_id=?", (int(user_id),)
        ).fetchone()
        extended = bool(row and int(row["expires_at"] or 0) > now)
        base = int(row["expires_at"]) if extended else now
        granted_at = int(row["granted_at"]) if extended and row["granted_at"] else now
        expires_at = base + int(days) * 86400
        conn.execute("""
            INSERT INTO premium_users (user_id, name, username, granted_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                username=excluded.username,
                granted_at=excluded.granted_at,
                expires_at=excluded.expires_at
        """, (int(user_id), name or "", username or "", granted_at, expires_at))
        conn.commit()
        conn.close()
        return {"expires_at": expires_at, "extended": extended}

def get_premium_user(user_id):
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM premium_users WHERE user_id=?", (int(user_id),)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

def remove_premium_user(user_id):
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM premium_users WHERE user_id=?", (int(user_id),))
        conn.commit()
        removed = cur.rowcount
        conn.close()
        return removed > 0

def count_premium_users():
    import time as _time
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM premium_users WHERE expires_at > ?", (int(_time.time()),)
        ).fetchone()
        conn.close()
        return row["c"] if row else 0

def list_premium_users(limit=None, offset=0):
    import time as _time
    with _lock:
        conn = _connect()
        sql = "SELECT * FROM premium_users WHERE expires_at > ? ORDER BY expires_at ASC"
        params = [int(_time.time())]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [int(limit), int(offset)]
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def pop_expired_premium():
    import time as _time
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM premium_users WHERE expires_at <= ?", (int(_time.time()),)
        ).fetchall()
        expired = [dict(r) for r in rows]
        for r in expired:
            conn.execute("DELETE FROM premium_users WHERE user_id=?", (r["user_id"],))
        conn.commit()
        conn.close()
        return expired

def list_member_ids():
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT user_id FROM (
                SELECT user_id FROM bot_users
                UNION
                SELECT owner_user_id AS user_id FROM business_connections
            )
            WHERE user_id IS NOT NULL
              AND user_id NOT IN (SELECT user_id FROM banned_bot_users)
        """).fetchall()
        conn.close()
        return [r["user_id"] for r in rows]

def list_banned_bot_ids():
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT user_id FROM banned_bot_users").fetchall()
        conn.close()
        return {r["user_id"] for r in rows}

def count_active_accounts():
    with _lock:
        conn = _connect()
        row = conn.execute("""
            SELECT COUNT(DISTINCT owner_user_id) AS c
            FROM business_connections
            WHERE is_enabled=1
              AND owner_user_id NOT IN (SELECT user_id FROM banned_bot_users)
        """).fetchone()
        conn.close()
        return row["c"] if row else 0

def list_active_accounts(limit=8, offset=0):
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT bc.owner_user_id AS user_id,
                   MAX(bc.owner_name) AS name,
                   MAX(bc.owner_username) AS username,
                   MAX(bc.created_at) AS connected_at,
                   (SELECT COUNT(*) FROM business_clients c
                     WHERE c.owner_user_id = bc.owner_user_id) AS clients,
                   (SELECT MAX(c.last_message_at) FROM business_clients c
                     WHERE c.owner_user_id = bc.owner_user_id) AS last_activity
            FROM business_connections bc
            WHERE bc.is_enabled=1
              AND bc.owner_user_id NOT IN (SELECT user_id FROM banned_bot_users)
            GROUP BY bc.owner_user_id
            ORDER BY connected_at DESC
            LIMIT ? OFFSET ?
        """, (int(limit), int(offset))).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def list_active_owner_chat_ids():
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT DISTINCT owner_user_id, owner_chat_id
            FROM business_connections
            WHERE is_enabled=1
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def list_broadcast_recipients():
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT user_id FROM bot_users
            WHERE user_id NOT IN (SELECT user_id FROM banned_bot_users)
        """).fetchall()
        conn.close()
        return [r["user_id"] for r in rows]

def find_known_user(user_id=None, username=None):
    with _lock:
        conn = _connect()
        row = None
        if user_id is not None:
            uid = int(user_id)
            row = conn.execute(
                "SELECT user_id, name, username FROM bot_users WHERE user_id=?", (uid,)
            ).fetchone() or conn.execute(
                "SELECT owner_user_id AS user_id, owner_name AS name, owner_username AS username "
                "FROM business_connections WHERE owner_user_id=? LIMIT 1", (uid,)
            ).fetchone() or conn.execute(
                "SELECT client_user_id AS user_id, client_name AS name, client_username AS username "
                "FROM business_clients WHERE client_user_id=? LIMIT 1", (uid,)
            ).fetchone()
        elif username:
            uname = username.lstrip("@").lower()
            row = conn.execute(
                "SELECT user_id, name, username FROM bot_users WHERE lower(username)=?", (uname,)
            ).fetchone() or conn.execute(
                "SELECT owner_user_id AS user_id, owner_name AS name, owner_username AS username "
                "FROM business_connections WHERE lower(owner_username)=? LIMIT 1", (uname,)
            ).fetchone() or conn.execute(
                "SELECT client_user_id AS user_id, client_name AS name, client_username AS username "
                "FROM business_clients WHERE lower(client_username)=? LIMIT 1", (uname,)
            ).fetchone()
        conn.close()
        return dict(row) if row else None


def add_hosted_bot(owner_id, display_name, filename, dir_path, python_version):
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO hosted_bots "
            "(owner_id, display_name, filename, dir_path, python_version, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'stopped', strftime('%s','now'), strftime('%s','now'))",
            (owner_id, display_name, filename, dir_path, python_version),
        )
        conn.commit()
        new_id = cur.lastrowid
        conn.close()
        return new_id

def get_hosted_bot(bot_id):
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM hosted_bots WHERE id=?", (bot_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

def list_hosted_bots(owner_id=None):
    with _lock:
        conn = _connect()
        if owner_id is None:
            rows = conn.execute("SELECT * FROM hosted_bots ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM hosted_bots WHERE owner_id=? ORDER BY id DESC", (owner_id,)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

def update_hosted_bot(bot_id, **fields):
    if not fields:
        return
    with _lock:
        conn = _connect()
        cols = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [bot_id]
        conn.execute(
            f"UPDATE hosted_bots SET {cols}, updated_at=strftime('%s','now') WHERE id=?",
            values,
        )
        conn.commit()
        conn.close()

def delete_hosted_bot(bot_id):
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM hosted_bots WHERE id=?", (bot_id,))
        conn.commit()
        conn.close()


OWNER_BACKUP_TABLES = [
    "auto_replies", "online_replies", "force_sub_channels",
    "banned_words", "banned_word_replies", "muted_users", "blocked_users", "force_sub_exempt",
]

def _owner_settings_keys(all_rows, owner_id):
    oid = str(int(owner_id))
    pattern = re.compile(rf"(?:^|_){re.escape(oid)}(?:_|$)")
    return {k: v for k, v in all_rows if pattern.search(k)}

def export_owner_backup(owner_id):
    owner_id = int(owner_id)
    with _lock:
        conn = _connect()
        data = {"owner_id": owner_id, "exported_at": int(time.time())}
        for t in OWNER_BACKUP_TABLES:
            rows = conn.execute(f"SELECT * FROM {t} WHERE owner_user_id=?", (owner_id,)).fetchall()
            data[t] = [dict(r) for r in rows]
        all_settings = conn.execute("SELECT key, value FROM settings").fetchall()
        conn.close()
    data["settings"] = _owner_settings_keys(all_settings, owner_id)
    return data

def restore_owner_backup(owner_id, data):
    owner_id = int(owner_id)
    with _lock:
        conn = _connect()
        cur = conn.cursor()
        for t in OWNER_BACKUP_TABLES:
            info = cur.execute(f"PRAGMA table_info({t})").fetchall()
            valid_cols = {r[1] for r in info}
            cur.execute(f"DELETE FROM {t} WHERE owner_user_id=?", (owner_id,))
            for row in data.get(t) or []:
                row = dict(row)
                row["owner_user_id"] = owner_id
                cols = [c for c in row.keys() if c in valid_cols and c != "id"]
                if not cols:
                    continue
                placeholders = ",".join("?" for _ in cols)
                col_names = ",".join(cols)
                cur.execute(
                    f"INSERT INTO {t} ({col_names}) VALUES ({placeholders})",
                    tuple(row[c] for c in cols),
                )
        oid = str(owner_id)
        pattern = re.compile(rf"(?:^|_){re.escape(oid)}(?:_|$)")
        for k, v in (data.get("settings") or {}).items():
            if pattern.search(k):
                cur.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, v),
                )
        conn.commit()
        conn.close()

def export_full_backup():
    with _lock:
        conn = _connect()
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        data = {"exported_at": int(time.time()), "tables": {}}
        for t in tables:
            rows = conn.execute(f"SELECT * FROM {t}").fetchall()
            data["tables"][t] = [dict(r) for r in rows]
        conn.close()
    return data

def restore_full_backup(data):
    tables_data = (data or {}).get("tables") or {}
    with _lock:
        conn = _connect()
        cur = conn.cursor()
        valid_tables = {
            r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        for table, rows in tables_data.items():
            if table not in valid_tables:
                continue
            info = cur.execute(f"PRAGMA table_info({table})").fetchall()
            valid_cols = {r[1] for r in info}
            cur.execute(f"DELETE FROM {table}")
            for row in rows or []:
                cols = [c for c in row.keys() if c in valid_cols]
                if not cols:
                    continue
                placeholders = ",".join("?" for _ in cols)
                col_names = ",".join(cols)
                cur.execute(
                    f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})",
                    tuple(row[c] for c in cols),
                )
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# تنظيف أول بأول + مسح بيانات الأونر بعد فصل البوت
# ---------------------------------------------------------------------------

def list_all_bot_user_ids():
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT user_id FROM bot_users").fetchall()
        conn.close()
        return [int(r["user_id"]) for r in rows]


def cleanup_messages_older_than(days):
    """مسح الرسائل المحفوظة الأقدم من N يوم (مش محتاجينها ومبتفيدش)."""
    cutoff = int(time.time()) - int(float(days) * 86400)
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM messages WHERE date IS NOT NULL AND date < ?", (cutoff,))
        n = cur.rowcount
        conn.commit()
        conn.close()
        return n


def maintenance_compact(full_vacuum=False):
    """تفريغ ملف WAL، وضغط الملف كله مرة باليوم عشان ميتضخمش."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if full_vacuum:
                conn.execute("VACUUM")
        except sqlite3.Error:
            pass
        conn.close()


_PURGE_SKIP_TABLE_WORDS = ("ban", "premium", "vip", "admin", "trial", "bot_users")
_PURGE_KEEP_SETTING_WORDS = ("trial", "ban")


def purge_owner_data(owner_id):
    """مسح كل بيانات أونر فصل البوت (بعد انتهاء مهلة الساعتين). بيحافظ على مفاتيح التجربة والحظر."""
    owner_id = int(owner_id)
    pattern = re.compile(rf"(?:^|_){re.escape(str(owner_id))}(?:_|$)")
    removed = {}
    with _lock:
        conn = _connect()
        cur = conn.cursor()
        bc_ids = [r[0] for r in cur.execute(
            "SELECT business_connection_id FROM business_connections WHERE owner_user_id=?", (owner_id,)
        ).fetchall() if r[0]]
        try:
            bc_ids += [r[0] for r in cur.execute(
                "SELECT DISTINCT business_connection_id FROM business_clients WHERE owner_user_id=?", (owner_id,)
            ).fetchall() if r[0]]
        except sqlite3.Error:
            pass
        bc_ids = list(dict.fromkeys(bc_ids))

        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        for t in tables:
            if any(w in t.lower() for w in _PURGE_SKIP_TABLE_WORDS):
                continue
            cols = {r[1] for r in cur.execute(f"PRAGMA table_info({t})").fetchall()}
            if "owner_user_id" in cols:
                c = cur.execute(f"DELETE FROM {t} WHERE owner_user_id=?", (owner_id,))
                removed[t] = c.rowcount
        for bc in bc_ids:
            cur.execute("DELETE FROM messages WHERE business_connection_id=?", (bc,))
        rows = cur.execute("SELECT key FROM settings").fetchall()
        dead = [
            r[0] for r in rows
            if pattern.search(r[0]) and not any(w in r[0].lower() for w in _PURGE_KEEP_SETTING_WORDS)
        ]
        for bc in bc_ids:
            dead.append(f"bcoff_{bc}")
        for k in dead:
            cur.execute("DELETE FROM settings WHERE key=?", (k,))
        removed["settings"] = len(dead)
        conn.commit()
        conn.close()
    return removed


# ---- تفعيل الكاش + إبطاله عند أي كتابة (لازم يفضل آخر الملف) ----
for _n in ("get_setting", "get_business_connection", "list_force_sub_channels",
           "is_muted", "is_force_sub_exempt"):
    if _n in globals():
        globals()[_n] = _ttl_cache(globals()[_n])

for _n in ("set_setting", "mute_user", "unmute_user", "add_force_sub_channel",
           "remove_force_sub_channel", "clear_force_sub_channels",
           "upsert_business_connection", "set_connection_enabled",
           "add_force_sub_exempt", "remove_force_sub_exempt",
           "restore_full_backup", "restore_owner_backup"):
    if _n in globals():
        globals()[_n] = _invalidate_after(globals()[_n])
