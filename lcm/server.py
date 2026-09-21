import os
import json
import time
import uuid
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
from lcm_core import LCM

LCM_SUMMARY_MODEL = os.environ.get("LCM_SUMMARY_MODEL", "qwen2.5:0.5b-instruct")
LCM_SUMMARY_BACKEND = os.environ.get("LCM_SUMMARY_BACKEND", "http://localhost:11434/api/chat")

LCM_SUMMARY_TIMEOUT = int(os.environ.get("LCM_SUMMARY_TIMEOUT_SECONDS", "120"))

def default_summarizer(transcript_text: str, max_output_tokens: int = 300, disable_thinking: bool = True) -> str:
    payload = {
        "model": LCM_SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": "Summarize this conversation excerpt concisely, in 1-2 sentences, preserving any specific facts, numbers, or decisions mentioned."},
            {"role": "user", "content": transcript_text},
        ],
        "stream": False,
    }
    if max_output_tokens:
        payload["options"] = {"num_predict": max_output_tokens}
    if disable_thinking:
        # Some reasoning models (e.g. qwen3.5) can spend their entire
        # generation budget deliberating over an ambiguous instruction
        # and never emit real content -- num_predict alone doesn't help
        # since it caps total tokens, not thinking specifically.
        # Disabling thinking for straightforward rewrite/reformatting
        # tasks avoids this failure mode entirely.
        payload["think"] = False
    resp = httpx.post(LCM_SUMMARY_BACKEND, json=payload, timeout=LCM_SUMMARY_TIMEOUT)
    return resp.json()["message"]["content"]

lcm = LCM(db_path=os.environ.get("LCM_DB_PATH", "lcm.db"), summarizer=default_summarizer)

app = FastAPI(title="LCM Service")

class MessageIn(BaseModel):
    session_id: str
    role: str
    content: str
    service: str = "unknown"
    model: Optional[str] = None
    has_image: bool = False
    thinking: Optional[str] = None
    tool_calls: Optional[list] = None

class SummarizeIn(BaseModel):
    session_id: str
    message_ids: List[int]

class SummarizeNodesIn(BaseModel):
    session_id: str
    node_ids: List[int]

class SearchIn(BaseModel):
    session_id: str
    term: str

# Defaults for auto-compaction after each message write. Configurable
# via env vars so callers aren't stuck with one fixed policy.
AUTO_COMPACT_MAX_TOKENS = int(os.environ.get("LCM_AUTO_COMPACT_MAX_TOKENS", "2000"))
AUTO_COMPACT_KEEP_RECENT_N = int(os.environ.get("LCM_AUTO_COMPACT_KEEP_RECENT_N", "8"))
AUTO_COMPACT_FOLD_THRESHOLD = int(os.environ.get("LCM_AUTO_COMPACT_FOLD_THRESHOLD", "5"))

@app.post("/message")
def add_message(msg: MessageIn):
    row_id = lcm.add_message(msg.session_id, msg.role, msg.content, msg.service, msg.model, msg.has_image, msg.thinking, msg.tool_calls)
    # Auto-compact after every write so callers never have to remember
    # to call /compact themselves -- mirrors how a real compactor runs
    # transparently as part of normal request handling.
    compact_result = compact(CompactIn(
        session_id=msg.session_id,
        max_tokens=AUTO_COMPACT_MAX_TOKENS,
        keep_recent_n=AUTO_COMPACT_KEEP_RECENT_N,
        fold_threshold=AUTO_COMPACT_FOLD_THRESHOLD,
    ))
    return {"id": row_id, "auto_compact": compact_result}

@app.get("/messages/{session_id}")
def get_messages(session_id: str):
    conn = lcm._conn()
    rows = conn.execute(
        "SELECT id, role, content, created_at, covered_by_node, model, has_image, thinking, tool_calls_json FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,)
    ).fetchall()
    conn.close()
    return [{
        "id": r[0], "role": r[1], "content": r[2], "created_at": r[3], "covered_by_node": r[4], "model": r[5], "has_image": bool(r[6]),
        "thinking": r[7], "tool_calls": json.loads(r[8]) if r[8] else None,
    } for r in rows]

