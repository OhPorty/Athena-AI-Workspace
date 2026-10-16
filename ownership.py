"""Whose data is whose. Every account has its own chats, memory facts, notes, research, tasks, bots, rooms, pins, ratings and push
subscriptions; models, endpoints for local engines, MCP servers and skills are shared.

An empty owner means the main account: everything that existed before there were several accounts is the main account's, and code that
runs outside any web request (tests, scripts) acts as the main account unless it says otherwise with user_context.as_user().
"""
import ipaddress
import os
import re
import shutil
import socket
import threading
import time
from urllib.parse import urlparse

import db
import settings
from user_context import current_user

_init_lock = threading.Lock()
_ready_for = None


def main_name() -> str:
    from auth_routes import auth_manager
    return auth_manager.main_user or ""


def acting_user() -> str:
    """The account the current code runs for."""
    return current_user() or main_name()


def is_main(user: str = None) -> bool:
    user = acting_user() if user is None else user
    return (not user) or user == main_name()


def can_use_whole_host(user: str = None) -> bool:
    from auth_routes import auth_manager
    user = acting_user() if user is None else user
    return is_main(user) or auth_manager.can_use_whole_host(user)


def owner_filter(user: str = None, column: str = "owner"):
    """(SQL condition, params) selecting the rows that belong to the account. Rows with no owner are the main account's."""
    user = acting_user() if user is None else user
    if is_main(user):
        return f"({column} = ? OR {column} = '' OR {column} IS NULL)", [user]
    return f"{column} = ?", [user]


def stored_owner(user: str = None) -> str:
    return acting_user() if user is None else user


def ensure_owner_column(conn, table: str):
    """Add the owner column to a table made before accounts existed."""
    columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if "owner" not in columns:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass     # another connection added it first


