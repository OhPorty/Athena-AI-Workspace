"""
Athena — a lean, LCM-backed chat/agent workspace with voice support.
"""
import os
import subprocess
import sqlite3
import io
import json
import sys
import signal
import atexit
import ctypes
import tempfile
import time
import threading
from typing import Optional
import httpx
import psutil
from fastapi import FastAPI, UploadFile, File, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from auth import AuthManager
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

LCM_URL = os.environ.get("ATHENA_LCM_URL", "http://localhost:8421")

# --- Bundled LCM (Lossless Context Management) service ---
# Vendored as a project-local dependency (not a system-wide service) so
# Athena is self-contained and portable if it's ever shared. Launched as
# a subprocess on startup, on its own port (8421, distinct from the old
# shared instance at 8420 which stays untouched for whatever else may
# still use it), with a fresh, empty database -- no migration from any
# prior LCM data.
_LCM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lcm")
_lcm_process = None

def _set_pdeathsig():
    # Linux-only: asks the kernel to send SIGTERM to this child if its
    # parent (Athena) dies for ANY reason, including a crash or kill -9,
    # so the bundled LCM can never outlive Athena as an orphaned process.
    PR_SET_PDEATHSIG = 1
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass

def _stop_bundled_lcm():
    global _lcm_process
    if _lcm_process and _lcm_process.poll() is None:
        print("[Athena] Stopping bundled LCM...", flush=True)
        _lcm_process.terminate()
        try:
            _lcm_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _lcm_process.kill()

def _handle_shutdown_signal(signum, frame):
    _stop_bundled_lcm()
    sys.exit(0)

def _start_bundled_lcm():
    global _lcm_process
    lcm_port = os.environ.get("LCM_PORT", "8421")
    env = os.environ.copy()
    env["LCM_PORT"] = lcm_port
    env["LCM_DB_PATH"] = os.path.join(_LCM_DIR, "lcm.db")
    _lcm_process = subprocess.Popen(
        [sys.executable, os.path.join(_LCM_DIR, "server.py")],
        env=env,
        cwd=_LCM_DIR,
        preexec_fn=_set_pdeathsig,
    )
    atexit.register(_stop_bundled_lcm)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    for _ in range(30):
        try:
            httpx.get(f"http://localhost:{lcm_port}/messages/__athena_startup_check__", timeout=1)
            print(f"[Athena] Bundled LCM ready on port {lcm_port}", flush=True)
            return
        except httpx.ConnectError:
            time.sleep(0.3)
    print("[Athena] WARNING: bundled LCM did not become reachable in time", flush=True)
OLLAMA_URL = os.environ.get("ATHENA_OLLAMA_URL", "http://localhost:11434/api/chat")
DEFAULT_MODEL = os.environ.get("ATHENA_DEFAULT_MODEL", "gpt-oss-20b-32k:latest")
WHISPER_MODEL_SIZE = os.environ.get("ATHENA_WHISPER_MODEL", "large-v3-turbo")
PIPER_VOICE_PATH = os.environ.get("ATHENA_PIPER_VOICE_PATH", "/home/ohporty/athena/voices/en_US-lessac-medium.onnx")

app = FastAPI(title="Athena")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------------------------------------------------------------------------
# Authentication -- single-user login gate. Adapted from Odysseus's proven
# pattern: an outermost ASGI middleware rejects/redirects every request
# except a small explicit exemption list, so nothing (no page, no API route)
# is reachable without a valid session, matching Odysseus's own "0 UI access
# without login" behavior.
# ---------------------------------------------------------------------------
auth_manager = AuthManager(os.path.join(os.path.dirname(os.path.abspath(__file__)), "athena_auth.json"))
SESSION_COOKIE = "athena_auth_token"

AUTH_EXEMPT_EXACT = {
    "/login",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/verify-2fa",
    "/api/auth/status",
    "/health",
}
AUTH_EXEMPT_PREFIXES = ["/static"]

def _is_auth_exempt(path: str) -> bool:
    if path in AUTH_EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES)

_PROXY_FWD_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "forwarded")

def _is_trusted_loopback(request: Request) -> bool:
    """True only for a DIRECT loopback connection with no proxy/tunnel
    forwarding headers -- a bare client.host check alone is unsafe
    behind Caddy's reverse_proxy (which also connects from 127.0.0.1),
    since that would let an external request routed through Caddy
    inherit the same trust as Pi's own genuine direct-loopback calls.
    Caddy adds X-Forwarded-* headers automatically; Pi's own in-process
    HTTP calls to Athena never do."""
    host = request.client.host if request.client else None
    if host not in ("127.0.0.1", "::1"):
        return False
    for h in _PROXY_FWD_HEADERS:
        if request.headers.get(h):
            return False
    return True

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_auth_exempt(path):
            return await call_next(request)
        # Internal-service bypass: lets Pi's own coding-agent process
        # call back into Athena (e.g. /v1/chat/completions, which Pi
        # uses for its own model completions) without a browser
        # session -- Pi has no way to hold one. Pi already sends this
        # exact bearer token automatically via its own models.json
        # config (apiKey: "athena"), so nothing on Pi's side needs to
        # change. Gated on genuine direct loopback, not just the token,
        # since the token alone (the app's own name) isn't a strong
        # secret on its own.
        auth_header = request.headers.get("authorization", "")
        if auth_header == "Bearer athena" and _is_trusted_loopback(request):
            return await call_next(request)
        if not auth_manager.is_configured:
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"error": "Setup required"})
            return RedirectResponse(url="/login", status_code=302)
        token = request.cookies.get(SESSION_COOKIE)
        if not auth_manager.validate_token(token):
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"error": "Not authenticated"})
            return RedirectResponse(url="/login", status_code=302)
        request.state.current_user = auth_manager.username
        return await call_next(request)

app.add_middleware(AuthMiddleware)

class AuthSetupIn(BaseModel):
    username: str
    password: str

class AuthLoginIn(BaseModel):
    username: str
    password: str

class Auth2FAVerifyIn(BaseModel):
    pending_token: str
    code: str

class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str

class Totp2FAConfirmIn(BaseModel):
    code: str

class Totp2FADisableIn(BaseModel):
    password: str

def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        key=SESSION_COOKIE, value=token, max_age=60 * 60 * 24 * 30,
        httponly=True, secure=True, samesite="lax",
    )

@app.get("/login", response_class=HTMLResponse)
def login_page():
    with open(os.path.join("static", "login.html")) as f:
        return f.read()

