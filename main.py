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
import queue
from typing import Optional
import httpx
import psutil
from fastapi import FastAPI, UploadFile, File, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse, FileResponse
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

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_auth_exempt(path):
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
    "values. When you are asked to research a specific, named software "
    "library, SDK, or API -- not a general topic -- do not rely on a broad "
    "web search alone: that returns scattered blog posts and tutorials of "
    "unknown age that get blended together into answers matching no real "
    "version. Instead, fetch that project's own official repository or "
    "documentation site directly (e.g. its GitHub README, its docs site) "
    "and base your answer on that source. If the library has multiple "
    "language SDKs for the same protocol or spec (e.g. Python, TypeScript, "
    "Go), never mix a convention from one language's SDK into another "
    "language's example -- python-style decorators on bare functions are "
    "not valid syntax in JavaScript or TypeScript, and vice versa; verify "
    "each language's example separately against that language's own SDK "
    "docs. For every specific API claim you state as fact -- an import "
    "path, a class name, a function signature -- name which source you "
    "got it from, so a person reading your answer can verify it "
    "themselves rather than trusting it blindly. When you generate a "
    "dependency declaration for an external library -- requirements.txt, "
    "pyproject.toml, package.json, go.mod, or any similar file -- always "
    "pin to a specific version or a bounded range (e.g. mcp>=1.28,<2), "
    "never an open-ended minimum like mcp>=1.0.0. An unbounded dependency "
    "silently resolves to whatever is newest at install time, which may "
    "be a different major version than whatever API you actually "
    "researched and wrote code against -- exactly the kind of breaking "
    "change that makes generated code fail long after you wrote it. When "
    "you research a library's current API, note which version that API "
    "belongs to, and pin your generated dependency file to that version. "
    "When research is about building something that creates, writes, "
    "or generates files or other output, it must explicitly name the "
    "specific API, method, or function that performs that write (for "
    "example fs.writeFile, or a library's own save/create method) and "
    "show it actually being called in at least one real example -- "
    "never leave the actual creation step implied, described only in "
    "prose, or absent from every example while only reading, analyzing, "
    "or reacting to things gets demonstrated. And when you are not "
    "certain whether a capability is available or restricted in some "
    "environment or API, say that uncertainty plainly -- never state a "
    "confident-sounding restriction you have not actually confirmed "
    "from a real source, since a plausible but invented rule is often "
    "followed just as strictly as a true one, and is much harder to "
    "catch. When a task asks you to review, verify, check, or update "
    "something against 'the documentation' or another external source, "
    "and you notice you only have a summary, a compacted account, or a "
    "secondhand description of that source in your current context -- "
    "not the source itself -- you must actually fetch the real source "
    "before proceeding. Noticing that gap in your own reasoning and then "
    "continuing anyway with the lossy version defeats the entire point "
    "of checking. This applies even when the summary comes from earlier "
    "in this same conversation, including your own memory system's "
    "compacted context, since compaction can silently drop the exact "
    "specific detail that turns out to matter most. When something is "
    "built as a string or text blob representing some OTHER artifact -- "
    "generated code in a different language, a JSON or YAML config "
    "assembled via string concatenation, an HTML template, a SQL query "
    "built by interpolation, a shell script written out as text, "
    "anything where the actual product lives inside a string rather "
    "than being written directly as its own native syntax that gets "
    "parsed and checked as such -- the correctness of the code doing "
    "the building is a separate question from the correctness of what "
    "it actually produces. Validate both independently. Code that "
    "assembles such an artifact can be flawless in its own language "
    "while the artifact it produces is still broken, since that "
    "artifact lives inside strings, invisible to normal review, rather "
    "than as code your own tooling would ever check directly. STRICT RULE: for "
    "any task about a specific location within a file -- finding a word "
    "or pattern, identifying which line something is on, or viewing a "
    "particular line or range -- always use bash's grep -n or sed -n "
    "first, never read_file. read_file returns raw text with no line "
    "numbers at all, which forces you to manually count lines to answer "
    "any 'which line' question, and that counting is genuinely easy to "
    "get wrong. grep -n labels every match with its real line number "
    "directly, so no counting is ever needed and the answer can't be "
    "off by one. Only use read_file as a last resort, when you "
    "genuinely need a small file's full contents and grep/sed truly "
    "can't answer the question."
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
    needed = len(combined_text) // 3 + 48000
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
    """List subdirectories AND files at a given path, for both the
    workspace picker (directories only matter there, workspace roots
    can't be files) and the save-as-file picker (files are shown too,
    read-only, so the user can see what's already there and avoid an
    accidental overwrite). Defaults to the user's home directory."""
    target = os.path.realpath(path) if path else os.path.expanduser("~")
    if not os.path.isdir(target):
        return {"error": f"Not a directory: {target}"}
    try:
        entries = os.listdir(target)
        dirs = sorted([
            name for name in entries
            if os.path.isdir(os.path.join(target, name)) and not name.startswith(".")
        ], key=str.lower)
        files = sorted([
            name for name in entries
            if os.path.isfile(os.path.join(target, name)) and not name.startswith(".")
        ], key=str.lower)
    except PermissionError:
        return {"error": f"Permission denied: {target}"}
    parent = os.path.dirname(target) if target != "/" else None
    return {"path": target, "parent": parent, "directories": dirs, "files": files}


