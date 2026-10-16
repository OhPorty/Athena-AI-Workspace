"""The Accounts tab's backend: the main account creates, resets and removes the other accounts, and decides whether prompts for models on
this machine run one at a time. Everything here is for the main account only, except the queue status that every account polls."""
import shutil
import sqlite3

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import bots
import db
import lcm_client
import ownership
import prompt_gate
import settings
from auth_routes import auth_manager
from logging_setup import logger
from user_context import current_user


def _only_main():
    if not ownership.is_main():
        raise HTTPException(status_code=403, detail="Only the main account can manage accounts.")


router = APIRouter()
admin = APIRouter(dependencies=[Depends(_only_main)])


class NewAccountIn(BaseModel):
    username: str
    password: str = ""
    full_access: bool = False
    share_authenticator: bool = False


class AccessIn(BaseModel):
    full_access: bool


class AccountSettingsIn(BaseModel):
    one_prompt_at_a_time: bool


def _overview():
    return {"users": auth_manager.list_users(), "one_prompt_at_a_time": bool(settings.load_settings().get("one_prompt_at_a_time")),
            "main_authenticator_on": auth_manager.main_has_authenticator()}


@admin.get("/api/accounts")
def list_accounts():
    return _overview()


@admin.post("/api/accounts")
def create_account(req: NewAccountIn):
    made = auth_manager.create_user(req.username, req.password or None, req.full_access, req.share_authenticator)
    if "error" in made:
        return _error(made["error"])
    _claim_existing_chats_for_main()
    return {**made, **_overview()}


@admin.post("/api/accounts/{username}/reset-password")
def reset_password(username: str):
    made = auth_manager.reset_password(username)
    if "error" in made:
        return _error(made["error"], 404)
    return {**made, **_overview()}


@admin.post("/api/accounts/{username}/reset-authenticator")
def reset_authenticator(username: str):
    if not auth_manager.reset_authenticator(username):
        return _error("That account's authenticator cannot be reset here.")
    return _overview()


@admin.put("/api/accounts/{username}/access")
def set_access(username: str, req: AccessIn):
    if not auth_manager.set_full_access(username, req.full_access):
        return _error("That account's access cannot be changed.")
    return _overview()


@admin.delete("/api/accounts/{username}")
def delete_account(username: str):
    if not auth_manager.delete_user(username):
        return _error("That account cannot be removed.")
    purge_user(username)
    return _overview()


@admin.put("/api/accounts/settings")
def set_account_settings(req: AccountSettingsIn):
    with settings.settings_lock:
        current = settings.load_settings()
        current["one_prompt_at_a_time"] = req.one_prompt_at_a_time
        settings.save_settings(current)
    return _overview()


@router.get("/api/queue/status")
def queue_status():
    return prompt_gate.status(current_user() or ownership.main_name())


router.include_router(admin)


def _error(message, status=400):
    return JSONResponse(status_code=status, content={"error": message})


# ---------- what an account owns ----------

def _claim_existing_chats_for_main():
    """The chats that were there before the first extra account are the main account's, so nobody else can ever claim them."""
    try:
        resp = httpx.get(f"{lcm_client.LCM_URL}/sessions", timeout=10)
        if resp.status_code == 200:
            ownership.claim_existing_sessions_for_main([s["id"] for s in resp.json()])
    except Exception as e:
        logger.warning(f"could not mark existing chats as the main account's: {e!r}")


def purge_user(username: str):
    """Remove everything an account owned: chats, memory, notes, research, tasks, bots, rooms, pins, ratings, push devices and its folder."""
    session_ids = ownership.session_ids_of(username) + [f"{ownership.BOTS_SESSION_BASE}--{username}"]
    for sid in session_ids:
        try:
            httpx.delete(f"{lcm_client.LCM_URL}/session/{sid}", timeout=10)
        except Exception as e:
            logger.warning(f"could not delete chat {sid!r} of removed account {username!r}: {e!r}")
    try:
        facts = httpx.get(f"{lcm_client.LCM_URL}/facts", timeout=10).json()
        for fact in facts:
            row_owner = _fact_owner(fact["id"])
            if row_owner == username:
                httpx.delete(f"{lcm_client.LCM_URL}/facts/{fact['id']}", timeout=10)
    except Exception as e:
        logger.warning(f"could not delete the memory of removed account {username!r}: {e!r}")
    conn = bots._bots_conn()
    try:
        room_ids = [r[0] for r in conn.execute("SELECT id FROM rooms WHERE owner = ?", (username,)).fetchall()]
        for rid in room_ids:
            conn.execute("DELETE FROM room_messages WHERE room_id = ?", (rid,))
        conn.execute("DELETE FROM rooms WHERE owner = ?", (username,))
        conn.execute("DELETE FROM bots WHERE owner = ?", (username,))
        conn.commit()
    finally:
        conn.close()
    conn = db.get_conn()
    try:
        for table in ("tasks", "research_runs", "ratings", "push_subscriptions"):
            try:
                conn.execute(f"DELETE FROM {table} WHERE owner = ?", (username,))
            except sqlite3.OperationalError:
                pass     # the table has never been used, so it does not exist yet
        for sid in session_ids:
            try:
                conn.execute("DELETE FROM pinned_messages WHERE session_id = ?", (sid,))
            except sqlite3.OperationalError:
                pass
        conn.execute("DELETE FROM session_owners WHERE owner = ?", (username,))
        conn.execute("DELETE FROM fact_owners WHERE owner = ?", (username,))
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(ownership.user_dir(username), ignore_errors=True)


def _fact_owner(fact_id):
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT owner FROM fact_owners WHERE fact_id = ?", (fact_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return row[0] if row else None