@app.delete("/session/{session_id}")
def delete_session(session_id: str):
    """Permanently remove every message, summary node, AND metadata
    row for this session -- the actual data-deletion counterpart to a
    UI 'delete chat' action, not just hiding it from a list. Also
    cleans up the sessions metadata table so a deleted chat doesn't
    linger as a stale entry in another device's session list.

    Cascades to every fork descended from this session (fork-of-a-fork
    included) -- deleting a session deletes its whole connected tree,
    not just the one row, since a fork only exists in relation to the
    session it branched from."""
    conn = lcm._conn()

    all_rows = conn.execute("SELECT id, parent_session_id FROM sessions").fetchall()
    children_of = {}
    for sid, parent_id in all_rows:
        if parent_id:
            children_of.setdefault(parent_id, []).append(sid)

    to_delete = [session_id]
    frontier = [session_id]
    while frontier:
        current = frontier.pop()
        for child_id in children_of.get(current, []):
            to_delete.append(child_id)
            frontier.append(child_id)

    placeholders = ",".join("?" * len(to_delete))
    msg_count = conn.execute(f"SELECT COUNT(*) FROM messages WHERE session_id IN ({placeholders})", to_delete).fetchone()[0]
    conn.execute(f"DELETE FROM messages WHERE session_id IN ({placeholders})", to_delete)
    conn.execute(f"DELETE FROM nodes WHERE session_id IN ({placeholders})", to_delete)
    conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", to_delete)
    conn.commit()
    conn.close()
    return {"session_id": session_id, "sessions_deleted": to_delete, "messages_deleted": msg_count}

class SessionMetaIn(BaseModel):
    id: str
    label: str
    pinned: bool = False
    created_at: float
    last_active: Optional[float] = None

@app.get("/sessions")
def list_sessions():
    """All session metadata (label, pinned, created_at) -- the
    server-side source of truth for the chat list, so every logged-in
    device sees the same sessions regardless of which one created
    them."""
    conn = lcm._conn()
    rows = conn.execute("SELECT id, label, pinned, created_at, last_active, parent_session_id FROM sessions").fetchall()
    conn.close()
    return [{"id": r[0], "label": r[1], "pinned": bool(r[2]), "created_at": r[3], "last_active": r[4], "parent_session_id": r[5]} for r in rows]

@app.post("/sessions")
def upsert_session(req: SessionMetaIn):
    """Create or update one session's metadata -- called on first
    message (creation), rename, and pin/unpin."""
    conn = lcm._conn()
    conn.execute(
        """INSERT INTO sessions (id, label, pinned, created_at, last_active) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET label = excluded.label, pinned = excluded.pinned, last_active = excluded.last_active""",
        (req.id, req.label, 1 if req.pinned else 0, req.created_at, req.last_active)
    )
    conn.commit()
    conn.close()
    return {"id": req.id}


class ForkSessionIn(BaseModel):
    message_id: int
    label: str