class MkdirIn(BaseModel):
    path: str
    name: str

class SaveNoteFileIn(BaseModel):
    path: str
    filename: str
    content: str

@app.post("/api/notes/save-to-file")
def save_note_to_file(req: SaveNoteFileIn):
    """Writes note content to an arbitrary filesystem location chosen
    via the same directory browser used for workspace selection --
    a general 'Save As', not scoped to any pre-configured workspace."""
    if not req.filename or "/" in req.filename or req.filename in (".", ".."):
        return {"error": "Invalid filename."}
    base = os.path.realpath(req.path)
    if not os.path.isdir(base):
        return {"error": f"Not a directory: {base}"}
    target = os.path.join(base, req.filename)
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"ok": True, "path": target}
    except Exception as e:
        return {"error": f"Could not save file: {e}"}

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

@app.delete("/api/workspace/delete")
def delete_workspace_entry(workspace: str, path: str = ""):
    """Deletes a file or directory (recursively) at a path within the
    given workspace, for the file browser panel's delete action --
    reuses the same _resolve_workspace_path confinement check every
    other file tool uses, so this can never delete anything outside
    the workspace regardless of what path is passed."""
    if not workspace:
        return {"error": "No workspace set."}
    if not path:
        return {"error": "Refusing to delete the workspace root itself."}
    try:
        target = _resolve_workspace_path(workspace, path)
        if target == os.path.realpath(workspace):
            return {"error": "Refusing to delete the workspace root itself."}
        if os.path.isdir(target):
            import shutil
            shutil.rmtree(target)
        elif os.path.isfile(target):
            os.remove(target)
        else:
            return {"error": f"Not found: {path}"}
        return {"ok": True}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"delete failed: {e}"}


import yaml
import re

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")

def _parse_skill_frontmatter(content: str):
    """Splits a SKILL.md's YAML frontmatter from its Markdown body.
    Matches the open Agent Skills / SKILL.md standard, so skills
    built here are usable elsewhere too and vice versa. Returns
    (metadata, body), or (None, content) if there's no valid
    frontmatter block."""
    if not content.startswith("---"):
        return None, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content
    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None, content
    return metadata, parts[2].lstrip(chr(10))