def _conn():
    conn = db.get_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS session_owners (session_id TEXT PRIMARY KEY, owner TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS fact_owners (fact_id INTEGER PRIMARY KEY, owner TEXT NOT NULL)")
    return conn


# ---------- chat sessions ----------

def session_owner(session_id: str):
    conn = _conn()
    try:
        row = conn.execute("SELECT owner FROM session_owners WHERE session_id = ?", (session_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def session_allowed(session_id: str, user: str = None, claim: bool = False) -> bool:
    """May this account touch the session? With claim=True an unknown session becomes the account's (the first message of a new chat).
    A session nobody has claimed is the main account's: it was there before accounts existed."""
    user = acting_user() if user is None else user
    if not session_id:
        return True
    conn = _conn()
    try:
        row = conn.execute("SELECT owner FROM session_owners WHERE session_id = ?", (session_id,)).fetchone()
        if row:
            return row[0] == user or (row[0] == "" and is_main(user))
        if claim:
            if not is_main(user) and _exists_in_lcm(session_id):
                return False        # a chat from before accounts that was never marked: it is the main account's, not first come
            conn.execute("INSERT OR IGNORE INTO session_owners (session_id, owner) VALUES (?, ?)", (session_id, user))
            conn.commit()
            return True
        return is_main(user)
    finally:
        conn.close()


def owner_of_session(session_id: str) -> str:
    """The account a chat belongs to (the main account's when nobody claimed it, and for its own delegation chat by name)."""
    if (session_id or "").startswith(BOTS_SESSION_BASE + "--"):
        return session_id[len(BOTS_SESSION_BASE) + 2:]
    owner = session_owner(session_id)
    return owner or main_name()


def _exists_in_lcm(session_id: str) -> bool:
    """Whether the chat service already knows this session. If it cannot be asked, assume it does: better to refuse a new chat than to
    hand someone else's old one away."""
    import httpx
    import lcm_client
    try:
        listed = httpx.get(f"{lcm_client.LCM_URL}/sessions", timeout=5)
        if listed.status_code != 200 or any(x.get("id") == session_id for x in listed.json()):
            return True
        messages = httpx.get(f"{lcm_client.LCM_URL}/messages/{session_id}", timeout=5)
        return messages.status_code != 200 or bool(messages.json())
    except Exception:
        return True


def own_sessions(sessions: list, user: str = None) -> list:
    """Keep only the sessions (dicts with an id) that belong to the account."""
    user = acting_user() if user is None else user
    conn = _conn()
    try:
        owners = dict(conn.execute("SELECT session_id, owner FROM session_owners").fetchall())
    finally:
        conn.close()
    return [s for s in sessions if (owners.get(s.get("id")) == user) or (is_main(user) and owners.get(s.get("id"), "") == "")]


def session_ids_of(user: str) -> list:
    conn = _conn()
    try:
        return [r[0] for r in conn.execute("SELECT session_id FROM session_owners WHERE owner = ?", (user,)).fetchall()]
    finally:
        conn.close()


def claim_existing_sessions_for_main(session_ids):
    """Called once a second account exists: every chat that was already there is the main account's."""
    main = main_name()
    if not main:
        return
    conn = _conn()
    try:
        conn.executemany("INSERT OR IGNORE INTO session_owners (session_id, owner) VALUES (?, ?)", [(sid, main) for sid in session_ids])
        conn.commit()
    finally:
        conn.close()


# ---------- memory facts ----------

def record_fact(fact_id: int, user: str = None):
    conn = _conn()
    try:
        conn.execute("INSERT OR REPLACE INTO fact_owners (fact_id, owner) VALUES (?, ?)", (fact_id, acting_user() if user is None else user))
        conn.commit()
    finally:
        conn.close()


def fact_allowed(fact_id: int, user: str = None) -> bool:
    user = acting_user() if user is None else user
    conn = _conn()
    try:
        row = conn.execute("SELECT owner FROM fact_owners WHERE fact_id = ?", (fact_id,)).fetchone()
    finally:
        conn.close()
    return is_main(user) if row is None or row[0] == "" else row[0] == user


def own_facts(facts: list, user: str = None) -> list:
    return [f for f in facts if fact_allowed(f.get("id"), user)] if facts else []


def forget_fact(fact_id: int):
    conn = _conn()
    try:
        conn.execute("DELETE FROM fact_owners WHERE fact_id = ?", (fact_id,))
        conn.commit()
    finally:
        conn.close()


# ---------- which endpoints an account may use ----------

HOSTED_PROVIDERS = {"openai", "openrouter", "anthropic", "google"}


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _is_private_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith((".local", ".lan", ".internal", ".home", ".localdomain")):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False        # cannot be reached anyway
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return True
    return False


def shared_local_urls() -> set:
    """The local-engine endpoints the main account set up; every account may run models on them."""
    return {_norm_url(e.get("url")) for e in settings.load_settings().get("endpoints", []) if e.get("type") == "local" and e.get("url")}


def endpoint_kind(provider: str, url: str, user: str = None):
    """'online' (a hosted service somewhere else), 'local' (an engine on this network), or None when this account may not use it.
    The main account may use anything; it is only classified."""
    user = acting_user() if user is None else user
    provider, norm = (provider or ""), _norm_url(url)
    host = urlparse(url).hostname if url else ""
    if provider in HOSTED_PROVIDERS and not (norm and _is_private_host(host)):
        return "online"
    if provider == "custom" and norm and not _is_private_host(host):
        return "online"
    if is_main(user):
        return "local"
    if norm and norm in shared_local_urls() and provider in ("", "custom", "openai"):
        return "local"
    if not norm and provider == "":
        return "local"      # the server's default engine, the same one every account shares
    return None


def is_online(provider: str, url: str) -> bool:
    return endpoint_kind(provider, url, user=main_name()) == "online"


# ---------- each account's private folder and workspaces ----------

USERS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_data")


def user_dir(user: str) -> str:
    safe = re.sub(r"[^a-z0-9._-]", "_", (user or "").lower())
    return os.path.join(USERS_ROOT, safe)


def workspace_root(user: str = None) -> str:
    """Where an account without full access may keep workspaces."""
    path = os.path.join(user_dir(acting_user() if user is None else user), "workspaces")
    os.makedirs(path, exist_ok=True)
    return path


def workspace_allowed(path: str, user: str = None) -> bool:
    """May this account bind this folder as a workspace? Accounts with full access may use any folder; the others only folders
    inside their own workspaces folder. An empty path (no workspace) is always fine."""
    if not path:
        return True
    user = acting_user() if user is None else user
    if can_use_whole_host(user):
        return True
    root = os.path.realpath(workspace_root(user))
    target = os.path.realpath(path)
    return target == root or target.startswith(root + os.sep)


# ---------- Athena's own delegation chat (one per account) ----------

BOTS_SESSION_BASE = "athena-bots-agent"


def resolve_session(session_id: str, user: str = None) -> str:
    """The page always asks for the one delegation chat by its fixed name; each account except the main one has its own."""
    user = acting_user() if user is None else user
    if session_id == BOTS_SESSION_BASE and not is_main(user):
        return f"{BOTS_SESSION_BASE}--{user}"
    return session_id


def is_bots_session(session_id: str) -> bool:
    return session_id == BOTS_SESSION_BASE or (session_id or "").startswith(BOTS_SESSION_BASE + "--")


def workspace_guard(workspace: str = ""):
    """FastAPI dependency for the file routes: a request naming a workspace this account may not use is refused."""
    from fastapi import HTTPException
    if workspace and not workspace_allowed(workspace):
        raise HTTPException(status_code=403, detail="This account can only use workspaces inside its own folder.")
