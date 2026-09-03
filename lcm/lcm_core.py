import sqlite3
import time

class LCM:
    def __init__(self, db_path="lcm.db", summarizer=None, keep_recent_n=4):
        self.db_path = db_path
        self.summarizer = summarizer
        self.keep_recent_n = keep_recent_n
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        conn = self._conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                covered_by_node INTEGER DEFAULT NULL,
                service TEXT DEFAULT 'unknown',
                model TEXT DEFAULT NULL,
                has_image INTEGER DEFAULT 0
            )
        """)
        # Migration for databases created before per-message model
        # attribution existed -- CREATE TABLE IF NOT EXISTS above only
        # covers a fresh install, not an upgrade of an existing one.
        existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()]
        if "model" not in existing_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN model TEXT DEFAULT NULL")
        if "has_image" not in existing_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN has_image INTEGER DEFAULT 0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                summary_text TEXT NOT NULL,
                level INTEGER NOT NULL,
                covers_message_ids TEXT DEFAULT NULL,
                covers_node_ids TEXT DEFAULT NULL,
                covered_by_node INTEGER DEFAULT NULL,
                created_at REAL NOT NULL
            )
        """)
        # Durable, cross-session memory -- distinct from nodes, which are
        # per-session compaction summaries. A fact here is meant to be
        # true regardless of which session it was learned in, and gets
        # surfaced in EVERY session's context, not just the one it came
        # from.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                source_session_id TEXT DEFAULT NULL,
                source_message_id INTEGER DEFAULT NULL,
                manual INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        # Tracks how far the automatic memory scanner has gotten, as a
        # global message-id watermark (messages.id is a single
        # auto-increment sequence across ALL sessions, so this one
        # number is enough to mean 'everything up to here has already
        # been considered for extraction, regardless of which session
        # it was in').
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_scanned_message_id INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO scan_state (id, last_scanned_message_id) VALUES (1, 0)
        """)
        # Session metadata (label, pinned) -- distinct from the messages
        # table's per-turn content. Without this, a session's human-
        # readable identity only ever existed in whichever browser's
        # localStorage created it, invisible to every other device even
        # though the actual message content already lived centrally here.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def add_message(self, session_id, role, content, service="unknown", model=None, has_image=False):
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at, service, model, has_image) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, time.time(), service, model, 1 if has_image else 0)
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id