def _scan_skills():
    """Lists every skill: a folder under SKILLS_DIR with a SKILL.md
    that has at least a name and description in its frontmatter.
    Only name+description are read here (progressive disclosure
    level 1) -- cheap enough to include for every skill on every
    request."""
    skills = []
    if not os.path.isdir(SKILLS_DIR):
        return skills
    for entry in sorted(os.listdir(SKILLS_DIR)):
        skill_md = os.path.join(SKILLS_DIR, entry, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        try:
            content = open(skill_md, encoding="utf-8").read()
        except Exception:
            continue
        metadata, _ = _parse_skill_frontmatter(content)
        if not metadata or not metadata.get("name") or not metadata.get("description"):
            continue
        skills.append({"name": metadata["name"], "description": metadata["description"], "folder": entry})
    return skills

def _load_skill(name: str):
    """Returns one skill's full body -- progressive disclosure level
    2, called by the model via the load_skill tool once it decides a
    skill is relevant based on its description."""
    for s in _scan_skills():
        if s["name"] == name:
            content = open(os.path.join(SKILLS_DIR, s["folder"], "SKILL.md"), encoding="utf-8").read()
            _, body = _parse_skill_frontmatter(content)
            return {"name": name, "content": body}
    return {"error": "No skill named '" + name + "' found."}

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "skill"

class SkillParseIn(BaseModel):
    content: str

@app.post("/api/skills/parse")
def parse_skill_upload(req: SkillParseIn):
    """Parses an uploaded SKILL.md's frontmatter+body without saving
    anything, so the person can review/edit in the form before it
    actually gets written to disk."""
    metadata, body = _parse_skill_frontmatter(req.content)
    if not metadata or not metadata.get("name") or not metadata.get("description"):
        return {"error": "This file doesn't have valid SKILL.md frontmatter (needs at least name and description)."}
    return {"name": metadata.get("name"), "description": metadata.get("description"), "body": body}

from bs4 import BeautifulSoup
import re as _re

@app.get("/api/skills")
def list_skills():
    return {"skills": _scan_skills()}

@app.get("/api/skills/{folder}")
def get_skill(folder: str):
    if ".." in folder or "/" in folder:
        return {"error": "Invalid folder name."}
    skill_md = os.path.join(SKILLS_DIR, folder, "SKILL.md")
    if not os.path.isfile(skill_md):
        return {"error": "Skill not found."}
    content = open(skill_md, encoding="utf-8").read()
    metadata, body = _parse_skill_frontmatter(content)
    if not metadata:
        return {"error": "Skill file has no valid frontmatter."}
    return {"name": metadata.get("name"), "description": metadata.get("description"), "body": body, "folder": folder}

class SkillIn(BaseModel):
    name: str
    description: str
    body: str
    folder: Optional[str] = None

@app.post("/api/skills")
def save_skill(req: SkillIn):
    os.makedirs(SKILLS_DIR, exist_ok=True)
    folder = req.folder or _slugify(req.name)
    if ".." in folder or "/" in folder:
        return {"error": "Invalid folder name."}
    skill_dir = os.path.join(SKILLS_DIR, folder)
    os.makedirs(skill_dir, exist_ok=True)
    frontmatter = yaml.safe_dump({"name": req.name, "description": req.description}, default_flow_style=False, sort_keys=False)
    content = "---" + chr(10) + frontmatter + "---" + chr(10) + chr(10) + req.body
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "folder": folder}

@app.delete("/api/skills/{folder}")
def delete_skill(folder: str):
    if ".." in folder or "/" in folder:
        return {"error": "Invalid folder name."}
    skill_dir = os.path.join(SKILLS_DIR, folder)
    if os.path.isdir(skill_dir):
        import shutil
        shutil.rmtree(skill_dir)
        return {"ok": True}
    return {"error": "Skill not found."}

SKILL_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Loads the full instructions for one available skill by name. Call this when a skill's description (listed in your system context) matches what you're being asked to do.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The exact skill name, as listed in your available skills."},
                },
                "required": ["name"],
            },
        },
    },
]

def _get_skills_context() -> str:
    skills = _scan_skills()
    if not skills:
        return ""
    lines = ["Available skills: call load_skill(name) to load one's full instructions when its description matches the current task."]
    for s in skills:
        lines.append("- " + s["name"] + ": " + s["description"])
    return chr(10) + chr(10) + chr(10).join(lines)


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

import re as _re_webfetch

def _web_fetch(url: str):
    """Fetch and extract clean readable text -- uses BeautifulSoup for
    real HTML parsing (same technique Odysseus uses), not a regex
    tag-strip. A regex-only approach leaves nav/ad/script text mixed
    into the output, which was confusing local models into treating
    real content as unreliable. Structural elements (lists, headings)
    get separators so the model can still parse a readable shape.

    A bare GitHub repo root URL (github.com/owner/repo, no further
    path) gets rewritten to fetch that repo's raw README.md directly
    from raw.githubusercontent.com instead. Confirmed directly by
    testing: modern GitHub repo pages are React-rendered SPAs whose
    real content (README, file tree) only loads via JavaScript --
    fetching the raw HTML here just returns UI chrome ('You signed in
    with another tab...', 'Uh oh! There was an error while loading')
    which is non-empty so it survives this function's own empty-text
    check, but is useless to the model. The raw README route has none
    of that problem since it's a plain text file, no rendering at all."""
    github_repo_match = _re_webfetch.match(r"^https?://github\.com/([^/]+)/([^/]+)/?$", url)
    if github_repo_match:
        owner, repo = github_repo_match.group(1), github_repo_match.group(2)
        for branch in ("main", "master"):
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
            try:
                raw_resp = httpx.get(raw_url, timeout=15, follow_redirects=True)
                if raw_resp.status_code == 200 and raw_resp.text.strip():
                    header = f"# {owner}/{repo} README\nSource: {raw_url}\n\n"
                    return {"url": raw_url, "content": (header + raw_resp.text)[:6000]}
            except Exception:
                pass
        # Both branches failed -- fall through to the normal HTML fetch
        # below rather than giving up, since some repos use a different
        # default branch name entirely.

    # A /blob/branch/path URL is GitHub's own viewer for one specific
    # file -- same React-SPA rendering problem as the repo root case
    # above, just for a single file instead of the README. The branch
    # is already given in the URL itself here, so no guessing between
    # main/master is needed the way it is for the repo-root case.
    blob_match = _re_webfetch.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", url)
    if blob_match:
        owner, repo, branch, file_path = blob_match.groups()
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
        try:
            raw_resp = httpx.get(raw_url, timeout=15, follow_redirects=True)
            if raw_resp.status_code == 200 and raw_resp.text.strip():
                header = f"# {owner}/{repo} -- {file_path}\nSource: {raw_url}\n\n"
                return {"url": raw_url, "content": (header + raw_resp.text)[:6000]}
        except Exception:
            pass
        # Raw fetch failed -- fall through to the normal HTML fetch below.

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


