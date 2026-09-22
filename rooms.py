import re
import json
import time
import queue
import hashlib
import threading
from typing import Optional, List

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

import settings
import activity
import task_context
import generation_streaming
import host_locks
from host_locks import OLLAMA_URL
import bots

router = APIRouter()


class RoomFindOrCreateIn(BaseModel):
    kind: str  # 'dm' or 'group'
    human_party: Optional[str] = None  # 'user', 'athena', or None (bot-to-bot DM / group room)
    member_bot_ids: List[int]
    label: Optional[str] = None


def _row_to_room(r):
    return {
        "id": r[0], "kind": r[1], "human_party": r[2], "label": r[3],
        "member_bot_ids": json.loads(r[4]) if r[4] else [],
        "created_at": r[5], "last_active": r[6], "is_everyone_room": bool(r[7]),
    }

_ROOM_COLUMNS = "id, kind, human_party, label, member_bot_ids_json, created_at, last_active, is_everyone_room"


@router.post("/api/rooms/everyone")
def get_or_create_everyone_room():
    """The one canonical room containing every bot -- membership isn't
    a fixed choice like a normal group, it's always 'whichever bots
    currently exist', refreshed here on every open rather than kept in
    sync via create/delete hooks elsewhere. Response routing doesn't
    actually depend on this stored list at all (a group room's replies
    are driven entirely by @mention, checked against the full roster
    regardless of room membership) -- this refresh is purely so the
    displayed member list in the UI stays accurate."""
    conn = bots._bots_conn()
    try:
        all_bot_ids = [r[0] for r in conn.execute("SELECT id FROM bots").fetchall()]
        existing = conn.execute(f"SELECT {_ROOM_COLUMNS} FROM rooms WHERE is_everyone_room = 1").fetchone()
        if existing:
            room_id = existing[0]
            conn.execute("UPDATE rooms SET member_bot_ids_json = ? WHERE id = ?", (json.dumps(all_bot_ids), room_id))
            conn.commit()
            return _row_to_room(conn.execute(f"SELECT {_ROOM_COLUMNS} FROM rooms WHERE id = ?", (room_id,)).fetchone())
        now = time.time()
        cur = conn.execute(
            "INSERT INTO rooms (kind, human_party, label, member_bot_ids_json, created_at, last_active, is_everyone_room) VALUES ('group', NULL, 'Everyone', ?, ?, ?, 1)",
            (json.dumps(all_bot_ids), now, now)
        )
        conn.commit()
        return _row_to_room(conn.execute(f"SELECT {_ROOM_COLUMNS} FROM rooms WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


@router.get("/api/rooms")
def list_rooms():
    conn = bots._bots_conn()
    try:
        rows = conn.execute(f"SELECT {_ROOM_COLUMNS} FROM rooms ORDER BY last_active DESC, created_at DESC").fetchall()
        return [_row_to_room(r) for r in rows]
    finally:
        conn.close()


@router.post("/api/rooms/find_or_create")
def find_or_create_room(req: RoomFindOrCreateIn):
    """For DMs specifically: lazily reuse an existing room rather than
    creating a new one every time the same pairing is opened -- a DM
    is identified by its kind, human_party, AND exact member set, so
    the user's own chat with a bot and Athena's task-conversation with
    that same bot are always two distinct rooms, never merged, per the
    isolation requirement. Group rooms are always created fresh (no
    matching/reuse), since there's no single natural identity for
    'the' group room among a given set of bots the way there is for a
    DM pair."""
    conn = bots._bots_conn()
    try:
        sorted_members = json.dumps(sorted(req.member_bot_ids))
        if req.kind == "dm":
            existing = conn.execute(
                "SELECT " + _ROOM_COLUMNS + " FROM rooms WHERE kind = 'dm' AND "
                "(human_party IS ? OR human_party = ?) AND member_bot_ids_json = ?",
                (req.human_party, req.human_party, sorted_members)
            ).fetchone()
            if existing:
                return _row_to_room(existing)
        now = time.time()
        cur = conn.execute("""
            INSERT INTO rooms (kind, human_party, label, member_bot_ids_json, created_at, last_active)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (req.kind, req.human_party, req.label or "", sorted_members, now, now))
        conn.commit()
        return _row_to_room(conn.execute(f"SELECT {_ROOM_COLUMNS} FROM rooms WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


class RoomMessageIn(BaseModel):
    sender_type: str  # 'user', 'athena', or 'bot'
    sender_bot_id: Optional[int] = None
    content: str


@router.get("/api/rooms/{room_id}/messages")
def get_room_messages(room_id: int):
    conn = bots._bots_conn()
    try:
        rows = conn.execute(
            "SELECT id, room_id, sender_type, sender_bot_id, content, created_at FROM room_messages WHERE room_id = ? ORDER BY id ASC",
            (room_id,)
        ).fetchall()
        return [{"id": r[0], "room_id": r[1], "sender_type": r[2], "sender_bot_id": r[3], "content": r[4], "created_at": r[5]} for r in rows]
    finally:
        conn.close()


@router.post("/api/rooms/{room_id}/messages")
def post_room_message(room_id: int, req: RoomMessageIn):
    conn = bots._bots_conn()
    try:
        now = time.time()
        cur = conn.execute(
            "INSERT INTO room_messages (room_id, sender_type, sender_bot_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (room_id, req.sender_type, req.sender_bot_id, req.content, now)
        )
        conn.execute("UPDATE rooms SET last_active = ? WHERE id = ?", (now, room_id))
        conn.commit()
        return {"id": cur.lastrowid, "room_id": room_id, "sender_type": req.sender_type, "sender_bot_id": req.sender_bot_id, "content": req.content, "created_at": now}
    finally:
        conn.close()



def _room_messages_to_chat_messages(room_messages, responding_bot_id, system_prompt):
    """Convert a room's raw message rows into the standard
    role/content shape a chat completion expects. Only two real roles
    exist (user/assistant) but a room can have several distinct
    senders (the user, Athena, and multiple different bots) -- the
    responding bot's own past turns become 'assistant'; everything
    else becomes 'user' with the actual sender's name prefixed into
    the content, so the model can still tell who said what."""
    out = [{"role": "system", "content": system_prompt}]
    for m in room_messages:
        if m["sender_type"] == "bot" and m["sender_bot_id"] == responding_bot_id:
            out.append({"role": "assistant", "content": m["content"]})
        else:
            if m["sender_type"] == "user":
                label = "User"
            elif m["sender_type"] == "athena":
                label = "Athena"
            else:
                label = m.get("sender_bot_name") or f"Bot #{m.get('sender_bot_id')}"
            out.append({"role": "user", "content": f"[{label}]: {m['content']}"})
    return out



def _unload_bot_model(bot):
    """Best-effort unload after a bot's turn, dispatched by its
    configured strategy. Failures here are logged, never raised --
    an unload failing shouldn't break the turn that already
    completed successfully; it just means the next participant on
    this host may contend for VRAM a little longer than intended."""
    strategy = bot.get("unload_strategy") or "none"
    endpoint_url = bot.get("endpoint_url") or ""
    try:
        if strategy == "ollama_keep_alive":
            target = (endpoint_url.rstrip("/") + "/api/chat") if endpoint_url else OLLAMA_URL
            httpx.post(target, json={"model": bot["model"], "messages": [], "keep_alive": 0}, timeout=15)
        elif strategy == "llamacpp_unload":
            base = endpoint_url.rstrip("/") if endpoint_url else ""
            if base:
                httpx.post(base + "/models/unload", json={"model": bot["model"]}, timeout=15)
        elif strategy == "vllm_sleep":
            base = endpoint_url.rstrip("/") if endpoint_url else ""
            if base:
                httpx.post(base + "/sleep?level=1", timeout=15)
        # "http_hook" and "none" are no-ops for now -- http_hook has no
        # configured hook URL in the schema yet; add one explicitly
        # when a real custom-backend use case needs it, rather than
        # building unused config ahead of time.
    except Exception as e:
        print(f"[Bots] unload failed for bot {bot.get('id')} ({strategy}): {e}", flush=True)



def _parse_mentions(content, all_bots):
    """Resolve @name tokens in a group-room message against the live
    roster, case-insensitively. Only names that match an existing bot
    become real mentions; anything else (an email-shaped @, a random
    @word) is just ignored rather than erroring, since a message isn't
    guaranteed to only ever contain intentional mentions."""
    by_name_lower = {b["name"].lower(): b for b in all_bots}
    mentioned_ids = []
    for token in re.findall(r"@([A-Za-z0-9_-]+)", content):
        bot = by_name_lower.get(token.lower())
        if bot and bot["id"] not in mentioned_ids:
            mentioned_ids.append(bot["id"])
    return mentioned_ids



def _resolve_dm_responder(room, sender_bot_id):
    """A DM's responder is whoever ISN'T the sender. A 1-bot DM
    (user/Athena talking to a bot) always has that one bot respond.
    A 2-bot DM (bot-to-bot) has the other bot respond -- never both,
    and never the sender itself."""
    members = room["member_bot_ids"]
    if len(members) == 1:
        return list(members)
    return [b for b in members if b != sender_bot_id]



_ATHENA_ROOM_ALLOWED_TOOL_NAMES = {
    "list_bots", "draft_bot_prompt", "message_bot", "list_rooms",
    "read_room_messages", "message_room", "create_bot", "update_bot",
}


def _dispatch_athena_room_tool_call(tc):
    """Delegation-only dispatcher for Athena's room-turn loop -- a thin
    wrapper over the shared main.dispatch_tool, restricted to just the
    delegation tools above. Never includes workspace write tools
    (bash_exec, file edits) -- those stay confined to her private 1:1
    session, per the agreed design. No passthrough to the generic LCM
    tools/call endpoint either (passthrough_session_id=None) -- an
    unrecognized name here is genuinely unexpected, not a valid LCM
    recall tool the way it might be for the other two dispatchers."""
    from main import dispatch_tool, ToolContext  # deferred: main.py imports rooms.py at module load
    fn = tc.get("function", {})
    name = fn.get("name", "")
    args = fn.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    ctx = ToolContext(
        passthrough_session_id=None,
        unknown_tool_message_template="Unknown tool for Athena's room turn: {name}",
    )
    return dispatch_tool(name, args, ctx, allowed_names=_ATHENA_ROOM_ALLOWED_TOOL_NAMES)



_ATHENA_ROOM_MAX_ROUNDS = 8



def _room_key(room_id):
    """The generic string key used to track a room-turn's background
    generation in the exact same generation_streaming.active_generations/generation_streaming.generation_snapshots
    dicts main chat and Athena2 already use -- distinct from a real LCM
    session_id (which is always a UUID), so no collision risk, and it
    means zero new tracking infrastructure is needed for rooms at all."""
    return f"room-{room_id}"



def _run_athena_room_turn_gen(room_id):
    """Generator version of Athena's room turn -- yields the exact same
    SSE-shaped chunks generate() does (thinking/delta/tool_start/
    tool_output/done), so it can run through the identical background-
    thread + snapshot machinery main chat and Athena2 already use. Saves
    her final reply into room_messages itself, right before yielding
    done, the same way generate() calls send_to_lcm right before its
    own done chunk -- so a real reply always lands in the room exactly
    once generation actually finishes, whether anyone's still watching
    or not."""
    current_settings = settings.load_settings()
    defaults = current_settings.get("athena_agent_model") or {}
    model = defaults.get("model")
    if not model:
        yield f"data: {json.dumps({'error': "No model is configured for Athena's own agent turns yet. Set one via her Agent Defaults / model picker first."})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"
        return

    conn = bots._bots_conn()
    try:
        room_row = conn.execute(f"SELECT {_ROOM_COLUMNS} FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if not room_row:
            yield f"data: {json.dumps({'error': f'Room {room_id} not found.'})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
            return
        history_rows = conn.execute(
            "SELECT sender_type, sender_bot_id, content FROM room_messages WHERE room_id = ? ORDER BY id ASC",
            (room_id,)
        ).fetchall()
        all_bot_rows = conn.execute(f"SELECT {bots._BOT_COLUMNS} FROM bots").fetchall()
        bots_by_id = {b["id"]: b for b in [bots._row_to_bot(r) for r in all_bot_rows]}
    finally:
        conn.close()

    history = [{"sender_type": r[0], "sender_bot_id": r[1], "content": r[2],
                "sender_bot_name": bots_by_id.get(r[1], {}).get("name") if r[1] else None} for r in history_rows]

    from main import _stream_completion, CODING_HARNESS_SYSTEM_PROMPT  # deferred: main.py imports rooms.py at module load
    room_turn_workspace = settings.load_settings().get("workspace") or ""
    system_prompt = CODING_HARNESS_SYSTEM_PROMPT + bots._get_bot_delegation_prompt_section(room_turn_workspace)
    chat_messages = [{"role": "system", "content": system_prompt}]
    for m in history:
        if m["sender_type"] == "athena":
            chat_messages.append({"role": "assistant", "content": m["content"]})
        else:
            label = "User" if m["sender_type"] == "user" else (m.get("sender_bot_name") or f"Bot #{m.get('sender_bot_id')}")
            chat_messages.append({"role": "user", "content": f"[{label}]: {m['content']}"})

    fake_req = bots._FakeReqForDispatch(
        provider=defaults.get("provider", ""),
        model=model,
        endpoint_url=defaults.get("endpoint_url", ""),
        api_key=defaults.get("api_key", ""),
    )
    endpoint_url = defaults.get("endpoint_url") or ""
    provider = defaults.get("provider") or ""
    target_url = (endpoint_url.rstrip("/") + "/api/chat") if endpoint_url and provider == "" else (OLLAMA_URL if provider == "" else "")

    fingerprints = []
    full_reply = ""
    cancel_flag = threading.Event()

    for round_num in range(_ATHENA_ROOM_MAX_ROUNDS):
        activity.touch()
        round_reply = ""
        round_tool_calls = []
        try:
            for chunk in _stream_completion(fake_req, target_url, chat_messages, bots.BOT_DELEGATION_TOOL_SCHEMAS, 8192, cancel_flag):
                msg = chunk.get("message", {})
                thinking_delta = msg.get("thinking", "")
                if thinking_delta:
                    yield f"data: {json.dumps({'thinking': thinking_delta})}\n\n"
                delta = msg.get("content", "")
                if delta:
                    round_reply += delta
                    yield f"data: {json.dumps({'delta': delta})}\n\n"
                if msg.get("tool_calls"):
                    round_tool_calls.extend(msg["tool_calls"])
                if chunk.get("done"):
                    break
        except Exception as e:
            yield f"data: {json.dumps({'error': f"Athena's room turn failed: {e}"})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
            return

        full_reply += round_reply

        if not round_tool_calls:
            _save_athena_room_reply(room_id, full_reply)
            yield f"data: {json.dumps({'done': True})}\n\n"
            return

        chat_messages.append({"role": "assistant", "content": round_reply, "tool_calls": round_tool_calls})
        for tc in round_tool_calls:
            tool_name = tc.get("function", {}).get("name", "unknown")
            yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name})}\n\n"
            fingerprint = hashlib.md5(json.dumps(tc.get("function", {}), sort_keys=True).encode()).hexdigest()
            repeat_count = fingerprints.count(fingerprint)
            if repeat_count >= 2:
                result = {"error": "BLOCKED: this exact call has already been made multiple times this turn. Use the result you already have, or make a genuinely different call."}
            else:
                result = _dispatch_athena_room_tool_call(tc)
                fingerprints.append(fingerprint)
            yield f"data: {json.dumps({'type': 'tool_output', 'tool': tool_name, 'output': result})}\n\n"
            chat_messages.append({"role": "tool", "content": json.dumps(result)})

    fallback = full_reply or "I made several tool calls but wasn't able to settle on a final answer within my round limit -- ask me to continue or narrow the task."
    _save_athena_room_reply(room_id, fallback)
    yield f"data: {json.dumps({'done': True})}\n\n"



def _save_athena_room_reply(room_id, content):
    reply_time = time.time()
    conn = bots._bots_conn()
    try:
        conn.execute(
            "INSERT INTO room_messages (room_id, sender_type, sender_bot_id, content, created_at) VALUES (?, 'athena', NULL, ?, ?)",
            (room_id, content, reply_time)
        )
        conn.execute("UPDATE rooms SET last_active = ? WHERE id = ?", (reply_time, room_id))
        conn.commit()
    finally:
        conn.close()



def _start_athena_room_turn_background(room_id):
    """Starts Athena's room turn in a background thread, reusing the
    exact same queue/snapshot/active-generations machinery main chat
    and Athena2 already use -- keyed by _room_key(room_id) instead of
    a real session_id. Fire-and-forget: the caller doesn't wait for
    this, since the whole point is that a room turn now survives the
    requesting connection closing, same as everything else."""
    key = _room_key(room_id)
    gen = _run_athena_room_turn_gen(room_id)
    event_queue = queue.Queue()
    generation_streaming.active_generations[key] = event_queue
    generation_streaming.generation_snapshots[key] = {"thinking": "", "content": "", "tool_calls": []}
    threading.Thread(target=generation_streaming.drain_generator_to_queue, args=(gen, event_queue, key), daemon=True).start()



class RoomSendIn(BaseModel):
    sender_type: str
    sender_bot_id: Optional[int] = None
    content: str



@router.post("/api/rooms/{room_id}/send")
def send_room_message(room_id: int, req: RoomSendIn):
    """The real conversational entry point: store the incoming
    message, work out who should respond (per-DM: the other party,
    always; per-group-room: only @mentioned bots, never a silent
    default responder), dispatch each responder under its host's
    concurrency lock, run its unload strategy afterward, and store
    each reply -- all visible in the same room_messages history."""
    conn = bots._bots_conn()
    try:
        room_row = conn.execute(f"SELECT {_ROOM_COLUMNS} FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if not room_row:
            return {"error": f"Room {room_id} not found."}
        room = _row_to_room(room_row)

        now = time.time()
        conn.execute(
            "INSERT INTO room_messages (room_id, sender_type, sender_bot_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (room_id, req.sender_type, req.sender_bot_id, req.content, now)
        )
        conn.execute("UPDATE rooms SET last_active = ? WHERE id = ?", (now, room_id))
        conn.commit()

        all_bot_rows = conn.execute(f"SELECT {bots._BOT_COLUMNS} FROM bots").fetchall()
        all_bots = [bots._row_to_bot(r) for r in all_bot_rows]
        bots_by_id = {b["id"]: b for b in all_bots}

        if room["kind"] == "dm":
            responder_ids = _resolve_dm_responder(room, req.sender_bot_id)
        else:
            responder_ids = _parse_mentions(req.content, all_bots)

        athena_mentioned = (
            room.get("is_everyone_room")
            and req.sender_type != "athena"
            and re.search(r"@athena\b", req.content, re.IGNORECASE) is not None
        )

        replies = []
        for bot_id in responder_ids:
            bot = bots_by_id.get(bot_id)
            if not bot:
                continue
            history_rows = conn.execute(
                "SELECT sender_type, sender_bot_id, content FROM room_messages WHERE room_id = ? ORDER BY id ASC",
                (room_id,)
            ).fetchall()
            history = [{"sender_type": r[0], "sender_bot_id": r[1], "content": r[2],
                        "sender_bot_name": bots_by_id.get(r[1], {}).get("name") if r[1] else None} for r in history_rows]
            system_prompt = bots._build_bot_system_prompt(bot)
            chat_messages = _room_messages_to_chat_messages(history, bot_id, system_prompt)

            host_key = host_locks.endpoint_host(bot.get("endpoint_url"))
            lock = host_locks.get_host_lock(host_key)
            with lock:
                result = bots._call_bot_endpoint(bot, chat_messages)
                _unload_bot_model(bot)

            if "error" in result and not result.get("content"):
                replies.append({"bot_id": bot_id, "error": result["error"]})
                continue
            if "error" in result:
                # A round-limit or similar warning rode along with real
                # content -- write the actual findings to the room, don't
                # discard them just because a warning was also attached.
                result = {"content": result["content"] + "\n\n[Note: " + result["error"] + "]"}

            reply_time = time.time()
            conn.execute(
                "INSERT INTO room_messages (room_id, sender_type, sender_bot_id, content, created_at) VALUES (?, 'bot', ?, ?, ?)",
                (room_id, bot_id, result["content"], reply_time)
            )
            conn.execute("UPDATE rooms SET last_active = ? WHERE id = ?", (reply_time, room_id))
            conn.commit()
            replies.append({"bot_id": bot_id, "content": result["content"], "created_at": reply_time})

        athena_started = False
        if athena_mentioned:
            _start_athena_room_turn_background(room_id)
            athena_started = True

        return {"room_id": room_id, "replies": replies, "athena_started": athena_started}
    finally:
        conn.close()



def _message_room_tool(room_id, content):
    if task_context.current_task_hash.get() is not None:
        return {"error": "A delegation task is already active this turn -- message_room cannot be used mid-delegation, including as a workaround for a failed or rejected step. Use run_delegation_step or plan_delegation instead, even if a prior step failed."}
    return send_room_message(room_id, RoomSendIn(sender_type="athena", sender_bot_id=None, content=content))



def _list_rooms_tool():
    conn = bots._bots_conn()
    try:
        rows = conn.execute(f"SELECT {_ROOM_COLUMNS} FROM rooms ORDER BY last_active DESC").fetchall()
        return [_row_to_room(r) for r in rows]
    finally:
        conn.close()



def _read_room_messages_tool(room_id):
    return get_room_messages(room_id)



