import sqlite3
import time
import httpx
from store import DB_PATH

def summarize_range(session_id, from_id, to_id, model="qwen3.5:4b-q5-xl"):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? AND id BETWEEN ? AND ? ORDER BY id",
        (session_id, from_id, to_id)
    ).fetchall()

    transcript = "\n".join(f"{role}: {content}" for role, content in rows)

    resp = httpx.post("http://localhost:11434/api/chat", json={
        "model": model,
        "messages": [
            {"role": "system", "content": "Summarize this conversation excerpt concisely, in 1-2 sentences, preserving any specific facts, numbers, or decisions mentioned."},
            {"role": "user", "content": transcript},
        ],
        "stream": False,
    }, timeout=60)

    summary_text = resp.json()["message"]["content"]

    cur = conn.execute(
        "INSERT INTO summary_nodes (session_id, summary_text, covers_from_id, covers_to_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, summary_text, from_id, to_id, time.time())
    )
    node_id = cur.lastrowid

    conn.execute(
        "UPDATE messages SET summarized_into = ? WHERE session_id = ? AND id BETWEEN ? AND ?",
        (node_id, session_id, from_id, to_id)
    )
    conn.commit()
    conn.close()
    return node_id, summary_text

if __name__ == "__main__":
    node_id, summary = summarize_range("test-session-1", 1, 4)
    print(f"Created summary node {node_id}:")
    print(summary)