@app.get("/api/auth/status")
def auth_status(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    return auth_manager.status(token)

@app.post("/api/auth/setup")
def auth_setup(req: AuthSetupIn, response: Response):
    if auth_manager.is_configured:
        return JSONResponse(status_code=400, content={"error": "Already configured"})
    if not auth_manager.setup(req.username, req.password):
        return JSONResponse(status_code=400, content={"error": f"Setup failed -- username required, password must be at least {auth_manager.policy()['password_min_length']} characters"})
    token = auth_manager.create_session()
    _set_session_cookie(response, token)
    return {"ok": True}

@app.post("/api/auth/login")
def auth_login(req: AuthLoginIn, response: Response):
    if not auth_manager.verify_password(req.username, req.password):
        return JSONResponse(status_code=401, content={"error": "Invalid credentials"})
    if auth_manager.totp_enabled:
        pending_token = auth_manager.create_pending_2fa()
        return {"requires_totp": True, "pending_token": pending_token}
    token = auth_manager.create_session()
    _set_session_cookie(response, token)
    return {"ok": True}

@app.post("/api/auth/verify-2fa")
def auth_verify_2fa(req: Auth2FAVerifyIn, response: Response):
    if not auth_manager.consume_pending_2fa(req.pending_token):
        return JSONResponse(status_code=401, content={"error": "Login expired -- please try again"})
    if not auth_manager.totp_verify(req.code):
        return JSONResponse(status_code=401, content={"error": "Invalid code"})
    token = auth_manager.create_session()
    _set_session_cookie(response, token)
    return {"ok": True}

@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth_manager.revoke_token(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}

@app.post("/api/auth/change-password")
def auth_change_password(req: ChangePasswordIn):
    if not auth_manager.change_password(req.current_password, req.new_password):
        return JSONResponse(status_code=400, content={"error": "Current password incorrect, or new password too short"})
    return {"ok": True}

@app.post("/api/auth/2fa/setup")
def auth_2fa_setup():
    secret = auth_manager.totp_generate_secret()
    if not secret:
        return JSONResponse(status_code=400, content={"error": "Account not configured"})
    return {"secret": secret, "otpauth_uri": auth_manager.totp_provisioning_uri(secret)}

@app.post("/api/auth/2fa/confirm")
def auth_2fa_confirm(req: Totp2FAConfirmIn):
    backup_codes = auth_manager.totp_confirm_enable(req.code)
    if backup_codes is None:
        return JSONResponse(status_code=400, content={"error": "Invalid code"})
    return {"ok": True, "backup_codes": backup_codes}

@app.post("/api/auth/2fa/disable")
def auth_2fa_disable(req: Totp2FADisableIn):
    if not auth_manager.totp_disable(req.password):
        return JSONResponse(status_code=400, content={"error": "Incorrect password"})
    return {"ok": True}

BASE_SYSTEM_PROMPT = (
    "You are Athena, a helpful assistant. Be direct and concise."
)

AGENT_SYSTEM_SUFFIX = (
    " You have access to tools listed below when relevant -- use them "
    "rather than guessing when you need real information. When presenting "
    "structured or tabular data (schedules, forecasts, comparisons, lists "
    "of items with multiple fields each), format it as a real markdown "
    "table with | column | headers | -- not a run-on list of colon-separated "
    "values."
)

# Direct port of Odysseus's proven _pick_dynamic_ctx formula and step
# list (src/llm_core.py) -- same steps, same len(text)//3 + 16000
# headroom, same ceiling default. Not reinvented -- Odysseus's version
# is confirmed working in production; this mirrors it exactly rather
# than trusting a from-scratch reimplementation that kept landing at
# the wrong number in testing.
_CTX_STEPS = [4096, 16384, 32768, 65536, 131072]
MAX_CTX_DEFAULT = int(os.environ.get("ATHENA_MAX_CTX", "65536"))  # matches Odysseus's OLLAMA_NUM_CTX_OVERRIDE

def estimate_tokens(messages: list, tools: list = None) -> int:
    total_chars = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            total_chars += len(content)
    if tools:
        total_chars += len(json.dumps(tools))
    return max(1, total_chars // 4)

def pick_dynamic_ctx(messages: list, tools: list = None, max_ctx: int = MAX_CTX_DEFAULT) -> int:
    combined_text = ""
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            combined_text += content
    if tools:
        combined_text += json.dumps(tools)
    needed = len(combined_text) // 3 + 16000
    for step in _CTX_STEPS:
        if step >= needed and step <= max_ctx:
            return step
    return max_ctx


class ChatIn(BaseModel):
    session_id: str
    message: str
    model: str = DEFAULT_MODEL
    max_ctx: int = 0  # 0 = dynamic sizing; any other value is an explicit override
    workspace: str = ""  # empty = no workspace bound; file tools stay disabled
    endpoint_url: str = ""  # empty = use the default OLLAMA_URL
    search_url: str = ""  # SearXNG base URL; empty = web_search/web_fetch tools unavailable
    use_pi: bool = False  # route through the Pi coding harness instead of Athena's own chat loop; requires workspace to be set
    images: list = []  # base64-encoded image strings (no data: prefix), passed through to vision-capable models
    attachments: list = []  # [{"name": str, "content": str}] text-file attachments, inlined into the prompt


class TtsIn(BaseModel):
    text: str


def get_lcm_context(session_id: str) -> list:
    try:
        resp = httpx.get(f"{LCM_URL}/context/{session_id}", timeout=30)
        print(f"[DEBUG] LCM context status={resp.status_code} body={resp.text[:300]!r}", flush=True)
        if resp.status_code == 200:
            return resp.json().get("context", [])
    except Exception as e:
        print(f"[DEBUG] LCM context EXCEPTION: {e!r}", flush=True)
    return []


def get_lcm_tools() -> list:
    try:
        resp = httpx.get(f"{LCM_URL}/tools/schema", timeout=2)
        if resp.status_code == 200:
            return resp.json().get("tools", [])
    except Exception:
        pass
    return []


RATINGS_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratings.db")

def _ratings_conn():
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

@app.post("/api/rate")
def rate_message(req: RatingIn):
    """Upsert-or-delete semantics: re-rating or removing a vote always
    recomputes cleanly from current state, since /api/model-stats
    aggregates directly from whatever rows currently exist -- a changed
    or removed vote is automatically reflected with no separate
    increment/decrement bookkeeping to get wrong."""
    conn = _ratings_conn()
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

@app.get("/api/ratings/{session_id}")
def get_ratings(session_id: str):
    """Returns {message_id: {rating, reason}} for a session, so the
    frontend can re-apply which button should show as selected after
    a reload -- ratings live in this DB, not in the message history
    LCM returns, since LCM has no concept of a 'rating' field."""
    conn = _ratings_conn()
    try:
        rows = conn.execute(
            "SELECT message_id, rating, reason FROM ratings WHERE session_id = ?",
            (session_id,)
        ).fetchall()
        return {str(r[0]): {"rating": r[1], "reason": r[2]} for r in rows}
    finally:
        conn.close()

@app.get("/api/model-stats")
def get_model_stats():
    """Aggregate up/down counts per model, for showing real quality
    signal on the Models page -- computed fresh from current rows every
    call, so it's always correct even after votes change or get removed."""
    conn = _ratings_conn()
    try:
        rows = conn.execute("""
            SELECT model,
                   SUM(CASE WHEN rating = 'up' THEN 1 ELSE 0 END) as up_count,
                   SUM(CASE WHEN rating = 'down' THEN 1 ELSE 0 END) as down_count
            FROM ratings
            GROUP BY model
        """).fetchall()
        return {r[0]: {"up": r[1], "down": r[2]} for r in rows}
    finally:
        conn.close()


def send_to_lcm(session_id: str, role: str, content: str, model: str = None, has_image: bool = False):
    """Returns the real LCM message ID on success, or None on failure --
    the ID is what ratings attach to, since it's the one stable
    identifier that survives across page reloads and session history
    reloads (unlike a frontend array index, which is meaningless once
    messages get re-fetched from LCM in a different order/subset).

    Timeout is deliberately generous (not the usual few seconds) because
    LCM runs auto-compaction synchronously on every /message call, which
    can call out to an LLM for summarization -- a short timeout here was
    silently dropping the returned ID on longer messages, which broke
    both rating-attachment and per-message model tracking without any
    visible error."""
    try:
        resp = httpx.post(f"{LCM_URL}/message", json={
            "session_id": session_id,
            "role": role,
            "content": content,
            "service": "athena",
            "model": model,
            "has_image": has_image,
        }, timeout=30)
        if resp.status_code == 200:
            return resp.json().get("id")
        print(f"[Athena] send_to_lcm got HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
    except Exception as e:
        print(f"[Athena] send_to_lcm failed: {e}", flush=True)
    return None


@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html") as f:
        return f.read()


class DetectModelsIn(BaseModel):
    url: str

@app.post("/api/detect-models")
def detect_models(req: DetectModelsIn):
    base = req.url.rstrip("/")
    try:
        resp = httpx.get(f"{base}/api/tags", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            names = [m.get("name") for m in data.get("models", []) if m.get("name")]
            return {"models": names}
        return {"error": f"Endpoint responded with HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"Could not reach endpoint: {e}"}


@app.get("/api/workspace/browse")
def browse_workspace(path: str = ""):
    """List subdirectories at a given path, for the workspace picker.
    Defaults to the user's home directory. Only lists directories --
    this is for choosing a workspace ROOT, not general file browsing
    (that's what the model's own list_files tool is for, once a
    workspace is actually bound)."""
    target = os.path.realpath(path) if path else os.path.expanduser("~")
    if not os.path.isdir(target):
        return {"error": f"Not a directory: {target}"}
    try:
        dirs = sorted([
            name for name in os.listdir(target)
            if os.path.isdir(os.path.join(target, name)) and not name.startswith(".")
        ], key=str.lower)
    except PermissionError:
        return {"error": f"Permission denied: {target}"}
    parent = os.path.dirname(target) if target != "/" else None
    return {"path": target, "parent": parent, "directories": dirs}


class MkdirIn(BaseModel):
    path: str
    name: str

@app.post("/api/workspace/mkdir")
def mkdir_workspace(req: MkdirIn):
    """Create a new directory inside the given path, for the workspace
    picker's 'New Folder' action."""
    if not req.name or "/" in req.name or req.name in (".", ".."):
        return {"error": "Invalid folder name."}
    base = os.path.realpath(req.path)
    if not os.path.isdir(base):
        return {"error": f"Not a directory: {base}"}
    new_dir = os.path.join(base, req.name)
    try:
        os.makedirs(new_dir, exist_ok=False)
        return {"path": new_dir}
    except FileExistsError:
        return {"error": "A folder with that name already exists."}
    except Exception as e:
        return {"error": f"Could not create folder: {e}"}


@app.get("/api/workspace/files")
def list_workspace_files(workspace: str, path: str = ""):
    """List files and directories (with sizes) at a path relative to the
    given workspace root, for the file-browser panel. Reuses
    _resolve_workspace_path so the UI browser shares the same confinement
    boundary as the agent's list_files/read_file/write_file/edit_file tools."""
    if not workspace:
        return {"error": "No workspace set."}
    try:
        target = _resolve_workspace_path(workspace, path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(target):
        return {"error": f"Not a directory: {path}"}
    try:
        entries = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            is_dir = os.path.isdir(full)
            try:
                size = None if is_dir else os.path.getsize(full)
            except OSError:
                size = None
            entries.append({"name": name, "type": "dir" if is_dir else "file", "size": size})
        return {"path": path, "entries": entries}
    except PermissionError:
        return {"error": f"Permission denied: {path}"}


@app.get("/api/workspace/read")
def read_workspace_file(workspace: str, path: str = ""):
    """Read a file's content for the file-browser panel's preview,
    reusing the same _read_file implementation the agent's read_file
    tool uses."""
    if not workspace:
        return {"error": "No workspace set."}
    return _read_file(workspace, path)


WEB_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web and automatically fetch readable content from the top results -- returns full page text, not just snippets, so you usually don't need a separate web_fetch call after this. Use for anything current/external not already in context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and extract readable text from a specific URL. Use after web_search to read a promising result, or when given a URL directly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
]

def _web_search(search_url: str, query: str):
    """Search then auto-fetch the top results concurrently, keeping only
    the ones that actually parsed to real content -- ports Odysseus's
    proven approach (services/search/core.py comprehensive_web_search):
    the model never has to decide "try another URL", since failed/blocked
    fetches are silently filtered out before it ever sees the response.
    This is what fixed Odysseus's own indecisive-looping problem."""
    try:
        resp = httpx.get(f"{search_url.rstrip('/')}/search", params={"q": query, "format": "json"}, timeout=15)
        if resp.status_code != 200:
            return {"error": f"SearXNG responded with HTTP {resp.status_code}"}
        results = resp.json().get("results", [])[:5]
        if not results:
            return {"error": "No search results found."}

        import concurrent.futures
        fetched = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_result = {
                executor.submit(_web_fetch, r.get("url", "")): r
                for r in results if r.get("url")
            }
            for future in concurrent.futures.as_completed(future_to_result):
                r = future_to_result[future]
                try:
                    fetch_result = future.result()
                    if "content" in fetch_result and fetch_result["content"]:
                        fetched.append({
                            "title": r.get("title"),
                            "url": r.get("url"),
                            "content": fetch_result["content"][:2500],
                        })
                except Exception:
                    pass  # a single failed fetch shouldn't fail the whole search

        if not fetched:
            # Every candidate failed to fetch -- fall back to snippets alone
            # rather than returning nothing at all.
            return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")} for r in results]

        return fetched
    except Exception as e:
        return {"error": f"Search failed: {e}"}

def _web_fetch(url: str):
    """Fetch and extract clean readable text -- uses BeautifulSoup for
    real HTML parsing (same technique Odysseus uses), not a regex
    tag-strip. A regex-only approach leaves nav/ad/script text mixed
    into the output, which was confusing local models into treating
    real content as unreliable. Structural elements (lists, headings)
    get separators so the model can still parse a readable shape."""
    try:
        from bs4 import BeautifulSoup
        resp = httpx.get(url, timeout=15, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; AthenaBot/1.0)"
        })
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        text = soup.get_text(separator="\n", strip=True)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        text = "\n".join(lines)

        if not text:
            return {"error": f"web_fetch: {url}: no readable text content (page may need JS)"}

        header = f"# {title}\nSource: {url}\n\n" if title else f"Source: {url}\n\n"
        output = header + text
        return {"url": url, "content": output[:6000]}
    except Exception as e:
        return {"error": f"Fetch failed: {e}"}


FILE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories at a path within the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to workspace root. Use '.' for the root."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to workspace root."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file, or overwrite an existing one entirely. Use edit_file instead if you only need to change part of an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace root."},
                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one exact occurrence of text in an existing file. old_text must match uniquely -- include enough surrounding context if the text could appear more than once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace root."},
                    "old_text": {"type": "string", "description": "Exact text to find and replace. Must appear exactly once in the file."},
                    "new_text": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
]

def _resolve_workspace_path(workspace: str, rel_path: str) -> str:
    """Resolve a model-supplied relative path against the workspace root,
    refusing to ever resolve outside it. This is the actual safety
    boundary for file tools -- without it, a path like '../../etc/passwd'
    or an absolute path would let the model read/write anywhere on the
    filesystem the Athena process has access to, not just the intended
    workspace directory."""
    workspace_root = os.path.realpath(workspace)
    candidate = os.path.realpath(os.path.join(workspace_root, rel_path or "."))
    if candidate != workspace_root and not candidate.startswith(workspace_root + os.sep):
        raise ValueError(f"Path '{rel_path}' resolves outside the workspace, refusing.")
    return candidate

def _list_files(workspace: str, rel_path: str):
    try:
        target = _resolve_workspace_path(workspace, rel_path)
        if not os.path.isdir(target):
            return {"error": f"Not a directory: {rel_path}"}
        entries = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            entries.append({"name": name, "type": "dir" if os.path.isdir(full) else "file"})
        return {"path": rel_path, "entries": entries}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"list_files failed: {e}"}

def _read_file(workspace: str, rel_path: str):
    try:
        target = _resolve_workspace_path(workspace, rel_path)
        if not os.path.isfile(target):
            return {"error": f"Not a file: {rel_path}"}
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"path": rel_path, "content": content[:20000]}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"read_file failed: {e}"}