@app.post("/sessions/{session_id}/fork")
def fork_session(session_id: str, req: ForkSessionIn):
    """Create a new, independent session containing a copy of every
    message in session_id up to and including message_id, plus any
    summary nodes that are entirely eligible (see below) -- letting
    the caller rewind to a specific point in a conversation and
    continue down a different path from there, without touching or
    losing the original session.

    A summary node is only copied if EVERY message and sub-node it
    covers falls at-or-before the fork point -- a node straddling the
    cutoff (summarizing messages both before and after it) can't be
    copied as-is into a session that doesn't contain its later half.
    Any message whose original covering node is excluded for this
    reason is copied as a plain, uncovered message instead: it becomes
    visible in the forked session rather than stying folded into a
    summary, which is correct (not a bug) -- the fork may show
    slightly more raw detail early on than the original session did
    at that exact moment.
    """
    conn = lcm._conn()
    cutoff = req.message_id

    messages = conn.execute(
        "SELECT id, role, content, created_at, covered_by_node, service, model, has_image "
        "FROM messages WHERE session_id = ? AND id <= ? ORDER BY id",
        (session_id, cutoff)
    ).fetchall()
    if not messages:
        conn.close()
        return {"error": "No messages found at or before that point."}

    all_nodes = conn.execute(
        "SELECT id, summary_text, level, covers_message_ids, covers_node_ids, covered_by_node, created_at "
        "FROM nodes WHERE session_id = ?",
        (session_id,)
    ).fetchall()
    nodes_by_id = {n[0]: n for n in all_nodes}

    eligibility_cache = {}

    def node_eligible(node_id):
        if node_id in eligibility_cache:
            return eligibility_cache[node_id]
        node = nodes_by_id.get(node_id)
        if node is None:
            eligibility_cache[node_id] = False
            return False
        _, _, _, covers_msg_json, covers_node_json, _, _ = node
        covers_msg_ids = json.loads(covers_msg_json) if covers_msg_json else []
        covers_node_ids = json.loads(covers_node_json) if covers_node_json else []
        if any(mid > cutoff for mid in covers_msg_ids):
            eligibility_cache[node_id] = False
            return False
        if not all(node_eligible(nid) for nid in covers_node_ids):
            eligibility_cache[node_id] = False
            return False
        eligibility_cache[node_id] = True
        return True

    eligible_node_ids = sorted(nid for nid in nodes_by_id if node_eligible(nid))

    new_session_id = str(uuid.uuid4())
    now = time.time() * 1000
    conn.execute(
        "INSERT INTO sessions (id, label, pinned, created_at, last_active, parent_session_id) VALUES (?, ?, 0, ?, ?, ?)",
        (new_session_id, req.label, now, now, session_id)
    )

    msg_id_map = {}
    for (old_id, role, content, created_at, covered_by_node, service, model, has_image) in messages:
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at, covered_by_node, service, model, has_image) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_session_id, role, content, created_at, None, service, model, has_image)
        )
        msg_id_map[old_id] = cur.lastrowid

    node_id_map = {}
    for old_id in eligible_node_ids:
        _, summary_text, level, covers_msg_json, covers_node_json, _, created_at = nodes_by_id[old_id]
        covers_msg_ids = json.loads(covers_msg_json) if covers_msg_json else []
        covers_node_ids = json.loads(covers_node_json) if covers_node_json else []
        new_covers_msg_ids = [msg_id_map[m] for m in covers_msg_ids if m in msg_id_map]
        new_covers_node_ids = [node_id_map[n] for n in covers_node_ids if n in node_id_map]
        cur = conn.execute(
            "INSERT INTO nodes (session_id, summary_text, level, covers_message_ids, covers_node_ids, covered_by_node, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_session_id, summary_text, level, json.dumps(new_covers_msg_ids),
             json.dumps(new_covers_node_ids) if new_covers_node_ids else None, None, created_at)
        )
        node_id_map[old_id] = cur.lastrowid

    for old_id, new_id in msg_id_map.items():
        orig_covered_by = next(m[4] for m in messages if m[0] == old_id)
        if orig_covered_by is not None and orig_covered_by in node_id_map:
            conn.execute("UPDATE messages SET covered_by_node = ? WHERE id = ?", (node_id_map[orig_covered_by], new_id))

    for old_id, new_id in node_id_map.items():
        orig_covered_by = nodes_by_id[old_id][5]
        if orig_covered_by is not None and orig_covered_by in node_id_map:
            conn.execute("UPDATE nodes SET covered_by_node = ? WHERE id = ?", (node_id_map[orig_covered_by], new_id))

    conn.commit()
    conn.close()
    return {"session_id": new_session_id, "messages_copied": len(msg_id_map), "nodes_copied": len(node_id_map)}


@app.post("/summarize")
def summarize(req: SummarizeIn):
    conn = lcm._conn()
    placeholders = ",".join("?" * len(req.message_ids))
    rows = conn.execute(
        f"SELECT role, content FROM messages WHERE session_id = ? AND id IN ({placeholders}) ORDER BY id",
        (req.session_id, *req.message_ids)
    ).fetchall()
    transcript = "\n".join(f"{role}: {content}" for role, content in rows)
    summary_text = lcm.summarizer(transcript)

    cur = conn.execute(
        "INSERT INTO nodes (session_id, summary_text, level, covers_message_ids, created_at) VALUES (?, ?, 1, ?, ?)",
        (req.session_id, summary_text, json.dumps(req.message_ids), time.time())
    )
    node_id = cur.lastrowid
    placeholders2 = ",".join("?" * len(req.message_ids))
    conn.execute(
        f"UPDATE messages SET covered_by_node = ? WHERE session_id = ? AND id IN ({placeholders2})",
        (node_id, req.session_id, *req.message_ids)
    )
    conn.commit()
    conn.close()
    return {"node_id": node_id, "summary": summary_text}

@app.post("/summarize/nodes")
def summarize_nodes(req: SummarizeNodesIn):
    """Recursive step: summarize a set of existing nodes into one
    higher-level node. This is what keeps long sessions bounded --
    instead of an ever-growing flat list of summary nodes, nodes
    themselves get folded into higher-level nodes, forming a DAG."""
    conn = lcm._conn()
    placeholders = ",".join("?" * len(req.node_ids))
    rows = conn.execute(
        f"SELECT id, summary_text, level FROM nodes WHERE session_id = ? AND id IN ({placeholders}) ORDER BY id",
        (req.session_id, *req.node_ids)
    ).fetchall()

    if not rows:
        conn.close()
        return {"error": "no matching nodes found"}

    transcript = "\n".join(f"[Summary {nid}]: {text}" for nid, text, _ in rows)
    summary_text = lcm.summarizer(transcript)
    max_level = max(level for _, _, level in rows)

    cur = conn.execute(
        "INSERT INTO nodes (session_id, summary_text, level, covers_node_ids, created_at) VALUES (?, ?, ?, ?, ?)",
        (req.session_id, summary_text, max_level + 1, json.dumps(req.node_ids), time.time())
    )
    new_node_id = cur.lastrowid

    placeholders2 = ",".join("?" * len(req.node_ids))
    conn.execute(
        f"UPDATE nodes SET covered_by_node = ? WHERE session_id = ? AND id IN ({placeholders2})",
        (new_node_id, req.session_id, *req.node_ids)
    )
    conn.commit()
    conn.close()
    return {"node_id": new_node_id, "level": max_level + 1, "summary": summary_text}