# Read-only shell access. Structured, not a raw command string: the
# model supplies a bare command name plus a list of arguments, which
# are passed directly to subprocess.run with shell=False -- no shell
# is ever invoked at all, so shell metacharacters (;, &&, |, $(...),
# backticks, redirection) have no special meaning whatsoever if they
# appear in an argument; they're just literal text passed to the
# allowlisted command itself. This is a structural guarantee against
# command injection, not a blocklist of dangerous patterns to detect --
# there's no shell present for an injection to exploit in the first
# place. On top of that: only specific, genuinely read-only commands
# are allowed at all, and specific dangerous flags are rejected even
# for allowed commands (sed's -i, find's -delete/-exec), since those
# would give write access through a "read" tool otherwise. This tool
# never requires a workspace and is never confined to one -- it can
# read anywhere this process has filesystem access, matching the
# read-anywhere/write-only-in-workspace split this design is built on.
BASH_ALLOWED_COMMANDS = {
    "ls", "cat", "grep", "find", "head", "tail", "wc", "pwd",
    "sed", "stat", "diff", "sort", "uniq", "file", "tree", "du", "date",
}
BASH_DANGEROUS_FLAGS = {
    "sed": {"-i", "--in-place"},
    "find": {"-delete", "-exec", "-execdir", "-fprintf", "-fprint", "-fprint0", "-fls"},
}

def _execute_readonly_bash(command: str, args: list, cwd: str = ""):
    if not command:
        return {"error": "Missing 'command'. Example: to run grep -n pattern file.txt, set command to 'grep' (just the program name) and args to ['-n', 'pattern', 'file.txt'] (a list of separate arguments)."}
    if not isinstance(command, str) or " " in command or command.startswith("[") or command.startswith('"'):
        return {
            "error": "'" + str(command) + "' looks like a full command line or a JSON array, not a bare program name. "
            "command must be ONLY the program name by itself, e.g. 'grep' -- never the whole command line, "
            "and never the command name repeated inside args. Everything after the program name goes in "
            "args as separate list items instead: to run grep -n pattern file.txt, use "
            "command='grep' and args=['-n', 'pattern', 'file.txt']."
        }
    if command not in BASH_ALLOWED_COMMANDS:
        return {"error": "Command '" + command + "' is not allowed. Allowed commands: " + ", ".join(sorted(BASH_ALLOWED_COMMANDS))}
    dangerous = BASH_DANGEROUS_FLAGS.get(command, set())
    shell_operators = ("|", ">", "<", "&", ";", "$(", "`", "&&", "||")
    for arg in args:
        if not isinstance(arg, str):
            return {"error": "All arguments must be strings."}
        if any(op in arg for op in shell_operators):
            return {
                "error": "Argument '" + arg + "' contains a shell operator (pipe, redirect, chaining, or substitution). "
                "There is no shell here at all -- this tool runs the program directly, so operators like |, >, 2>/dev/null, "
                "&&, or $(...) have no special meaning and can't do what they'd do in a real shell; they'd just be passed "
                "as literal, meaningless text to the program. Make separate bash calls instead and read each result "
                "yourself -- for example, to ignore a 'not found' error from find, just call find normally and ignore "
                "any error in the response, rather than trying to redirect it away."
            }
        if arg in dangerous or any(arg.startswith(d) for d in dangerous):
            return {"error": "Argument '" + arg + "' is not allowed for '" + command + "' -- this tool is strictly read-only, no in-place edits or deletions."}
    try:
        result = subprocess.run(
            [command] + list(args),
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            cwd=cwd if cwd else None,
        )
        output = result.stdout
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        return {"command": command, "args": args, "output": output[:10000], "exit_code": result.returncode}
    except FileNotFoundError:
        return {"error": "Command '" + command + "' not found on this system."}
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out after 15 seconds."}
    except Exception as e:
        return {"error": f"bash execution failed: {e}"}