def _write_file(workspace: str, rel_path: str, file_content: str):
    try:
        target = _resolve_workspace_path(workspace, rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(file_content)
        return {"path": rel_path, "written": True, "bytes": len(file_content)}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"write_file failed: {e}"}

def _edit_file(workspace: str, rel_path: str, old_text: str, new_text: str):
    try:
        target = _resolve_workspace_path(workspace, rel_path)
        if not os.path.isfile(target):
            return {"error": f"Not a file: {rel_path}"}
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        count = content.count(old_text)
        if count == 0:
            return {"error": "old_text not found in file"}
        if count > 1:
            return {"error": f"old_text appears {count} times -- must be unique, add more context"}
        content = content.replace(old_text, new_text)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return {"path": rel_path, "edited": True}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"edit_file failed: {e}"}


# Self-contained Pi config lives inside the Athena repo (not the user's
# personal ~/.pi/agent, which may be shared with other unrelated Pi
# projects) -- keeps the whole coding-harness setup portable: clone
# Athena elsewhere and this directory comes with it.
PI_AGENT_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi-agent-config")
PI_MODELS_JSON_PATH = os.path.join(PI_AGENT_CONFIG_DIR, "models.json")

def _guess_thinking_format(model_name: str) -> dict:
    """Heuristic mapping from model name to Pi's compat.thinkingFormat,
    based on Pi's own documented format list (qwen, deepseek, zai, etc).
    Only qwen is confirmed tested tonight -- others are best-effort so
    a new model at least has a chance of getting real thinking output
    instead of silently getting none."""
    name = model_name.lower()
    if "qwen" in name:
        return {"thinkingFormat": "qwen"}
    if "deepseek" in name:
        return {"thinkingFormat": "deepseek"}
    return {}