@app.get("/context/{session_id}")
def get_active_context(session_id: str, keep_recent_n: int = 4):
    # Force compaction using this endpoint's own keep_recent_n before
    # reading anything, so messages beyond the visible tail are always
    # either summarized or nonexistent -- never silently dropped just
    # because a different endpoint's threshold hadn't fired yet.
    compact(CompactIn(session_id=session_id, keep_recent_n=keep_recent_n, max_tokens=10**9))

    conn = lcm._conn()
    nodes = conn.execute(
        "SELECT id, summary_text FROM nodes WHERE session_id = ? AND covered_by_node IS NULL ORDER BY id",
        (session_id,)
    ).fetchall()
    raw = conn.execute(
        "SELECT id, role, content FROM messages WHERE session_id = ? AND covered_by_node IS NULL ORDER BY id",
        (session_id,)
    ).fetchall()
    conn.close()

    context = [{"role": "system", "content": f"[Summary, node {nid}]: {text}"} for nid, text in nodes]
    for _, role, content in raw[-keep_recent_n:]:
        context.append({"role": role, "content": content})
    return {"context": context}

@app.post("/recall/search")
def recall_search(req: SearchIn):
    """Searches BOTH compacted summary nodes AND raw original messages
    for the term. Searching nodes alone missed anything not yet
    compacted, and -- since summaries are LLM-generated paraphrases --
    also missed exact-wording/quote searches even for compacted history,
    which defeats the point of a 'lossless' memory system for search
    purposes even though storage itself was never lossy."""
    conn = lcm._conn()
    node_rows = conn.execute(
        "SELECT id, summary_text, level, covers_message_ids, covers_node_ids FROM nodes WHERE session_id = ? AND summary_text LIKE ?",
        (req.session_id, f"%{req.term}%")
    ).fetchall()
    message_rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE session_id = ? AND content LIKE ? ORDER BY id",
        (req.session_id, f"%{req.term}%")
    ).fetchall()
    conn.close()
    return {
        "exact_message_matches": [
            {"message_id": r[0], "role": r[1], "content": r[2]}
            for r in message_rows
        ],
        "summary_matches": [
            {
                "node_id": r[0], "summary": r[1], "level": r[2],
                "message_ids": json.loads(r[3]) if r[3] else [],
                "node_ids": json.loads(r[4]) if r[4] else [],
            }
            for r in node_rows
        ],
    }

def _expand_recursive(conn, session_id, node_id):
    """Walk down the DAG: if this node covers raw messages, return them.
    If it covers other nodes, recurse into each of those until we reach
    raw messages. Always returns the full original text, no matter how
    many levels of summarization sit on top of it."""
    node = conn.execute(
        "SELECT covers_message_ids, covers_node_ids FROM nodes WHERE id = ? AND session_id = ?",
        (node_id, session_id)
    ).fetchone()
    if not node:
        return []

    covers_message_ids, covers_node_ids = node
    results = []

    if covers_message_ids:
        message_ids = json.loads(covers_message_ids)
        placeholders = ",".join("?" * len(message_ids))
        rows = conn.execute(
            f"SELECT id, role, content FROM messages WHERE id IN ({placeholders}) ORDER BY id",
            message_ids
        ).fetchall()
        results.extend({"id": r[0], "role": r[1], "content": r[2]} for r in rows)

    if covers_node_ids:
        for child_id in json.loads(covers_node_ids):
            results.extend(_expand_recursive(conn, session_id, child_id))

    return results

@app.get("/recall/expand/{node_id}")
def recall_expand(node_id: int, session_id: str):
    conn = lcm._conn()
    results = _expand_recursive(conn, session_id, node_id)
    conn.close()
    if not results:
        return {"error": "node not found or covers nothing"}
    return results

