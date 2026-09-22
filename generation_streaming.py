import json

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

# Generic "run a background generation, stream it via a queue" machinery,
# reused by both chat_stream's own chat loop and the Athena-room turn loop
# -- this belongs to neither one specifically, which is why it has its own
# module rather than living inside whichever one happened to be written
# first.

# A person closing the browser tab or navigating away stops that tab from
# listening -- it does nothing to the backend, which has no way to know the
# client walked away and just keeps talking to the model in the background
# regardless. This explicit flag, checked inside the generator's own loop,
# is what actually stops the backend's work, not just the frontend's
# display of it.
cancel_flags = {}

# session_id -> queue.Queue(), present only while a background generation
# thread is actively draining that session's generator. Lets the frontend
# ask "is this session still generating" after a closed tab/navigation and
# the browser reconnects to it, and lets drain_generator_to_queue below
# know where to clean up when it's done. Deliberately separate from
# cancel_flags above -- that one stops the work explicitly (the stop
# button); this one just tracks whether it's currently running at all.
active_generations = {}

# A continuously-updated live snapshot of an in-progress generation --
# thinking so far, content so far, and each tool call's status/output --
# kept separately from the single-consumer event queue above, so ANY
# number of clients can ask "what's actually happening right now" via a
# real status check, not just whoever happens to be the one actively
# draining the queue. This is what makes "still generating" mean something
# more than a boolean spinner.
generation_snapshots = {}


def update_snapshot(key, chunk):
    """Parses one SSE-formatted chunk string (exactly what a chat/room
    generator yields) and updates the live snapshot for this session/room
    key in place."""
    if not chunk.startswith("data: "):
        return
    try:
        obj = json.loads(chunk[6:].strip())
    except (json.JSONDecodeError, ValueError):
        return
    snap = generation_snapshots.setdefault(key, {"thinking": "", "content": "", "tool_calls": []})
    if obj.get("thinking"):
        snap["thinking"] += obj["thinking"]
    if obj.get("delta"):
        snap["content"] += obj["delta"]
    if obj.get("type") == "tool_start":
        snap["tool_calls"].append({"tool": obj.get("tool"), "status": "running", "output": None})
    if obj.get("type") == "tool_output":
        for tc in reversed(snap["tool_calls"]):
            if tc["tool"] == obj.get("tool") and tc["status"] == "running":
                tc["status"] = "done"
                tc["output"] = obj.get("output")
                break


def drain_generator_to_queue(gen, event_queue, session_id):
    """Runs an existing SSE-yielding generator (a chat/room agentic
    generation loop, completely unmodified) to completion in a background
    thread, pushing each yielded chunk into a queue instead of handing it
    back to any particular HTTP response directly. This is what lets
    generation survive the requesting browser tab closing or navigating
    away: this thread keeps pulling from `gen` regardless of whether
    anything is still reading from the queue on the other end, since it's a
    separate OS thread with no participation at all in the HTTP request's
    own cancellation. The generation logic itself is untouched -- this is
    purely an outer layer around it."""
    try:
        for chunk in gen:
            update_snapshot(session_id, chunk)
            event_queue.put(chunk)
    except Exception as e:
        print(f"[DEBUG] background generation for session {session_id!r} raised: {e!r}", flush=True)
    finally:
        event_queue.put(None)  # sentinel: no more chunks coming
        active_generations.pop(session_id, None)
        generation_snapshots.pop(session_id, None)


class CancelIn(BaseModel):
    session_id: str


@router.post("/api/chat/cancel")
def cancel_chat(req: CancelIn):
    print(f"[CANCEL-DEBUG] cancel request for session_id={req.session_id!r}, known flags={list(cancel_flags.keys())!r}", flush=True)
    flag = cancel_flags.get(req.session_id)
    if flag:
        flag.set()
        print(f"[CANCEL-DEBUG] flag found and set for {req.session_id!r}", flush=True)
    else:
        print(f"[CANCEL-DEBUG] NO matching flag for {req.session_id!r}", flush=True)
    return {"cancelled": bool(flag)}


@router.get("/api/chat/status/{session_id}")
def chat_status(session_id: str):
    """Lets the frontend check, right after loading a session, whether a
    generation for it is still running in the background (e.g. the tab was
    closed or navigated away mid-response) -- so it can show a "still
    running" placeholder and poll instead of just displaying whatever
    partial state was last saved. Also returns the live, continuously-
    updated snapshot (thinking so far, content so far, tool calls with
    status/output) so a reconnecting client can render genuine in-progress
    state, not just a boolean spinner."""
    generating = session_id in active_generations
    snapshot = generation_snapshots.get(session_id) if generating else None
    return {"generating": generating, "snapshot": snapshot}