def _ensure_pi_model_registered(model_name: str):
    """Make sure Pi's models.json has an entry for whatever model is
    currently selected in Athena, so Pi always works with 'whatever
    model we have selected for that message' instead of requiring
    every model to be manually pre-added to a static config file.
    Runs before every Pi invocation -- cheap (small JSON file) and
    keeps the two systems in sync automatically."""
    try:
        with open(PI_MODELS_JSON_PATH) as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {"providers": {}}

    athena_provider = config.setdefault("providers", {}).setdefault("athena", {
        "baseUrl": "http://localhost:9500/v1",
        "api": "openai-completions",
        "apiKey": "athena",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "models": [],
    })
    models = athena_provider.setdefault("models", [])

    # Prune entries for models that no longer actually exist in Ollama.
    # The old version of this function only ever appended -- it never
    # removed anything -- so every model ever used stayed listed
    # forever, even long after being deleted from Ollama, silently
    # looking like a hardcoded/stale list even though it was
    # technically auto-updating the whole time.
    try:
        ollama_base = OLLAMA_URL.rsplit("/api/", 1)[0]
        tags_resp = httpx.get(f"{ollama_base}/api/tags", timeout=3)
        live_model_names = {m["name"] for m in tags_resp.json().get("models", [])}
        models[:] = [m for m in models if m.get("id") in live_model_names or m.get("id") == model_name]
    except Exception as e:
        print(f"[Athena] Couldn't verify live Ollama models for Pi sync, leaving list as-is: {e}", flush=True)

    for m in models:
        if m.get("id") == model_name:
            return  # already registered

    models.append({
        "id": model_name,
        "name": f"{model_name} (dynamic ctx via Athena)",
        "reasoning": True,
        "contextWindow": MAX_CTX_DEFAULT,
        "maxTokens": 8192,
        "compat": _guess_thinking_format(model_name),
    })

    with open(PI_MODELS_JSON_PATH, "w") as f:
        json.dump(config, f, indent=2)


def _run_pi_agent(req: "ChatIn"):
    _ensure_pi_model_registered(req.model)
    """Stream a response via the Pi coding harness instead of Athena's
    own chat loop. Pi handles its own tool-calling and session memory
    (via --session-id, matching Athena's session_id so history persists
    across messages in the same chat) -- this function only translates
    Pi's JSON event stream into the same SSE shapes Athena's frontend
    already understands (delta/tool_start/tool_output/done), so the
    existing thinking panel and tool cards work unmodified."""
    pi_binary = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_modules", ".bin", "pi")
    cmd = [
        pi_binary,
        "--provider", "athena",
        "--model", req.model,
        "--mode", "json",
        "--session-id", req.session_id,
        "-p", req.message,
    ]

    def generate():
        full_reply = ""
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=req.workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env={**os.environ, "PI_CODING_AGENT_DIR": PI_AGENT_CONFIG_DIR},
            )
        except FileNotFoundError:
            yield f"data: {json.dumps({'delta': 'Pi binary not found -- check node_modules/.bin/pi exists.'})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
            return

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "message_update":
                ame = event.get("assistantMessageEvent", {})
                ame_type = ame.get("type")
                if ame_type == "text_delta":
                    delta = ame.get("delta", "")
                    full_reply += delta
                    yield f"data: {json.dumps({'delta': delta})}\n\n"
                elif ame_type == "thinking_delta":
                    yield f"data: {json.dumps({'thinking': ame.get('delta', '')})}\n\n"
                elif ame_type == "toolcall_start":
                    yield f"data: {json.dumps({'type': 'tool_start', 'tool': ame.get('toolName', 'unknown')})}\n\n"

            elif etype == "tool_execution_end":
                yield f"data: {json.dumps({'type': 'tool_output', 'tool': event.get('toolName', 'unknown'), 'output': event.get('result')})}\n\n"

            elif etype == "agent_settled":
                proc.wait()
                _msg_id = send_to_lcm(req.session_id, "assistant", full_reply, model=req.model)
                yield f"data: {json.dumps({'done': True, 'assistant_message_id': _msg_id})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


_last_activity_ts = time.time()

