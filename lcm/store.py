import sqlite3
import json
import time

DB_PATH = "lcm_test.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            summarized_into INTEGER DEFAULT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS summary_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            covers_from_id INTEGER NOT NULL,
            covers_to_id INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def add_message(session_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (session_id, role, content, time.time())
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id

def get_all_messages(session_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, role, content, summarized_into FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,)
    ).fetchall()
    conn.close()
    return rows

if __name__ == "__main__":
    init_db()
    print("DB initialized at", DB_PATH)