def recall_range(session_id: str, offset: int = 0, limit: int = 20, order: str = "asc", role: str = None):
    """General-purpose positional access to a session's raw messages --
    a slice by offset/limit/direction, with total count included so the
    model can reason about where it is in the conversation. This is a
    single primitive that covers 'first message' (offset=0, limit=1,
    order=asc), 'most recent message' (offset=0, limit=1, order=desc),
    an arbitrary middle slice ('messages 10 through 20': offset=10,
    limit=10), and paging through an entire long conversation -- none
    of which lcm_recall_search (a keyword/LIKE lookup with no concept
    of position) can ever answer correctly, however it's phrased."""
    conn = lcm._conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    query = "SELECT id, role, content FROM messages WHERE session_id = ?"
    params = [session_id]
    if role:
        query += " AND role = ?"
        params.append(role)
    query += f" ORDER BY id {'ASC' if order == 'asc' else 'DESC'} LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    conn.close()
    rows = list(rows)
    if order == "desc":
        rows.reverse()  # always hand back chronological order, regardless of which end was fetched from
    return {
        "total_messages_in_session": total,
        "offset": offset,
        "returned_count": len(rows),
        "messages": [{"message_id": r[0], "role": r[1], "content": r[2]} for r in rows],
    }

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token, a standard approximation
    that doesn't require a real tokenizer dependency."""
    return max(1, len(text) // 4)

def _context_token_count(conn, session_id: str, keep_recent_n: int) -> int:
    nodes = conn.execute(
        "SELECT summary_text FROM nodes WHERE session_id = ? AND covered_by_node IS NULL",
        (session_id,)
    ).fetchall()
    raw = conn.execute(
        "SELECT content FROM messages WHERE session_id = ? AND covered_by_node IS NULL ORDER BY id",
        (session_id,)
    ).fetchall()
    total = sum(estimate_tokens(t[0]) for t in nodes)
    total += sum(estimate_tokens(r[0]) for r in raw[-keep_recent_n:])
    return total

class CompactIn(BaseModel):
    session_id: str
    max_tokens: int = 500
    keep_recent_n: int = 4
    fold_threshold: int = 5  # fold top-level nodes once there are this many

def _has_unsummarized_backlog(conn, session_id: str, keep_recent_n: int) -> bool:
    """True if there are raw messages older than the protected recent
    tail that have NOT been folded into a summary node yet. This check
    is independent of token budget on purpose: without it, a small
    token count on the visible tail can mask a real backlog of older
    messages that are silently excluded from active context without
    ever being summarized -- lossy in effect, even though the rows
    still exist untouched in the Immutable Store."""
    raw = conn.execute(
        "SELECT id FROM messages WHERE session_id = ? AND covered_by_node IS NULL ORDER BY id",
        (session_id,)
    ).fetchall()
    return len(raw) > keep_recent_n

@app.post("/compact")
def compact(req: CompactIn):
    """Auto-trigger: summarize oldest raw messages and/or fold oldest
    top-level nodes until active context fits max_tokens AND no
    unsummarized backlog remains, or there's nothing left to compact."""
    actions = []
    conn = lcm._conn()

    while (
        _context_token_count(conn, req.session_id, req.keep_recent_n) > req.max_tokens
        or _has_unsummarized_backlog(conn, req.session_id, req.keep_recent_n)
    ):
        # Step 1: any raw messages beyond the protected recent tail?
        raw = conn.execute(
            "SELECT id FROM messages WHERE session_id = ? AND covered_by_node IS NULL ORDER BY id",
            (req.session_id,)
        ).fetchall()
        excess_raw = raw[:-req.keep_recent_n] if len(raw) > req.keep_recent_n else []

        if excess_raw:
            ids_to_summarize = [r[0] for r in excess_raw]
            conn.close()
            result = summarize(SummarizeIn(session_id=req.session_id, message_ids=ids_to_summarize))
            actions.append({"action": "summarize_messages", "message_ids": ids_to_summarize, "result": result})
            conn = lcm._conn()
            continue

        # Step 2: too many top-level nodes cluttering context? Fold oldest ones.
        top_nodes = conn.execute(
            "SELECT id FROM nodes WHERE session_id = ? AND covered_by_node IS NULL ORDER BY id",
            (req.session_id,)
        ).fetchall()

        if len(top_nodes) >= req.fold_threshold:
            # Fold the oldest half of them together
            fold_count = max(2, len(top_nodes) // 2)
            ids_to_fold = [n[0] for n in top_nodes[:fold_count]]
            conn.close()
            result = summarize_nodes(SummarizeNodesIn(session_id=req.session_id, node_ids=ids_to_fold))
            actions.append({"action": "fold_nodes", "node_ids": ids_to_fold, "result": result})
            conn = lcm._conn()
            continue

        # Nothing left we can safely compact further (would eat into the
        # protected recent tail, or there's only one top-level node left).
        break

    conn.close()
    return {"actions_taken": actions, "actions_count": len(actions)}

# --- Cross-service privacy boundary ---
# LCM may draw on history from multiple services (Odysseus, EnigmaBot, a
# future custom workspace, etc.) to make responses feel naturally
# well-informed, but this must never surface as an explicit recall --
# no "you said X", no "at that time", no naming which service something
# came from. The goal is ambient familiarity, not quotable memory: the
# same way a person who knows you well speaks with that knowledge baked
# in, never citing when or where they learned it.
#
# Three independent layers enforce this, since prompting alone is not
# reliable enough for a hard requirement:
#   1. Cross-service facts are reformatted as standing background facts
#      (a profile note), never as transcript/event summaries -- this
#      removes the temporal/quote framing at the source, before it can
#      ever be phrased as a recall.
#   2. The reformatting prompt explicitly forbids naming a service.
#   3. A deterministic pattern filter scans final text for recall-style
#      phrasing ("you said", "previously", "at that time", "you told
#      me") and known service names, redacting either regardless of why
#      they appeared.

KNOWN_SERVICE_NAMES = [
    n.strip() for n in os.environ.get(
        "LCM_KNOWN_SERVICE_NAMES", "Odysseus,EnigmaBot,Hermes,Pi,OpenCode"
    ).split(",") if n.strip()
]

# Phrases that imply "I am recalling a specific prior exchange" -- these
# are the actual leak pattern, independent of whether a service is named.
_RECALL_PATTERN_RE = None

def _get_recall_pattern():
    global _RECALL_PATTERN_RE
    if _RECALL_PATTERN_RE is None:
        import re
        phrases = [
            r"you (once |previously |earlier )?(said|mentioned|told me|asked)",
            r"at (that|this) time",
            r"(earlier|previously|before)[, ]+you",
            r"as you (said|mentioned|told me)",
            r"in (our|a) (previous|earlier|prior) (conversation|chat|session)",
            r"last time (we|you)",
            r"you told (me|us)",
        ]
        _RECALL_PATTERN_RE = re.compile("|".join(phrases), re.IGNORECASE)
    return _RECALL_PATTERN_RE

def _scrub_service_names(text: str) -> str:
    import re
    scrubbed = text
    for name in KNOWN_SERVICE_NAMES:
        if name.lower() in scrubbed.lower():
            scrubbed = re.sub(re.escape(name), "[REDACTED]", scrubbed, flags=re.IGNORECASE)
    return scrubbed

def _scrub_recall_language(text: str) -> str:
    """Hard safety net: if recall-style phrasing slips through despite
    the ambient-facts reformatting, flag it rather than silently letting
    it pass -- callers should treat a flagged result as unsafe to show."""
    match = _get_recall_pattern().search(text)
    if match:
        return None  # signal: unsafe, caller must not use this text as-is
    return text

_ORIGINAL_SUMMARIZER = lcm.summarizer

def _cross_service_safe_summarizer(transcript_text: str) -> str:
    raw_summary = _ORIGINAL_SUMMARIZER(transcript_text)
    return _scrub_service_names(raw_summary)

lcm.summarizer = _cross_service_safe_summarizer

def _to_ambient_facts(summary_text: str) -> str:
    """Reformat an event/transcript-style summary into standing
    background facts with no temporal or quote framing -- the actual
    fix for the leak pattern, not just a filter after the fact."""
    prompt = (
        "Rewrite the following as 1-2 standing background facts about a "
        "person, in the style of a profile note. Do NOT phrase anything "
        "as something that was said, asked, or discussed -- no 'they "
        "said', no 'at some point', no time references at all. Do NOT "
        "name specific product or company names (like Twitch, YouTube, Discord). Generic status words like 'partner' or 'subscriber' are fine to keep. State only durable "
        "facts, as if you simply already know them.\n\n"
        f"Source: {summary_text}"
    )
    try:
        ambient = _ORIGINAL_SUMMARIZER(prompt, max_output_tokens=300, disable_thinking=True)
    except Exception as e:
        # A slow/unreachable summarizer should drop this one fact, not
        # crash the whole cross-service context request.
        print(f"[lcm] ambient-facts reformatting failed, dropping fact: {e}")
        return None
    ambient = _scrub_service_names(ambient)
    safe = _scrub_recall_language(ambient)
    print(f"[lcm-debug] ambient text before recall-check: {ambient!r}")
    return safe  # None if still unsafe after reformatting -- caller must check

class CrossServiceContextIn(BaseModel):
    session_ids: List[str]
    keep_recent_n: int = 4

@app.post("/context/cross-service")
def get_cross_service_context(req: CrossServiceContextIn):
    """Assemble ambient background context spanning multiple
    sessions/services for the same person. Facts are reformatted to
    remove any temporal/quote/service framing before being returned;
    any fact that can't be made safe is dropped rather than risking a
    leak. Only top-level summary nodes are used as source material for
    this -- raw messages from OTHER sessions are never injected
    directly, since raw text is exactly the kind of quotable, session-
    specific content this boundary exists to prevent."""
    conn = lcm._conn()
    ambient_facts = []
    dropped_count = 0
    for sid in req.session_ids:
        nodes = conn.execute(
            "SELECT summary_text FROM nodes WHERE session_id = ? AND covered_by_node IS NULL ORDER BY id",
            (sid,)
        ).fetchall()
        for (text,) in nodes:
            safe_fact = _to_ambient_facts(text)
            if safe_fact:
                ambient_facts.append(safe_fact)
            else:
                dropped_count += 1
    conn.close()

    context = [{"role": "system", "content": f"[Background]: {fact}"} for fact in ambient_facts]
    return {"context": context, "facts_included": len(ambient_facts), "facts_dropped_unsafe": dropped_count}

# --- Tool interface: lets a model call recall itself ---
# Rather than every consuming service hardcoding LCM's REST routes, LCM
# exposes its own tool schema (OpenAI/native-function-calling shape) plus
# a single generic dispatch endpoint. A consuming service just needs to:
#   1. GET /tools/schema and hand the definitions to its own model
#   2. When the model calls one of these tools, POST the {name, arguments}
#      pair to /tools/call and feed the result back to the model
# This keeps LCM usable by any service without them needing to know its
# internal routes -- only the tool contract.

LCM_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "lcm_recall_search",
            "description": (
                "Search THIS CONVERSATION's own past messages -- NOT the "
                "internet, NOT web search. Use this whenever the user "
                "refers to something they told you earlier in this chat "
                "(a fact, a preference, a name, a decision, an exact "
                "quote) that isn't visible in your current context "
                "because it was compacted into a summary, or simply "
                "scrolled out of view. This is always the right first "
                "choice for 'what did I say/mention/tell you about X' "
                "questions -- never use web_search for those. Returns "
                "two kinds of results: exact_message_matches are the "
                "verbatim original wording, safe to quote directly; "
                "summary_matches are paraphrased and only tell you a "
                "match exists nearby -- call lcm_recall_expand on a "
                "node_id from those to get the exact original wording."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "The current session id."},
                    "term": {"type": "string", "description": "Keyword or phrase to search for."},
                },
                "required": ["session_id", "term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lcm_recall_expand",
            "description": (
                "Given a node_id from lcm_recall_search, return the exact "
                "original messages that summary was built from -- word "
                "for word, not paraphrased. Use this when a summary is "
                "too vague and you need the precise original wording, "
                "numbers, or details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "The current session id."},
                    "node_id": {"type": "integer", "description": "The node_id returned by lcm_recall_search."},
                },
                "required": ["session_id", "node_id"],
            },
        },
    },
]