@app.post("/api/chat")
def chat_stream(req: ChatIn):
    global _last_activity_ts
    _last_activity_ts = time.time()
    if req.use_pi:
        if not req.workspace:
            def _no_workspace_error():
                yield f"data: {json.dumps({'delta': 'Pi requires a workspace to be set -- pick one from the workspace pill first.'})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
            return StreamingResponse(_no_workspace_error(), media_type="text/event-stream")
        send_to_lcm(req.session_id, "user", req.message)
        return _run_pi_agent(req)

    print(f"[DEBUG] model={req.model!r} endpoint_url={req.endpoint_url!r}", flush=True)
    effective_message = req.message
    for att in req.attachments:
        att_content = (att.get("content") or "")[:20000]
        effective_message += f"\n\n[Attached file: {att.get('name', 'file')}]\n```\n{att_content}\n```"
    _user_msg_id = send_to_lcm(req.session_id, "user", effective_message, has_image=bool(req.images))
    if _user_msg_id is None:
        # Don't silently generate a response for a message that was
        # never actually persisted -- that's exactly the confusing
        # failure mode where the user gets a real-looking reply in the
        # moment, then it vanishes on the next reload with no
        # indication anything went wrong.
        def _save_failed_error():
            yield f"data: {json.dumps({'delta': 'Your message could not be saved -- Athena\'s memory service is unreachable right now. Please try again in a moment.'})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        return StreamingResponse(_save_failed_error(), media_type="text/event-stream")

    context = get_lcm_context(req.session_id)

    # Perpetual agent mode -- one tool set, always available. LCM's recall
    # tools are always on; file write/edit tools (once the Pi bridge is
    # wired in) will only be included here when req.workspace is non-empty,
    # so the model can never edit files on a session with no workspace
    # bound to it, regardless of how it reads an ambiguous prompt.
    system_prompt = BASE_SYSTEM_PROMPT + AGENT_SYSTEM_SUFFIX + _get_memory_context()
    tools = get_lcm_tools()
    if req.search_url:
        tools = tools + WEB_TOOL_SCHEMAS
    if req.workspace:
        tools = tools + FILE_TOOL_SCHEMAS

    _raw_messages = [{"role": "system", "content": system_prompt}] + context
    # Some chat templates (e.g. qwen3.5's) require every system-role
    # message to be grouped at the very start of the conversation --
    # LCM returns one separate system message per summary node, which
    # otherwise ends up interleaved with real user/assistant turns and
    # breaks that requirement. Merge them into a single leading system
    # message, preserving order, then keep everything else as-is.
    _system_parts = [m["content"] for m in _raw_messages if m.get("role") == "system"]
    _non_system = [m for m in _raw_messages if m.get("role") != "system"]
    messages = [{"role": "system", "content": "\n\n".join(_system_parts)}] + _non_system
    if req.images:
        for _m in reversed(messages):
            if _m.get("role") == "user":
                _m["images"] = req.images
                break
    effective_max = req.max_ctx if req.max_ctx > 0 else MAX_CTX_DEFAULT
    ctx_size = pick_dynamic_ctx(messages, tools, max_ctx=effective_max)
    if req.max_ctx > 0:
        ctx_size = req.max_ctx
    prompt_tokens = estimate_tokens(messages, tools)

    def _execute_tool_call(tool_call):
        """Dispatch a single tool call. Right now only LCM's own tools
        (lcm_recall_search, lcm_recall_expand) exist -- forward those to
        LCM's generic /tools/call endpoint. Anything else currently has
        nowhere to go and returns a clear error rather than failing silently."""
        fn = tool_call.get("function", {})
        name = fn.get("name", "")
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if name == "web_search":
            return _web_search(req.search_url, args.get("query", ""))
        if name == "web_fetch":
            return _web_fetch(args.get("url", ""))
        if name == "list_files":
            return _list_files(req.workspace, args.get("path", "."))
        if name == "read_file":
            return _read_file(req.workspace, args.get("path", ""))
        if name == "write_file":
            return _write_file(req.workspace, args.get("path", ""), args.get("content", ""))
        if name == "edit_file":
            return _edit_file(req.workspace, args.get("path", ""), args.get("old_text", ""), args.get("new_text", ""))

        args["session_id"] = req.session_id
        try:
            resp = httpx.post(f"{LCM_URL}/tools/call", json={"name": name, "arguments": args}, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("result")
            return {"error": f"Tool call failed: HTTP {resp.status_code}"}
        except Exception as e:
            return {"error": f"Tool call failed: {e}"}

    def generate():
        yield f"data: {json.dumps({'user_message_id': _user_msg_id})}\n\n"
        full_reply = ""
        _target_url = (req.endpoint_url.rstrip("/") + "/api/chat") if req.endpoint_url else OLLAMA_URL
        _messages = list(messages)
        print(f"[DEBUG] _messages roles={[m.get("role") for m in _messages]!r}", flush=True)
        MAX_ROUNDS = 5
        last_eval_count = None
        last_eval_duration = None

        for _round in range(MAX_ROUNDS):
            round_reply = ""
            round_tool_calls = []

            with httpx.stream("POST", _target_url, json={
                "model": req.model,
                "messages": _messages,
                "tools": tools if tools else None,
                "options": {"num_ctx": ctx_size},
                "stream": True,
            }, timeout=180) as resp:
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = chunk.get("message", {})
                    print(f"[DEBUG] chunk={chunk}", flush=True)
                    thinking_delta = msg.get("thinking", "")
                    if thinking_delta:
                        yield f"data: {json.dumps({'thinking': thinking_delta})}\n\n"
                    delta = msg.get("content", "")
                    if delta:
                        round_reply += delta
                        full_reply += delta
                        yield f"data: {json.dumps({'delta': delta})}\n\n"
                    if msg.get("tool_calls"):
                        round_tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done"):
                        last_eval_count = chunk.get("eval_count")
                        last_eval_duration = chunk.get("eval_duration")
                        break

            if not round_tool_calls:
                _msg_id = send_to_lcm(req.session_id, "assistant", full_reply, model=req.model)
                tokens_per_sec = round(last_eval_count / (last_eval_duration / 1e9), 1) if last_eval_count and last_eval_duration else None
                yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'tokens_per_sec': tokens_per_sec, 'assistant_message_id': _msg_id})}\n\n"
                return

            # Model wants to call tool(s) -- execute each, tell the
            # frontend what's happening, then loop back with results
            # appended so the model can use them for its next turn.
            _messages.append({"role": "assistant", "content": round_reply, "tool_calls": round_tool_calls})
            for tc in round_tool_calls:
                tool_name = tc.get("function", {}).get("name", "unknown")
                yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name})}\n\n"
                result = _execute_tool_call(tc)
                yield f"data: {json.dumps({'type': 'tool_output', 'tool': tool_name, 'output': result})}\n\n"
                _messages.append({"role": "tool", "content": json.dumps(result)})
        else:
            fallback_msg = "I wasn't able to settle on an answer after several tool calls -- the search results may be inconsistent or the page I need isn't easily fetchable. Try rephrasing, or ask me to check a specific source directly."
            full_reply = fallback_msg
            yield f"data: {json.dumps({'delta': fallback_msg})}\n\n"
            _msg_id = send_to_lcm(req.session_id, "assistant", full_reply, model=req.model)
            tokens_per_sec = round(last_eval_count / (last_eval_duration / 1e9), 1) if last_eval_count and last_eval_duration else None
            yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'tokens_per_sec': tokens_per_sec, 'note': 'max tool rounds reached', 'assistant_message_id': _msg_id})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Voice: faster-whisper (local, free) for STT, Piper (local, free, ONNX) for TTS
# ---------------------------------------------------------------------------
_whisper_model = None
_piper_voice = None