BASH_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a read-only shell command. Only these commands are allowed: ls, cat, grep, find, head, tail, wc, pwd, sed, stat, diff, sort, uniq, file, tree, du, date. No shell chaining, pipes, or redirection -- provide the command and its arguments as a separate list, not as one combined string. sed's -i flag and find's -delete/-exec flags are blocked; this tool can never write or modify anything, on any file, regardless of workspace. Works anywhere on the filesystem this process can read, not limited to any workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bare command name, e.g. 'grep' or 'ls'. No path, no shell operators."},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Arguments to the command, each as a separate array element (e.g. [\"-n\", \"pattern\", \"file.txt\"] for grep -n pattern file.txt).",
                    },
                },
                "required": ["command", "args"],
            },
        },
    },
]


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
    {
        "type": "function",
        "function": {
            "name": "replace_lines",
            "description": "Replace an exact range of lines in a file by line number, given the new content to put there -- use this instead of edit_file whenever you already know the exact line numbers (e.g. from grep -n or sed -n via bash), since it never requires reproducing old text byte-for-byte and so can't fail on a whitespace mismatch. Requires expected_content: what you believe is currently at that exact line range, used as a safety check before applying anything. If your line numbers turn out to be stale, this will try to find the expected content nearby and correct itself automatically, or fail safely and show you the real current content rather than corrupting the file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace root."},
                    "start_line": {"type": "integer", "description": "First line to replace (1-indexed)."},
                    "end_line": {"type": "integer", "description": "Last line to replace, inclusive (1-indexed). Same as start_line to replace a single line."},
                    "new_content": {"type": "string", "description": "The new text to put in place of that line range. This completely replaces the range, it is not inserted alongside it."},
                    "expected_content": {"type": "string", "description": "What you believe is currently at lines start_line-end_line, exactly as you last saw it. Used to verify your line numbers are still accurate before making any change."},
                },
                "required": ["path", "start_line", "end_line", "new_content", "expected_content"],
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

def _replace_lines(workspace: str, rel_path: str, start_line, end_line, new_content: str, expected_content: str):
    """Replaces an exact line range by number, sidestepping edit_file's
    fragile requirement to reproduce old text byte-for-byte. Requires
    expected_content -- a sanity check against what's actually at that
    line range right now, not an exact-match requirement like
    old_text. If it genuinely doesn't match (most often because an
    earlier edit shifted the file's line numbers and these ones are
    now stale), this does NOT blindly apply the change: it searches a
    window around the given range for the expected content and, if
    found once and unambiguously, applies the edit there instead and
    reports the correction. If it's not found nearby, or found more
    than once, it fails safely and returns the real current content at
    that location, rather than corrupting the file the way a blind
    line-number replacement could."""
    try:
        target = _resolve_workspace_path(workspace, rel_path)
        if not os.path.isfile(target):
            return {"error": f"Not a file: {rel_path}"}
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        lines = content.split("\n")

        try:
            start_line = int(start_line)
            end_line = int(end_line)
        except (TypeError, ValueError):
            return {"error": "start_line and end_line must be numbers."}
        if start_line < 1 or start_line > len(lines) or end_line < start_line:
            return {"error": f"Invalid range for a file with {len(lines)} lines (start_line={start_line}, end_line={end_line})."}

        end = min(len(lines), end_line)
        actual_block = "\n".join(lines[start_line - 1:end])
        expected_stripped = (expected_content or "").strip()

        if actual_block.strip() != expected_stripped:
            window_start = max(0, start_line - 1 - 25)
            window_end = min(len(lines), end_line + 25)
            window_lines = lines[window_start:window_end]
            expected_lines = expected_stripped.split("\n") if expected_stripped else []

            matches = []
            if expected_lines:
                for offset in range(len(window_lines) - len(expected_lines) + 1):
                    candidate = "\n".join(window_lines[offset:offset + len(expected_lines)]).strip()
                    if candidate == expected_stripped:
                        matches.append(window_start + offset + 1)

            if len(matches) == 1:
                real_start = matches[0]
                real_end = real_start + len(expected_lines) - 1
                new_lines = new_content.split("\n")
                updated_lines = lines[:real_start - 1] + new_lines + lines[real_end:]
                with open(target, "w", encoding="utf-8") as f:
                    f.write("\n".join(updated_lines))
                return {
                    "path": rel_path,
                    "edited": True,
                    "note": f"Your line numbers ({start_line}-{end_line}) were stale, but the expected content was found unambiguously nearby at lines {real_start}-{real_end} and the edit was applied there instead. Re-check line numbers for this file before your next edit, since they may have shifted again.",
                }

            reason = "did not match" if not matches else f"was found {len(matches)} times nearby, which is ambiguous"
            return {
                "error": f"expected_content {reason} at or near lines {start_line}-{end_line}. Actual current content at that exact range right now:\n{actual_block}\n\nRe-verify the real content and line numbers before retrying, rather than guessing again."
            }

        new_lines = new_content.split("\n")
        updated_lines = lines[:start_line - 1] + new_lines + lines[end:]
        with open(target, "w", encoding="utf-8") as f:
            f.write("\n".join(updated_lines))

        delta = len(new_lines) - (end - start_line + 1)
        result = {"path": rel_path, "edited": True, "lines_replaced": f"{start_line}-{end_line}"}
        if delta != 0:
            result["warning"] = f"This changed the file's line count by {delta:+d}. Any other line numbers you had for this file are now stale -- re-check before another replace_lines call."
        return result
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"replace_lines failed: {e}"}


