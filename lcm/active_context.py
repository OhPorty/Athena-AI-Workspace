import sqlite3
from store import DB_PATH

def build_active_context(session_id, keep_recent_n=2):
    conn = sqlite3.connect(DB_PATH)

    # Get all summary nodes for this session, in order
    summaries = conn.execute(
        "SELECT id, summary_text, covers_from_id, covers_to_id FROM summary_nodes WHERE session_id = ? ORDER BY covers_to_id",
        (session_id,)
    ).fetchall()

    # Get all messages NOT covered by any summary (i.e. summarized_into IS NULL)
    raw_rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE session_id = ? AND summarized_into IS NULL ORDER BY id",
        (session_id,)
    ).fetchall()

    conn.close()

    context = []
    for node_id, summary_text, from_id, to_id in summaries:
        context.append({"role": "system", "content": f"[Earlier conversation summary, msgs {from_id}-{to_id}]: {summary_text}"})

    # Keep only the last N raw messages fully verbatim
    for row_id, role, content in raw_rows[-keep_recent_n:]:
        context.append({"role": role, "content": content})

    return context

if __name__ == "__main__":
    from store import add_message
    sid = "test-session-1"
    add_message(sid, "user", "Should I bring an umbrella tomorrow?")

    ctx = build_active_context(sid, keep_recent_n=2)
    print("Active context that would be sent to the model:")
    for msg in ctx:
        print(f"  [{msg['role']}] {msg['content']}")