LCM_TOOL_SCHEMA.append({
    "type": "function",
    "function": {
        "name": "lcm_recall_range",
        "description": (
            "Get messages of THIS CONVERSATION by POSITION rather than "
            "content -- use this for 'what was my first message', "
            "'what did I just say', 'show me messages 10 through 20', "
            "'how many messages have we exchanged', or anything about "
            "WHERE something falls in the conversation. Do NOT use "
            "lcm_recall_search for these -- that tool searches for a "
            "keyword/phrase and has no concept of position, so it will "
            "search for words like 'first' literally instead of "
            "answering the actual question. Returns total_messages_in_"
            "session so you know the conversation's full length, plus "
            "the requested slice."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The current session id."},
                "offset": {"type": "integer", "description": "How many messages to skip from the chosen end before returning results. Defaults to 0."},
                "limit": {"type": "integer", "description": "Maximum number of messages to return. Defaults to 20."},
                "order": {"type": "string", "enum": ["asc", "desc"], "description": "'asc' counts from the start of the conversation (offset=0 is the first message ever). 'desc' counts from the end (offset=0 is the most recent message). Results are always returned in chronological order either way."},
                "role": {"type": "string", "enum": ["user", "assistant"], "description": "Optional -- restrict to only this role's messages."},
            },
            "required": ["session_id"],
        },
    },
})