# Per-session cancel signal for in-progress generations. The frontend's
# stop button aborting its own fetch only stops the CLIENT from
# listening -- it does nothing to the backend, which has no way to
# know the client walked away and just keeps talking to Ollama in the
# background regardless. This explicit flag, checked inside the
# generator's own loop below, is what actually stops the backend's
# work, not just the frontend's display of it.
_cancel_flags = {}

class CancelIn(BaseModel):
    session_id: str

@app.post("/api/chat/cancel")
def cancel_chat(req: CancelIn):
    print(f"[CANCEL-DEBUG] cancel request for session_id={req.session_id!r}, known flags={list(_cancel_flags.keys())!r}", flush=True)
    flag = _cancel_flags.get(req.session_id)
    if flag:
        flag.set()
        print(f"[CANCEL-DEBUG] flag found and set for {req.session_id!r}", flush=True)
    else:
        print(f"[CANCEL-DEBUG] NO matching flag for {req.session_id!r}", flush=True)
    return {"cancelled": bool(flag)}


_last_activity_ts = time.time()

@app.post("/api/chat")
def chat_stream(req: ChatIn):
    global _last_activity_ts
    _last_activity_ts = time.time()
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
    # tools and the read-only bash tool are always on; file write/edit
    # tools are only included here when req.workspace is non-empty, so
    # the model can never edit files on a session with no workspace
    # bound to it, regardless of how it reads an ambiguous prompt.
    system_prompt = BASE_SYSTEM_PROMPT + AGENT_SYSTEM_SUFFIX + _get_memory_context() + _get_skills_context()
    tools = get_lcm_tools() + BASH_TOOL_SCHEMAS
    if req.search_url:
        tools = tools + WEB_TOOL_SCHEMAS
    if req.workspace:
        tools = tools + FILE_TOOL_SCHEMAS
    if _scan_skills():
        tools = tools + SKILL_TOOL_SCHEMAS

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
        if name == "load_skill":
            return _load_skill(args.get("name", ""))
        if name == "bash":
            return _execute_readonly_bash(args.get("command", ""), args.get("args", []), req.workspace)
        if name == "list_files":
            return _list_files(req.workspace, args.get("path", "."))
        if name == "read_file":
            return _read_file(req.workspace, args.get("path", ""))
        if name == "write_file":
            return _write_file(req.workspace, args.get("path", ""), args.get("content", ""))
        if name == "edit_file":
            return _edit_file(req.workspace, args.get("path", ""), args.get("old_text", ""), args.get("new_text", ""))
        if name == "replace_lines":
            return _replace_lines(req.workspace, args.get("path", ""), args.get("start_line"), args.get("end_line"), args.get("new_content", ""), args.get("expected_content", ""))

        args["session_id"] = req.session_id
        try:
            resp = httpx.post(f"{LCM_URL}/tools/call", json={"name": name, "arguments": args}, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("result")
            return {"error": f"Tool call failed: HTTP {resp.status_code}"}
        except Exception as e:
            return {"error": f"Tool call failed: {e}"}

    def generate():
        cancel_flag = threading.Event()
        _cancel_flags[req.session_id] = cancel_flag
        yield f"data: {json.dumps({'user_message_id': _user_msg_id})}\n\n"
        full_reply = ""
        _target_url = (req.endpoint_url.rstrip("/") + "/api/chat") if req.endpoint_url else OLLAMA_URL
        _messages = list(messages)
        print(f"[DEBUG] _messages roles={[m.get("role") for m in _messages]!r}", flush=True)
        MAX_ROUNDS = 1000  # effectively unbounded; the stop button is the real safety net now
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
                    if cancel_flag.is_set():
                        break
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

            if cancel_flag.is_set():
                if full_reply:
                    send_to_lcm(req.session_id, "assistant", full_reply, model=req.model)
                yield f"data: {json.dumps({'done': True, 'cancelled': True})}\n\n"
                _cancel_flags.pop(req.session_id, None)
                return

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
    last_active: Optional[float] = None

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

_notes_lock = threading.Lock()
_NOTES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "athena_notes.json")

