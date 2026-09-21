import os
import sqlite3

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

RATINGS_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratings.db")


def ratings_conn():
    conn = sqlite3.connect(RATINGS_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            message_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            model TEXT NOT NULL,
            rating TEXT NOT NULL,
            reason TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    return conn


class RatingIn(BaseModel):
    message_id: int
    session_id: str
    model: str
    rating: str = ""  # "up", "down", or "" to remove the rating entirely
    reason: str = ""


@router.post("/api/rate")
def rate_message(req: RatingIn):
    """Upsert-or-delete semantics: re-rating or removing a vote always
    recomputes cleanly from current state, since /api/model-stats
    aggregates directly from whatever rows currently exist -- a changed
    or removed vote is automatically reflected with no separate
    increment/decrement bookkeeping to get wrong."""
    conn = ratings_conn()
    try:
        if not req.rating:
            conn.execute("DELETE FROM ratings WHERE message_id = ?", (req.message_id,))
        else:
            conn.execute("""
                INSERT INTO ratings (message_id, session_id, model, rating, reason, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(message_id) DO UPDATE SET
                    rating = excluded.rating,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
            """, (req.message_id, req.session_id, req.model, req.rating, req.reason))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.get("/api/ratings/{session_id}")
def get_ratings(session_id: str):
    """Returns {message_id: {rating, reason}} for a session, so the
    frontend can re-apply which button should show as selected after
    a reload -- ratings live in this DB, not in the message history
    LCM returns, since LCM has no concept of a 'rating' field."""
    conn = ratings_conn()
    try:
        rows = conn.execute(
            "SELECT message_id, rating, reason FROM ratings WHERE session_id = ?",
            (session_id,)
        ).fetchall()
        return {str(r[0]): {"rating": r[1], "reason": r[2]} for r in rows}
    finally:
        conn.close()


PINS_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pins.db")


def _pins_conn():
    conn = sqlite3.connect(PINS_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pinned_messages (
            message_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            content TEXT NOT NULL,
            pinned_at TEXT NOT NULL
        )
    """)
    return conn


class PinIn(BaseModel):
    message_id: int
    session_id: str
    content: str = ""
    pinned: bool = True


@router.post("/api/pin")
def pin_message(req: PinIn):
    """Upsert-or-delete, mirroring /api/rate. Stores the message's actual
    content at pin time (a snapshot), not just a reference to it --
    that's deliberate: the whole point of pinning is to survive LCM's
    own auto-compaction of older context, so the pinned copy has to be
    completely independent of whatever LCM does to its own history
    later."""
    conn = _pins_conn()
    try:
        if not req.pinned:
            conn.execute("DELETE FROM pinned_messages WHERE message_id = ?", (req.message_id,))
        else:
            conn.execute("""
                INSERT INTO pinned_messages (message_id, session_id, content, pinned_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(message_id) DO UPDATE SET
                    content = excluded.content,
                    pinned_at = excluded.pinned_at
            """, (req.message_id, req.session_id, req.content))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.get("/api/pins/{session_id}")
def get_pins(session_id: str):
    """Returns {message_id: {content}} for a session, so the frontend
    can re-apply which messages show the pin toggle as active after a
    reload."""
    conn = _pins_conn()
    try:
        rows = conn.execute(
            "SELECT message_id, content FROM pinned_messages WHERE session_id = ? ORDER BY pinned_at ASC",
            (session_id,)
        ).fetchall()
        return {str(r[0]): {"content": r[1]} for r in rows}
    finally:
        conn.close()


def get_pinned_context(session_id: str) -> str:
    """Formats currently-pinned messages for this session into a system-
    prompt block. Injected fresh on every turn regardless of how far
    back in the conversation the original message now sits, or whether
    LCM's own recall/compaction would have surfaced it -- this is the
    actual fix for the model losing track of an earlier multi-step plan
    a few messages later instead of consulting it."""
    conn = _pins_conn()
    try:
        rows = conn.execute(
            "SELECT content FROM pinned_messages WHERE session_id = ? ORDER BY pinned_at ASC",
            (session_id,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    items = "\n\n".join(f"[Pinned message {i+1}]\n{r[0]}" for i, r in enumerate(rows))
    return (
        "\n\n--- PINNED CONTEXT ---\n"
        "The user has pinned the following message(s) to stay in context for the "
        "rest of this session, regardless of how far back they now sit in the "
        "conversation. Treat this as still-active, authoritative context -- for "
        "example, if this is a step-by-step plan, keep following it exactly rather "
        "than reconstructing or guessing the steps from memory.\n\n"
        f"{items}\n"
        "--- END PINNED CONTEXT ---"
    )
