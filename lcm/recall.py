import sqlite3
from store import DB_PATH

def lcm_grep(session_id, search_term):
    """Search summary node text for a term; return matching nodes."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, summary_text, covers_from_id, covers_to_id FROM summary_nodes WHERE session_id = ? AND summary_text LIKE ?",
        (session_id, f"%{search_term}%")
    ).fetchall()
    conn.close()
    return rows

def lcm_expand(session_id, node_id):
    """Given a summary node id, return the full original messages it covers."""
    conn = sqlite3.connect(DB_PATH)
    node = conn.execute(
        "SELECT covers_from_id, covers_to_id FROM summary_nodes WHERE id = ? AND session_id = ?",
        (node_id, session_id)
    ).fetchone()
    if not node:
        conn.close()
        return None
    from_id, to_id = node
    rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE session_id = ? AND id BETWEEN ? AND ? ORDER BY id",
        (session_id, from_id, to_id)
    ).fetchall()
    conn.close()
    return rows

if __name__ == "__main__":
    sid = "test-session-1"

    print("=== lcm_grep('rain') ===")
    matches = lcm_grep(sid, "rain")
    for node_id, summary_text, from_id, to_id in matches:
        print(f"  Node {node_id} (covers msgs {from_id}-{to_id}): {summary_text}")

    if matches:
        target_node = matches[0][0]
        print()
        print(f"=== lcm_expand(node {target_node}) ===")
        original = lcm_expand(sid, target_node)
        for row_id, role, content in original:
            print(f"  [{row_id}] {role}: {content}")