def _get_piper_voice():
    global _piper_voice
    if _piper_voice is None:
        from piper import PiperVoice
        if not os.path.exists(PIPER_VOICE_PATH):
            raise FileNotFoundError(
                f"Piper voice model not found at {PIPER_VOICE_PATH}. "
                f"Download one from https://github.com/rhasspy/piper/releases "
                f"or https://huggingface.co/rhasspy/piper-voices "
                f"(need both the .onnx file and its matching .onnx.json config)."
            )
        _piper_voice = PiperVoice.load(PIPER_VOICE_PATH)
    return _piper_voice

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cuda", compute_type="float16")
    return _whisper_model


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    try:
        model = _get_whisper_model()
    except Exception as e:
        return {"error": f"Whisper model failed to load: {e}"}

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        segments, _info = model.transcribe(tmp_path, beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments)
        return {"text": text}
    except Exception as e:
        return {"error": f"Transcription failed: {e}"}
    finally:
        os.unlink(tmp_path)


@app.post("/api/tts")
async def tts(req: TtsIn):
    import wave
    try:
        voice = _get_piper_voice()
    except Exception as e:
        return {"error": f"Piper TTS unavailable: {e}"}

    buf = io.BytesIO()
    try:
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize(req.text, wav_file)
    except Exception as e:
        return {"error": f"TTS synthesis failed: {e}"}

    buf.seek(0)
    return StreamingResponse(buf, media_type="audio/wav")


@app.get("/api/history/{session_id}")
def get_history(session_id: str):
    try:
        resp = httpx.get(f"{LCM_URL}/messages/{session_id}", timeout=3)
        if resp.status_code == 200:
            msgs = resp.json()
            return [{"role": m["role"], "content": m["content"], "messageId": m["id"], "model": m.get("model"), "hasImage": m.get("has_image", False)} for m in msgs]
    except Exception:
        pass
    return []

# ---------------------------------------------------------------------------
# OpenAI-compat translation proxy: lets the Pi coding harness (or any
# other OpenAI-compat client) get Athena's real dynamic ctx sizing.
# Pi's own provider config speaks OpenAI /v1/chat/completions, which has
# no field for num_ctx at all -- Ollama's OpenAI-compat shim silently
# ignores context sizing entirely, always falling back to whatever a
# model's Modelfile happens to default to (often just 4096). This proxy
# receives the OpenAI-shaped request, computes real ctx using the exact
# same pick_dynamic_ctx used for Athena's own chat, forwards to Ollama's
# NATIVE /api/chat with num_ctx set correctly, then translates the
# response back into OpenAI shape. Point Pi's base_url at this endpoint
# instead of Ollama directly and it gets proper dynamic ctx for free,
# with zero per-model Modelfile baking required.
# ---------------------------------------------------------------------------

def _normalize_message_content(messages: list) -> list:
    """OpenAI-format clients (like Pi) may send content as an array of
    blocks (e.g. [{"type": "text", "text": "..."}]) for multimodal
    support. Ollama's native API only accepts a plain string -- passing
    the array shape through unchanged causes a Go-side unmarshal error
    on Ollama's end, which surfaces as a silent {'error': ...} chunk
    with no finish_reason, confusing OpenAI-compat clients that expect
    a clean stream end.

    Real OpenAI spec also requires tool_calls[].function.arguments to
    be a JSON-encoded STRING (which is what a spec-correct client like
    Pi sends back in conversation history), but Ollama's native API
    outputs -- and expects -- that same field as a real object/dict.
    Forwarding the string form unchanged causes Ollama's Go parser to
    choke trying to unmarshal a string where it expects an object,
    surfacing as "Value looks like object, but can't find closing '}'
    symbol". Convert it back to an object before forwarding."""
    normalized = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            text_parts = [
                block.get("text", "") for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            m = {**m, "content": "\n".join(text_parts)}
        elif content is None:
            # Spec-correct OpenAI clients send content: null on an
            # assistant message that only carries tool_calls. Ollama's
            # Go struct expects content to always be a string; null
            # produces the same confusing "can't find closing brace"
            # class of parse error as the array-content case above.
            m = {**m, "content": ""}

        tool_calls = m.get("tool_calls")
        if tool_calls:
            fixed_calls = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    # A prior failed/malformed tool call can leave a
                    # non-object value (e.g. []) recorded in history.
                    # Ollama's Go struct requires an object here --
                    # coerce anything else to empty rather than
                    # crashing every subsequent request in the
                    # conversation on a single bad historical entry.
                    args = {}
                fixed_calls.append({**tc, "function": {**fn, "arguments": args}})
            m = {**m, "tool_calls": fixed_calls}

        normalized.append(m)
    return normalized


@app.post("/v1/chat/completions")
async def openai_compat_proxy(request: Request):
    body = await request.json()
    model = body.get("model", DEFAULT_MODEL)
    messages = _normalize_message_content(body.get("messages", []))
    tools = body.get("tools")
    stream = body.get("stream", False)

    _summary = []
    for i, m in enumerate(messages):
        tc = m.get("tool_calls")
        tc_info = ""
        if tc:
            for t in tc:
                args = t.get("function", {}).get("arguments")
                tc_info += f" tool_call_args_type={type(args).__name__}:{repr(args)[:80]}"
        _summary.append(f"[{i}] role={m.get('role')} content_type={type(m.get('content')).__name__}{tc_info}")
    print("[PROXY-DEBUG] message summary:\n" + "\n".join(_summary), flush=True)
    if tools:
        for t in tools:
            if t.get("function", {}).get("name") == "read":
                print(f"[PROXY-DEBUG] read tool schema={json.dumps(t, indent=2)}", flush=True)
    ctx_size = pick_dynamic_ctx(messages, tools)

    ollama_payload = {
        "model": model,
        "messages": messages,
        "options": {"num_ctx": ctx_size},
        "stream": stream,
    }
    if tools:
        ollama_payload["tools"] = tools

    if not stream:
        resp = httpx.post(OLLAMA_URL, json=ollama_payload, timeout=180)
        data = resp.json()
        msg = data.get("message", {})
        return {
            "id": "athena-proxy",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": msg.get("content", ""), "tool_calls": msg.get("tool_calls")},
                "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop",
            }],
        }

    def generate():
        with httpx.stream("POST", OLLAMA_URL, json=ollama_payload, timeout=180) as resp:
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                print(f"[PROXY-DEBUG] chunk={chunk}", flush=True)
                msg = chunk.get("message", {})

                delta = {}
                if msg.get("thinking"):
                    # De facto OpenAI-compat convention for reasoning
                    # models (vLLM, LiteLLM, etc): delta.reasoning_content.
                    # Without this, thinking tokens Ollama genuinely sends
                    # were silently dropped in translation -- Pi never
                    # even had a chance to surface them, regardless of
                    # any --thinking level or models.json config.
                    delta = {"reasoning_content": msg["thinking"]}
                elif msg.get("content"):
                    delta = {"content": msg["content"]}
                elif msg.get("tool_calls"):
                    # Real OpenAI streaming format requires each tool call to
                    # have a top-level "index" and function.arguments as a
                    # JSON-encoded STRING (clients accumulate it as string
                    # fragments across chunks). Ollama sends the whole call
                    # complete in one chunk with arguments as a real object
                    # and no top-level index -- forwarding that shape
                    # unchanged silently breaks spec-compliant streaming
                    # clients like Pi, which end up recording empty
                    # arguments despite the real data having been sent.
                    fixed_tool_calls = []
                    for i, tc in enumerate(msg["tool_calls"]):
                        fn = tc.get("function", {})
                        args = fn.get("arguments", {})
                        fixed_tool_calls.append({
                            "index": i,
                            "id": tc.get("id", f"call_{i}"),
                            "type": "function",
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": json.dumps(args) if isinstance(args, dict) else (args or ""),
                            },
                        })
                    delta = {"tool_calls": fixed_tool_calls}

                openai_chunk = {
                    "id": "athena-proxy",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": delta,
                        "finish_reason": ("tool_calls" if msg.get("tool_calls") else "stop") if chunk.get("done") else None,
                    }],
                }
                yield f"data: {json.dumps(openai_chunk)}\n\n"
                if chunk.get("done"):
                    yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