def _load_notes():
    try:
        with open(_NOTES_PATH) as f:
            return json.load(f)
    except Exception:
        return []

def _save_notes(notes):
    """Atomic write (temp file + os.replace), same pattern as
    settings, so a crash or a concurrent write from another device
    mid-write can never leave a corrupted notes file behind. This
    replaces the old localStorage-only storage, which never synced
    notes across devices at all -- a note saved on one device was
    simply invisible everywhere else."""
    tmp_path = _NOTES_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(notes, f)
    os.replace(tmp_path, _NOTES_PATH)

class NotesIn(BaseModel):
    notes: list

@app.get("/api/notes")
def get_notes():
    with _notes_lock:
        return {"notes": _load_notes()}

@app.post("/api/notes")
def save_notes_endpoint(req: NotesIn):
    with _notes_lock:
        _save_notes(req.notes)
    return {"ok": True}

class MemoryModelIn(BaseModel):
    model: Optional[str] = None

class SettingsIn(BaseModel):
    workspace: Optional[str] = None
    endpoints: Optional[list] = None
    search_url: Optional[str] = None
    default_model: Optional[dict] = None
    theme: Optional[dict] = None

class MCPServerIn(BaseModel):
    name: str
    command: str
    args: Optional[list] = None

@app.get("/api/mcp/servers")
def list_mcp_servers():
    from mcp_manager import mcp_manager
    servers = mcp_manager.list_servers()
    for s in servers:
        tools = mcp_manager.list_tools(s["name"])
        s["tools"] = [{"name": t.name, "description": t.description, "inputSchema": t.input_schema} for t in tools]
    return {"servers": servers}

@app.post("/api/mcp/servers")
def add_mcp_server(req: MCPServerIn):
    """Adds a server to persisted config AND connects immediately,
    so the person sees real success/failure right away instead of
    only finding out on the next restart."""
    from mcp_manager import mcp_manager
    with _settings_lock:
        settings = _load_settings()
        servers = settings.setdefault("mcp_servers", [])
        servers = [s for s in servers if s["name"] != req.name]
        servers.append({"name": req.name, "command": req.command, "args": req.args or []})
        settings["mcp_servers"] = servers
        _save_settings(settings)
    try:
        tools = mcp_manager.connect_server(req.name, req.command, req.args or [])
        return {"connected": True, "tool_count": len(tools)}
    except Exception as e:
        return {"connected": False, "error": str(e)}

@app.delete("/api/mcp/servers/{name}")
def remove_mcp_server(name: str):
    from mcp_manager import mcp_manager
    mcp_manager.disconnect_server(name)
    with _settings_lock:
        settings = _load_settings()
        settings["mcp_servers"] = [s for s in settings.get("mcp_servers", []) if s["name"] != name]
        _save_settings(settings)
    return {"removed": True}

class MCPCallIn(BaseModel):
    server: str
    tool: str
    arguments: Optional[dict] = None

@app.post("/api/mcp/call")
def call_mcp_tool(req: MCPCallIn):
    """Generic tool-call passthrough -- Athena has no idea what any
    given tool does, it just forwards the call and hands back the raw
    result. If the result's text content happens to parse as JSON,
    that's included too, for the frontend's shape-sniffing renderer to
    work with; the raw text is always included as a safe fallback."""
    from mcp_manager import mcp_manager
    try:
        result = mcp_manager.call_tool(req.server, req.tool, req.arguments or {})
    except Exception as e:
        return {"error": str(e)}

    text_parts = [item.text for item in result.content if hasattr(item, "text")]
    raw_text = "\n".join(text_parts)

    parsed = None
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        pass

    return {"isError": getattr(result, "isError", False), "raw_text": raw_text, "parsed": parsed}