@app.get("/tools/schema")
def get_tools_schema():
    return {"tools": LCM_TOOL_SCHEMA}

class ToolCallIn(BaseModel):
    name: str
    arguments: dict

@app.post("/tools/call")
def call_tool(req: ToolCallIn):
    if req.name == "lcm_recall_search":
        result = recall_search(SearchIn(
            session_id=req.arguments.get("session_id", ""),
            term=req.arguments.get("term", ""),
        ))
        return {"result": result}
    elif req.name == "lcm_recall_expand":
        result = recall_expand(
            node_id=req.arguments.get("node_id"),
            session_id=req.arguments.get("session_id", ""),
        )
        return {"result": result}
    elif req.name == "lcm_recall_range":
        result = recall_range(
            session_id=req.arguments.get("session_id", ""),
            offset=req.arguments.get("offset", 0),
            limit=req.arguments.get("limit", 20),
            order=req.arguments.get("order", "asc"),
            role=req.arguments.get("role"),
        )
        return {"result": result}
    else:
        return {"error": f"Unknown tool: {req.name}"}

class FactIn(BaseModel):
    content: str
    source_session_id: Optional[str] = None
    source_message_id: Optional[int] = None
    manual: bool = False

class ScanStateIn(BaseModel):
    last_scanned_message_id: int