class MemoryIn(BaseModel):
    content: str

@app.get("/api/memory")
def list_memory():
    try:
        resp = httpx.get(f"{LCM_URL}/facts", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.post("/api/memory")
def add_memory(req: MemoryIn):
    try:
        resp = httpx.post(f"{LCM_URL}/facts", json={"content": req.content, "manual": True}, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.delete("/api/memory/{fact_id}")
def delete_memory(fact_id: int):
    try:
        resp = httpx.delete(f"{LCM_URL}/facts/{fact_id}", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.delete("/api/messages/{message_id}")
def delete_message(message_id: int):
    try:
        resp = httpx.delete(f"{LCM_URL}/messages/{message_id}", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

class SessionMetaIn(BaseModel):
    id: str
    label: str
    pinned: bool = False
    created_at: float

@app.get("/api/sessions")
def list_sessions_proxy():
    try:
        resp = httpx.get(f"{LCM_URL}/sessions", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.post("/api/sessions")
def upsert_session_proxy(req: SessionMetaIn):
    try:
        resp = httpx.post(f"{LCM_URL}/sessions", json=req.model_dump(), timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.delete("/api/history/{session_id}")
def delete_history(session_id: str):
    """Delete a chat session's messages and summary nodes from LCM --
    real data deletion, not just removing it from the sidebar list."""
    try:
        resp = httpx.delete(f"{LCM_URL}/session/{session_id}", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM delete failed: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM delete failed: {e}"}

def _get_gpu_stats():
    """Reads GPU utilization/memory straight from the driver via
    nvidia-smi -- deliberately NOT from any inference engine's own API,
    so this works the same whether the backend serving models is
    Ollama, llama.cpp, vLLM, or anything else. Returns None (not an
    error) on any non-NVIDIA machine so the UI can show 'No GPU
    detected' instead of breaking."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return None
        gpus = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 3:
                gpus.append({
                    "utilization_percent": float(parts[0]),
                    "memory_used_mb": float(parts[1]),
                    "memory_total_mb": float(parts[2]),
                })
        return gpus if gpus else None
    except Exception:
        return None

@app.get("/api/system/stats")
def system_stats():
    """Host-level resource stats -- RAM/CPU via psutil, GPU via
    nvidia-smi -- deliberately engine-agnostic. Which model/session is
    active is tracked client-side by Athena itself, not read from any
    particular inference engine's API."""
    vm = psutil.virtual_memory()
    cpu_percent = psutil.cpu_percent(interval=0.3)
    gpus = _get_gpu_stats()
    return {
        "ram": {"used_gb": round(vm.used / (1024 ** 3), 2), "total_gb": round(vm.total / (1024 ** 3), 2), "percent": vm.percent},
        "cpu_percent": cpu_percent,
        "gpus": gpus,
    }

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "athena_settings.json")
_settings_lock = threading.Lock()

def _load_settings():
    try:
        with open(_SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_settings(settings):
    """Atomic write (temp file + os.replace) so a crash or a
    concurrent write from another device mid-write can never leave
    behind a half-written, corrupted JSON file -- which previously
    silently made _load_settings() return {} (an unparseable file is
    caught by its broad except and treated as 'no settings yet'),
    making real saved data look like it had vanished entirely."""
    tmp_path = _SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(settings, f)
    os.replace(tmp_path, _SETTINGS_PATH)

class MemoryModelIn(BaseModel):
    model: Optional[str] = None

class SettingsIn(BaseModel):
    workspace: Optional[str] = None
    use_pi: Optional[bool] = None
    endpoints: Optional[list] = None
    search_url: Optional[str] = None
    default_model: Optional[dict] = None
    theme: Optional[dict] = None

@app.get("/api/settings")
def get_settings():
    return _load_settings()

@app.post("/api/settings")
def update_settings(req: SettingsIn):
    """Partial update -- only fields actually present in the request
    body get merged in, so one device saving just its workspace change
    can never accidentally wipe out another device's endpoints list or
    default model. Lock-guarded end to end (read, merge, write) so two
    devices saving at nearly the same moment serialize safely instead
    of one's update silently clobbering the other's."""
    with _settings_lock:
        settings = _load_settings()
        settings.update(req.model_dump(exclude_unset=True))
        _save_settings(settings)
        return settings

@app.get("/api/settings/memory-model")
def get_memory_model():
    return {"model": _load_settings().get("memory_extraction_model")}

@app.post("/api/settings/memory-model")
def set_memory_model(req: MemoryModelIn):
    settings = _load_settings()
    settings["memory_extraction_model"] = req.model
    _save_settings(settings)
    return {"model": req.model}

def _get_memory_context() -> str:
    """Formats current durable facts (from every session, not just this
    one) into a system-prompt block. This is the actual payoff of the
    whole memory-extraction pipeline -- without this, facts would just
    sit in LCM's database having zero effect on any conversation.
    Returns an empty string when there are no facts yet, so a fresh
    install with nothing learned behaves identically to before this
    feature existed."""
    try:
        resp = httpx.get(f"{LCM_URL}/facts", timeout=5)
        if resp.status_code != 200:
            return ""
        facts = resp.json()
    except Exception:
        return ""
    if not facts:
        return ""
    facts_list = "\n".join(f"- {f['content']}" for f in facts)
    return (
        "\n\nWhat you know about the user from PAST conversations "
        "(not just this one) -- treat this as settled background, not "
        "something to re-derive, question, or bring up unprompted "
        "unless it's actually relevant to what they're asking right now:\n"
        f"{facts_list}"
    )

_MEMORY_SCAN_INTERVAL_SECONDS = 60
_MEMORY_IDLE_THRESHOLD_SECONDS = 600  # 10 minutes

_MEMORY_EXTRACTION_SYSTEM_PROMPT = """You are a careful memory-extraction assistant. You are shown a chunk of recent conversation transcript, plus a list of facts already known about the user. Your ONLY job is to identify NEW, durable, genuinely important facts about the user that are NOT already covered by the existing facts list -- even if worded differently.

Rules:
- Only extract facts the user stated directly about themselves: identity, stated preferences, ongoing projects, explicit decisions. Never the assistant's own suggestions, and never a guess or inference the user didn't actually state.
- Skip anything trivial, one-off, or already covered by an existing fact in meaning, not just exact wording.
- When in doubt, extract NOTHING. A missed fact is cheap to catch later; a wrong or duplicate one is expensive since it pollutes every future conversation until someone notices and deletes it.
- Never invent facts not actually present in the transcript.

Respond with ONLY a JSON array of new fact strings, e.g. ["Works as a mechanical engineer", "Prefers dark roast coffee"]. If there is nothing new and genuinely worth keeping, respond with exactly: []"""

def _run_memory_scan(model: str):
    scan_state = httpx.get(f"{LCM_URL}/scan-state", timeout=10).json()
    last_id = scan_state.get("last_scanned_message_id", 0)

    new_messages = httpx.get(f"{LCM_URL}/messages/since/{last_id}", timeout=10).json()
    if not new_messages:
        return  # nothing new -- also naturally prevents re-scanning during a long idle stretch

    existing_facts = httpx.get(f"{LCM_URL}/facts", timeout=10).json()
    existing_facts_text = "\n".join(f"- {f['content']}" for f in existing_facts) or "(none yet)"
    transcript_text = "\n".join(f"[{m['role']}] {m['content']}" for m in new_messages)

    max_id_seen = max(m["id"] for m in new_messages)
    last_session_id = new_messages[-1]["session_id"]

    try:
        resp = httpx.post(OLLAMA_URL, json={
            "model": model,
            "messages": [
                {"role": "system", "content": _MEMORY_EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": f"Existing facts already known:\n{existing_facts_text}\n\nNew conversation since last scan:\n{transcript_text}"},
            ],
            "stream": False,
            "think": False,
        }, timeout=120)
        raw = resp.json()["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.strip("`").lstrip("json").strip()
        new_facts = json.loads(raw)
    except Exception as e:
        print(f"[Athena] Memory scan: extraction failed, will retry next cycle: {e}", flush=True)
        return  # deliberately do NOT advance scan-state, so this batch gets retried

    for fact_text in new_facts:
        if isinstance(fact_text, str) and fact_text.strip():
            httpx.post(f"{LCM_URL}/facts", json={
                "content": fact_text.strip(),
                "source_session_id": last_session_id,
                "source_message_id": max_id_seen,
                "manual": False,
            }, timeout=10)

    if new_facts:
        print(f"[Athena] Memory scan: added {len(new_facts)} new fact(s)", flush=True)
    httpx.post(f"{LCM_URL}/scan-state", json={"last_scanned_message_id": max_id_seen}, timeout=10)

def _memory_scan_loop():
    print("[Athena] Memory scan loop started (checks every 60s)", flush=True)
    while True:
        time.sleep(_MEMORY_SCAN_INTERVAL_SECONDS)
        try:
            model = _load_settings().get("memory_extraction_model")
            if not model:
                print("[Athena] Memory scan check: no model configured, skipping", flush=True)
                continue  # no model configured -- do nothing, no error
            idle_seconds = time.time() - _last_activity_ts
            if idle_seconds < _MEMORY_IDLE_THRESHOLD_SECONDS:
                remaining = int(_MEMORY_IDLE_THRESHOLD_SECONDS - idle_seconds)
                print(f"[Athena] Memory scan check: still active, {remaining}s until idle threshold", flush=True)
                continue  # still active, wait for a real idle window
            print(f"[Athena] Memory scan check: idle threshold reached, scanning with {model}", flush=True)
            _run_memory_scan(model)
        except Exception as e:
            print(f"[Athena] Memory scan loop error (will retry next cycle): {e}", flush=True)

@app.get("/health")
def health():
    return {"status": "ok"}


def _check_requirements():
    """Verify every package pinned in requirements.txt is actually
    installed before Athena tries to boot, so a missing dependency (e.g.
    someone cloning this fresh without running pip install -r
    requirements.txt) surfaces as one clear message here instead of a
    confusing traceback the first time some unrelated route imports it."""
    import importlib.metadata as _im
    req_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    if not os.path.exists(req_path):
        return
    missing = []
    mismatched = []
    with open(req_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "==" not in line:
                continue
            name, pinned_version = line.split("==", 1)
            try:
                installed_version = _im.version(name)
                if installed_version != pinned_version:
                    mismatched.append(f"{name} (have {installed_version}, requirements.txt has {pinned_version})")
            except _im.PackageNotFoundError:
                missing.append(name)
    if missing:
        print("=" * 70, flush=True)
        print("[Athena] Missing required packages:", ", ".join(missing), flush=True)
        print("[Athena] Run: pip install -r requirements.txt", flush=True)
        print("=" * 70, flush=True)
        sys.exit(1)
    if mismatched:
        print("[Athena] WARNING: version mismatch (may still work fine):", flush=True)
        for m in mismatched:
            print(f"  - {m}", flush=True)

def _check_pi_sandbox():
    """Read-only diagnostic: on Ubuntu 24.04+ (and other distros with
    AppArmor's unprivileged-userns restriction), Pi's bundled sandbox
    helper needs a scoped AppArmor profile to create the user namespace
    it sandboxes commands in -- without it, every Pi tool call fails
    with a confusing 'No such file or directory' several layers deep
    instead of the real cause. This never modifies anything or invokes
    sudo itself (Athena's own process shouldn't self-escalate); it just
    surfaces the real problem and the exact fix, computed fresh against
    THIS machine's actual architecture and paths so it stays correct
    wherever Athena is deployed, not just here. Prints a result either
    way (not just on failure) so this is visibly confirmed working
    rather than silently assumed."""
    import platform

    arch_map = {"x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}
    arch_dir = arch_map.get(platform.machine())
    if not arch_dir:
        print(f"[Athena] Pi sandbox check: skipped (unrecognized architecture {platform.machine()!r})", flush=True)
        return

    sandbox_root = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "pi-agent-config", "npm", "node_modules", "@carderne",
        "sandbox-runtime", "vendor", "seccomp",
    )
    binary_path = os.path.join(sandbox_root, arch_dir, "apply-seccomp")
    if not os.path.isfile(binary_path):
        print("[Athena] Pi sandbox check: skipped (Pi's sandbox isn't set up on this machine)", flush=True)
        return

    try:
        result = subprocess.run(
            [binary_path, "true"], capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        print(f"[Athena] WARNING: couldn't run Pi's sandbox helper to check it: {e}", flush=True)
        return

    if result.returncode == 0:
        print("[Athena] Pi sandbox check: OK -- user namespace creation works, no fix needed.", flush=True)
        return

    stderr = (result.stderr or "").strip()
    print("=" * 70, flush=True)
    print("[Athena] WARNING: Pi's sandbox helper can't create its required", flush=True)
    print("  user namespace on this system, so Pi tool calls will fail.", flush=True)
    print(f"  Real error: {stderr}", flush=True)
    if "userns" in stderr.lower() or "capability" in stderr.lower():
        print("  This is Ubuntu 24.04+'s AppArmor unprivileged-userns", flush=True)
        print("  restriction. Fix (requires sudo, run once):", flush=True)
        print(flush=True)
        profile_block = (
            "    sudo tee /etc/apparmor.d/athena-apply-seccomp > /dev/null << 'EOF'\n"
            "abi <abi/4.0>,\n"
            "include <tunables/global>\n"
            "\n"
            "profile athena-apply-seccomp " + binary_path + " flags=(unconfined) {\n"
            "  userns,\n"
            "  include if exists <local/athena-apply-seccomp>\n"
            "}\n"
            "EOF\n"
            "    sudo apparmor_parser -r /etc/apparmor.d/athena-apply-seccomp"
        )
        print(profile_block, flush=True)
    else:
        print("  Cause doesn't match the known AppArmor userns issue --", flush=True)
        print("  the real error above will need its own investigation.", flush=True)
    print("=" * 70, flush=True)

if __name__ == "__main__":
    import uvicorn
    import threading
    _check_requirements()
    _check_pi_sandbox()
    _start_bundled_lcm()
    threading.Thread(target=_memory_scan_loop, daemon=True).start()
    port = int(os.environ.get("ATHENA_PORT", "9500"))
    uvicorn.run(app, host="0.0.0.0", port=port)