MCP_UIS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_uis")

def _serve_mcp_ui_file(server_name: str, path: str):
    """Serves a custom, hand-built UI for one MCP server, if the
    person has created one -- entirely optional plugin folders,
    gitignored by default, so a public release of Athena never ships
    anyone's bespoke tool-specific UI. Falls back to a short, friendly
    explainer (not a bare 404) when no folder exists, so this doubles
    as living documentation for building one."""
    if ".." in server_name or ".." in path:
        return HTMLResponse("Invalid path", status_code=400)

    server_dir = os.path.join(MCP_UIS_DIR, server_name)
    if not os.path.isdir(server_dir):
        return HTMLResponse(f"""
        <html><body style="font-family: sans-serif; padding: 2rem; color: #666; max-width: 640px; margin: 0 auto;">
        <h2>No custom UI for &#39;{server_name}&#39; yet</h2>
        <p>To build one, create a folder at <code>mcp_uis/{server_name}/</code> containing an <code>index.html</code> (plus any JS/CSS/images it needs). It loads in an isolated iframe, so it can be styled however you like.</p>
        <p>It can call Athena&#39;s existing generic endpoints to interact with this server:</p>
        <ul>
            <li><code>GET /api/mcp/servers</code> &mdash; list connected servers and their discovered tools</li>
            <li><code>POST /api/mcp/call</code> &mdash; call a tool: <code>{{"server": "{server_name}", "tool": "...", "arguments": {{...}}}}</code></li>
        </ul>
        </body></html>
        """)

    if not path:
        path = "index.html"
    file_path = os.path.join(server_dir, path)
    real_server_dir = os.path.realpath(server_dir)
    real_file_path = os.path.realpath(file_path)
    if not real_file_path.startswith(real_server_dir):
        return HTMLResponse("Invalid path", status_code=400)
    if not os.path.isfile(file_path):
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(file_path)

def _load_mcp_ui_backends():
    """Generic plugin mechanism: any mcp_uis/<name>/backend.py that
    defines a FastAPI APIRouter named `router` gets mounted under
    /mcp-ui-api/<name>/. Athena's core has zero knowledge of what's
    inside a plugin's backend -- this is the one place tool-specific
    server-side logic is allowed to exist at all, deliberately
    isolated to plugin folders that are gitignored by default, never
    touching Athena's own codebase."""
    import importlib.util
    if not os.path.isdir(MCP_UIS_DIR):
        return
    for name in os.listdir(MCP_UIS_DIR):
        backend_path = os.path.join(MCP_UIS_DIR, name, "backend.py")
        if os.path.isfile(backend_path):
            try:
                spec = importlib.util.spec_from_file_location(f"mcp_ui_backend_{name}", backend_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "router"):
                    app.include_router(module.router, prefix=f"/mcp-ui-api/{name}")
                    print(f"[Athena] Loaded custom backend for MCP UI plugin '{name}'", flush=True)
            except Exception as e:
                print(f"[Athena] Failed to load custom backend for MCP UI plugin '{name}': {e}", flush=True)

_load_mcp_ui_backends()

@app.get("/mcp-ui/{server_name}")
def serve_mcp_ui_root(server_name: str):
    return _serve_mcp_ui_file(server_name, "index.html")

@app.get("/mcp-ui/{server_name}/{path:path}")
def serve_mcp_ui_file(server_name: str, path: str):
    return _serve_mcp_ui_file(server_name, path)

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

if __name__ == "__main__":
    import uvicorn
    import threading
    _check_requirements()
    _start_bundled_lcm()
    threading.Thread(target=_memory_scan_loop, daemon=True).start()

    from mcp_manager import mcp_manager
    mcp_manager.start()
    for server_cfg in _load_settings().get("mcp_servers", []):
        try:
            tools = mcp_manager.connect_server(server_cfg["name"], server_cfg["command"], server_cfg.get("args", []))
            print(f"[Athena] Connected to MCP server '{server_cfg['name']}' -- {len(tools)} tools discovered", flush=True)
        except Exception as e:
            print(f"[Athena] Failed to connect to MCP server '{server_cfg['name']}': {e}", flush=True)

    port = int(os.environ.get("ATHENA_PORT", "9500"))
    uvicorn.run(app, host="0.0.0.0", port=port)