@app.get("/facts")
def list_facts():
    """All durable, cross-session facts -- what Athena has learned
    about the user regardless of which session it came from."""
    conn = lcm._conn()
    rows = conn.execute(
        "SELECT id, content, source_session_id, source_message_id, manual, created_at FROM facts ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0], "content": r[1], "source_session_id": r[2],
            "source_message_id": r[3], "manual": bool(r[4]), "created_at": r[5],
        }
        for r in rows
    ]

@app.post("/facts")
def add_fact(fact: FactIn):
    conn = lcm._conn()
    cur = conn.execute(
        "INSERT INTO facts (content, source_session_id, source_message_id, manual, created_at) VALUES (?, ?, ?, ?, ?)",
        (fact.content, fact.source_session_id, fact.source_message_id, 1 if fact.manual else 0, time.time())
    )
    conn.commit()
    fact_id = cur.lastrowid
    conn.close()
    return {"id": fact_id}

@app.delete("/facts/{fact_id}")
def delete_fact(fact_id: int):
    conn = lcm._conn()
    conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
    conn.commit()
    conn.close()
    return {"deleted": fact_id}

@app.get("/messages/since/{last_id}")
def messages_since(last_id: int):
    """Every message across ALL sessions newer than last_id, for the
    memory scanner -- deliberately not scoped to one session_id, since
    the scan runs periodically over everything that happened anywhere
    since it last ran, not any single conversation."""
    conn = lcm._conn()
    rows = conn.execute(
        "SELECT id, session_id, role, content FROM messages WHERE id > ? ORDER BY id",
        (last_id,)
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "session_id": r[1], "role": r[2], "content": r[3]}
        for r in rows
    ]

@app.delete("/messages/{message_id}")
def delete_message_pair(message_id: int):
    """Deletes a message AND its conversational partner -- the
    assistant reply immediately after a deleted user message, or the
    user turn immediately before a deleted assistant reply -- so chat
    history never ends up with an orphaned half of an exchange. Relies
    on messages alternating user/assistant per session, which holds
    because intermediate tool-call steps are never persisted to LCM,
    only final turns are."""
    conn = lcm._conn()
    row = conn.execute("SELECT session_id, role FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "message not found"}
    session_id, role = row
    deleted_ids = [message_id]
    if role == "user":
        pair = conn.execute(
            "SELECT id FROM messages WHERE session_id = ? AND role = 'assistant' AND id > ? ORDER BY id ASC LIMIT 1",
            (session_id, message_id)
        ).fetchone()
    else:
        pair = conn.execute(
            "SELECT id FROM messages WHERE session_id = ? AND role = 'user' AND id < ? ORDER BY id DESC LIMIT 1",
            (session_id, message_id)
        ).fetchone()
    if pair:
        deleted_ids.append(pair[0])
    placeholders = ",".join("?" * len(deleted_ids))
    conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", deleted_ids)
    conn.commit()
    conn.close()
    return {"deleted_ids": deleted_ids}

@app.get("/scan-state")
def get_scan_state():
    conn = lcm._conn()
    row = conn.execute("SELECT last_scanned_message_id FROM scan_state WHERE id = 1").fetchone()
    conn.close()
    return {"last_scanned_message_id": row[0] if row else 0}

@app.post("/scan-state")
def set_scan_state(state: ScanStateIn):
    conn = lcm._conn()
    conn.execute("UPDATE scan_state SET last_scanned_message_id = ? WHERE id = 1", (state.last_scanned_message_id,))
    conn.commit()
    conn.close()
    return {"last_scanned_message_id": state.last_scanned_message_id}

@app.get("/health")
def health():
    return {"status": "ok", "db_path": lcm.db_path}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("LCM_PORT", "8420"))
    uvicorn.run(app, host="127.0.0.1", port=port)
