"""
Athena — a lean, LCM-backed chat/agent workspace with voice support.
"""
import os
import subprocess
from urllib.parse import urlparse
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
import hashlib
import contextvars
import queue
import select
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List
from datetime import datetime, timedelta
import calendar
import uuid
import gzip
import httpx
import psutil
import argparse
from fastapi import FastAPI, UploadFile, File, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse, FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from auth import AuthManager
from rag import SimpleCodeRAG
import laya_gate
import settings
import task_context
import activity
import generation_streaming
import lcm_client

RAG_DB_PATH = os.environ.get("ATHENA_RAG_DB", "rag_index.db")
rag = SimpleCodeRAG(RAG_DB_PATH)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

OLLAMA_URL = os.environ.get("ATHENA_OLLAMA_URL", "http://localhost:11434/api/chat")
# A flat timeout=180 applies that value to connect/read/write/pool alike,
# which was too short on its read side: prompt evaluation on a large model
# with a large, accumulated context (the common case in a long
# athena_delegation turn) can genuinely take longer than 180s on its own,
# with zero bytes sent back until it's done -- httpx's read timeout has no
# way to distinguish that from a truly dead connection, so it fires either
# way. Split so a dead/unreachable server is still caught fast (short
# connect timeout) while a slow-but-alive one gets real patience on the
# read side -- these are different failure modes and shouldn't share one
# number. Two separate read budgets, not one shared value: a bot (sub
# agent) round is one bounded, single-topic investigation step, while
# Athena's own turn can legitimately run long chains of rounds on old/
# slow hardware -- she needs real patience, a bot needs a real ceiling.
_SUB_AGENT_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)      # 10 minutes -- one bot round
_MAIN_AGENT_TIMEOUT = httpx.Timeout(connect=10.0, read=3600.0, write=30.0, pool=10.0)    # 1 hour -- Athena's own turn
DEFAULT_MODEL = os.environ.get("ATHENA_DEFAULT_MODEL", "gpt-oss-20b-32k:latest")
WHISPER_MODEL_SIZE = os.environ.get("ATHENA_WHISPER_MODEL", "large-v3-turbo")
PIPER_VOICE_PATH = os.environ.get("ATHENA_PIPER_VOICE_PATH", "/home/ohporty/athena/voices/en_US-lessac-medium.onnx")

app = FastAPI(title="Athena")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(generation_streaming.router)

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

# Casual-chat system-prompt suffix, built the same named-section way as
# CODING_HARNESS_SECTIONS below -- its own independent registry, its own
# wording. Deliberately NOT shared text with the coding-harness sections:
# casual and coding-harness mode are separate layers by design (DeepSeek-
# Harness-style -- a stable per-mode prompt, not one prompt with mode-
# conditional bits spliced together), so each layer gets tuned on its own
# terms rather than collapsed into a single shared block. This used to be
# one 80-line unbroken paragraph mixing everything below into a single
# run-on block with no structure between concerns -- splitting it out
# doesn't change what it says, just how legible it is to edit later.
CASUAL_AGENT_SECTIONS = [
    ("casual_tool_use",
     "You have access to tools listed below when relevant -- use them rather than guessing "
     "when you need real information."),

    ("plan_first",
     "## When a question needs more than a couple of lookups\n"
     "If answering something is genuinely going to take more than a couple of tool calls -- "
     "several searches, a fetch followed by more fetches to track down the right source, "
     "cross-checking more than one thing -- call write_plan first with the short ordered list "
     "of what you're actually going to check, before diving in. This is a real requirement, "
     "not a suggestion: if you make several tool calls in a row without ever calling "
     "write_plan, every tool except write_plan disappears until you call it. It does not cap "
     "how much you can look up afterward -- once you've written a plan, your full tool access "
     "returns and stays available. A simple one-lookup question never needs this at all."),

    ("formatting",
     "## Formatting structured data\n"
     "When presenting structured or tabular data (schedules, forecasts, comparisons, lists "
     "of items with multiple fields each), format it as a real markdown table with | column | "
     "headers | -- not a run-on list of colon-separated values."),

    ("research_discipline",
     "## Researching something real\n"
     "When you are asked to research a specific, named software library, SDK, or API -- not "
     "a general topic -- do not rely on a broad web search alone: that returns scattered blog "
     "posts and tutorials of unknown age that get blended together into answers matching no "
     "real version. Instead, fetch that project's own official repository or documentation "
     "site directly (e.g. its GitHub README, its docs site) and base your answer on that "
     "source. If the library has multiple language SDKs for the same protocol or spec (e.g. "
     "Python, TypeScript, Go), never mix a convention from one language's SDK into another "
     "language's example -- python-style decorators on bare functions are not valid syntax in "
     "JavaScript or TypeScript, and vice versa; verify each language's example separately "
     "against that language's own SDK docs. For every specific API claim you state as fact -- "
     "an import path, a class name, a function signature -- name which source you got it "
     "from, so a person reading your answer can verify it themselves rather than trusting it "
     "blindly."),

    ("dependency_pinning",
     "## Pinning dependency versions\n"
     "When you generate a dependency declaration for an external library -- requirements.txt, "
     "pyproject.toml, package.json, go.mod, or any similar file -- always pin to a specific "
     "version or a bounded range (e.g. mcp>=1.28,<2), never an open-ended minimum like "
     "mcp>=1.0.0. An unbounded dependency silently resolves to whatever is newest at install "
     "time, which may be a different major version than whatever API you actually researched "
     "and wrote code against -- exactly the kind of breaking change that makes generated code "
     "fail long after you wrote it. When you research a library's current API, note which "
     "version that API belongs to, and pin your generated dependency file to that version."),

    ("explicit_write_calls",
     "## Naming the actual write, not just describing it\n"
     "When research is about building something that creates, writes, or generates files or "
     "other output, it must explicitly name the specific API, method, or function that "
     "performs that write (for example fs.writeFile, or a library's own save/create method) "
     "and show it actually being called in at least one real example -- never leave the "
     "actual creation step implied, described only in prose, or absent from every example "
     "while only reading, analyzing, or reacting to things gets demonstrated."),

    ("uncertainty",
     "## Naming uncertainty plainly\n"
     "When you are not certain whether a capability is available or restricted in some "
     "environment or API, say that uncertainty plainly -- never state a confident-sounding "
     "restriction you have not actually confirmed from a real source, since a plausible but "
     "invented rule is often followed just as strictly as a true one, and is much harder to "
     "catch."),

    ("source_fidelity",
     "## Using the real source, not a summary of it\n"
     "When a task asks you to review, verify, check, or update something against 'the "
     "documentation' or another external source, and you notice you only have a summary, a "
     "compacted account, or a secondhand description of that source in your current context "
     "-- not the source itself -- you must actually fetch the real source before proceeding. "
     "Noticing that gap in your own reasoning and then continuing anyway with the lossy "
     "version defeats the entire point of checking. This applies even when the summary comes "
     "from earlier in this same conversation, including your own memory system's compacted "
     "context, since compaction can silently drop the exact specific detail that turns out to "
     "matter most."),

    ("generated_artifacts",
     "## Artifacts built as strings\n"
     "When something is built as a string or text blob representing some OTHER artifact -- "
     "generated code in a different language, a JSON or YAML config assembled via string "
     "concatenation, an HTML template, a SQL query built by interpolation, a shell script "
     "written out as text, anything where the actual product lives inside a string rather "
     "than being written directly as its own native syntax that gets parsed and checked as "
     "such -- the correctness of the code doing the building is a separate question from the "
     "correctness of what it actually produces. Validate both independently. Code that "
     "assembles such an artifact can be flawless in its own language while the artifact it "
     "produces is still broken, since that artifact lives inside strings, invisible to normal "
     "review, rather than as code your own tooling would ever check directly."),
]
# Note: there is deliberately no dead-tool STRICT RULE here about grep -n
# vs. read_file for line-location questions -- casual mode's tool list
# never includes bash or read_file (see _get_mode_tools; only workspace/
# athena_delegation modes get BASH_TOOL_SCHEMAS/FILE_TOOL_SCHEMAS), so an
# instruction to use them here was always unreachable. That guidance
# already lives, correctly scoped, in CODING_HARNESS_SECTIONS'
# tool_selection section below, where those tools actually exist.

# Coding-harness system prompt, built as named, independently
# addressable sections assembled in order at request time -- not one
# flat string. A future mode can select a different subset or order
# without touching the sections it doesn't use, and a mode can splice
# in its own extra section (see _assemble_prompt_sections) the same
# way a workspace-bound delegation session layers BOT_DELEGATION
# on top of the same base sections rather than duplicating them.
CODING_HARNESS_SECTIONS = [
    ("identity",
     "You are Athena, in agentic coding mode. A real workspace is bound to this session: "
     "you can read, search, and modify real files, and run real commands. Relevant memory "
     "has already been retrieved and provided as context below -- treat it as established "
     "fact about this person and their work, not something to re-ask for or re-derive."),

    ("plan_before_acting",
     "## Before you act\n"
     "For anything touching more than one file, or more than a couple of lines, or requiring "
     "several tool calls to even figure out the shape of the problem: call write_plan first "
     "with the short ordered list of what you're actually going to do. A trivial one-line fix "
     "doesn't need one. This is enforced, not just advised: if you make several tool calls in "
     "a row without ever calling write_plan, every other tool disappears until you call it -- "
     "so don't be surprised if bash/read_file/etc. suddenly aren't in your tool list anymore; "
     "that's this gate, not an error. It never reduces how much you can actually do -- once "
     "you've written a plan, full tool access returns and stays available for the rest of the "
     "turn. A plan can be wrong once you see real file contents -- call update_plan_step to "
     "mark a step done, failed, or in progress as you go, and call write_plan again if the "
     "whole approach needs to change -- but skipping it and improvising edit-by-edit is how "
     "partial, inconsistent changes happen."),

    ("grounded_claims",
     "## Ground every claim in a real tool result\n"
     "Only a completed tool call establishes a fact about this codebase. Never describe a "
     "file as read, checked, or changed unless a real tool call for that action is actually "
     "present in this conversation. If you're not certain, say so and make the call -- "
     "don't narrate an action you haven't taken."),

    ("loop_signals",
     "## If you see a blocked or interrupted message\n"
     "This harness enforces two things structurally, not just by asking: it will refuse to "
     "repeat an identical tool call, and it will cut off detected repetition in your own "
     "output. Neither is an error to route around. Both mean the same thing: stop, and "
     "either state your actual conclusion from what you already have, or say plainly that "
     "you don't have one yet."),

    ("tool_selection",
     "## Picking the right tool\n"
     "Use grep/sed to locate something by line -- never read_file for that, since it "
     "returns no line numbers. Before reading or grepping any file yourself for a "
     "codebase question, call search_codebase first -- this is a default, not an optional "
     "step for unfamiliar territory. Skipping straight to read_file/bash on a guess wastes "
     "context searching by hand for something search_codebase would have pointed to in one "
     "call. Treat its results as a pointer, not ground truth -- read the real file before "
     "editing, but let search_codebase tell you which file and roughly where first. Use "
     "find_definition/find_references/type_info for real semantic questions about a symbol, "
     "not text search. Use backup_file before an edit whose current state matters and isn't "
     "already in version control this turn -- never a hand-rolled copy."),

    ("research_discipline",
     "## Researching something real\n"
     "For a specific named library, SDK, or API: don't rely on general web search alone -- "
     "fetch that project's own docs or repo directly. If it has SDKs for multiple languages, "
     "verify each language's example against that language's own docs rather than carrying a "
     "convention from one language's SDK into another's syntax. State which source backs any "
     "specific claim (an import path, a function signature). Pin dependency versions "
     "explicitly rather than leaving them open-ended.\n\n"
     "Use tools for real information. State uncertainty plainly rather than presenting a "
     "guess as settled fact."),
]


def _assemble_prompt_sections(sections, section_ids, extra_sections=None):
    """Build a system prompt from named sections, in the order given by
    section_ids, looked up from the given sections registry (a list of
    (id, text) tuples -- CODING_HARNESS_SECTIONS or CASUAL_AGENT_SECTIONS
    today). extra_sections lets a caller splice in one-off section text
    (keyed by id) without it needing to live in either registry above."""
    lookup = dict(sections)
    if extra_sections:
        lookup.update(extra_sections)
    return "\n\n".join(lookup[sid] for sid in section_ids if sid in lookup)


# Default: every section, in registry order -- kept under this same
# name so every existing reference to the coding-harness prompt keeps
# working unchanged while the underlying mechanism stays genuinely
# modular rather than one flat string.
CODING_HARNESS_SYSTEM_PROMPT = _assemble_prompt_sections(CODING_HARNESS_SECTIONS, [sid for sid, _ in CODING_HARNESS_SECTIONS])

# Same mechanism, casual mode's own independent registry (see
# CASUAL_AGENT_SECTIONS above). Leading "\n\n" so BASE_SYSTEM_PROMPT +
# AGENT_SYSTEM_SUFFIX (no separator at that call site, in
# _get_mode_system_prompt) still reads as two cleanly separated blocks,
# the same way _get_bot_delegation_prompt_section already leads with its
# own "\n\n" when layered onto CODING_HARNESS_SYSTEM_PROMPT.
AGENT_SYSTEM_SUFFIX = "\n\n" + _assemble_prompt_sections(CASUAL_AGENT_SECTIONS, [sid for sid, _ in CASUAL_AGENT_SECTIONS])


_SEARCH_STOPWORDS = {"the", "a", "an", "of", "for", "and", "or", "in", "on", "to", "is", "are", "what", "how", "does", "do", "with", "vs", "current"}


def _light_stem(word):
    """Strip common suffixes so morphological variants of the same
    root word (generation/generating, models/model) compare equal --
    deliberately crude, not a real stemmer, just enough to stop
    obvious variants from being treated as unrelated words."""
    for suffix in ("ions", "ion", "ing", "ies", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _search_query_similarity(a, b):
    """Word-overlap similarity between two search queries, ignoring
    common stopwords and light stemming -- catches a search that's
    been reworded but is still on the same topic, which exact
    tool-call fingerprint matching misses entirely since a model can
    dodge that block by just rephrasing the query."""
    words_a = set(_light_stem(w) for w in re.findall(r"[a-z0-9]+", a.lower()) if w not in _SEARCH_STOPWORDS)
    words_b = set(_light_stem(w) for w in re.findall(r"[a-z0-9]+", b.lower()) if w not in _SEARCH_STOPWORDS)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


_large_tool_outputs = {}

_large_tool_output_counter = [0]
_LARGE_TOOL_OUTPUT_THRESHOLD = 3000
_LARGE_TOOL_OUTPUT_MAX_STORED = 50

# A bot's scratch file is already a deliberately curated, information-dense
# deliverable -- not raw noise like a read_file dump -- so a generic head/
# tail preview cuts exactly the itemized findings out of the middle. Exempt
# these from _compress_tool_result entirely rather than relying on Athena2
# remembering to call get_full_tool_output every time.
_UNCOMPRESSED_TOOL_NAMES = {"read_scratch_file", "read_tool_call_history"}


def _compress_tool_result(result):
    """If a tool's result is large enough to meaningfully burn
    context, store the full original and return a compressed version
    instead -- head and tail shown (where the immediately relevant
    content usually is), with a clear note on how much was cut and
    how to retrieve the rest if genuinely needed. Rather than either
    silently truncating with no way back, or always paying the full
    token cost regardless of whether the omitted middle ever
    matters."""
    try:
        serialized = json.dumps(result)
    except (TypeError, ValueError):
        return result
    if len(serialized) <= _LARGE_TOOL_OUTPUT_THRESHOLD:
        return result

    _large_tool_output_counter[0] += 1
    output_id = str(_large_tool_output_counter[0])
    _large_tool_outputs[output_id] = result
    if len(_large_tool_outputs) > _LARGE_TOOL_OUTPUT_MAX_STORED:
        oldest_id = min(_large_tool_outputs.keys(), key=lambda k: int(k))
        _large_tool_outputs.pop(oldest_id, None)

    if isinstance(result, dict):
        compressed = dict(result)
        for field in ("output", "content"):
            value = compressed.get(field)
            if isinstance(value, str) and len(value) > _LARGE_TOOL_OUTPUT_THRESHOLD:
                head, tail = value[:1200], value[-800:]
                omitted = len(value) - len(head) - len(tail)
                compressed[field] = (
                    f"{head}\n\n... [{omitted} characters omitted -- use get_full_tool_output with "
                    f"output_id '{output_id}' if you genuinely need the omitted middle] ...\n\n{tail}"
                )
                return compressed
    return {
        "note": f"This result was large ({len(serialized)} characters) and has been stored in full. "
        f"Use get_full_tool_output with output_id '{output_id}' to retrieve it if genuinely needed.",
        "preview": serialized[:1500],
    }


def _get_full_tool_output_tool(output_id):
    result = _large_tool_outputs.get(str(output_id))
    if result is None:
        return {"error": f"No stored output with id '{output_id}'. It may have aged out, or the id is wrong."}
    return result


CONTEXT_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_full_tool_output",
            "description": "Retrieve the full, uncompressed result of an earlier tool call that was shown to you as a compressed head/tail preview because it was too large. Only call this if the omitted middle genuinely matters -- most of the time the preview already has what you need.",
            "parameters": {
                "type": "object",
                "properties": {"output_id": {"type": "string", "description": "The output_id named in the compressed result's note."}},
                "required": ["output_id"],
            },
        },
    },
]


def _resolve_mode_and_workspace(req):
    """Explicit, named mode resolution -- not inferred piecemeal from
    workspace truthiness scattered across several call sites. Athena's
    own delegation session (ATHENA_BOTS_SESSION_ID) always runs in
    real coding-harness mode: she was designed as full workspace
    capability with delegation layered on top, never a lesser casual-
    chat base, so her own request's workspace field (which her
    frontend never actually populates) falls back to the configured
    default workspace instead of silently downgrading her to casual
    chat -- which is what was actually happening before this existed."""
    if req.session_id == ATHENA_BOTS_SESSION_ID:
        workspace = req.workspace or settings.load_settings().get("workspace") or ""
        return "athena_delegation", workspace
    if req.workspace:
        return "workspace", req.workspace
    return "casual", ""


def _get_mode_system_prompt(mode, req):
    """The stable identity and rules only -- memory, skills, and
    pinned-message context are deliberately NOT concatenated in here.
    They're dynamic, situational material that changes every request,
    kept as a separate context message instead (see
    _get_dynamic_context_message) rather than fused into the same
    block as the model's actual operating instructions, the same
    distinction DeepSeek Harness draws between a stable system prompt
    and dynamically-sourced runtime context."""
    if mode in ("workspace", "athena_delegation"):
        base = CODING_HARNESS_SYSTEM_PROMPT
    else:
        base = BASE_SYSTEM_PROMPT + AGENT_SYSTEM_SUFFIX
    if mode == "athena_delegation":
        base = base + _get_bot_delegation_prompt_section(req.workspace, async_enabled=bool(settings.load_settings().get("delegation_async_enabled")), laya_gating_enabled=bool(settings.load_settings().get("laya_gating_enabled")))
    return base


def _get_dynamic_context_message(req):
    """Memory, skills, and pinned-message context, assembled as their
    own separate message rather than string-concatenated onto the
    system prompt. Returns None when there's genuinely nothing to
    include, so the caller can skip adding an empty message."""
    content = _get_memory_context() + _get_skills_context(req.allowed_skills) + _get_pinned_context(req.session_id)
    if not content.strip():
        return None
    return {"role": "user", "content": content}


# Blind-exploration tools that require search_codebase to have been
# called at least once this turn first -- the same "orient before you
# dig" discipline already asked for in prose (CODING_HARNESS_SECTIONS'
# tool_selection section) but enforced structurally: a model can drift
# off a prompt instruction under pressure, it can't call a tool that
# isn't in its tool list.
_SEARCH_GATED_TOOL_NAMES = {"bash", "read_file"}

# The full set of self-investigation tools, cut off once a turn has
# leaned on them past the threshold below AND no delegation has
# happened yet this turn -- narrowing what's reachable down to
# delegation (plus whatever write tools were already available) rather
# than asking the model to notice it's over-exploring and delegate on
# its own judgment. Once she's delegated at least once this turn, the
# cutoff no longer applies: initial research should generally go
# through sub-agents (this still forces that), but she isn't locked out
# of her own follow-up digging on top of real delegation findings --
# e.g. confirming something a bot's scratch note pointed to.
_EXPLORATION_TOOL_NAMES = {
    "bash", "read_file", "list_files", "search_codebase",
    "find_definition", "find_references", "type_info",
}
_DELEGATION_TOOL_NAMES = {"run_delegation_step", "plan_delegation"}
_SELF_INVESTIGATION_TOOL_THRESHOLD = 6

# Below this many total tool calls in one turn (any tool, any mode), a
# turn is small enough not to need an explicit plan. At or past it,
# _get_mode_tools narrows the tool list down to write_plan alone until
# it's called -- a real structural checkpoint, not a prose reminder,
# replacing what used to be enforced only by CODING_HARNESS_SECTIONS'
# plan_before_acting sentence. Deliberately mode-independent (unlike
# _SELF_INVESTIGATION_TOOL_THRESHOLD, which only applies in
# athena_delegation): the observed failure mode -- 23 tool calls
# rediscovering a fixed weather API endpoint -- happened in plain casual
# chat, which had no per-turn structure of any kind before this existed.
# This is NOT a cap on how much work can happen -- once write_plan has
# been called, the gate never re-applies for the rest of the turn, so
# total tool-call volume is unaffected; it only forces one checkpoint.
_PLAN_REQUIRED_THRESHOLD = 3


def _get_mode_tools(mode, req, called_tool_names=None, exploration_call_count=0, total_tool_call_count=0):
    """Casual chat no longer carries bash/backup_file -- a mode only
    gets tools it actually needs, matching the same principle behind
    bots never getting write tools at the registration level rather
    than being told not to use them.

    called_tool_names/exploration_call_count/total_tool_call_count let a
    caller recompute this per round within one turn (see chat_stream's
    generation loop) so tool availability can shift as the model's own
    actions this turn accumulate, instead of being fixed once at request
    start."""
    tools = lcm_client.get_lcm_tools() + PLAN_TOOL_SCHEMAS
    if req.search_url:
        tools = tools + WEB_TOOL_SCHEMAS
    if mode in ("workspace", "athena_delegation"):
        tools = tools + BASH_TOOL_SCHEMAS + BACKUP_TOOL_SCHEMAS + RAG_TOOL_SCHEMAS + FILE_TOOL_SCHEMAS + LSP_TOOL_SCHEMAS + CONTEXT_TOOL_SCHEMAS
    if _scan_skills():
        tools = tools + SKILL_TOOL_SCHEMAS
    if mode == "athena_delegation":
        tools = tools + _bot_delegation_tool_schemas_for_mode(bool(settings.load_settings().get("delegation_async_enabled")))
    called = called_tool_names or set()
    if mode in ("workspace", "athena_delegation"):
        if "search_codebase" not in called:
            tools = [t for t in tools if t.get("function", {}).get("name") not in _SEARCH_GATED_TOOL_NAMES]
        if (mode == "athena_delegation" and exploration_call_count >= _SELF_INVESTIGATION_TOOL_THRESHOLD
                and not (called & _DELEGATION_TOOL_NAMES)):
            tools = [t for t in tools if t.get("function", {}).get("name") not in _EXPLORATION_TOOL_NAMES]
        if mode == "athena_delegation" and "plan_delegation" in called:
            # Hard commitment: once a plan has been submitted, any
            # correction (a rejected step, an incomplete result) must go
            # through resubmitting plan_delegation, never an ad hoc
            # one-off run_delegation_step call. This is the structural
            # fix for the observed failure mode where a rejected
            # plan_delegation step led to abandoning the plan entirely
            # in favor of reworded, one-at-a-time retries.
            tools = [t for t in tools if t.get("function", {}).get("name") != "run_delegation_step"]
    if total_tool_call_count >= _PLAN_REQUIRED_THRESHOLD and "write_plan" not in called:
        tools = [t for t in tools if t.get("function", {}).get("name") == "write_plan"]
    if req.allowed_tools is not None:
        _allowed = set(req.allowed_tools)
        tools = [t for t in tools if t.get("function", {}).get("name") in _allowed]
    return tools


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
    allowed_tools: Optional[List[str]] = None  # None = no restriction (normal chat); a task run passes its own explicit allowlist
    allowed_skills: Optional[List[str]] = None  # same, for which skills load_skill can actually load
    provider: str = ""  # "" = Ollama-native (default); "openai"/"openrouter"/"custom"/"anthropic"/"google" route through an adapter in _stream_completion
    api_key: str = ""  # only used when provider is set


class TtsIn(BaseModel):
    text: str




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
@app.post("/api/pin")
def pin_message(req: PinIn):
    """Upsert-or-delete, mirroring /api/rate. Stores the message's
actual
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
@app.get("/api/pins/{session_id}")
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
def _get_pinned_context(session_id: str) -> str:
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


def _search_codebase(query: str, limit: int = 3):
    """Tool-callable RAG search over the indexed codebase, returning
    structured results the model can inspect and, if needed, refine
    with a follow-up query -- rather than a fixed block of context
    passively injected every single turn regardless of whether it's
    actually needed or matches what the model is currently after."""
    if not query:
        return {"error": "query is required"}
    try:
        limit = max(1, min(int(limit), 10))
    except (TypeError, ValueError):
        limit = 3
    try:
        results = rag.search(query, limit=limit)
    except RuntimeError as e:
        # The embedding model failed to load (or is still loading for the
        # first time and hasn't finished) -- surface a clear, structured
        # error instead of letting this propagate up out of tool dispatch.
        return {"error": f"search_codebase is temporarily unavailable: {e}"}
    if not results:
        return {"query": query, "results": [], "note": "No matches found -- try different or broader search terms."}
    return {
        "query": query,
        "results": [
            {"filepath": r["filepath"], "content": r["content"][:3000], "score": r["score"]}
            for r in results
        ],
    }


class RAGIndexIn(BaseModel):
    workspace: str = "."

@app.post("/api/rag/index")
def index_codebase(req: RAGIndexIn):
    """Manually triggers RAG indexing for a workspace directory.
    Call this after codebase changes to keep the index up to date."""
    workspace = req.workspace or "."
    try:
        rag.index_codebase(workspace)
        return {"ok": True}
    except Exception as e:
        return {"error": f"Indexing failed: {e}"}

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




@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html") as f:
        return f.read()


class DetectModelsIn(BaseModel):
    url: str = ""
    type: str = ""
    provider: str = ""
    api_key: str = ""

@app.post("/api/detect-models")
def detect_models(req: DetectModelsIn):
    """Live model listing -- local Ollama endpoints via /api/tags as
    before; online providers via each one's own models endpoint, all
    of which now exist (OpenAI, Anthropic, and OpenRouter all expose
    one; Google's is prefixed differently and handled separately
    below). Returns plain model-id strings in every case, so the
    frontend's enabledModels/detectedModels handling stays identical
    regardless of which kind of endpoint this was called for."""
    try:
        if req.type == "local":
            base = req.url.rstrip("/")
            resp = httpx.get(f"{base}/api/tags", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                names = [m.get("name") for m in data.get("models", []) if m.get("name")]
                return {"models": names}
            return {"error": f"Endpoint responded with HTTP {resp.status_code}"}
        if req.provider == "openai" or req.provider == "custom":
            if not req.api_key:
                return {"error": "An API key is required for OpenAI-compatible model detection."}
            base = (req.url or "https://api.openai.com/v1").rstrip("/")
            resp = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {req.api_key}"}, timeout=10)
            if resp.status_code == 200:
                names = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
                return {"models": sorted(names)}
            return {"error": f"Endpoint responded with HTTP {resp.status_code}: {resp.text[:200]}"}

        if req.provider == "openrouter":
            resp = httpx.get("https://openrouter.ai/api/v1/models", timeout=10)
            if resp.status_code == 200:
                names = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
                return {"models": sorted(names)}
            return {"error": f"OpenRouter responded with HTTP {resp.status_code}"}
        if req.provider == "anthropic":
            resp = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": req.api_key, "anthropic-version": "2023-06-01"},
                timeout=10,
            )
            if resp.status_code == 200:
                names = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
                return {"models": sorted(names)}
            return {"error": f"Endpoint responded with HTTP {resp.status_code}: {resp.text[:200]}"}
        if req.provider == "google":
            resp = httpx.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={req.api_key}",
                timeout=10,
            )
            if resp.status_code == 200:
                names = [m["name"].split("/", 1)[-1] for m in resp.json().get("models", []) if m.get("name")]
                return {"models": sorted(names)}
            return {"error": f"Endpoint responded with HTTP {resp.status_code}: {resp.text[:200]}"}
        base = req.url.rstrip("/")
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
    return _read_file(workspace, path, offset)

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

def _get_skills_context(allowed_skills=None) -> str:
    skills = _scan_skills()
    if allowed_skills is not None:
        allowed_set = set(allowed_skills)
        skills = [s for s in skills if s["name"] in allowed_set]
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


def _rm_is_recursive(args: list) -> bool:
    for a in args:
        if a == "--recursive":
            return True
        if a.startswith("-") and not a.startswith("--") and "r" in a[1:].lower():
            return True
    return False


def _rm_targets_broad(args: list) -> bool:
    positional = [a for a in args if not a.startswith("-")]
    broad = {".", "/", "*", "..", ""}
    return (not positional) or any(p in broad for p in positional)


# Write-capable shell access, confined to a single workspace. Like the
# read-only bash tool, this never invokes a real shell (shell=False),
# so shell metacharacters in an argument are inert literal text, not
# injection surface. Confinement here is structurally weaker than the
# file tools' _resolve_workspace_path though: cwd is fixed to the
# workspace root, but individual arguments aren't path-validated (no
# reliable way to tell "this argument is a path" from "this argument
# is just a string" across an arbitrary allowlisted command). The
# actual safety boundary is: a small allowlist of genuinely useful
# commands, cwd pinned to the workspace, and explicit rejection of the
# two clearly destructive patterns (broad recursive rm, force-push) --
# not a guarantee that no path outside the workspace can ever be named.
BASH_EXEC_ALLOWED_COMMANDS = {
    "npm", "npx", "yarn", "pip", "pip3", "python3", "node", "pytest",
    "git", "make", "mkdir", "touch", "mv", "cp", "rm",
}
BASH_EXEC_TIMEOUT_SECONDS = 300


def _validate_bash_exec_call(command: str, args: list):
    """Shared safety validation for both the blocking and background
    forms of bash_exec -- same allowlist, same shell-operator block,
    same rm/force-push refusals, so a background command gets exactly
    the same structural guarantees a blocking one does. Returns an
    error dict if invalid, or None if the call is safe to run."""
    if not command:
        return {"error": "Missing 'command'. Example: to run npm install, set command to 'npm' (just the program name) and args to ['install'] (a list of separate arguments)."}
    if not isinstance(command, str) or " " in command or command.startswith("[") or command.startswith('"'):
        return {
            "error": "'" + str(command) + "' looks like a full command line or a JSON array, not a bare program name. "
            "command must be ONLY the program name by itself, e.g. 'npm' -- never the whole command line, "
            "and never the command name repeated inside args. Everything after the program name goes in "
            "args as separate list items instead: to run npm install --save-dev foo, use "
            "command='npm' and args=['install', '--save-dev', 'foo']."
        }
    if command not in BASH_EXEC_ALLOWED_COMMANDS:
        return {"error": "Command '" + command + "' is not allowed. Allowed commands: " + ", ".join(sorted(BASH_EXEC_ALLOWED_COMMANDS))}
    shell_operators = ("|", ">", "<", "&", ";", "$(", "`", "&&", "||")
    for arg in args:
        if not isinstance(arg, str):
            return {"error": "All arguments must be strings."}
        if any(op in arg for op in shell_operators):
            return {
                "error": "Argument '" + arg + "' contains a shell operator (pipe, redirect, chaining, or substitution). "
                "There is no shell here at all -- this tool runs the program directly, so operators like |, >, 2>/dev/null, "
                "&&, or $(...) have no special meaning and can't do what they'd do in a real shell; they'd just be passed "
                "as literal, meaningless text to the program. Make separate bash_exec calls instead."
            }
    if command == "rm" and _rm_is_recursive(args) and _rm_targets_broad(args):
        return {"error": "Refusing: recursive rm with a broad or missing target (e.g. '.', '/', '*', '..', or no target at all). Name specific files or directories instead."}
    if command == "git" and args and args[0] == "push":
        force_flags = {"-f", "--force", "--force-with-lease"}
        if any(a in force_flags or a.startswith("--force") for a in args):
            return {"error": "Refusing: force-push is blocked. Run a normal 'git push' instead."}
    return None


ATHENA_SANDBOX_IMAGE = "athena-sandbox:latest"


def _docker_sandbox_available():
    """Checked live on every call rather than cached -- a stale cached
    'yes' would be exactly the false-confidence failure mode a sandbox
    can't afford: if Docker or the image genuinely isn't there right
    now, bash_exec must say so plainly and refuse, never silently run
    unsandboxed."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", ATHENA_SANDBOX_IMAGE],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _build_docker_sandbox_cmd(command, args, workspace):
    """Every bash_exec command runs inside this container, not
    directly on the host -- allowlisting/pattern-blocking in
    _validate_bash_exec_call decides WHETHER a command runs; this
    decides what it can actually reach once it does. Hardened per
    DeepSeek Harness's own real-world Docker practice: dropped
    capabilities, no privilege escalation, read-only root filesystem
    (only the mounted workspace and /tmp are writable), no Docker
    socket, no credential paths, nothing beyond the workspace itself
    visible. Network stays available (install-type commands need it);
    filesystem containment is the actual protection here."""
    real_workspace = os.path.realpath(workspace)
    uid, gid = os.getuid(), os.getgid()
    return [
        "docker", "run", "--rm",
        "--network", "bridge",
        "-v", f"{real_workspace}:{real_workspace}",
        "-w", real_workspace,
        "--user", f"{uid}:{gid}",
        "-e", "HOME=/tmp",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--read-only",
        "--tmpfs", "/tmp",
        "--memory", "512m",
        "--cpus", "1",
        "--pids-limit", "100",
        ATHENA_SANDBOX_IMAGE,
        command,
    ] + list(args)


def _execute_write_bash(command: str, args: list, workspace: str):
    if not workspace:
        return {"error": "bash_exec requires an active workspace."}
    err = _validate_bash_exec_call(command, args)
    if err:
        return err
    if not _docker_sandbox_available():
        return {"error": "The sandbox container isn't available right now (Docker or the athena-sandbox image is missing) -- refusing to run this command unsandboxed rather than silently skipping the isolation it's supposed to have."}
    try:
        result = subprocess.run(
            _build_docker_sandbox_cmd(command, args, workspace),
            capture_output=True,
            text=True,
            timeout=BASH_EXEC_TIMEOUT_SECONDS,
            shell=False,
        )
        output = result.stdout
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        return {"command": command, "args": args, "output": output[:10000], "exit_code": result.returncode}
    except FileNotFoundError:
        return {"error": "Command '" + command + "' not found on this system."}
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {BASH_EXEC_TIMEOUT_SECONDS} seconds."}
    except Exception as e:
        return {"error": f"bash execution failed: {e}"}


# Background command execution -- same allowlist/safety validation as
# the blocking bash_exec above, but returns immediately with a handle
# instead of waiting for the command to finish. Built as the deliberate
# alternative to a real PTY: gives the actual capability a PTY exists
# for (long-running processes, checking on progress) without losing
# the structural safety guarantee (shell=False, no real shell ever
# involved) that a true interactive terminal can't preserve.
_bg_processes = {}
_bg_processes_lock = threading.Lock()
_bg_process_counter = [0]


def _start_background_bash(command: str, args: list, workspace: str):
    if not workspace:
        return {"error": "bash_exec requires an active workspace."}
    err = _validate_bash_exec_call(command, args)
    if err:
        return err
    if not _docker_sandbox_available():
        return {"error": "The sandbox container isn't available right now (Docker or the athena-sandbox image is missing) -- refusing to run this command unsandboxed rather than silently skipping the isolation it's supposed to have."}
    try:
        proc = subprocess.Popen(
            _build_docker_sandbox_cmd(command, args, workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            shell=False,
        )
    except FileNotFoundError:
        return {"error": "Command '" + command + "' not found on this system."}
    except Exception as e:
        return {"error": f"Failed to start background process: {e}"}

    with _bg_processes_lock:
        _bg_process_counter[0] += 1
        process_id = str(_bg_process_counter[0])
        entry = {"proc": proc, "command": command, "args": args, "output": "", "output_lock": threading.Lock(), "started_at": time.time()}
        _bg_processes[process_id] = entry

    def _drain():
        try:
            for line in proc.stdout:
                with entry["output_lock"]:
                    entry["output"] += line
                    if len(entry["output"]) > 50000:
                        entry["output"] = entry["output"][-50000:]
        except Exception:
            pass

    threading.Thread(target=_drain, daemon=True).start()
    return {"process_id": process_id, "command": command, "args": args, "status": "started"}


def _check_background_bash(process_id: str):
    entry = _bg_processes.get(str(process_id))
    if not entry:
        return {"error": f"No background process with id '{process_id}'. It may have already been stopped, or the id is wrong -- check with a process_id returned by a prior background-start call."}
    proc = entry["proc"]
    with entry["output_lock"]:
        output = entry["output"]
    exit_code = proc.poll()
    return {
        "process_id": process_id, "command": entry["command"], "args": entry["args"],
        "running": exit_code is None, "exit_code": exit_code, "output": output[-10000:],
    }


def _stop_background_bash(process_id: str, force: bool = False):
    entry = _bg_processes.get(str(process_id))
    if not entry:
        return {"error": f"No background process with id '{process_id}'."}
    proc = entry["proc"]
    if proc.poll() is not None:
        return {"error": f"Process '{process_id}' has already exited (code {proc.poll()})."}
    try:
        proc.kill() if force else proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    except Exception as e:
        return {"error": f"Failed to stop process: {e}"}
    return {"process_id": process_id, "stopped": True, "exit_code": proc.poll()}


def _read_backup_manifest():
    manifest_path = os.path.join(_BACKUP_ROOT, "manifest.jsonl")
    if not os.path.isfile(manifest_path):
        return []
    entries = []
    with open(manifest_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _restore_one_entry(entry):
    """Restore a single manifest entry back to its original path,
    backing up whatever's currently there first (via the same
    content-addressable _backup_file) so a restore is never a
    one-way, unrecoverable action -- if it turns out to be the wrong
    version, the pre-restore state is itself just another backup to
    restore from."""
    target_path = entry["path"]
    content_hash = entry["hash"]
    object_path = os.path.join(_BACKUP_ROOT, "objects", content_hash[:2], content_hash[2:] + ".gz")
    if not os.path.isfile(object_path):
        return {"path": target_path, "error": f"Backup object missing on disk for hash {content_hash}."}
    pre_restore_backup = None
    if os.path.isfile(target_path):
        pre_restore_backup = _backup_one_file(target_path, None, [])
    os.makedirs(os.path.dirname(target_path), exist_ok=True) if os.path.dirname(target_path) else None
    with gzip.open(object_path, "rb") as f:
        content = f.read()
    with open(target_path, "wb") as f:
        f.write(content)
    try:
        rag.index_codebase(".")
    except Exception as e:
        print(f"[RAG] auto-reindex after restore failed: {e}", flush=True)
    return {
        "path": target_path,
        "restored_hash": content_hash,
        "restored_from_timestamp": entry["timestamp"],
        "bytes": len(content),
        "pre_restore_backup_hash": pre_restore_backup["hash"] if pre_restore_backup else None,
    }


def _restore_file(path: str = "", snapshot_id: str = "", restore_hash: str = "", restore_timestamp: float = None):
    if bool(path) == bool(snapshot_id):
        return {"error": "Provide exactly one of 'path' or 'snapshot_id', not both and not neither."}

    entries = _read_backup_manifest()

    if snapshot_id:
        matches = [e for e in entries if e.get("snapshot_id") == snapshot_id]
        if not matches:
            return {"error": f"No backups found for snapshot_id '{snapshot_id}'."}
        results = [_restore_one_entry(e) for e in matches]
        return {"snapshot_id": snapshot_id, "files_restored": len(results), "results": results}

    matches = [e for e in entries if e.get("path") == path]
    if not matches:
        return {"error": f"No backups found for path '{path}'."}
    if restore_hash:
        chosen = next((e for e in matches if e.get("hash") == restore_hash), None)
        if not chosen:
            return {"error": f"No backup found for path '{path}' with hash '{restore_hash}'."}
    elif restore_timestamp is not None:
        eligible = [e for e in matches if e["timestamp"] <= restore_timestamp]
        if not eligible:
            return {"error": f"No backup found for path '{path}' at or before timestamp {restore_timestamp}."}
        chosen = max(eligible, key=lambda e: e["timestamp"])
    else:
        chosen = max(matches, key=lambda e: e["timestamp"])

    return _restore_one_entry(chosen)


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
    {
        "type": "function",
        "function": {
            "name": "bash_exec",
            "description": "Run a write-capable shell command, confined to the current workspace (cwd is pinned to the workspace root). Only these commands are allowed: npm, npx, yarn, pip, pip3, python3, node, pytest, git, make, mkdir, touch, mv, cp, rm. No shell chaining, pipes, or redirection -- provide the command and its arguments as a separate list, not as one combined string. Recursive rm with a broad target (., /, *, .., or no target) and git push --force are refused. Runs inside an isolated sandbox container: network access works (git clone/push over HTTPS, npm/pip installs), but nothing outside the mounted workspace is visible or writable -- no host SSH keys, no credential files, no other directories. If a command fails specifically because it can't find or write to something outside the workspace, that's this containment working as intended, not a bug to work around. Requires an active workspace; there is no bash_exec without one. Timeout is 300 seconds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bare command name, e.g. 'npm' or 'git'. No path, no shell operators."},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Arguments to the command, each as a separate array element (e.g. [\"install\"] for npm install, or [\"commit\", \"-m\", \"fix bug\"] for git commit -m \"fix bug\").",
                    },
                },
                "required": ["command", "args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_exec_start",
            "description": "Start a bash_exec command in the background instead of waiting for it to finish -- for anything long-running (a dev server, a build watcher) or where you need to check progress partway through. Same allowlist and safety rules as bash_exec. Returns a process_id immediately; use bash_exec_check to see output so far, and bash_exec_stop to end it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bare command name, e.g. 'npm'."},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments to the command, each as a separate array element."},
                },
                "required": ["command", "args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_exec_check",
            "description": "Check a background command started with bash_exec_start -- returns its output so far and whether it's still running.",
            "parameters": {
                "type": "object",
                "properties": {"process_id": {"type": "string", "description": "The process_id returned by bash_exec_start."}},
                "required": ["process_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_exec_stop",
            "description": "Stop a background command started with bash_exec_start. Tries a graceful stop first unless force is true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "process_id": {"type": "string", "description": "The process_id returned by bash_exec_start."},
                    "force": {"type": "boolean", "description": "If true, kill immediately instead of asking it to stop gracefully first."},
                },
                "required": ["process_id"],
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
            "description": "Read a file's contents. Large files are capped at 20000 characters per call -- if the response has has_more: true, call again with offset set to the returned next_offset to continue reading from where you left off, rather than assuming you've seen the whole file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace root."},
                    "offset": {"type": "integer", "description": "Character offset to start reading from. Omit or use 0 to start from the beginning."},
                },
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

BACKUP_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "backup_file",
            "description": "Save a copy of a file's (or an entire directory's) current content before making changes to it, so it can be recovered later if something goes wrong. Works on a single file or a whole directory -- pass a directory path to back up everything inside it (recursively, skipping .git/node_modules/build-output-style directories) as one coherent snapshot. Deduplicated by content -- backing up something whose content hasn't actually changed since a previous backup costs no extra storage. Use this before editing anything whose current state matters and hasn't already been captured (e.g. by version control) this turn.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Absolute path to the file or directory to back up (e.g. /home/user/project or /home/user/project/main.py)."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restore_file",
            "description": "Restore a previously backed-up file (or an entire directory snapshot) back to disk, undoing a change by bringing back an earlier version. Provide EITHER 'path' (restores that one file's most recent backup, or a specific one if 'restore_hash' or 'restore_timestamp' is also given) OR 'snapshot_id' (restores every file from that whole-directory backup at once) -- never both. Whatever is currently at the target path is itself backed up first, automatically, before being overwritten, so a restore is never a one-way action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the single file to restore. Omit if using snapshot_id instead."},
                    "snapshot_id": {"type": "string", "description": "Restore an entire directory snapshot (from a directory backup_file call) back to that exact point in time. Omit if using path instead."},
                    "restore_hash": {"type": "string", "description": "Optional: restore this exact backed-up version of 'path' by its content hash, instead of the most recent one."},
                    "restore_timestamp": {"type": "number", "description": "Optional: restore the most recent backup of 'path' that was taken at or before this unix timestamp, instead of the latest one overall."},
                },
                "required": [],
            },
        },
    },
]

# Mode-independent -- available in casual, workspace, and athena_delegation
# alike (see _get_mode_tools), unlike every other schema list here which is
# gated to specific modes. Backs the plan-required structural gate in
# chat_stream's generate() loop: once a turn has made several tool calls
# without writing a plan, every tool except write_plan disappears until it's
# called. This replaces relying on the prose-only "state a short plan
# first" sentence that used to be the entire mechanism (see
# CODING_HARNESS_SECTIONS' plan_before_acting section) -- prompt-only
# guardrails here have repeatedly been observed to degrade under pressure;
# see _SELF_INVESTIGATION_TOOL_THRESHOLD above for the same lesson applied
# to blind self-investigation.
PLAN_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "write_plan",
            "description": "Record (or completely replace) the ordered list of steps for what you're about to do this turn. Call this once you can see a task is going to take more than a couple of tool calls, before diving into the work -- not after. This does not limit how many tool calls you can make afterward; it only requires deciding on a structure first. Calling this again replaces the previous plan entirely (use update_plan_step instead if you just want to mark progress on the existing plan).",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of short, concrete step descriptions, e.g. ['Find where X is defined', 'Check how Y calls it', 'Make the fix', 'Verify it']."
                    },
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan_step",
            "description": "Mark progress on one step of the plan already recorded via write_plan. Returns the full plan re-rendered with the update applied. Fails with a clear error if write_plan hasn't been called yet this turn, or if the index is out of range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Zero-based index of the step to update, matching its position in the list passed to write_plan."},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "done", "failed"], "description": "New status for this step."},
                    "note": {"type": "string", "description": "Optional short note about this step's outcome (e.g. what was found, or why it failed)."},
                },
                "required": ["index", "status"],
            },
        },
    },
]

RAG_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_codebase",
            "description": "Semantic search over the indexed codebase (built ahead of time, separately from this conversation) -- embeds your query and ranks chunks of real code by meaning, not by literal word overlap, so a query phrased differently from the code's own vocabulary can still find it. Each Python function/class is its own chunk (a large class is split per-method); other file types are chunked by section. Use this for orientation questions (where does X live, what does this area of the codebase roughly do) before falling back to list_files or grep to explore blind. Results are a starting point, not a source of truth: a chunk can still be stale relative to changes made earlier this session, and an oversized function may be split across more than one result -- once you're actually about to make an edit, read the real file directly first. If results aren't useful, try a different, more specific query rather than giving up on the tool entirely.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language or keyword search query describing what you're looking for."},
                    "limit": {"type": "integer", "description": "Maximum number of result chunks to return (default 3, max 10)."},
                },
                "required": ["query"],
            },
        },
    },
]


# Long-lived LSP process registry, mirroring the bundled-LCM subprocess
# pattern above: one pyright-langserver process per workspace root,
# started lazily on first use and reused across sessions/turns rather
# than restarted per query (re-indexing a real codebase from cold is
# slow, and every session working on the same workspace should share
# one warm process the same way multiple editor windows on one project
# share a single language server). An idle sweep tears down processes
# nobody has used in a while, since a self-hosted server that runs for
# weeks shouldn't accumulate one live pyright process per workspace
# ever touched.
_lsp_clients = {}
_lsp_clients_lock = threading.Lock()
LSP_IDLE_TIMEOUT_SECONDS = 1800


def _get_lsp_client(workspace: str):
    from lsp_client import LSPClient
    root = os.path.realpath(workspace)
    now = time.time()
    with _lsp_clients_lock:
        for key, client in list(_lsp_clients.items()):
            if now - client.last_used > LSP_IDLE_TIMEOUT_SECONDS:
                try:
                    client.shutdown()
                except Exception:
                    pass
                del _lsp_clients[key]
        client = _lsp_clients.get(root)
        if client is None:
            client = LSPClient(root)
            _lsp_clients[root] = client
        client.last_used = now
        return client


def _shutdown_lsp_clients():
    with _lsp_clients_lock:
        for client in _lsp_clients.values():
            try:
                client.shutdown()
            except Exception:
                pass
        _lsp_clients.clear()


atexit.register(_shutdown_lsp_clients)


LSP_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "find_definition",
            "description": "Find where a symbol (function, class, variable) is actually defined, using real semantic code analysis (not text search) -- follows imports and scoping the way a real editor's 'go to definition' does. Give the line where the symbol is USED (or defined), and the exact visible text of the symbol itself; no character/column counting needed. Requires an active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, relative to the workspace root."},
                    "line": {"type": "integer", "description": "1-indexed line number where the symbol appears."},
                    "symbol": {"type": "string", "description": "The exact visible text of the symbol, e.g. '_execute_readonly_bash'."},
                },
                "required": ["path", "line", "symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": "Find every real usage of a symbol across the whole workspace, using semantic analysis -- not a text/grep match, so it won't miss aliased imports or false-match unrelated identical names elsewhere. Give the line where the symbol is defined (or any usage of it) and its exact visible text; no character/column counting needed. Requires an active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, relative to the workspace root."},
                    "line": {"type": "integer", "description": "1-indexed line number where the symbol appears."},
                    "symbol": {"type": "string", "description": "The exact visible text of the symbol, e.g. '_execute_readonly_bash'."},
                },
                "required": ["path", "line", "symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_info",
            "description": "Get the real inferred type and signature of a symbol at a specific point in the code, the way hovering in a real editor would show -- more reliable than guessing from context. Give the line where the symbol appears and its exact visible text; no character/column counting needed. Requires an active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file, relative to the workspace root."},
                    "line": {"type": "integer", "description": "1-indexed line number where the symbol appears."},
                    "symbol": {"type": "string", "description": "The exact visible text of the symbol, e.g. '_execute_readonly_bash'."},
                },
                "required": ["path", "line", "symbol"],
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

_READ_FILE_CHUNK_SIZE = 20000

def _read_file(workspace: str, rel_path: str, offset: int = 0):
    try:
        target = _resolve_workspace_path(workspace, rel_path)
        if not os.path.isfile(target):
            return {"error": f"Not a file: {rel_path}"}
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        offset = max(0, offset or 0)
        total_chars = len(content)
        chunk = content[offset:offset + _READ_FILE_CHUNK_SIZE]
        next_offset = offset + len(chunk)
        has_more = next_offset < total_chars
        result = {"path": rel_path, "content": chunk, "total_chars": total_chars, "has_more": has_more}
        if has_more:
            result["next_offset"] = next_offset
        return result
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
        try:
            rag.index_codebase(workspace)
        except Exception as e:
            print(f"[RAG] auto-reindex after write failed: {e}", flush=True)
        return {"path": rel_path, "edited": True}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"edit_file failed: {e}"}

# Stored next to Athena's own app data (pins.db, tasks.db, etc.), never
# inside a workspace -- so a backup can never be accidentally
# git-committed, wiped if the workspace gets reset/deleted, or clutter
# the actual project directory the user is working in.
_BACKUP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "file_backups")

# Skipped when backing up a whole directory -- either already under
# their own version control, or fully regeneratable, so backing them
# up would just burn storage and time for no real recovery value.
_BACKUP_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build", ".next"}

def _backup_one_file(abs_path, snapshot_id, manifest_lines):
    """The actual per-file content-addressable backup step, factored
    out so both a single-file and a whole-directory backup share the
    exact same logic and guarantees, rather than the directory case
    reimplementing it separately. Appends one manifest line (not yet
    written to disk) to manifest_lines and returns a small per-file
    result dict; the caller decides when to actually flush the batch."""
    with open(abs_path, "rb") as f:
        raw = f.read()
    content_hash = hashlib.sha256(raw).hexdigest()
    objects_dir = os.path.join(_BACKUP_ROOT, "objects", content_hash[:2])
    object_path = os.path.join(objects_dir, content_hash[2:] + ".gz")
    already_existed = os.path.isfile(object_path)
    if not already_existed:
        os.makedirs(objects_dir, exist_ok=True)
        with gzip.open(object_path, "wb") as f:
            f.write(raw)
    manifest_lines.append(json.dumps({
        "path": abs_path,
        "hash": content_hash,
        "timestamp": time.time(),
        "bytes": len(raw),
        "snapshot_id": snapshot_id,
    }))
    return {"path": abs_path, "hash": content_hash, "deduplicated": already_existed, "bytes": len(raw)}

def _backup_file(path: str):
    """Content-addressable backup, the same underlying idea as Git's
    own object store: each file's content is hashed (SHA-256) and
    stored at a path derived entirely from that hash, gzip-compressed,
    with a separate append-only manifest recording which path and
    timestamp each hash belongs to. This gives two properties for
    free, as a structural consequence of the design rather than
    special-cased logic to enforce: (1) identical content is only
    ever stored once no matter how many times it gets backed up, and
    (2) a backup taken after a real edit can never collide with and
    silently overwrite one taken before it, since different content
    mathematically produces a different hash and therefore a
    different storage path.

    Not scoped to any workspace -- unlike the other file tools, a
    backup is read-only with respect to its source (it only ever
    copies content into Athena's own storage, never touches the
    original), so the usual workspace-boundary safety check doesn't
    apply the same way here. Works on either a single file or a whole
    directory. For a directory, every file underneath it (skipping
    common noise dirs) is backed up individually using the exact same
    logic, but all tagged with one shared snapshot_id, so a future
    restore tool can recognize them as one coherent snapshot and
    restore the whole directory back to that exact point in time at
    once, rather than as a pile of unrelated individual file backups."""
    try:
        target = os.path.realpath(os.path.expanduser(path))
        manifest_lines = []
        if os.path.isfile(target):
            result = _backup_one_file(target, None, manifest_lines)
        elif os.path.isdir(target):
            snapshot_id = uuid.uuid4().hex
            files_backed_up = []
            for root, dirs, files in os.walk(target):
                dirs[:] = [d for d in dirs if d not in _BACKUP_SKIP_DIRS]
                for name in files:
                    abs_path = os.path.join(root, name)
                    files_backed_up.append(_backup_one_file(abs_path, snapshot_id, manifest_lines))
            result = {
                "path": target,
                "snapshot_id": snapshot_id,
                "files_backed_up": len(files_backed_up),
                "files_deduplicated": sum(1 for f in files_backed_up if f["deduplicated"]),
                "total_bytes": sum(f["bytes"] for f in files_backed_up),
            }
        else:
            return {"error": f"Not a file or directory: {path}"}
        os.makedirs(_BACKUP_ROOT, exist_ok=True)
        manifest_path = os.path.join(_BACKUP_ROOT, "manifest.jsonl")
        with open(manifest_path, "a", encoding="utf-8") as f:
            for line in manifest_lines:
                f.write(line + "\n")
        return result
    except Exception as e:
        return {"error": f"backup_file failed: {e}"}

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
                try:
                    rag.index_codebase(workspace)
                except Exception as e:
                    print(f"[RAG] auto-reindex after write failed: {e}", flush=True)
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
        try:
            rag.index_codebase(workspace)
        except Exception as e:
            print(f"[RAG] auto-reindex after write failed: {e}", flush=True)

        delta = len(new_lines) - (end - start_line + 1)
        result = {"path": rel_path, "edited": True, "lines_replaced": f"{start_line}-{end_line}"}
        if delta != 0:
            result["warning"] = f"This changed the file's line count by {delta:+d}. Any other line numbers you had for this file are now stale -- re-check before another replace_lines call."
        return result
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"replace_lines failed: {e}"}


# ==================== Online provider adapters ====================
# generate()'s round loop, loop detection, tool-call execution, and
# doom-loop defenses are all built around Ollama's own streaming chunk
# shape: {"message": {"content": str, "tool_calls": [...], "thinking":
# str}, "done": bool, "eval_count": int, "eval_duration": int}.
# _stream_completion is the one seam where a request's actual
# provider matters -- everything downstream of it reads that exact
# shape regardless of which provider actually served the request, so
# none of the carefully-tested logic built around it needs to change
# at all to support a new provider; only a new adapter here does.

def _translate_messages_to_openai(messages):
    """Ollama's tool-call convention has no persistent IDs -- a call
    and its result are matched by position/order alone. OpenAI's
    format requires an explicit id on each tool_call and a matching
    tool_call_id on the message carrying its result, so this assigns
    synthetic, per-turn IDs when translating, tracking them just long
    enough to match each following role="tool" message to the specific
    call it's actually responding to."""
    out = []
    pending_tool_call_ids = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            tool_call_id = pending_tool_call_ids.pop(0) if pending_tool_call_ids else f"call_{uuid.uuid4().hex[:8]}"
            out.append({"role": "tool", "tool_call_id": tool_call_id, "content": m.get("content", "")})
            continue
        entry = {"role": role}
        images = m.get("images")
        content = m.get("content", "")
        if images:
            parts = [{"type": "text", "text": content}] if content else []
            for img in images:
                parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}})
            entry["content"] = parts
        else:
            entry["content"] = content
        tool_calls = m.get("tool_calls")
        if tool_calls:
            openai_calls = []
            pending_tool_call_ids = []
            for tc in tool_calls:
                call_id = f"call_{uuid.uuid4().hex[:8]}"
                pending_tool_call_ids.append(call_id)
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                if not isinstance(args, str):
                    args = json.dumps(args)
                openai_calls.append({"id": call_id, "type": "function", "function": {"name": fn.get("name", ""), "arguments": args}})
            entry["tool_calls"] = openai_calls
        out.append(entry)
    return out

def _iter_sse_lines(resp):
    """Manually buffers raw response bytes and only yields a line once
    it has been fully received, rather than trusting a text/line
    iterator to always get network chunk boundaries right. This is a
    real, independently-documented failure mode for exactly this kind
    of SSE parsing -- a single logical line (including a multi-byte
    UTF-8 character within it) can land split across two separate
    reads, corrupting or dropping fragments of the decoded text
    without ever raising a visible error. Buffering raw bytes and only
    decoding once a complete \n-terminated line has actually arrived
    avoids that regardless of exactly how the underlying chunks split."""
    buffer = b""
    for raw_chunk in resp.iter_raw():
        buffer += raw_chunk
        while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            yield line_bytes.decode("utf-8", errors="ignore").rstrip("\r")

def _stream_openai_compatible(req, messages, tools, cancel_flag, timeout=_MAIN_AGENT_TIMEOUT):
    """Covers OpenAI itself, OpenRouter, and any custom OpenAI-
    compatible endpoint -- all three speak the same /chat/completions
    wire format. Tool schemas need no translation at all: Ollama
    adopted OpenAI's own function-calling convention, so the exact
    same tools list built for a native Ollama request is valid here
    unchanged."""
    if req.endpoint_url:
        base_url = req.endpoint_url.rstrip("/")
    elif req.provider == "openrouter":
        base_url = "https://openrouter.ai/api/v1"
    else:
        base_url = "https://api.openai.com/v1"
    url = base_url + "/chat/completions"
    headers = {"Authorization": f"Bearer {req.api_key}", "Content-Type": "application/json"}
    payload = {
        "model": req.model,
        "messages": _translate_messages_to_openai(messages),
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    accumulated_tool_calls = {}
    try:
        with httpx.stream("POST", url, headers=headers, json=payload, timeout=timeout) as resp:
            if resp.status_code != 200:
                error_text = resp.read().decode(errors="replace")
                try:
                    error_detail = json.loads(error_text).get("error", {}).get("message") or error_text[:300]
                except (json.JSONDecodeError, AttributeError):
                    error_detail = error_text[:300]
                if resp.status_code == 401:
                    friendly = f"Authentication failed -- check the API key for this endpoint.\n\n{error_detail}"
                elif resp.status_code == 429:
                    friendly = f"Rate limit or usage quota exceeded for this provider.\n\n{error_detail}"
                elif resp.status_code == 400:
                    friendly = f"The request was rejected (bad request) -- this can mean an unsupported parameter or a model name the provider doesn't recognize.\n\n{error_detail}"
                elif resp.status_code >= 500:
                    friendly = f"The provider's own servers returned an error (HTTP {resp.status_code}) -- this is on their end, not a local problem.\n\n{error_detail}"
                else:
                    friendly = f"Request failed (HTTP {resp.status_code}).\n\n{error_detail}"
                yield {"message": {"content": f"[Online provider error] {friendly}"}, "done": True}
                return
            for line in _iter_sse_lines(resp):
                if cancel_flag.is_set():
                    return
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str.strip() == "[DONE]":
                    yield {"message": {"content": ""}, "done": True}
                    return
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choice = (event.get("choices") or [{}])[0]
                delta = choice.get("delta", {})
                out_msg = {}
                if delta.get("content"):
                    out_msg["content"] = delta["content"]
                if delta.get("reasoning"):
                    # OpenRouter's own extension for reasoning-capable
                    # models -- separate from "content", and previously
                    # not read at all here, so a model that reasons
                    # before calling a tool (very common) had its
                    # entire visible output silently dropped: nothing
                    # in the thinking panel, nothing in the reply, only
                    # the tool call itself showed up.
                    out_msg["thinking"] = delta["reasoning"]
                if delta.get("tool_calls"):
                    for tc_delta in delta["tool_calls"]:
                        idx = tc_delta.get("index", 0)
                        if idx not in accumulated_tool_calls:
                            accumulated_tool_calls[idx] = {"id": tc_delta.get("id", ""), "name": "", "arguments": ""}
                        if tc_delta.get("id"):
                            accumulated_tool_calls[idx]["id"] = tc_delta["id"]
                        fn_delta = tc_delta.get("function", {})
                        if fn_delta.get("name"):
                            accumulated_tool_calls[idx]["name"] += fn_delta["name"]
                        if fn_delta.get("arguments"):
                            accumulated_tool_calls[idx]["arguments"] += fn_delta["arguments"]
                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    # Tool calls arrive incrementally across many chunks
                    # and are only complete once finish_reason shows up
                    # -- unlike Ollama, which always sends a tool call
                    # whole, never as deltas, so this is the point to
                    # finally emit them as complete objects matching
                    # what the rest of the loop actually expects.
                    if accumulated_tool_calls:
                        out_msg["tool_calls"] = []
                        for idx in sorted(accumulated_tool_calls.keys()):
                            tc = accumulated_tool_calls[idx]
                            try:
                                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                            except json.JSONDecodeError:
                                args = {}
                            out_msg["tool_calls"].append({"function": {"name": tc["name"], "arguments": args}})
                    usage = event.get("usage") or {}
                    yield {
                        "message": out_msg,
                        "done": True,
                        "eval_count": usage.get("completion_tokens"),
                        "eval_duration": None,  # not reported by these APIs -- tokens/sec just won't show for online-provider responses
                    }
                    return
                if out_msg:
                    yield {"message": out_msg, "done": False}
    except httpx.RequestError as e:
        # Connection-level failure (DNS, timeout, refused, etc.) --
        # without this, the exception would propagate silently up
        # through the background thread and generation would just
        # stop with no reply at all, rather than a visible explanation.
        yield {"message": {"content": f"[Online provider error] Could not reach the provider: {e}"}, "done": True}
        return

def _translate_messages_to_anthropic(messages):
    """Anthropic's format differs from Ollama's/OpenAI's in several
    real ways: the system prompt is a separate top-level field, not a
    message; tool calls live inside an assistant message's content
    array as tool_use blocks (each with its own id); and tool results
    must be sent back as a user-role message containing tool_result
    blocks -- there's no separate "tool" role at all. This walks
    Ollama's message list and produces both the extracted system text
    and Anthropic-shaped messages, tracking each tool_use id so the
    result(s) that follow reference the correct one, and batching
    consecutive tool results into one user message the way Anthropic
    expects rather than one message per result."""
    system_parts = []
    out = []
    pending_tool_use_ids = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(m["content"])
            continue
        if role == "tool":
            tool_use_id = pending_tool_use_ids.pop(0) if pending_tool_use_ids else f"toolu_{uuid.uuid4().hex[:8]}"
            result_block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": m.get("content", "")}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) and all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                out[-1]["content"].append(result_block)
            else:
                out.append({"role": "user", "content": [result_block]})
            continue
        content = m.get("content", "")
        images = m.get("images")
        tool_calls = m.get("tool_calls")
        blocks = []
        if content:
            blocks.append({"type": "text", "text": content})
        if images:
            for img in images:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}})
        if tool_calls:
            pending_tool_use_ids = []
            for tc in tool_calls:
                tool_id = f"toolu_{uuid.uuid4().hex[:8]}"
                pending_tool_use_ids.append(tool_id)
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                blocks.append({"type": "tool_use", "id": tool_id, "name": fn.get("name", ""), "input": args})
        out.append({"role": role, "content": blocks if blocks else content})
    return chr(10).join(system_parts), out

def _translate_tools_to_anthropic(tools):
    out = []
    for t in (tools or []):
        fn = t.get("function", {})
        out.append({"name": fn.get("name", ""), "description": fn.get("description", ""), "input_schema": fn.get("parameters", {"type": "object", "properties": {}})})
    return out

def _anthropic_error_message(status_code, error_detail):
    if status_code == 401:
        return f"Authentication failed -- check the API key for this endpoint.\n\n{error_detail}"
    if status_code == 429:
        return f"Rate limit or usage quota exceeded for this provider.\n\n{error_detail}"
    if status_code == 400:
        return f"The request was rejected (bad request) -- this can mean an unsupported parameter or a model name the provider doesn't recognize.\n\n{error_detail}"
    if status_code >= 500:
        return f"The provider's own servers returned an error (HTTP {status_code}) -- this is on their end, not a local problem.\n\n{error_detail}"
    return f"Request failed (HTTP {status_code}).\n\n{error_detail}"

def _stream_anthropic(req, messages, tools, cancel_flag, timeout=_MAIN_AGENT_TIMEOUT):
    """Anthropic's own /v1/messages format -- separate system field,
    tool_use/tool_result content blocks instead of Ollama's/OpenAI's
    role="tool" convention, and a named-SSE-event streaming protocol
    (content_block_start/delta/stop, message_delta, message_stop)
    rather than one uniform delta shape."""
    system_text, anthropic_messages = _translate_messages_to_anthropic(messages)
    url = (req.endpoint_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": req.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": req.model,
        # max_tokens must exceed thinking's budget_tokens, since the
        # thinking budget counts against the same output allowance --
        # raised from a flat 8192 to leave real room for the actual
        # reply on top of it.
        "max_tokens": 16000,
        "thinking": {"type": "enabled", "budget_tokens": 8000},
        "messages": anthropic_messages,
        "stream": True,
    }
    if system_text:
        payload["system"] = system_text
    if tools:
        payload["tools"] = _translate_tools_to_anthropic(tools)
    current_blocks = {}
    try:
        with httpx.stream("POST", url, headers=headers, json=payload, timeout=timeout) as resp:
            if resp.status_code != 200:
                error_text = resp.read().decode(errors="replace")
                try:
                    error_detail = json.loads(error_text).get("error", {}).get("message") or error_text[:300]
                except (json.JSONDecodeError, AttributeError):
                    error_detail = error_text[:300]
                friendly = _anthropic_error_message(resp.status_code, error_detail)
                yield {"message": {"content": f"[Online provider error] {friendly}"}, "done": True}
                return
            output_tokens = None
            for line in _iter_sse_lines(resp):
                if cancel_flag.is_set():
                    return
                if not line or not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "content_block_start":
                    idx = event.get("index", 0)
                    block = event.get("content_block", {})
                    block_type = block.get("type")
                    if block_type == "tool_use":
                        current_blocks[idx] = {"type": "tool_use", "id": block.get("id", ""), "name": block.get("name", ""), "json": ""}
                    elif block_type == "thinking":
                        current_blocks[idx] = {"type": "thinking", "text": ""}
                    else:
                        current_blocks[idx] = {"type": "text", "text": ""}
                elif etype == "content_block_delta":
                    idx = event.get("index", 0)
                    delta = event.get("delta", {})
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if idx in current_blocks:
                            current_blocks[idx]["text"] = current_blocks[idx].get("text", "") + text
                        if text:
                            yield {"message": {"content": text}, "done": False}
                    elif delta_type == "thinking_delta":
                        # Separate field name from a regular text delta
                        # (Anthropic's own convention, mirroring how
                        # Ollama's "thinking" is kept apart from
                        # "content") -- mapped the same way here so it
                        # renders in the same thinking panel.
                        thinking_text = delta.get("thinking", "")
                        if idx in current_blocks:
                            current_blocks[idx]["text"] = current_blocks[idx].get("text", "") + thinking_text
                        if thinking_text:
                            yield {"message": {"thinking": thinking_text}, "done": False}
                    elif delta_type == "input_json_delta":
                        if idx in current_blocks:
                            current_blocks[idx]["json"] = current_blocks[idx].get("json", "") + delta.get("partial_json", "")
                elif etype == "message_delta":
                    usage = event.get("usage") or {}
                    if usage.get("output_tokens") is not None:
                        output_tokens = usage["output_tokens"]
                elif etype == "message_stop":
                    tool_calls = []
                    for idx in sorted(current_blocks.keys()):
                        b = current_blocks[idx]
                        if b.get("type") == "tool_use":
                            try:
                                args = json.loads(b["json"]) if b["json"] else {}
                            except json.JSONDecodeError:
                                args = {}
                            tool_calls.append({"function": {"name": b.get("name", ""), "arguments": args}})
                    out_msg = {}
                    if tool_calls:
                        out_msg["tool_calls"] = tool_calls
                    yield {"message": out_msg, "done": True, "eval_count": output_tokens, "eval_duration": None}
                    return
    except httpx.RequestError as e:
        yield {"message": {"content": f"[Online provider error] Could not reach the provider: {e}"}, "done": True}
        return

def _translate_messages_to_google(messages):
    """Google's format uses "model" instead of "assistant" for the
    role name, always represents content as an array of "parts"
    rather than a plain string, and matches tool calls/results by
    function name and position rather than an explicit id the way
    OpenAI and Anthropic both require -- simpler in that one respect,
    but the content shape itself differs from every other provider."""
    system_parts = []
    out = []
    pending_tool_names = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(m["content"])
            continue
        if role == "tool":
            name = pending_tool_names.pop(0) if pending_tool_names else ""
            try:
                response_obj = json.loads(m.get("content", "{}"))
                if not isinstance(response_obj, dict):
                    response_obj = {"result": response_obj}
            except json.JSONDecodeError:
                response_obj = {"result": m.get("content", "")}
            part = {"functionResponse": {"name": name, "response": response_obj}}
            if out and out[-1]["role"] == "user" and all("functionResponse" in p for p in out[-1]["parts"]):
                out[-1]["parts"].append(part)
            else:
                out.append({"role": "user", "parts": [part]})
            continue
        google_role = "model" if role == "assistant" else "user"
        parts = []
        content = m.get("content", "")
        if content:
            parts.append({"text": content})
        images = m.get("images")
        if images:
            for img in images:
                parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img}})
        tool_calls = m.get("tool_calls")
        if tool_calls:
            pending_tool_names = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                pending_tool_names.append(name)
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                parts.append({"functionCall": {"name": name, "args": args}})
        if parts:
            out.append({"role": google_role, "parts": parts})
    return chr(10).join(system_parts), out

def _translate_tools_to_google(tools):
    declarations = []
    for t in (tools or []):
        fn = t.get("function", {})
        declarations.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return [{"function_declarations": declarations}] if declarations else None

def _stream_google(req, messages, tools, cancel_flag, timeout=_MAIN_AGENT_TIMEOUT):
    """Google Gemini's generateContent streaming API -- uses ?alt=sse
    to get proper SSE framing rather than Google's default
    single-JSON-array response, since the whole _stream_completion
    abstraction is built around consuming an event-by-event stream."""
    system_text, google_messages = _translate_messages_to_google(messages)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{req.model}:streamGenerateContent?key={req.api_key}&alt=sse"
    payload = {
        "contents": google_messages,
        # Off by default -- without this, thinking-capable Gemini
        # models (2.5+) still reason internally but never surface any
        # of it, same underlying gap as Anthropic needing its own
        # thinking parameter explicitly set.
        "generationConfig": {"thinkingConfig": {"includeThoughts": True}},
    }
    if system_text:
        payload["system_instruction"] = {"parts": [{"text": system_text}]}
    google_tools = _translate_tools_to_google(tools)
    if google_tools:
        payload["tools"] = google_tools
    try:
        with httpx.stream("POST", url, json=payload, timeout=timeout) as resp:
            if resp.status_code != 200:
                error_text = resp.read().decode(errors="replace")
                try:
                    error_detail = json.loads(error_text).get("error", {}).get("message") or error_text[:300]
                except (json.JSONDecodeError, AttributeError, TypeError):
                    error_detail = error_text[:300]
                friendly = _anthropic_error_message(resp.status_code, error_detail)  # same generic 401/429/400/5xx framing applies here too
                yield {"message": {"content": f"[Online provider error] {friendly}"}, "done": True}
                return
            output_tokens = None
            for line in _iter_sse_lines(resp):
                if cancel_flag.is_set():
                    return
                if not line or not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                candidates = event.get("candidates") or []
                if not candidates:
                    continue
                candidate = candidates[0]
                content = candidate.get("content", {})
                parts = content.get("parts", [])
                out_msg = {}
                text_out = ""
                thinking_out = ""
                tool_calls = []
                for part in parts:
                    if "text" in part:
                        # Thinking and regular answer text arrive mixed
                        # together in the same parts array, distinguished
                        # only by this boolean flag -- not a separate
                        # part type or field name the way Anthropic and
                        # OpenRouter both do it.
                        if part.get("thought"):
                            thinking_out += part["text"]
                        else:
                            text_out += part["text"]
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        tool_calls.append({"function": {"name": fc.get("name", ""), "arguments": fc.get("args", {})}})
                if text_out:
                    out_msg["content"] = text_out
                if thinking_out:
                    out_msg["thinking"] = thinking_out
                if tool_calls:
                    out_msg["tool_calls"] = tool_calls
                usage = event.get("usageMetadata") or {}
                if usage.get("candidatesTokenCount") is not None:
                    output_tokens = usage["candidatesTokenCount"]
                finish_reason = candidate.get("finishReason")
                if finish_reason:
                    yield {"message": out_msg, "done": True, "eval_count": output_tokens, "eval_duration": None}
                    return
                if out_msg:
                    yield {"message": out_msg, "done": False}
    except httpx.RequestError as e:
        yield {"message": {"content": f"[Online provider error] Could not reach the provider: {e}"}, "done": True}
        return

def _stream_completion(req, target_url, messages, tools, ctx_size, cancel_flag, timeout=_MAIN_AGENT_TIMEOUT):
    """Single entry point for actually running one round of generation
    -- yields Ollama-shaped chunks no matter which provider a request
    targets. Ollama-native requests (provider == "") pass straight
    through to Ollama's own /api/chat, completely unchanged from
    before online providers existed at all.

    timeout defaults to _MAIN_AGENT_TIMEOUT (Athena's own turn, real
    patience on old/slow hardware) -- _call_bot_endpoint explicitly
    passes _SUB_AGENT_TIMEOUT instead, since a bot round is one bounded,
    single-topic investigation step that should hit a real ceiling
    rather than running indefinitely."""
    if req.provider in ("openai", "openrouter", "custom"):
        yield from _stream_openai_compatible(req, messages, tools, cancel_flag, timeout=timeout)
        return
    if req.provider == "anthropic":
        yield from _stream_anthropic(req, messages, tools, cancel_flag, timeout=timeout)
        return
    if req.provider == "google":
        yield from _stream_google(req, messages, tools, cancel_flag, timeout=timeout)
        return
    with httpx.stream("POST", target_url, json={
        "model": req.model,
        "messages": messages,
        "tools": tools if tools else None,
        "options": {"num_ctx": ctx_size},
        "stream": True,
    }, timeout=timeout) as resp:
        for line in resp.iter_lines():
            if cancel_flag.is_set():
                return
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


@app.post("/api/chat")
def chat_stream(req: ChatIn):
    activity.touch()
    print(f"[DEBUG] model={req.model!r} endpoint_url={req.endpoint_url!r}", flush=True)
    effective_message = req.message
    for att in req.attachments:
        att_content = (att.get("content") or "")[:20000]
        effective_message += f"\n\n[Attached file: {att.get('name', 'file')}]\n```\n{att_content}\n```"
    _user_msg_id = lcm_client.send_to_lcm(req.session_id, "user", effective_message, has_image=bool(req.images))
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

    context = lcm_client.get_lcm_context(req.session_id)

    # Perpetual agent mode -- one tool set, always available. LCM's recall
    # tools and the read-only bash tool are always on; file write/edit
    # tools are only included here when req.workspace is non-empty, so
    # the model can never edit files on a session with no workspace
    # bound to it, regardless of how it reads an ambiguous prompt.
    mode, effective_workspace = _resolve_mode_and_workspace(req)
    req.workspace = effective_workspace
    system_prompt = _get_mode_system_prompt(mode, req)
    # Sized off the maximum tool set this turn could ever reach (as if
    # search_codebase were already called), not whatever round 1 actually
    # starts with -- ctx_size must not be picked too small for a tool
    # list that later grows back as the turn progresses.
    tools = _get_mode_tools(mode, req, called_tool_names={"search_codebase"})
    print(f"[TOOLS DEBUG] {[t.get('function', {}).get('name') for t in tools]}", flush=True)

    _dynamic_context_msg = _get_dynamic_context_message(req)
    _raw_messages = [{"role": "system", "content": system_prompt}] + ([_dynamic_context_msg] if _dynamic_context_msg else []) + context
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

    # Per-turn plan state, mutated in place (never rebound) so both
    # _execute_tool_call and generate() -- sibling closures directly inside
    # chat_stream, not nested inside each other -- see the same list without
    # needing `nonlocal`. Each entry: {"text": str, "status": str, "note": str}.
    _current_plan = []

    def _render_plan():
        if not _current_plan:
            return {"plan": [], "note": "No plan recorded yet -- call write_plan first."}
        lines = []
        for i, step in enumerate(_current_plan):
            marker = {"done": "[x]", "failed": "[!]", "in_progress": "[~]"}.get(step["status"], "[ ]")
            note = f" -- {step['note']}" if step.get("note") else ""
            lines.append(f"{i}. {marker} {step['text']}{note}")
        return {"plan": "\n".join(lines)}

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
        if name == "bash_exec":
            return _execute_write_bash(args.get("command", ""), args.get("args", []), req.workspace)
        if name == "bash_exec_start":
            return _start_background_bash(args.get("command", ""), args.get("args", []), req.workspace)
        if name == "bash_exec_check":
            return _check_background_bash(args.get("process_id", ""))
        if name == "bash_exec_stop":
            return _stop_background_bash(args.get("process_id", ""), args.get("force", False))
        if name == "get_full_tool_output":
            return _get_full_tool_output_tool(args.get("output_id", ""))
        if name == "find_definition":
            return _get_lsp_client(req.workspace).find_definition(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
        if name == "find_references":
            return _get_lsp_client(req.workspace).find_references(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
        if name == "type_info":
            return _get_lsp_client(req.workspace).type_info(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
        if name == "list_files":
            return _list_files(req.workspace, args.get("path", "."))
        if name == "read_file":
            return _read_file(req.workspace, args.get("path", ""), args.get("offset", 0))
        if name == "write_file":
            return _write_file(req.workspace, args.get("path", ""), args.get("content", ""))
        if name == "edit_file":
            return _edit_file(req.workspace, args.get("path", ""), args.get("old_text", ""), args.get("new_text", ""))
        if name == "replace_lines":
            return _replace_lines(req.workspace, args.get("path", ""), args.get("start_line"), args.get("end_line"), args.get("new_content", ""), args.get("expected_content", ""))
        if name == "backup_file":
            return _backup_file(args.get("path", ""))
        if name == "restore_file":
            return _restore_file(args.get("path", ""), args.get("snapshot_id", ""), args.get("restore_hash", ""), args.get("restore_timestamp"))
        if name in ("list_bots", "draft_bot_prompt", "message_bot", "list_rooms", "read_room_messages", "message_room", "create_bot", "update_bot", "run_delegation_step", "read_scratch_file", "read_tool_call_history", "plan_delegation"):
            if req.session_id != ATHENA_BOTS_SESSION_ID:
                return {"error": "Bot delegation tools are only available in the dedicated Athena Bots session."}
            if name == "list_bots":
                return _list_bots_tool()
            if name == "draft_bot_prompt":
                return _draft_bot_prompt(args.get("name", ""), args.get("job_scope", ""), args.get("tools", []), args.get("additional_constraints"))
            if name == "message_bot":
                if task_context.current_task_hash.get() is not None:
                    return {"error": "A delegation task is already active this turn -- message_bot cannot be used mid-delegation, including as a workaround for a failed or rejected step. Use run_delegation_step or plan_delegation instead, even if a prior step failed."}
                return _message_bot_tool(args.get("bot_id"), args.get("content", ""))
            if name == "list_rooms":
                return _list_rooms_tool()
            if name == "read_room_messages":
                return _read_room_messages_tool(args.get("room_id"))
            if name == "message_room":
                if task_context.current_task_hash.get() is not None:
                    return {"error": "A delegation task is already active this turn -- message_room cannot be used mid-delegation, including as a workaround for a failed or rejected step. Use run_delegation_step or plan_delegation instead, even if a prior step failed."}
                return _message_room_tool(args.get("room_id"), args.get("content", ""))
            if name == "create_bot":
                return _create_bot_tool(args.get("name", ""), args.get("description", ""), args.get("allowed_tools", []))
            if name == "update_bot":
                return _update_bot_tool(args.get("bot_id"), args.get("description"), args.get("allowed_tools"))
            if name == "run_delegation_step":
                task_context.get_task_hash(req.session_id)
                if settings.load_settings().get("delegation_async_enabled"):
                    return _run_delegation_step_async_tool(req, args.get("bot_id"), args.get("instruction", ""))
                return _run_delegation_step_tool(args.get("bot_id"), args.get("instruction", ""))
            if name == "read_scratch_file":
                return _read_scratch_file(args.get("bot_name", ""))
            if name == "read_tool_call_history":
                return _read_tool_call_history(args.get("bot_name", ""))
            if name == "plan_delegation":
                task_context.get_task_hash(req.session_id)
                if settings.load_settings().get("delegation_async_enabled"):
                    return _plan_delegation_async_tool(req, args.get("steps", []))
                return _plan_delegation_tool(args.get("steps", []))
        if name == "search_codebase":
            return _search_codebase(args.get("query", ""), args.get("limit", 3))
        if name == "write_plan":
            steps = args.get("steps", [])
            if not steps or not isinstance(steps, list) or not all(isinstance(s, str) and s.strip() for s in steps):
                return {"error": "steps must be a non-empty list of non-empty strings."}
            _current_plan.clear()
            _current_plan.extend({"text": s, "status": "pending", "note": ""} for s in steps)
            return _render_plan()
        if name == "update_plan_step":
            if not _current_plan:
                return {"error": "No plan recorded yet -- call write_plan first."}
            index = args.get("index")
            status = args.get("status", "")
            if not isinstance(index, int) or not (0 <= index < len(_current_plan)):
                return {"error": f"index must be between 0 and {len(_current_plan) - 1}, got {index!r}."}
            if status not in ("pending", "in_progress", "done", "failed"):
                return {"error": f"status must be one of pending/in_progress/done/failed, got {status!r}."}
            _current_plan[index]["status"] = status
            _current_plan[index]["note"] = args.get("note", "")
            return _render_plan()

        args["session_id"] = req.session_id
        try:
            resp = httpx.post(f"{lcm_client.LCM_URL}/tools/call", json={"name": name, "arguments": args}, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("result")
            return {"error": f"Tool call failed: HTTP {resp.status_code}"}
        except Exception as e:
            return {"error": f"Tool call failed: {e}"}

    def generate():
        cancel_flag = threading.Event()
        generation_streaming.cancel_flags[req.session_id] = cancel_flag
        # Doom-loop detection: fingerprint each tool call (name + exact
        # arguments) and track the last 20 in this turn. If the same
        # fingerprint would be executed a 3rd time, refuse to run it
        # again and tell the model directly instead -- this is a real,
        # structural block, not just a text suggestion, since a model
        # stuck re-gathering the same already-known information can
        # simply ignore an injected warning but cannot bypass an
        # actual execution refusal. Modeled on a documented pattern
        # used by other production coding-agent harnesses for exactly
        # this failure mode.
        _tool_call_fingerprints = []
        _recent_search_queries = []
        def _tool_call_fingerprint(tc):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            try:
                args_str = json.dumps(args, sort_keys=True)
            except TypeError:
                args_str = str(args)
            raw = fn.get("name", "") + "|" + args_str
            return hashlib.md5(raw.encode()).hexdigest()
        def _detect_text_loop(text):
            """Model-agnostic loop detector for plain-text generation.
            Catches a model stuck repeating itself -- restated reasoning,
            a hallucinated fake tool call written as plain text instead
            of a real structured one, anything -- by watching for actual
            repeated content, not by pattern-matching any one model's
            own tool-call syntax. Syntax-matching would only ever work
            for the specific model it was written against, which is the
            opposite of what this harness needs: it has to hold up for
            whatever model someone points it at, including small,
            resource-constrained models on edge-case hardware that show
            this failure mode worst.

            Layered rather than one fixed check: a longer window (40
            chars, 3 repeats) catches a whole sentence or reasoning
            fragment being restated; a shorter window (18 chars, 5
            repeats) catches a shorter recurring phrase that doesn't
            happen to line up as an exact 40-char match -- interspersed
            with slightly different surrounding text each time, which
            the single long-window check alone would miss entirely."""
            def _tail_repeats(window, min_repeats):
                if len(text) < window * min_repeats:
                    return False
                tail = text[-window:]
                if len(tail.strip()) < window * 0.5:
                    return False  # mostly whitespace -- not a meaningful signal
                return text.count(tail) >= min_repeats
            return _tail_repeats(40, 3) or _tail_repeats(18, 5)
        yield f"data: {json.dumps({'user_message_id': _user_msg_id})}\n\n"
        full_reply = ""
        full_thinking = ""
        full_tool_calls = []
        _target_url = (req.endpoint_url.rstrip("/") + "/api/chat") if req.endpoint_url else OLLAMA_URL
        _messages = list(messages)
        print(f"[DEBUG] _messages roles={[m.get("role") for m in _messages]!r}", flush=True)
        MAX_ROUNDS = 1000  # effectively unbounded; the stop button is the real safety net now
        last_eval_count = None
        last_eval_duration = None
        consecutive_loop_detections = 0
        consecutive_ungrounded_claims = 0
        consecutive_empty_final_answers = 0
        made_any_tool_calls_this_turn = False
        # Tool availability is recomputed every round from these two,
        # not fixed once at request start -- see _get_mode_tools. This is
        # what makes read_file/bash gated behind search_codebase, and
        # blind self-investigation gated behind delegation, structural
        # instead of one more prompt instruction to drift off under
        # pressure.
        _called_tool_names = set()
        _exploration_call_count = 0
        _investigation_gate_notified = False
        _total_tool_call_count = 0
        _plan_gate_notified = False
        _delegation_commitment_notified = False

        for _round in range(MAX_ROUNDS):
            round_reply = ""
            round_tool_calls = []
            loop_detected = False
            _round_tools = _get_mode_tools(mode, req, _called_tool_names, _exploration_call_count, _total_tool_call_count)

            # Same per-host lock bot delegation calls already use
            # (_get_host_lock / _run_delegation_step_tool) -- without it,
            # Athena's own generation here and a concurrent bot call could
            # land on the same physical Ollama host at the same time (the
            # common case -- see _endpoint_host's own docstring), forcing
            # it to juggle two different models on hardware that can't
            # hold both in VRAM at once. That contention is the leading
            # suspect behind recurring RemoteProtocolError("incomplete
            # chunked read")/ReadTimeout failures observed specifically
            # in the athena-bots-agent session. Scoped to one round's
            # streaming call, not the whole multi-round turn, so a queued
            # bot call still gets a fair turn between rounds.
            try:
                with _get_host_lock(_endpoint_host(req.endpoint_url)):
                    for chunk in _stream_completion(req, _target_url, _messages, _round_tools, ctx_size, cancel_flag):
                        if cancel_flag.is_set():
                            break
                        # Refresh on every real chunk received, not just once
                        # when the request first arrives -- otherwise a single
                        # generation running longer than the idle threshold
                        # (a large tool-call argument buffered by Ollama, a
                        # big context, a slow model) gets misread as an idle
                        # window partway through, and the memory scan can
                        # fire a second, competing Ollama request while this
                        # one is still actively streaming.
                        activity.touch()
                        msg = chunk.get("message", {})
                        print(f"[DEBUG] chunk={chunk}", flush=True)
                        thinking_delta = msg.get("thinking", "")
                        if thinking_delta:
                            full_thinking += thinking_delta
                            yield f"data: {json.dumps({'thinking': thinking_delta})}\n\n"
                        delta = msg.get("content", "")
                        if delta:
                            round_reply += delta
                            full_reply += delta
                            yield f"data: {json.dumps({'delta': delta})}\n\n"
                            if _detect_text_loop(round_reply):
                                loop_detected = True
                                break
                        if msg.get("tool_calls"):
                            round_tool_calls.extend(msg["tool_calls"])
                        if chunk.get("done"):
                            last_eval_count = chunk.get("eval_count")
                            last_eval_duration = chunk.get("eval_duration")
                            break
            except httpx.RequestError as e:
                # The model backend became unreachable or dropped the
                # connection mid-response (server restarted/crashed, network
                # blip, or -- in athena_delegation mode -- resource
                # contention with a concurrent bot call on the same host).
                # Previously this surfaced nowhere: the exception propagated
                # up to generation_streaming.drain_generator_to_queue, which just logged it and
                # quietly cleaned up, so the turn appeared to silently stop
                # with zero explanation. Persist and show a real message
                # instead, reusing whatever partial reply already streamed.
                print(f"[DEBUG] generation for session {req.session_id!r} raised: {e!r}", flush=True)
                error_msg = f"Generation failed partway through: {e!r}. The model backend likely became unreachable or dropped the connection -- try again in a moment."
                _had_prior_content = bool(full_reply)
                full_reply += ("\n\n" if _had_prior_content else "") + error_msg
                _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                yield f"data: {json.dumps({'delta': ('\n\n' if _had_prior_content else '') + error_msg})}\n\n"
                yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'assistant_message_id': _msg_id, 'generation_error': True})}\n\n"
                return

            if cancel_flag.is_set():
                if full_reply:
                    lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                yield f"data: {json.dumps({'done': True, 'cancelled': True})}\n\n"
                generation_streaming.cancel_flags.pop(req.session_id, None)
                return

            if loop_detected:
                # Model-agnostic recovery: stop this round, drop the
                # repeated tail from what actually gets saved/fed back
                # (keep one copy of the repeated text, not the pile of
                # duplicates), then redirect rather than either silently
                # cutting the response short or forcing a premature
                # conclusion out of nothing -- same principle as the
                # tool-call fingerprint block, applied to plain text.
                consecutive_loop_detections += 1
                tail = round_reply[-40:]
                first_idx = round_reply.find(tail)
                trimmed_reply = round_reply[:first_idx + len(tail)] if first_idx != -1 else round_reply
                if trimmed_reply.strip():
                    _messages.append({"role": "assistant", "content": trimmed_reply})
                if consecutive_loop_detections >= 3:
                    # Redirecting hasn't helped after repeated tries --
                    # stop honestly rather than silently burning through
                    # the rest of MAX_ROUNDS on a model that can't recover.
                    failure_msg = "I got stuck repeating myself and wasn't able to recover after a few attempts -- stopping here rather than continuing to loop."
                    full_reply = (full_reply.rsplit(round_reply, 1)[0] if round_reply in full_reply else full_reply) + ("\n\n" + failure_msg if full_reply else failure_msg)
                    _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                    yield f"data: {json.dumps({'delta': ('\n\n' if trimmed_reply.strip() else '') + failure_msg})}\n\n"
                    yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'assistant_message_id': _msg_id})}\n\n"
                    return
                _messages.append({"role": "user", "content": (
                    "You were repeating the same text over and over without making progress, and "
                    "generation was stopped automatically. Do not restart the same line of reasoning. "
                    "Review what you already wrote above: if you reached a real conclusion before the "
                    "repetition started, state it directly and concisely. If you had not reached one, "
                    "say so plainly instead of restating the same points again."
                )})
                continue

            consecutive_loop_detections = 0

            if not round_tool_calls:
                # A substantial, tool-free round arriving after this same
                # turn already made real tool calls is the exact pattern
                # behind a real, observed failure: the model made a few
                # genuine tool calls, then started fabricating further
                # "I read/found/fixed X" narration with no real tool
                # activity behind it at all. This check is model-agnostic
                # by design -- it never parses the text itself for
                # language patterns (which would only ever catch one
                # model's specific phrasing), it only checks the one
                # verifiable fact available: whether a real, structured
                # tool call actually happened. Only fires in workspace
                # (coding-harness) sessions, where tool use is expected,
                # and only once real tool use has already started this
                # turn, so a genuine short question or a real final
                # answer that never needed tools at all is unaffected.
                if made_any_tool_calls_this_turn and req.workspace and len(round_reply) > 400:
                    consecutive_ungrounded_claims += 1
                    if consecutive_ungrounded_claims >= 3:
                        failure_msg = "I started describing further progress without actually making the tool calls to back it up, and wasn't able to correct that after a few attempts -- stopping here rather than continuing to report unverified work."
                        full_reply += ("\n\n" + failure_msg if full_reply else failure_msg)
                        _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                        yield f"data: {json.dumps({'delta': '\n\n' + failure_msg})}\n\n"
                        yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'assistant_message_id': _msg_id})}\n\n"
                        return
                    _messages.append({"role": "assistant", "content": round_reply})
                    _messages.append({"role": "user", "content": (
                        "You wrote a substantial amount of text describing further progress -- reading, "
                        "checking, or changing something -- but did not actually make a real tool call this "
                        "round to back any of it up. Do not treat anything you just described as having "
                        "actually happened; it did not. If you genuinely already have everything you need "
                        "from real tool results earlier in this conversation, state your actual final answer "
                        "now, directly and concisely, without describing further actions you have not taken. "
                        "Otherwise, make a real tool call right now for whatever you still need, instead of "
                        "describing what it would show."
                    )})
                    continue
                consecutive_ungrounded_claims = 0

                # The model did real tool-call work this turn, then ended
                # with a completely blank round -- no content, no tool
                # calls, done:true. Nothing else here catches this: it's
                # not a loop (no repeated text), not an ungrounded claim
                # (no text was written at all), so without this it sails
                # through as an ordinary successful completion and the
                # turn just ends with nothing to show for the work done.
                # Nudge for an actual answer a couple of times before
                # giving up honestly, same shape as the other two
                # recovery paths above.
                if made_any_tool_calls_this_turn and not full_reply.strip():
                    consecutive_empty_final_answers += 1
                    if consecutive_empty_final_answers >= 3:
                        failure_msg = "I did some real investigation this turn but wasn't able to put together an actual answer from it after a few attempts -- stopping here rather than ending with nothing. Check the tool results above for what was actually found."
                        full_reply = failure_msg
                        _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                        yield f"data: {json.dumps({'delta': failure_msg})}\n\n"
                        yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'assistant_message_id': _msg_id})}\n\n"
                        return
                    _messages.append({"role": "user", "content": (
                        "You finished a round of tool calls but ended with a completely blank reply -- no "
                        "answer, no further tool call. Based on what you've actually found so far this turn, "
                        "state your real conclusion or findings now, directly and concisely. If you're genuinely "
                        "stuck, say so plainly instead of ending with nothing."
                    )})
                    continue
                consecutive_empty_final_answers = 0

                _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
                tokens_per_sec = round(last_eval_count / (last_eval_duration / 1e9), 1) if last_eval_count and last_eval_duration else None
                yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'tokens_per_sec': tokens_per_sec, 'assistant_message_id': _msg_id})}\n\n"
                return

            made_any_tool_calls_this_turn = True
            consecutive_ungrounded_claims = 0

            _gate_was_open = _exploration_call_count < _SELF_INVESTIGATION_TOOL_THRESHOLD
            _plan_gate_was_open = _total_tool_call_count < _PLAN_REQUIRED_THRESHOLD
            _delegation_commitment_was_open = "plan_delegation" not in _called_tool_names
            for tc in round_tool_calls:
                _tc_name = tc.get("function", {}).get("name", "")
                _called_tool_names.add(_tc_name)
                if _tc_name in _EXPLORATION_TOOL_NAMES:
                    _exploration_call_count += 1
                _total_tool_call_count += 1

            # Model wants to call tool(s) -- execute each, tell the
            # frontend what's happening, then loop back with results
            # appended so the model can use them for its next turn.
            _messages.append({"role": "assistant", "content": round_reply, "tool_calls": round_tool_calls})
            for tc in round_tool_calls:
                tool_name = tc.get("function", {}).get("name", "unknown")
                yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name})}\n\n"
                fingerprint = _tool_call_fingerprint(tc)
                repeat_count = _tool_call_fingerprints.count(fingerprint)
                similar_query = None
                if repeat_count < 2 and tool_name == "web_search":
                    fn_args = tc.get("function", {}).get("arguments", {})
                    if isinstance(fn_args, str):
                        try:
                            fn_args = json.loads(fn_args)
                        except json.JSONDecodeError:
                            fn_args = {}
                    query = fn_args.get("query", "") if isinstance(fn_args, dict) else ""
                    if query:
                        similar_query = next((q for q in _recent_search_queries if _search_query_similarity(query, q) >= 0.7), None)
                if repeat_count >= 2:
                    result = {
                        "error": f"BLOCKED: this exact {tool_name} call (same tool, same arguments) has already "
                        f"been made {repeat_count} times this turn. It will not be run again -- you already have "
                        f"its result from earlier in this conversation. Do not repeat it a third time; instead, "
                        f"either use the information you already gathered to reach a conclusion, or make a "
                        f"genuinely different call (different tool, or different arguments) if you actually need "
                        f"new information."
                    }
                elif similar_query:
                    result = {
                        "error": f"BLOCKED: this search is on essentially the same topic as an earlier query this "
                        f"turn ('{similar_query}'), just reworded. Reformulating a search doesn't give you new "
                        f"information if the underlying question is the same -- use what that earlier search "
                        f"already returned, or search for something genuinely different if you actually need it."
                    }
                else:
                    result = _execute_tool_call(tc)
                    _tool_call_fingerprints.append(fingerprint)
                    if len(_tool_call_fingerprints) > 20:
                        _tool_call_fingerprints.pop(0)
                    if tool_name == "web_search":
                        fn_args = tc.get("function", {}).get("arguments", {})
                        if isinstance(fn_args, str):
                            try:
                                fn_args = json.loads(fn_args)
                            except json.JSONDecodeError:
                                fn_args = {}
                        query = fn_args.get("query", "") if isinstance(fn_args, dict) else ""
                        if query:
                            _recent_search_queries.append(query)
                            if len(_recent_search_queries) > 10:
                                _recent_search_queries.pop(0)
                yield f"data: {json.dumps({'type': 'tool_output', 'tool': tool_name, 'output': result})}\n\n"
                # Saved in the same shape the frontend's own live tool-call
                # display already uses (tool/status/output) -- NOT the raw
                # LLM tool_calls format, which has no result field at all
                # and uses function.name instead of a plain tool name, so
                # a reloaded history entry actually matches what streaming
                # already shows instead of rendering empty.
                full_tool_calls.append({"tool": tool_name, "status": "done", "output": result})
                # The person always sees the real, full result above (nothing hidden) --
                # only what actually goes back into the model's own context gets compressed,
                # since that's the thing burning tokens, not what's shown in the UI.
                _result_for_context = result if tool_name in _UNCOMPRESSED_TOOL_NAMES else _compress_tool_result(result)
                _messages.append({"role": "tool", "content": json.dumps(_result_for_context)})

            if (_gate_was_open and _exploration_call_count >= _SELF_INVESTIGATION_TOOL_THRESHOLD
                    and mode == "athena_delegation" and not _investigation_gate_notified
                    and not (_called_tool_names & _DELEGATION_TOOL_NAMES)):
                _investigation_gate_notified = True
                _messages.append({"role": "user", "content": (
                    "You've made enough self-investigation tool calls this turn that direct exploration "
                    "(bash, read_file, list_files, search_codebase, find_definition, find_references, "
                    "type_info) is no longer available for the rest of this turn -- only delegation "
                    "(run_delegation_step/plan_delegation), any write tools, and your existing findings "
                    "so far remain. Use plan_delegation or run_delegation_step to get any further "
                    "investigation done instead of continuing to look yourself."
                )})

            if (_plan_gate_was_open and _total_tool_call_count >= _PLAN_REQUIRED_THRESHOLD
                    and not _plan_gate_notified and "write_plan" not in _called_tool_names):
                _plan_gate_notified = True
                _messages.append({"role": "user", "content": (
                    "You've made several tool calls this turn without writing a plan. Every tool except "
                    "write_plan is now unavailable until you call it. This does not limit how much you can "
                    "do for the rest of this turn -- once you call write_plan with the ordered steps you're "
                    "actually going to take, full tool access returns and stays available. Call write_plan now."
                )})

            if (mode == "athena_delegation" and _delegation_commitment_was_open
                    and "plan_delegation" in _called_tool_names and not _delegation_commitment_notified):
                _delegation_commitment_notified = True
                _messages.append({"role": "user", "content": (
                    "You called plan_delegation this turn, so run_delegation_step is no longer available for "
                    "the rest of this turn -- that commitment is permanent once made. If a step was rejected "
                    "or came back incomplete, do not work around it with a one-off run_delegation_step call: "
                    "fix the step (or steps) and call plan_delegation again with the corrected list."
                )})
        else:
            fallback_msg = "I wasn't able to settle on an answer after several tool calls -- the search results may be inconsistent or the page I need isn't easily fetchable. Try rephrasing, or ask me to check a specific source directly."
            full_reply = fallback_msg
            yield f"data: {json.dumps({'delta': fallback_msg})}\n\n"
            _msg_id = lcm_client.send_to_lcm(req.session_id, "assistant", full_reply, model=req.model, thinking=full_thinking, tool_calls=full_tool_calls)
            tokens_per_sec = round(last_eval_count / (last_eval_duration / 1e9), 1) if last_eval_count and last_eval_duration else None
            yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'tokens_per_sec': tokens_per_sec, 'note': 'max tool rounds reached', 'assistant_message_id': _msg_id})}\n\n"

    # Run the generation loop above in a background thread, draining it
    # into a queue rather than handing it directly to this response --
    # this is the whole mechanism that lets it survive a closed tab or
    # navigation away. generate() itself is completely unmodified.
    gen = generate()
    event_queue = queue.Queue()
    generation_streaming.active_generations[req.session_id] = event_queue
    generation_streaming.generation_snapshots[req.session_id] = {"thinking": "", "content": "", "tool_calls": []}
    threading.Thread(target=generation_streaming.drain_generator_to_queue, args=(gen, event_queue, req.session_id), daemon=True).start()

    def _stream_from_queue():
        while True:
            chunk = event_queue.get()
            if chunk is None:
                break
            yield chunk

    return StreamingResponse(_stream_from_queue(), media_type="text/event-stream")


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
        resp = httpx.get(f"{lcm_client.LCM_URL}/messages/{session_id}", timeout=3)
        if resp.status_code == 200:
            msgs = resp.json()
            from datetime import datetime
            result = []
            for m in msgs:
                ca = m.get("created_at")
                if isinstance(ca, str):
                    try:
                        dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
                        ca = int(dt.timestamp() * 1000)
                    except Exception:
                        ca = None
                result.append({
                    "role": m["role"], "content": m["content"], "messageId": m["id"], "model": m.get("model"),
                    "hasImage": m.get("has_image", False), "thinking": m.get("thinking"), "toolCalls": m.get("tool_calls"),
                    "created_at": ca,
                })
            return result
    except Exception:
        pass
    return []

class MemoryIn(BaseModel):
    content: str

@app.get("/api/memory")
def list_memory():
    try:
        resp = httpx.get(f"{lcm_client.LCM_URL}/facts", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.post("/api/memory")
def add_memory(req: MemoryIn):
    try:
        resp = httpx.post(f"{lcm_client.LCM_URL}/facts", json={"content": req.content, "manual": True}, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.delete("/api/memory/{fact_id}")
def delete_memory(fact_id: int):
    try:
        resp = httpx.delete(f"{lcm_client.LCM_URL}/facts/{fact_id}", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.delete("/api/messages/{message_id}")
def delete_message(message_id: int):
    try:
        resp = httpx.delete(f"{lcm_client.LCM_URL}/messages/{message_id}", timeout=10)
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
        resp = httpx.get(f"{lcm_client.LCM_URL}/sessions", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

@app.post("/api/sessions")
def upsert_session_proxy(req: SessionMetaIn):
    try:
        resp = httpx.post(f"{lcm_client.LCM_URL}/sessions", json=req.model_dump(), timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"LCM error: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"LCM error: {e}"}

class ForkSessionProxyIn(BaseModel):
    message_id: int
    label: str

@app.post("/api/sessions/{session_id}/fork")
def fork_session_proxy(session_id: str, req: ForkSessionProxyIn):
    """Proxy to LCM's fork endpoint -- creates a new, independent
    session containing everything up to and including message_id,
    letting the user rewind to a point in a conversation and continue
    down a different path without losing or altering the original."""
    try:
        resp = httpx.post(f"{lcm_client.LCM_URL}/sessions/{session_id}/fork", json=req.model_dump(), timeout=15)
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
        resp = httpx.delete(f"{lcm_client.LCM_URL}/session/{session_id}", timeout=10)
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

class NotesIn(BaseModel):
    notes: list

@app.get("/api/notes")
def get_notes():
    with settings.notes_lock:
        return {"notes": settings.load_notes()}

@app.post("/api/notes")
def save_notes_endpoint(req: NotesIn):
    with settings.notes_lock:
        settings.save_notes(req.notes)
    return {"ok": True}

class MemoryModelIn(BaseModel):
    model: Optional[str] = None

class SettingsIn(BaseModel):
    workspace: Optional[str] = None
    endpoints: Optional[list] = None
    search_url: Optional[str] = None
    default_model: Optional[dict] = None
    theme: Optional[dict] = None
    model_aliases: Optional[dict] = None
    bot_creation_defaults: Optional[dict] = None
    athena_agent_model: Optional[dict] = None
    delegation_concurrency: Optional[int] = None
    bot_ptc_enabled: Optional[bool] = None
    delegation_async_enabled: Optional[bool] = None
    laya_gating_enabled: Optional[bool] = None

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
    with settings.settings_lock:
        settings = settings.load_settings()
        servers = settings.setdefault("mcp_servers", [])
        servers = [s for s in servers if s["name"] != req.name]
        servers.append({"name": req.name, "command": req.command, "args": req.args or []})
        settings["mcp_servers"] = servers
        settings.save_settings(settings)
    try:
        tools = mcp_manager.connect_server(req.name, req.command, req.args or [])
        return {"connected": True, "tool_count": len(tools)}
    except Exception as e:
        return {"connected": False, "error": str(e)}

@app.delete("/api/mcp/servers/{name}")
def remove_mcp_server(name: str):
    from mcp_manager import mcp_manager
    mcp_manager.disconnect_server(name)
    with settings.settings_lock:
        settings = settings.load_settings()
        settings["mcp_servers"] = [s for s in settings.get("mcp_servers", []) if s["name"] != name]
        settings.save_settings(settings)
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
    return settings.load_settings()

@app.post("/api/settings")
def update_settings(req: SettingsIn):
    """Partial update -- only fields actually present in the request
    body get merged in, so one device saving just its workspace change
    can never accidentally wipe out another device's endpoints list or
    default model. Lock-guarded end to end (read, merge, write) so two
    devices saving at nearly the same moment serialize safely instead
    of one's update silently clobbering the other's."""
    with settings.settings_lock:
        settings = settings.load_settings()
        settings.update(req.model_dump(exclude_unset=True))
        settings.save_settings(settings)
        return settings

@app.get("/api/settings/memory-model")
def get_memory_model():
    return {"model": settings.load_settings().get("memory_extraction_model")}

@app.post("/api/settings/memory-model")
def set_memory_model(req: MemoryModelIn):
    settings = settings.load_settings()
    settings["memory_extraction_model"] = req.model
    settings.save_settings(settings)
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
        resp = httpx.get(f"{lcm_client.LCM_URL}/facts", timeout=5)
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
    scan_state = httpx.get(f"{lcm_client.LCM_URL}/scan-state", timeout=10).json()
    last_id = scan_state.get("last_scanned_message_id", 0)

    new_messages = httpx.get(f"{lcm_client.LCM_URL}/messages/since/{last_id}", timeout=10).json()
    if not new_messages:
        return  # nothing new -- also naturally prevents re-scanning during a long idle stretch

    existing_facts = httpx.get(f"{lcm_client.LCM_URL}/facts", timeout=10).json()
    existing_facts_text = "\n".join(f"- {f['content']}" for f in existing_facts) or "(none yet)"
    transcript_text = "\n".join(f"[{m['role']}] {m['content']}" for m in new_messages)

    max_id_seen = max(m["id"] for m in new_messages)
    last_session_id = new_messages[-1]["session_id"]

    host_key = _endpoint_host(OLLAMA_URL)
    lock = _get_host_lock(host_key)
    try:
        with lock:
            resp = httpx.post(OLLAMA_URL, json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _MEMORY_EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Existing facts already known:\n{existing_facts_text}\n\nNew conversation since last scan:\n{transcript_text}"},
                ],
                "options": {"num_ctx": 32768},
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
            httpx.post(f"{lcm_client.LCM_URL}/facts", json={
                "content": fact_text.strip(),
                "source_session_id": last_session_id,
                "source_message_id": max_id_seen,
                "manual": False,
            }, timeout=10)

    if new_facts:
        print(f"[Athena] Memory scan: added {len(new_facts)} new fact(s)", flush=True)
    httpx.post(f"{lcm_client.LCM_URL}/scan-state", json={"last_scanned_message_id": max_id_seen}, timeout=10)

def _memory_scan_loop():
    print("[Athena] Memory scan loop started (checks every 60s)", flush=True)
    while True:
        time.sleep(_MEMORY_SCAN_INTERVAL_SECONDS)
        try:
            model = settings.load_settings().get("memory_extraction_model")
            if not model:
                print("[Athena] Memory scan check: no model configured, skipping", flush=True)
                continue  # no model configured -- do nothing, no error
            idle_seconds = time.time() - activity.last_activity_ts
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

# ==================== Tasks (scheduled automations) ====================
# A task is a prompt that runs on its own, on a schedule, and posts its
# result into a normal chat session -- unattended, so it needs real,
# explicit handicapping rather than the broad tool access a live,
# supervised chat gets. Every list below is read fresh on each call, so
# newly added tools/skills show up automatically without a restart,
# and (per the safety default) start disabled until explicitly opted
# into on a given task.

BOTS_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bots.db")

def _bots_conn():
    conn = sqlite3.connect(BOTS_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            endpoint_url TEXT,
            provider TEXT NOT NULL DEFAULT '',
            api_key TEXT DEFAULT '',
            model TEXT NOT NULL,
            description TEXT,
            allowed_tools_json TEXT NOT NULL DEFAULT '[]',
            unload_strategy TEXT NOT NULL DEFAULT 'none',
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            human_party TEXT,
            label TEXT NOT NULL,
            member_bot_ids_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_active REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS room_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            sender_type TEXT NOT NULL,
            sender_bot_id INTEGER,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    return conn


class BotIn(BaseModel):
    name: str
    endpoint_url: Optional[str] = None
    provider: str = ""  # "" = Ollama-native, "custom" = OpenAI-compatible (llama.cpp/vLLM/etc). Never anthropic/google -- bots are local-inference only.
    api_key: Optional[str] = ""
    model: str
    description: Optional[str] = None
    allowed_tools: List[str] = []
    unload_strategy: str = "none"  # 'ollama_keep_alive' | 'llamacpp_unload' | 'vllm_sleep' | 'http_hook' | 'none'

def _row_to_bot(r):
    return {
        "id": r[0], "name": r[1], "endpoint_url": r[2], "provider": r[3], "api_key": r[4], "model": r[5],
        "description": r[6], "allowed_tools": json.loads(r[7]) if r[7] else [],
        "unload_strategy": r[8], "created_at": r[9],
    }

_BOT_COLUMNS = "id, name, endpoint_url, provider, api_key, model, description, allowed_tools_json, unload_strategy, created_at"

@app.get("/api/bots")
def list_bots():
    conn = _bots_conn()
    try:
        rows = conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots ORDER BY created_at ASC").fetchall()
        return [_row_to_bot(r) for r in rows]
    finally:
        conn.close()

@app.post("/api/bots")
def create_bot(req: BotIn):
    conn = _bots_conn()
    try:
        cur = conn.execute("""
            INSERT INTO bots (name, endpoint_url, provider, api_key, model, description, allowed_tools_json, unload_strategy, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            req.name, req.endpoint_url, req.provider, req.api_key or "", req.model, req.description,
            json.dumps(req.allowed_tools), req.unload_strategy, time.time(),
        ))
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()

@app.put("/api/bots/{bot_id}")
def update_bot(bot_id: int, req: BotIn):
    conn = _bots_conn()
    try:
        conn.execute("""
            UPDATE bots SET name=?, endpoint_url=?, provider=?, api_key=?, model=?, description=?, allowed_tools_json=?, unload_strategy=?
            WHERE id=?
        """, (
            req.name, req.endpoint_url, req.provider, req.api_key or "", req.model, req.description,
            json.dumps(req.allowed_tools), req.unload_strategy, bot_id,
        ))
        conn.commit()
        return {"id": bot_id}
    finally:
        conn.close()

@app.delete("/api/bots/{bot_id}")
def delete_bot(bot_id: int):
    conn = _bots_conn()
    try:
        conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))
        conn.commit()
        return {"id": bot_id}
    finally:
        conn.close()


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

@app.post("/api/rooms/everyone")
def get_or_create_everyone_room():
    """The one canonical room containing every bot -- membership isn't
    a fixed choice like a normal group, it's always 'whichever bots
    currently exist', refreshed here on every open rather than kept in
    sync via create/delete hooks elsewhere. Response routing doesn't
    actually depend on this stored list at all (a group room's replies
    are driven entirely by @mention, checked against the full roster
    regardless of room membership) -- this refresh is purely so the
    displayed member list in the UI stays accurate."""
    conn = _bots_conn()
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

@app.get("/api/rooms")
def list_rooms():
    conn = _bots_conn()
    try:
        rows = conn.execute(f"SELECT {_ROOM_COLUMNS} FROM rooms ORDER BY last_active DESC, created_at DESC").fetchall()
        return [_row_to_room(r) for r in rows]
    finally:
        conn.close()

@app.post("/api/rooms/find_or_create")
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
    conn = _bots_conn()
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

@app.get("/api/rooms/{room_id}/messages")
def get_room_messages(room_id: int):
    conn = _bots_conn()
    try:
        rows = conn.execute(
            "SELECT id, room_id, sender_type, sender_bot_id, content, created_at FROM room_messages WHERE room_id = ? ORDER BY id ASC",
            (room_id,)
        ).fetchall()
        return [{"id": r[0], "room_id": r[1], "sender_type": r[2], "sender_bot_id": r[3], "content": r[4], "created_at": r[5]} for r in rows]
    finally:
        conn.close()

@app.post("/api/rooms/{room_id}/messages")
def post_room_message(room_id: int, req: RoomMessageIn):
    conn = _bots_conn()
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


class _FakeReqForDispatch:
    """Minimal stand-in for ChatIn, carrying only the fields
    _stream_completion / _stream_openai_compatible actually read
    (provider, model, endpoint_url, api_key). Bot dispatch has no
    session/workspace/tool-allowlist concerns a real ChatIn carries,
    so building one of those would mean populating a dozen irrelevant
    fields just to satisfy the type -- this carries exactly what's
    needed and nothing else."""
    def __init__(self, provider, model, endpoint_url, api_key):
        self.provider = provider
        self.model = model
        self.endpoint_url = endpoint_url
        self.api_key = api_key


_BOT_ALLOWED_PROVIDERS = {"", "openai", "openrouter", "custom"}  # never anthropic/google -- bots are local-inference only, enforced here as a backend guard even though the UI never offers those options


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


def _call_bot_endpoint(bot, chat_messages):
    """Run a bot's full turn to completion: send chat_messages, and if
    it calls a tool, actually execute it and loop back with the result
    until it produces a final answer with no more tool calls -- the
    same round-trip pattern generate() uses for Athena's own turns.
    Previously this ran exactly one non-streaming shot with tools=None
    and never even checked the response for tool_calls at all -- so a
    bot could never really call a tool: no function-calling ability
    was offered to Ollama, it produced a fast non-answer, and got
    unloaded right after, which looked like an instant flash-on then
    flash-off with no real work ever happening."""
    provider = bot.get("provider") or ""
    if provider not in _BOT_ALLOWED_PROVIDERS:
        return {"error": f"Bots may not use provider '{provider}' -- local inference only (Ollama-native or OpenAI-compatible)."}

    endpoint_url = bot.get("endpoint_url") or ""
    fake_req = _FakeReqForDispatch(
        provider=provider,
        model=bot["model"],
        endpoint_url=endpoint_url,
        api_key=bot.get("api_key") or "",
    )
    target_url = (endpoint_url.rstrip("/") + "/api/chat") if endpoint_url and provider == "" else (OLLAMA_URL if provider == "" else "")
    _settings_snapshot = settings.load_settings()
    workspace = _settings_snapshot.get("workspace") or ""
    ptc_enabled = bool(_settings_snapshot.get("bot_ptc_enabled"))
    allowed_tools = bot.get("allowed_tools") or []
    messages = list(chat_messages)
    session_key = f"bot-{bot.get('id', 'unknown')}"

    round_reply = ""
    seen_fingerprints = []
    # Every real tool call this turn makes, tool name + args + a
    # compressed outcome (see _summarize_tool_outcome) -- returned
    # alongside content/error so a caller can see HOW this turn reached
    # its answer, not just what it said. _run_delegation_step_tool
    # persists this to a per-task log; Athena2 only pays for it when she
    # actually calls read_tool_call_history, not on every step by default.
    tool_call_history = []
    # Per-tool-type call counts for this turn, tracked independently so
    # a binge on one group (e.g. explore) can't hide behind quiet use of
    # another (e.g. web) -- see _BOT_TOOL_TYPE_GROUPS.
    _tool_type_counts = {}
    _tool_type_warned = set()
    _tool_type_hard_stopped = set()
    for _ in range(_BOT_MAX_ROUNDS):
        activity.touch()
        round_reply = ""
        round_tool_calls = []
        cancel_flag = threading.Event()
        tool_schemas = _bot_tool_schemas(allowed_tools, _tool_type_hard_stopped, ptc_enabled)
        try:
            round_thinking = ""
            for chunk in _stream_completion(fake_req, target_url, messages, tool_schemas if tool_schemas else None, _BOT_CTX_SIZE, cancel_flag, timeout=_SUB_AGENT_TIMEOUT):
                msg = chunk.get("message", {})
                thinking_delta = msg.get("thinking", "")
                if thinking_delta:
                    round_thinking += thinking_delta
                delta = msg.get("content", "")
                if delta:
                    round_reply += delta
                    if _detect_text_loop(round_reply):
                        break
                if msg.get("tool_calls"):
                    round_tool_calls.extend(msg["tool_calls"])
                if chunk.get("done"):
                    if not round_reply and not round_tool_calls:
                        bot_name = bot.get("name")
                        print(f"[BOT DEBUG] bot={bot_name} round produced no content/tool_calls -- thinking was {len(round_thinking)} chars: {round_thinking[-500:]!r}", flush=True)
                    break
        except Exception as e:
            return {"error": f"Bot endpoint call failed: {e}", "tool_call_history": tool_call_history}

        if not round_tool_calls:
            return {"content": round_reply, "tool_call_history": tool_call_history}

        messages.append({"role": "assistant", "content": round_reply, "tool_calls": round_tool_calls})
        for tc in round_tool_calls:
            tool_name = tc.get("function", {}).get("name", "")
            group = _bot_tool_type_group(tool_name)
            if group:
                _tool_type_counts[group] = _tool_type_counts.get(group, 0) + 1
            fn_args = tc.get("function", {}).get("arguments", {})
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except json.JSONDecodeError:
                    fn_args = {}
            fingerprint = _tool_call_fingerprint(tc)
            if seen_fingerprints.count(fingerprint) >= 2:
                result = {"error": "BLOCKED: this exact tool call (same tool, same arguments) has already been made twice this turn. Use what you already have, or make a genuinely different call."}
                tool_call_history.append(_tool_call_history_entry(tool_name, fn_args, result))
            elif tool_name == "run_tool_program":
                result = _execute_bot_tool_program(
                    fn_args.get("code", ""), workspace, bot, session_key, allowed_tools,
                    _tool_type_counts, _tool_type_hard_stopped, seen_fingerprints,
                )
                tool_call_history.append(_tool_call_history_entry(tool_name, {"code": fn_args.get("code", "")}, {"status": result.get("status") or "error", "detail": result.get("detail") or result.get("error")}))
                tool_call_history.extend(result.get("tool_call_history", []))
                seen_fingerprints.append(fingerprint)
                if len(seen_fingerprints) > 20:
                    seen_fingerprints.pop(0)
            else:
                result = _execute_bot_tool_call(tc, workspace, bot, session_key)
                tool_call_history.append(_tool_call_history_entry(tool_name, fn_args, result))
                seen_fingerprints.append(fingerprint)
                if len(seen_fingerprints) > 20:
                    seen_fingerprints.pop(0)
            messages.append({"role": "tool", "content": json.dumps(result)})

        for group, count in _tool_type_counts.items():
            if count >= _BOT_TOOL_TYPE_HARD_STOP_AFTER and group not in _tool_type_hard_stopped:
                _tool_type_hard_stopped.add(group)
                messages.append({"role": "user", "content": (
                    f"Your '{group}' tools are no longer available for the rest of this turn -- you've used "
                    f"them {count} times without recording findings. Call append_scratch_note now with "
                    "whatever you've actually found so far; do not keep investigating."
                )})
            elif count >= _BOT_TOOL_TYPE_WARN_AFTER and group not in _tool_type_warned:
                _tool_type_warned.add(group)
                messages.append({"role": "user", "content": (
                    f"You've made {count} '{group}' tool calls this turn without calling append_scratch_note "
                    "yet. If you already have enough to answer, call append_scratch_note now instead of "
                    "continuing to investigate."
                )})

    return {
        "content": round_reply,
        "error": "Hit the round limit without a final answer after repeated real tool calls -- this may genuinely be too large a job for one turn; consider a narrower job_scope.",
        "tool_call_history": tool_call_history,
    }


_bot_host_locks = {}
_bot_host_locks_guard = threading.Lock()


def _endpoint_host(endpoint_url):
    """Extract just host:port (no scheme, no path) for lock-keying --
    two bots pointing at the same host, even via different frameworks
    (Ollama vs a local OpenAI-compatible server), must never generate
    concurrently. Falls back to OLLAMA_URL's own host when a bot has
    no explicit endpoint_url, since that's genuinely the same shared
    machine every other Ollama-native bot (and Athena's own default
    chat) also targets."""
    url = endpoint_url or OLLAMA_URL
    parsed = urlparse(url)
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else (parsed.hostname or url)


def _get_host_lock(host_key):
    with _bot_host_locks_guard:
        if host_key not in _bot_host_locks:
            _bot_host_locks[host_key] = threading.Lock()
        return _bot_host_locks[host_key]


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
    import re
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


def _dispatch_athena_room_tool_call(tc):
    """Delegation-only dispatcher for Athena's room-turn loop -- a
    small, dedicated table for just the delegation tools, since the
    main generate() loop's own _execute_tool_call is a closure over a
    live req object that doesn't exist for a mention-triggered room
    turn. Never includes workspace write tools (bash_exec, file
    edits) -- those stay confined to her private 1:1 session, per the
    agreed design."""
    fn = tc.get("function", {})
    name = fn.get("name", "")
    args = fn.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if name == "list_bots":
        return _list_bots_tool()
    if name == "draft_bot_prompt":
        return _draft_bot_prompt(args.get("name", ""), args.get("job_scope", ""), args.get("tools", []), args.get("additional_constraints"))
    if name == "message_bot":
        return _message_bot_tool(args.get("bot_id"), args.get("content", ""))
    if name == "list_rooms":
        return _list_rooms_tool()
    if name == "read_room_messages":
        return _read_room_messages_tool(args.get("room_id"))
    if name == "message_room":
        return _message_room_tool(args.get("room_id"), args.get("content", ""))
    if name == "create_bot":
        return _create_bot_tool(args.get("name", ""), args.get("description", ""), args.get("allowed_tools", []))
    if name == "update_bot":
        return _update_bot_tool(args.get("bot_id"), args.get("description"), args.get("allowed_tools"))
    return {"error": f"Unknown tool for Athena's room turn: {name}"}


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
    settings = settings.load_settings()
    defaults = settings.get("athena_agent_model") or {}
    model = defaults.get("model")
    if not model:
        yield f"data: {json.dumps({'error': "No model is configured for Athena's own agent turns yet. Set one via her Agent Defaults / model picker first."})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"
        return

    conn = _bots_conn()
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
        all_bot_rows = conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots").fetchall()
        bots_by_id = {b["id"]: b for b in [_row_to_bot(r) for r in all_bot_rows]}
    finally:
        conn.close()

    history = [{"sender_type": r[0], "sender_bot_id": r[1], "content": r[2],
                "sender_bot_name": bots_by_id.get(r[1], {}).get("name") if r[1] else None} for r in history_rows]

    room_turn_workspace = settings.load_settings().get("workspace") or ""
    system_prompt = CODING_HARNESS_SYSTEM_PROMPT + _get_bot_delegation_prompt_section(room_turn_workspace)
    chat_messages = [{"role": "system", "content": system_prompt}]
    for m in history:
        if m["sender_type"] == "athena":
            chat_messages.append({"role": "assistant", "content": m["content"]})
        else:
            label = "User" if m["sender_type"] == "user" else (m.get("sender_bot_name") or f"Bot #{m.get('sender_bot_id')}")
            chat_messages.append({"role": "user", "content": f"[{label}]: {m['content']}"})

    fake_req = _FakeReqForDispatch(
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
            for chunk in _stream_completion(fake_req, target_url, chat_messages, BOT_DELEGATION_TOOL_SCHEMAS, 8192, cancel_flag):
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
    conn = _bots_conn()
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


@app.post("/api/rooms/{room_id}/send")
def send_room_message(room_id: int, req: RoomSendIn):
    """The real conversational entry point: store the incoming
    message, work out who should respond (per-DM: the other party,
    always; per-group-room: only @mentioned bots, never a silent
    default responder), dispatch each responder under its host's
    concurrency lock, run its unload strategy afterward, and store
    each reply -- all visible in the same room_messages history."""
    conn = _bots_conn()
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

        all_bot_rows = conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots").fetchall()
        all_bots = [_row_to_bot(r) for r in all_bot_rows]
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
            system_prompt = _build_bot_system_prompt(bot)
            chat_messages = _room_messages_to_chat_messages(history, bot_id, system_prompt)

            host_key = _endpoint_host(bot.get("endpoint_url"))
            lock = _get_host_lock(host_key)
            with lock:
                result = _call_bot_endpoint(bot, chat_messages)
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


_BOT_TOOL_DESCRIPTIONS = {
    "bash": "read-only shell commands (ls, cat, grep, find, etc.) to look at files and search",
    "search_codebase": "semantic search over the indexed codebase for orientation",
    "find_definition": "jump to where a symbol is actually defined",
    "find_references": "find every real usage of a symbol across the workspace",
    "type_info": "check a symbol's real inferred type/signature",
    "web_search": "search the web for current information",
    "web_fetch": "fetch the full content of a specific URL",
    "read_file": "read a file's contents within the workspace",
    "list_files": "list files and directories within the workspace",
}


def _build_bot_system_prompt(bot):
    """Every bot's real system prompt is assembled here, uniformly,
    from its stored description (used as job_scope) and allowed_tools
    -- never the raw description text used directly as the whole
    prompt. This is what actually guarantees the safety structure
    (bounded scope, relay-don't-execute) applies to every bot, whether
    it was created through the UI form or by Athena's own
    draft_bot_prompt tool, since neither path can skip this step."""
    job_scope = bot.get("description") or f"Act as a general-purpose specialist assistant named {bot['name']}."
    tools = [
        {"name": t, "when_to_use": _BOT_TOOL_DESCRIPTIONS.get(t, "use when relevant to the job")}
        for t in (bot.get("allowed_tools") or [])
    ]
    return _draft_bot_prompt(bot["name"], job_scope, tools)


def _draft_bot_prompt(name, job_scope, tools, additional_constraints=None):
    """Assemble a bot's system prompt from structured fields. Grounded
    in Anthropic's own stated framework for what a system prompt
    actually needs to do -- state goals, constraints, and stop rules --
    rather than an elaborate procedural script. The one addition
    specific to bots: a bot never executes -- it investigates within
    job_scope and relays findings back to Athena, which is the real
    safety boundary the whole roster is built around."""
    tools = [t for t in tools if t.get("name") in BOT_ALLOWED_TOOL_NAMES]
    _bot_prompt_tool_cache[name.strip().lower()] = [t["name"] for t in tools]
    tool_lines = "\n".join(f"- {t['name']}: {t['when_to_use']}" for t in tools) if tools else "(no tools -- reasoning only)"
    constraints_block = f"\n\n## Additional Constraints\n{additional_constraints}" if additional_constraints else ""
    return f"""You are {name}.

## Think Before Acting
Plan your approach before calling a tool. If something doesn't match what you expected, say so plainly rather than guessing or working around it silently.

## Simplicity First
Investigate only what the job below actually requires. Don't expand scope on your own just because something seems related.

## Reading Large Files
Never read a large file from the start to the end just because it was mentioned in your job. Orient first -- use search_codebase or a targeted bash grep to find the specific section, function, or line range that actually answers your job, then use read_file with offset to read only that section. If read_file reports has_more: true, that does not mean you should keep paginating through the whole file -- only continue if the specific section you actually need is further in. You have a limited number of rounds and a limited context budget for this one job; exhaustively reading a large file page by page will leave you unable to report your findings at all. A partial, targeted read that answers the actual question is far more useful than an incomplete attempt at reading everything.

## Scoped Tool Use
Use only the tools relevant to this job:
{tool_lines}

## Goal-Driven, With a Stop Rule
Your job: {job_scope}
That's what must be true when you're done. If a tool call fails, report the failure honestly rather than retrying blindly or guessing at an answer anyway.

## Relay, Don't Execute
You investigate and report. You never write files, run commands, or make any real change yourself. When you've finished -- or when you're genuinely stuck -- if append_scratch_note is one of your tools, call it with your findings; that is the only way they actually reach Athena, so don't reply with them in plain text instead. If append_scratch_note is not one of your tools, reply with your findings directly. Either way, Athena is the one who acts on what you report -- you never do.{constraints_block}"""


ATHENA_BOTS_SESSION_ID = "athena-bots-agent"

def _get_bot_delegation_prompt_section(workspace=None, async_enabled=False, laya_gating_enabled=False):
    base = """

## Stay On the Current Task
Treat the person's most recent message as your current task. Earlier messages are context and history, not standing instructions -- if something discussed a few messages ago feels similar to what's being asked now, that doesn't make it the same task. This doesn't change how you work: keep using search_codebase and delegating to bots for the current request exactly as you normally would. It only means match what you investigate or delegate to what was JUST asked, not to an earlier topic that happens to feel related.

## Bot Delegation
You have access to a roster of specialist bots you can delegate investigate-only work to. Bots never write files or run commands -- they investigate and report back to you; you are the one who acts on their findings.

Delegate the moment you don't already know exactly which file (or small, specific set of files) contains the answer to what you're being asked -- that's the concrete signal to delegate, not a subjective judgment call about how 'significant' the task feels. If you'd have to explore to find out where the answer lives, that exploration is a bot's job, not yours. Decide once, before your first tool call on the topic: either you already know exactly where to look (read it yourself, it's fine), or you don't (delegate it, and don't explore it yourself first). Never investigate something yourself first and then ALSO delegate the same investigation to a bot -- that wastes both your own context and the bot's work for no reason.

Use list_bots to see who's available before delegating. draft_bot_prompt helps you write a new bot's system prompt in the right structure when creating one. Before creating a bot with create_bot, always check list_bots first and pick a name that isn't already in use -- a duplicate name will be rejected.

Bots must be broad, general-purpose specialists (e.g. "web research", "UI/frontend code", "backend/API work") -- never a narrow one-off bot scoped to a single specific task. Check whether an existing bot's field already fits before creating a new one; reuse it rather than creating something redundant. If the roster is currently empty, the first bot you create must be a fully general-purpose one with no specific niche at all, since there's nothing yet to route more specialized work to.

## Carrying Constraints Into a Step
A bot in an isolated delegation step knows NOTHING you and the person discussed in this conversation -- only the exact instruction text you write for that specific step. If the person gives you any constraint, restriction, or scope limit -- "don't look at X", "avoid anything listed in .gitignore", "only consider Y", "these files are personal, not for public documentation" -- you must explicitly restate that constraint inside the instruction text of every single step where it applies, every time, not just remember it yourself for the conversation. A constraint the person told YOU does not exist for the bot unless you wrote it into that step's instruction. This applies even when it feels repetitive across many steps -- repeating it costs nothing; omitting it means the bot has no way to know the restriction exists at all.

## Two Ways to Talk to a Bot
message_bot and message_room are ONLY for quick, informal back-and-forth that is not part of a real investigation -- e.g. asking a bot a one-off question with no expectation of a formal finding. message_bot is NEVER an acceptable way to perform, continue, retry, or work around delegation -- not as a first choice, not as a fallback, not "just to get an answer" when a delegation step is being difficult. If you are doing investigative work for the person, the only tools you may use are run_delegation_step and plan_delegation, start to finish, including every retry. read_room_messages lets you review any conversation's full history, including ones between two bots, since nothing here is hidden from you.

For real investigative work, use run_delegation_step or plan_delegation. These give a bot one single-topic instruction as a completely fresh, isolated call -- no DM history, no memory of prior steps -- which keeps a bot from getting bogged down or confused by its own past turns on a long investigation. The bot appends its findings to its own scratch file via append_scratch_note rather than replying with them directly; you then call read_scratch_file for that bot to see what it found. Use plan_delegation when you already know the full set of single-topic steps you want to run -- give it the whole list at once rather than calling run_delegation_step yourself one at a time. Bots run on small, resource-constrained local models, so a single step's instruction must cover ONE focused topic, never a compound multi-part request (e.g. "cover these nine areas: setup, config, LCM, voice, coding harness, web UI, API, dependencies, project structure") -- a bundled instruction like that overwhelms a small model and produces slow, incomplete, or truncated results regardless of which tool carries it.

If run_delegation_step or plan_delegation reports status "no_scratch_note", that step genuinely failed -- the bot did not record findings, full stop. Retry that one step once, ideally with a more explicit instruction telling the bot to call append_scratch_note. If it fails a second time, or if read_scratch_file comes back with no file, report that failure to the person honestly.

## Committing to a Plan
Once you call plan_delegation in a turn, that commitment is permanent for the rest of the turn: run_delegation_step structurally disappears from your tool list and will not come back. This is deliberate -- if a step in the plan comes back rejected (e.g. flagged as a compound, multi-topic instruction) or incomplete, the fix is to correct that step's wording (or split it into proper single-topic steps) and call plan_delegation again with the corrected list, never to fall back to one-off run_delegation_step calls as a workaround. Rewording the same ask and re-running it as an individual step instead of fixing and resubmitting the plan is exactly the pattern this exists to stop.

Under no circumstances switch to message_bot to "get an answer anyway" when a delegation step fails -- an informal reply obtained that way is not a substitute for a real investigation, was not produced under the same isolation guarantees, and must never be presented as a delegation finding. A reported failure is always the correct outcome over a message_bot workaround.

The same applies if a scratch file's content looks fabricated or ungrounded (invented file structure, generic placeholder-style detail, claims that don't match anything you already know to be true) -- treat that as a failed step too, not a usable finding. When you suspect that, use read_tool_call_history for that bot before deciding: it shows every tool call the bot actually made for each instruction and roughly what each one returned, so you can check whether the tool activity behind a note is really there instead of guessing from the note's wording alone.

Do NOT use read_file yourself on a large file (main.py or anything else of comparable size) while doing delegation work. You have a limited context budget for your own turn, and reading a large file directly -- especially across the multiple paginated calls a big file requires -- burns through that budget on investigation you don't need to do yourself, since a bot has its own separate, isolated context for exactly this. If you need to know something about a large file, delegate that investigation to a bot via run_delegation_step or plan_delegation and read its findings back with read_scratch_file, the same as any other investigative work in this mode. read_file is fine for small, targeted files you already know are short."""
    if async_enabled:
        base += (
            "\n\n## Async Delegation\n"
            "run_delegation_step and plan_delegation no longer wait for the bot(s) to finish. They return "
            "immediately with {\"status\": \"pending\", \"handle\": <job id>} -- that is not a failure, and "
            "there is no findings content in that return value yet. You are free to keep working (answer the "
            "person, delegate another step, or just end your turn) while the bot works in the background. "
            "When it finishes, its result arrives as a brand new message in this same conversation, and you "
            "should react to it then -- read the bot's scratch file (and tool call history if anything about "
            "it looks fabricated or ungrounded) and continue from there. Do not treat a 'pending' status as "
            "something to retry, poll, or wait on synchronously; just proceed with your turn."
        )
    if laya_gating_enabled:
        base += (
            "\n\n## Automatic Fabrication Screening\n"
            "A step that returns status \"possibly_fabricated\" already went through an automatic check comparing "
            "the bot's scratch note against its actual tool call history -- you don't need to also manually judge "
            "whether the note looks fabricated or ungrounded the way you would otherwise. Treat this status exactly "
            "like a failed step: don't use its findings, and use read_tool_call_history/read_scratch_file yourself "
            "before deciding whether to retry. A step returning \"done\" already passed this screening; you can "
            "still double-check it yourself if something about it still feels off, but you no longer have to "
            "eyeball every note for fabrication signs on your own."
        )
    defaults = (settings.load_settings().get("bot_creation_defaults") or {})
    naming_guidance = defaults.get("naming_guidance")
    if naming_guidance:
        base += f"\n\nWhen naming a new bot: {naming_guidance}"
    if not workspace:
        base += (
            "\n\nNo workspace is currently bound to this session. You have no ability to read, "
            "write, or run anything on real files right now -- any attempt will simply fail. If the "
            "person's request requires real file or command access, tell them plainly that a "
            "workspace needs to be set first, rather than attempting the action or guessing at what "
            "it might find."
        )
    return base


def _list_bots_tool():
    conn = _bots_conn()
    try:
        rows = conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots ORDER BY created_at ASC").fetchall()
        return [_row_to_bot(r) for r in rows]
    finally:
        conn.close()


BOT_SCRATCH_TOOL_NAME = "append_scratch_note"

APPEND_SCRATCH_NOTE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "append_scratch_note",
        "description": "Append your findings for this task to your own scratch file. Append-only -- no path control, no overwrite, no delete. Athena reads this file once, after your work is done, instead of you reporting back through chat.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The findings or notes to append."}
            },
            "required": ["content"]
        }
    }
}

def _append_scratch_note(bot_name, content):
    task_hash = task_context.current_task_hash.get()
    if task_hash is None:
        return {"error": "No active task context to write a scratch note into."}
    folder = os.path.join(task_context.BOT_TASK_FOLDER_ROOT, task_hash)
    os.makedirs(folder, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", bot_name.lower())
    path2 = os.path.join(folder, f"{safe_name}.md")
    with open(path2, "a", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n\n")
    return {"ok": True}


def _summarize_tool_outcome(result, max_len=200):
    """Compress one tool call's real result down to a short outcome
    string for tool_call_history -- full results (a file's contents, a
    page of search hits) can be huge, and the point of this history is
    letting Athena2 see WHICH tools a bot actually used and roughly
    whether each one worked, not duplicating everything the scratch
    file already carries."""
    if isinstance(result, dict) and "error" in result:
        msg = str(result["error"])
        return "error: " + (msg if len(msg) <= max_len else msg[:max_len] + "...")
    try:
        s = json.dumps(result)
    except TypeError:
        s = str(result)
    return "ok: " + (s if len(s) <= max_len else s[:max_len] + "...")


def _tool_call_history_entry(tool_name, args, result):
    return {"tool": tool_name, "args": args, "outcome": _summarize_tool_outcome(result)}


def _append_tool_call_history(bot_name, instruction, calls):
    """Append one delegation step's tool-call trail to a per-bot,
    per-task JSONL log -- same task-hash scoping and append-only shape
    as _append_scratch_note, just a separate file so Athena2 only pays
    for it when she actually asks (see read_tool_call_history), instead
    of it riding along in every run_delegation_step/plan_delegation
    response by default."""
    task_hash = task_context.current_task_hash.get()
    if task_hash is None or not calls:
        return
    folder = os.path.join(task_context.BOT_TASK_FOLDER_ROOT, task_hash)
    os.makedirs(folder, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", bot_name.lower())
    path2 = os.path.join(folder, f"{safe_name}.calls.jsonl")
    with open(path2, "a", encoding="utf-8") as f:
        f.write(json.dumps({"instruction": instruction, "calls": calls}) + "\n")


def _get_bot_row(bot_id):
    for b in _list_bots_tool():
        if b.get("id") == bot_id:
            return b
    return None


def _read_scratch_file(bot_name):
    task_hash = task_context.current_task_hash.get()
    if task_hash is None:
        return {"error": "No active task context -- nothing has been delegated yet this turn."}
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", bot_name.lower())
    fpath = os.path.join(task_context.BOT_TASK_FOLDER_ROOT, task_hash, f"{safe_name}.md")
    if not os.path.exists(fpath):
        return {"error": f"No scratch file for '{bot_name}' in the current task yet."}
    with open(fpath, "r", encoding="utf-8") as f:
        return {"content": f.read()}


def _read_tool_call_history(bot_name):
    """Read back every tool call a bot actually made across every step
    it's run in the current task, grouped by the instruction each batch
    of calls belongs to -- for checking HOW a bot reached its findings,
    e.g. when a scratch note looks fabricated or ungrounded and you want
    to see whether the tool calls behind it are actually there."""
    task_hash = task_context.current_task_hash.get()
    if task_hash is None:
        return {"error": "No active task context -- nothing has been delegated yet this turn."}
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", bot_name.lower())
    fpath = os.path.join(task_context.BOT_TASK_FOLDER_ROOT, task_hash, f"{safe_name}.calls.jsonl")
    if not os.path.exists(fpath):
        return {"error": f"No tool-call history for '{bot_name}' in the current task yet."}
    steps = []
    with open(fpath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                steps.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {"steps": steps}


_NUMBERED_ITEM_RE = re.compile(r"(?m)^\s*(?:\d+[\.\)]|[-*])\s+\S")

# Below this single-topic probability, Laya's compound-instruction check
# flags an instruction even though the regex above found nothing --
# backs up the regex on prose-phrased compound asks it structurally can't
# see (e.g. "check X and also see if Y", no numbering/bullets involved).
# Picked from standalone testing: true compound instructions scored
# 0.231-0.370 single-topic-probability, while most genuinely single-topic
# instructions scored 0.42-0.48 -- some overlap exists (this check has
# real recall over precision, accepted deliberately: a false positive
# here just costs an unnecessarily cautious plan_delegation call, not a
# wrong answer). Originally 0.4; lowered to 0.3 after real usage showed
# it was flagging genuinely single-topic instructions too often -- still
# catches every true-compound case from standalone testing except the
# single highest-scoring one (0.370), trading a small amount of recall
# for less over-flagging.
_LAYA_COMPOUND_THRESHOLD = 0.3

def _instruction_is_compound(instruction):
    """Cheap structural check: does this instruction try to cram
    multiple distinct asks into one step? A numbered/bulleted list of
    3+ items is the clearest signal -- a real single-topic instruction
    doesn't need one. This exists because prompt instructions alone
    have repeatedly failed to stop this on a small model under load;
    rejecting it structurally forces plan_delegation instead."""
    items = _NUMBERED_ITEM_RE.findall(instruction)
    if len(items) >= 3:
        return True, f"contains {len(items)} numbered/bulleted items"
    if len(instruction) > 1200:
        return True, f"is {len(instruction)} characters long (over the 1200 char guideline for a single-topic step)"
    if settings.load_settings().get("laya_gating_enabled"):
        p_single_topic = laya_gate.check_single_topic(instruction)
        if p_single_topic is not None and p_single_topic < _LAYA_COMPOUND_THRESHOLD:
            return True, "was flagged by the compound-instruction checker as covering more than one distinct topic"
    return False, ""

# Async delegation job tracking (see _run_delegation_step_async_tool /
# _plan_delegation_async_tool below) -- same shape as _bg_processes /
# _bg_processes_lock / _bg_process_counter, which already does "start a
# background unit of work, hand back an id immediately" for background
# bash processes. Not pruned, consistent with _bg_processes never being
# pruned either.
_delegation_jobs = {}
_delegation_jobs_lock = threading.Lock()
_delegation_job_counter = [0]
_delegation_delivery_locks = {}
_delegation_delivery_locks_guard = threading.Lock()


def _get_delegation_delivery_lock(session_id):
    """Mirrors _get_host_lock -- serializes delivery per session so two
    async jobs finishing close together on the same session can't both
    fire a chat_stream(req) call at the same time."""
    with _delegation_delivery_locks_guard:
        if session_id not in _delegation_delivery_locks:
            _delegation_delivery_locks[session_id] = threading.Lock()
        return _delegation_delivery_locks[session_id]


def _capture_req_fields(req):
    """Snapshot the fields needed to rebuild an equivalent ChatIn later,
    from the request object already in scope at the tool-dispatch site
    (this is always the one fixed athena-bots-agent session, so the
    same model/workspace/endpoint apply to the later, headless turn)."""
    return {
        "session_id": req.session_id,
        "model": req.model,
        "workspace": req.workspace,
        "endpoint_url": req.endpoint_url,
        "search_url": req.search_url,
        "allowed_tools": req.allowed_tools,
        "allowed_skills": req.allowed_skills,
        "provider": req.provider,
        "api_key": req.api_key,
    }


def _deliver_delegation_result(req_fields, content):
    """Injects `content` as a new turn into the session, headless --
    same discard-the-StreamingResponse trick _start_task_run uses to
    drive a chat turn with no live HTTP client. Waits for the session to
    be free of any in-progress generation first (mirrors
    _task_scheduler_loop's own generation_streaming.active_generations check) so this never
    fires a second concurrent generation into the same session."""
    session_id = req_fields["session_id"]
    lock = _get_delegation_delivery_lock(session_id)
    with lock:
        while session_id in generation_streaming.active_generations:
            time.sleep(2)
        req = ChatIn(
            session_id=session_id,
            message=content,
            model=req_fields["model"],
            workspace=req_fields["workspace"],
            endpoint_url=req_fields["endpoint_url"],
            search_url=req_fields["search_url"],
            allowed_tools=req_fields["allowed_tools"],
            allowed_skills=req_fields["allowed_skills"],
            provider=req_fields["provider"],
            api_key=req_fields["api_key"],
        )
        try:
            chat_stream(req)  # returned StreamingResponse deliberately never read -- see _start_task_run for the same pattern
        except Exception as e:
            print(f"[DELEGATION] failed to deliver async result for session {session_id!r}: {e!r}", flush=True)


def _next_delegation_job_id():
    with _delegation_jobs_lock:
        _delegation_job_counter[0] += 1
        return str(_delegation_job_counter[0])


# Below this accuracy probability, the fabrication check flags a scratch
# note as not grounded in what the bot's tools actually returned this
# step. Picked from standalone testing: a genuinely grounded note scored
# 0.724 "accurate", while fabricated/vague variants scored 0.385 or
# lower -- 0.45 sits in that gap with margin on both sides.
_LAYA_FABRICATION_THRESHOLD = 0.45

def _maybe_flag_fabrication(bot, instruction, scratch_path, before_size, step_history):
    """Returns a possibly_fabricated result dict to use INSTEAD of a
    "done" status, or None if the note looks fine (or the checker is off/
    unavailable). Reads only the bytes appended since before_size, so this
    checks exactly the note this step just wrote, not earlier steps'
    accumulated content in the same scratch file."""
    if not settings.load_settings().get("laya_gating_enabled"):
        return None
    with open(scratch_path, "r", encoding="utf-8") as f:
        f.seek(before_size)
        new_note = f.read()
    p_accurate = laya_gate.check_note_accurate(new_note, step_history)
    if p_accurate is not None and p_accurate < _LAYA_FABRICATION_THRESHOLD:
        return {
            "status": "possibly_fabricated",
            "detail": f"This note doesn't look grounded in this step's actual tool activity (Laya's estimate: {p_accurate:.2f} probability it's accurate) -- treat this as a failed step, not a usable finding. Use read_tool_call_history to check what the bot actually did, and read_scratch_file to see the note itself before deciding whether to retry.",
            "bot": bot["name"], "instruction": instruction, "tool_calls_recorded": len(step_history),
        }
    return None


def _run_delegation_step_tool(bot_id, instruction):
    is_compound, reason = _instruction_is_compound(instruction)
    if is_compound:
        return {
            "status": "rejected",
            "error": f"This instruction {reason} -- that is a compound, multi-topic request, not a single-topic step. "
                     "Break it into separate single-topic steps and use plan_delegation instead of one run_delegation_step call.",
            "instruction": instruction,
        }
    bot = _get_bot_row(bot_id)
    if bot is None:
        return {"error": f"No bot with id {bot_id}.", "instruction": instruction}
    task_hash = task_context.current_task_hash.get()
    if task_hash is None:
        return {"error": "No active task context -- this should only be called mid-delegation.", "instruction": instruction}

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", bot["name"].lower())
    scratch_path = os.path.join(task_context.BOT_TASK_FOLDER_ROOT, task_hash, f"{safe_name}.md")
    before_size = os.path.getsize(scratch_path) if os.path.exists(scratch_path) else 0

    system_prompt = _build_bot_system_prompt(bot)
    user_content = (
        f"{instruction}\n\n"
        "This is an isolated delegation step -- when you have your findings, "
        "call append_scratch_note with them. Do not reply with plain text; "
        "use the tool, since that is the only way your findings reach Athena from here."
    )
    chat_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    # Same per-host lock plan_delegation's own concurrency setting relies
    # on (see _plan_delegation_tool) -- steps can run in parallel threads,
    # but two of them landing on the same physical inference host must
    # still never generate at the same time.
    host_lock = _get_host_lock(_endpoint_host(bot.get("endpoint_url")))
    with host_lock:
        result = _call_bot_endpoint(bot, chat_messages)
    # Accumulated across the initial call and (if it happens) the retry
    # below, then persisted as one entry so read_tool_call_history shows
    # everything this one step actually did, not just the final attempt.
    step_history = list(result.get("tool_call_history", []))
    if result.get("error"):
        _append_tool_call_history(bot["name"], instruction, step_history)
        return {"status": "error", "detail": result["error"], "bot": bot["name"], "instruction": instruction, "tool_calls_recorded": len(step_history)}

    after_size = os.path.getsize(scratch_path) if os.path.exists(scratch_path) else 0
    if after_size <= before_size:
        # Internal auto-retry: one nudge in the SAME isolated conversation,
        # continuing from the bot's own reply, before ever surfacing a
        # failure to Athena2. This mirrors the external retry Athena2 was
        # already doing herself (a whole extra run_delegation_step call) --
        # moving it in-process saves her a full round trip for the common
        # case where one nudge is all it takes.
        chat_messages.append({"role": "assistant", "content": result.get("content", "")})
        chat_messages.append({
            "role": "user",
            "content": (
                "You did not call append_scratch_note. That is the only way your findings "
                "reach Athena -- a plain text reply is discarded. Call append_scratch_note "
                "now with your findings from above. Do not reply with plain text again."
            ),
        })
        with host_lock:
            retry_result = _call_bot_endpoint(bot, chat_messages)
        step_history += retry_result.get("tool_call_history", [])
        if retry_result.get("error"):
            _append_tool_call_history(bot["name"], instruction, step_history)
            return {"status": "error", "detail": retry_result["error"], "bot": bot["name"], "instruction": instruction, "tool_calls_recorded": len(step_history)}
        after_retry_size = os.path.getsize(scratch_path) if os.path.exists(scratch_path) else 0
        _append_tool_call_history(bot["name"], instruction, step_history)
        if after_retry_size <= before_size:
            return {
                "status": "no_scratch_note",
                "detail": "The bot replied without calling append_scratch_note, even after one internal retry -- no findings were recorded for this step.",
                "bot": bot["name"],
                "instruction": instruction,
                "tool_calls_recorded": len(step_history),
            }
        fabrication_flag = _maybe_flag_fabrication(bot, instruction, scratch_path, before_size, step_history)
        if fabrication_flag is not None:
            return fabrication_flag
        return {"status": "done", "detail": "Step complete after one internal retry. Use read_scratch_file to see this bot's findings.", "bot": bot["name"], "instruction": instruction, "tool_calls_recorded": len(step_history)}

    _append_tool_call_history(bot["name"], instruction, step_history)
    fabrication_flag = _maybe_flag_fabrication(bot, instruction, scratch_path, before_size, step_history)
    if fabrication_flag is not None:
        return fabrication_flag
    return {"status": "done", "detail": "Step complete. Use read_scratch_file to see this bot's findings.", "bot": bot["name"], "instruction": instruction, "tool_calls_recorded": len(step_history)}


def _plan_delegation_tool(steps):
    if not steps or not isinstance(steps, list):
        return {"error": "steps must be a non-empty list of {bot_id, instruction} objects."}
    results = [None] * len(steps)
    runnable = []
    for i, step in enumerate(steps):
        bot_id = step.get("bot_id") if isinstance(step, dict) else None
        instruction = step.get("instruction", "") if isinstance(step, dict) else ""
        if not bot_id or not instruction:
            results[i] = {"step": i, "status": "error", "detail": "Missing bot_id or instruction."}
            continue
        runnable.append((i, bot_id, instruction))

    # Sequential (1) by default -- Athena targets hardware of all sizes,
    # including old/cheap machines that shouldn't pay for concurrency
    # they can't afford. A person running stronger hardware can raise
    # this in settings; the per-host lock in _run_delegation_step_tool
    # still serializes any two steps that land on the same physical
    # inference host regardless of what this is set to.
    concurrency = max(1, int(settings.load_settings().get("delegation_concurrency") or 1))

    if concurrency <= 1 or len(runnable) <= 1:
        for i, bot_id, instruction in runnable:
            r = _run_delegation_step_tool(bot_id, instruction)
            results[i] = {"step": i, "bot_id": bot_id, **r}
    else:
        # Each worker thread starts with its own empty context, so
        # task_context.current_task_hash.get() would come back None in there and
        # break scratch-file scoping -- read the value once up front and
        # set it explicitly in each worker instead. (A single shared
        # contextvars.Context can't be entered from more than one thread
        # at a time, so copy_context() + one shared ctx.run() isn't an
        # option here.)
        task_hash = task_context.current_task_hash.get()

        def _run(item):
            i, bot_id, instruction = item
            task_context.current_task_hash.set(task_hash)
            r = _run_delegation_step_tool(bot_id, instruction)
            return i, {"step": i, "bot_id": bot_id, **r}

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for i, r in executor.map(_run, runnable):
                results[i] = r

    return {"plan_results": results, "note": "Findings were not returned here -- use read_scratch_file per bot to review them."}


def _delegation_step_worker(job_id, task_hash, req_fields, bot_id, instruction):
    task_context.current_task_hash.set(task_hash)  # each thread starts with its own empty context -- see _plan_delegation_tool's own comment on this
    try:
        result = _run_delegation_step_tool(bot_id, instruction)
    except Exception as e:
        result = {"status": "error", "detail": f"Unhandled exception during background delegation: {e}", "instruction": instruction}
    with _delegation_jobs_lock:
        _delegation_jobs[job_id] = {"status": "error" if result.get("status") in ("error", "rejected") else "done", "kind": "step", "session_id": req_fields["session_id"], "result": result}
    bot_name = result.get("bot", "The bot")
    summary = (
        f"[Async delegation job {job_id} complete] {bot_name} on instruction {instruction!r} -- "
        f"status: {result.get('status')}. {result.get('detail', result.get('error', ''))} "
        f"Use read_scratch_file('{bot_name}') and read_tool_call_history('{bot_name}') for full findings."
    )
    _deliver_delegation_result(req_fields, summary)


def _run_delegation_step_async_tool(req, bot_id, instruction):
    is_compound, reason = _instruction_is_compound(instruction)
    if is_compound:
        return {
            "status": "rejected",
            "error": f"This instruction {reason} -- that is a compound, multi-topic request, not a single-topic step. "
                     "Break it into separate single-topic steps and use plan_delegation instead of one run_delegation_step call.",
            "instruction": instruction,
        }
    job_id = _next_delegation_job_id()
    task_hash = task_context.current_task_hash.get()
    req_fields = _capture_req_fields(req)
    with _delegation_jobs_lock:
        _delegation_jobs[job_id] = {"status": "running", "kind": "step", "session_id": req.session_id, "result": None}
    threading.Thread(target=_delegation_step_worker, args=(job_id, task_hash, req_fields, bot_id, instruction), daemon=True).start()
    return {
        "status": "pending",
        "handle": job_id,
        "detail": "Running in the background -- you'll get a new message in this conversation when it's done. "
                  "You can keep working on other things or end your turn now; you don't need to wait on this.",
    }


def _delegation_plan_worker(job_id, task_hash, req_fields, steps):
    task_context.current_task_hash.set(task_hash)
    try:
        plan_result = _plan_delegation_tool(steps)
    except Exception as e:
        plan_result = {"error": f"Unhandled exception during background plan delegation: {e}"}
    with _delegation_jobs_lock:
        _delegation_jobs[job_id] = {"status": "done", "kind": "plan", "session_id": req_fields["session_id"], "result": plan_result}
    step_results = plan_result.get("plan_results", [])
    lines = [f"[Async delegation plan {job_id} complete] {len(step_results)} step(s) finished:"]
    for r in step_results:
        bot_name = r.get("bot", f"bot_id {r.get('bot_id')}")
        lines.append(f"- step {r.get('step')} ({bot_name}): {r.get('status')} -- {r.get('detail', r.get('error', ''))}")
    lines.append("Use read_scratch_file per bot for full findings.")
    _deliver_delegation_result(req_fields, "\n".join(lines))


def _plan_delegation_async_tool(req, steps):
    if not steps or not isinstance(steps, list):
        return {"error": "steps must be a non-empty list of {bot_id, instruction} objects."}
    job_id = _next_delegation_job_id()
    task_hash = task_context.current_task_hash.get()
    req_fields = _capture_req_fields(req)
    with _delegation_jobs_lock:
        _delegation_jobs[job_id] = {"status": "running", "kind": "plan", "session_id": req.session_id, "result": None}
    threading.Thread(target=_delegation_plan_worker, args=(job_id, task_hash, req_fields, steps), daemon=True).start()
    return {
        "status": "pending",
        "handle": job_id,
        "detail": f"Running {len(steps)} step(s) in the background -- you'll get ONE new message in this conversation "
                  "summarizing every step once the whole plan finishes. You can keep working on other things or end "
                  "your turn now; you don't need to wait on this.",
    }


def _message_bot_tool(bot_id, content):
    conn = _bots_conn()
    try:
        room_row = conn.execute(
            "SELECT " + _ROOM_COLUMNS + " FROM rooms WHERE kind = 'dm' AND human_party = 'athena' AND member_bot_ids_json = ?",
            (json.dumps([bot_id]),)
        ).fetchone()
        if room_row:
            room = _row_to_room(room_row)
        else:
            now = time.time()
            cur = conn.execute(
                "INSERT INTO rooms (kind, human_party, label, member_bot_ids_json, created_at, last_active) VALUES ('dm', 'athena', ?, ?, ?, ?)",
                (f"Athena & bot #{bot_id}", json.dumps([bot_id]), now, now)
            )
            conn.commit()
            room = _row_to_room(conn.execute(f"SELECT {_ROOM_COLUMNS} FROM rooms WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()
    result = send_room_message(room["id"], RoomSendIn(sender_type="athena", sender_bot_id=None, content=content))
    return result


def _message_room_tool(room_id, content):
    return send_room_message(room_id, RoomSendIn(sender_type="athena", sender_bot_id=None, content=content))


def _list_rooms_tool():
    conn = _bots_conn()
    try:
        rows = conn.execute(f"SELECT {_ROOM_COLUMNS} FROM rooms ORDER BY last_active DESC").fetchall()
        return [_row_to_room(r) for r in rows]
    finally:
        conn.close()


def _read_room_messages_tool(room_id):
    return get_room_messages(room_id)


def _create_bot_tool(name, description, allowed_tools):
    """Deliberately takes no model/endpoint from the caller -- Athena
    could easily hallucinate a model name that isn't actually
    installed on any registered endpoint, since she has no way to see
    what's real. Every bot she creates uses the person's own
    pre-configured default (set via Athena2's Agent Defaults menu)
    instead, removing the guess entirely rather than just warning
    about it."""
    settings = settings.load_settings()
    defaults = settings.get("bot_creation_defaults") or {}
    model = defaults.get("model")
    endpoint_url = defaults.get("endpoint_url")
    provider = defaults.get("provider", "")
    if not model:
        return {"error": "No default model is configured for bot creation yet. Ask the person to set one in Athena's Agent Defaults menu before creating a bot."}
    if not allowed_tools:
        allowed_tools = _bot_prompt_tool_cache.get(name.strip().lower(), [])
    allowed_tools = [t for t in allowed_tools if t in BOT_ALLOWED_TOOL_NAMES]
    if not allowed_tools:
        return {"error": "No valid read-only tools were provided for this bot. Call draft_bot_prompt first with a real tools list (or pass allowed_tools directly) -- a bot can't be created with no tools."}
    conn = _bots_conn()
    try:
        existing_names = [r[0] for r in conn.execute("SELECT name FROM bots").fetchall()]
        if name.strip().lower() in (n.lower() for n in existing_names):
            return {"error": f"A bot named '{name}' already exists. Existing bots: {', '.join(existing_names)}. Choose a genuinely different name, not a numbered variant."}
        cur = conn.execute("""
            INSERT INTO bots (name, endpoint_url, provider, api_key, model, description, allowed_tools_json, unload_strategy, created_at)
            VALUES (?, ?, ?, '', ?, ?, ?, 'ollama_keep_alive', ?)
        """, (name, endpoint_url, provider, model, description, json.dumps(allowed_tools), time.time()))
        conn.commit()
        return _row_to_bot(conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def _update_bot_tool(bot_id, description=None, allowed_tools=None):
    """Deliberately narrow -- Athena can refine a bot's own instructions
    and tool access as it learns what works, but identity fields
    (name, model, endpoint) are left to the person managing the
    roster via the UI, not something Athena rewrites on its own."""
    conn = _bots_conn()
    try:
        existing = conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots WHERE id = ?", (bot_id,)).fetchone()
        if not existing:
            return {"error": f"No bot with id {bot_id}."}
        current = _row_to_bot(existing)
        new_description = description if description is not None else current["description"]
        new_tools = allowed_tools if allowed_tools is not None else current["allowed_tools"]
        if allowed_tools is not None:
            new_tools = [t for t in new_tools if t in BOT_ALLOWED_TOOL_NAMES]
        conn.execute("UPDATE bots SET description=?, allowed_tools_json=? WHERE id=?",
                     (new_description, json.dumps(new_tools), bot_id))
        conn.commit()
        return _row_to_bot(conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots WHERE id = ?", (bot_id,)).fetchone())
    finally:
        conn.close()


BOT_DELEGATION_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "run_delegation_step",
        "description": "Give one bot one single-topic instruction as a fresh, isolated call -- no DM/room history involved, no memory of prior steps. The bot investigates internally and appends its findings to its own scratch file via append_scratch_note; this call returns only a status, never the findings themselves. Use read_scratch_file afterward to see what it found.",
        "parameters": {"type": "object", "properties": {
            "bot_id": {"type": "integer", "description": "The bot to run this step with."},
            "instruction": {"type": "string", "description": "A single, self-contained topic or question -- not a multi-part task."},
        }, "required": ["bot_id", "instruction"]},
    }},
    {"type": "function", "function": {
        "name": "read_scratch_file",
        "description": "Read the current task's scratch file for one bot -- its accumulated findings from any run_delegation_step calls so far this task. Scoped automatically to the task you're currently delegating within.",
        "parameters": {"type": "object", "properties": {
            "bot_name": {"type": "string", "description": "The bot's name, exactly as it appears in list_bots."},
        }, "required": ["bot_name"]},
    }},
    {"type": "function", "function": {
        "name": "read_tool_call_history",
        "description": "See exactly which tools a bot actually called (and roughly what each one returned) across every run_delegation_step it's done in the current task, grouped by instruction. Findings live in the scratch file, not here -- use this when you need to check HOW a bot reached what it wrote, not what it wrote. The clearest use: a scratch note looks fabricated or ungrounded (invented detail, claims that don't match anything real) -- check here for whether the tool calls that would back it up actually happened.",
        "parameters": {"type": "object", "properties": {
            "bot_name": {"type": "string", "description": "The bot's name, exactly as it appears in list_bots."},
        }, "required": ["bot_name"]},
    }},
    {"type": "function", "function": {
        "name": "plan_delegation",
        "description": "Give a whole roadmap of single-topic delegation steps at once, instead of calling run_delegation_step yourself one at a time. Each step runs in the same strict isolation as run_delegation_step (fresh context, no history) -- but you supply the full list up front rather than remembering to call each one separately. Returns only per-step status; use read_scratch_file per bot afterward to see what each one found.",
        "parameters": {"type": "object", "properties": {
            "steps": {
                "type": "array",
                "description": "One entry per single-topic step. Do not bundle multiple questions into one instruction -- split them into separate steps instead.",
                "items": {
                    "type": "object",
                    "properties": {
                        "bot_id": {"type": "integer"},
                        "instruction": {"type": "string", "description": "One single, self-contained topic or question."},
                    },
                    "required": ["bot_id", "instruction"],
                },
            },
        }, "required": ["steps"]},
    }},
    {"type": "function", "function": {
        "name": "create_bot",
        "description": "Create a new bot in the roster, using the person's pre-configured default model and endpoint automatically -- you never choose a model yourself. description must be a SHORT job-scope sentence (what this bot's job is), never a full rendered system prompt -- the real system prompt is always assembled server-side from this sentence plus the bot's final allowed_tools. Use draft_bot_prompt first to see what the assembled prompt will look like and to help you write a good job-scope sentence, but never paste its full output here.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "description": {"type": "string", "description": "A short sentence describing this bot's job scope -- e.g. 'Investigates codebase structure and reports findings.' NOT a full system prompt; the real prompt is built from this plus allowed_tools automatically."},
            "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "Read-only tool names this bot can use, e.g. ['bash', 'search_codebase']."},
        }, "required": ["name", "description", "allowed_tools"]},
    }},
    {"type": "function", "function": {
        "name": "update_bot",
        "description": "Update an existing bot's description (system prompt) and/or allowed tools -- refine it as you learn what works. Does not change its name, model, or endpoint.",
        "parameters": {"type": "object", "properties": {
            "bot_id": {"type": "integer"},
            "description": {"type": "string"},
            "allowed_tools": {"type": "array", "items": {"type": "string"}},
        }, "required": ["bot_id"]},
    }},

    {"type": "function", "function": {
        "name": "list_bots",
        "description": "See every bot in the roster, with their id, name, model, and description. Use this before delegating to know who's available.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "draft_bot_prompt",
        "description": "Assemble a well-structured system prompt for a new (or existing) bot from a name, job scope, and its available tools. Use this whenever creating a bot's instructions rather than writing them freehand. A bot's real tool access is always determined separately by its allowed_tools list, enforced independently of anything in this prompt -- additional_constraints must never claim, imply, or grant write/execute/file-modification capability, since a bot can never actually have it regardless of what its prompt says.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "The bot's name."},
            "job_scope": {"type": "string", "description": "What this bot's job is -- the specific thing it should investigate or accomplish."},
            "tools": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "when_to_use": {"type": "string"}}}, "description": "The tools this bot has, each with a short note on when to use it."},
            "additional_constraints": {"type": "string", "description": "Optional: anything else this bot specifically should or shouldn't do -- investigate-only scope, never write/execute permissions, which this bot can never actually have."},
        }, "required": ["name", "job_scope", "tools"]},
    }},
    {"type": "function", "function": {
        "name": "message_bot",
        "description": "Send a message to a specific bot (your own DM with it, separate from the user's personal conversations with that same bot) and get its reply. Creates the DM automatically on first use.",
        "parameters": {"type": "object", "properties": {
            "bot_id": {"type": "integer", "description": "The bot's id, from list_bots."},
            "content": {"type": "string", "description": "What to say to the bot."},
        }, "required": ["bot_id", "content"]},
    }},
    {"type": "function", "function": {
        "name": "list_rooms",
        "description": "See every existing conversation room (DMs and group rooms), including ones you're not directly part of -- nothing is hidden from you.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "read_room_messages",
        "description": "Read the full message history of any room by id, including a bot-to-bot DM you're not a member of.",
        "parameters": {"type": "object", "properties": {"room_id": {"type": "integer"}}, "required": ["room_id"]},
    }},
    {"type": "function", "function": {
        "name": "message_room",
        "description": "Post a message to a group room. Only bots explicitly @mentioned in the message will respond.",
        "parameters": {"type": "object", "properties": {
            "room_id": {"type": "integer"},
            "content": {"type": "string", "description": "Include @BotName to have that specific bot respond."},
        }, "required": ["room_id", "content"]},
    }},
]


_ASYNC_RUN_DELEGATION_STEP_DESCRIPTION = (
    "Give one bot one single-topic instruction. Runs in the background and returns immediately with "
    "{\"status\": \"pending\", \"handle\": <job id>} -- it does NOT wait for the bot to finish. When the bot "
    "is done, its result arrives as a new message in this same conversation; you don't need to poll or wait "
    "for it, and you're free to keep working (including delegating another step) in the meantime. Use "
    "read_scratch_file/read_tool_call_history once that message tells you the job is complete."
)
_ASYNC_PLAN_DELEGATION_DESCRIPTION = (
    "Give a whole roadmap of single-topic delegation steps at once. Runs in the background and returns "
    "immediately with {\"status\": \"pending\", \"handle\": <job id>} for the whole plan -- it does NOT wait "
    "for the steps to finish. Once every step in the plan is done, ONE combined message summarizing all of "
    "them arrives in this same conversation; you don't need to poll or wait for it."
)


def _bot_delegation_tool_schemas_for_mode(async_enabled):
    """BOT_DELEGATION_TOOL_SCHEMAS describes run_delegation_step/plan_delegation
    as blocking calls, which is accurate everywhere except Athena2's own
    /api/chat loop when delegation_async_enabled is on (see
    _run_delegation_step_async_tool/_plan_delegation_async_tool). The Athena
    room loop and a bot's own tool pool never go through that async dispatch
    path, so they must always keep the synchronous description regardless of
    the setting -- only _get_mode_tools' athena_delegation branch calls this
    with async_enabled=True."""
    if not async_enabled:
        return BOT_DELEGATION_TOOL_SCHEMAS
    schemas = []
    for s in BOT_DELEGATION_TOOL_SCHEMAS:
        fn = s.get("function", {})
        if fn.get("name") == "run_delegation_step":
            schemas.append({"type": "function", "function": {**fn, "description": _ASYNC_RUN_DELEGATION_STEP_DESCRIPTION}})
        elif fn.get("name") == "plan_delegation":
            schemas.append({"type": "function", "function": {**fn, "description": _ASYNC_PLAN_DELEGATION_DESCRIPTION}})
        else:
            schemas.append(s)
    return schemas


# Hard server-side lock on what a bot may actually execute, independent
# of anything stored in allowed_tools_json -- a bot's design principle
# is strictly read-only/investigate-only (no bash_exec, no write_file/
# edit_file, no backup_file/restore_file), so this set is the real
# enforcement point, not the prompt text or the DB column.
BOT_ALLOWED_TOOL_NAMES = {
    "bash", "list_files", "read_file", "search_codebase",
    "find_definition", "find_references", "type_info",
    "web_search", "web_fetch",
    "list_bots", "list_rooms", "message_bot", "read_room_messages", "draft_bot_prompt",
    "lcm_recall_search", "lcm_recall_expand", "lcm_recall_range",
}

_BOT_CTX_SIZE = 32768
_BOT_MAX_ROUNDS = 200  # absolute outer safety net; per-tool-type limits below are what actually catches an exploration binge in practice

# Grouped, independently-budgeted counters -- the flat MAX_ROUNDS ceiling
# let a bot burn dozens of bash/list_files calls without ever reaching
# append_scratch_note, since nothing distinguished "still investigating"
# from "stuck re-exploring." Each group gets its own warn/hard-stop
# count instead, same structural pattern as Athena2's own exploration
# gate (_get_mode_tools) -- append_scratch_note itself is never in a
# group, so it's always reachable regardless of what else got throttled.
_BOT_TOOL_TYPE_GROUPS = {
    "explore": {"bash", "list_files", "read_file", "search_codebase", "find_definition", "find_references", "type_info"},
    "web": {"web_search", "web_fetch"},
    "social": {"list_bots", "list_rooms", "message_bot", "read_room_messages", "draft_bot_prompt"},
    "recall": {"lcm_recall_search", "lcm_recall_expand", "lcm_recall_range"},
}
_BOT_TOOL_TYPE_WARN_AFTER = 5
_BOT_TOOL_TYPE_HARD_STOP_AFTER = 10


def _bot_tool_type_group(tool_name):
    for group, names in _BOT_TOOL_TYPE_GROUPS.items():
        if tool_name in names:
            return group
    return None

# name.lower() -> list of tool names, populated by _draft_bot_prompt so
# create_bot can recover the real list if the model forgets to repeat
# allowed_tools on the follow-up create_bot call.
_bot_prompt_tool_cache = {}


def _tool_call_fingerprint(tc):
    """Shared with generate()'s own tool-call dedup -- identical hash
    of tool name + arguments, so a bot calling the same tool with the
    same arguments twice in a row gets caught exactly like Athena's
    own turns do, not by a separately-drifting copy of this logic."""
    fn = tc.get("function", {})
    args = fn.get("arguments", {})
    try:
        args_str = json.dumps(args, sort_keys=True)
    except TypeError:
        args_str = str(args)
    raw = fn.get("name", "") + "|" + args_str
    return hashlib.md5(raw.encode()).hexdigest()


def _detect_text_loop(text):
    """Shared with generate()'s own text-loop detector -- same layered
    tail-repeat check (40 chars/3 repeats, 18 chars/5 repeats), so a
    bot narrating a fake tool call as repeated text gets caught the
    same way a stuck main-chat generation does."""
    def _tail_repeats(window, min_repeats):
        if len(text) < window * min_repeats:
            return False
        tail = text[-window:]
        if len(tail.strip()) < window * 0.5:
            return False
        return text.count(tail) >= min_repeats
    return _tail_repeats(40, 3) or _tail_repeats(18, 5)


# --- Programmatic Tool Calling (PTC) for bots -----------------------------
# Settings-gated, off by default: lets a bot write one Python script that
# calls several tools in sequence instead of one round-trip per call. This
# is a reliability tradeoff, not a hardware one -- a small/weaker local
# model is more likely to write buggy orchestration code than to make
# clean individual tool calls, so it stays opt-in for whoever is running a
# model capable enough to benefit from it.
#
# The script runs in a fresh subprocess (plain `python3 -u -c <stub>`, not
# a re-import of this app), talking back to this process over a line-
# delimited JSON pipe on stdin/stdout: it sends {"type":"call",...} for
# each tool call, blocks for a {"type":"result"|"error",...} reply, and
# finally sends {"type":"done"} or {"type":"script_error",...}. The real
# tool implementations, DB connections, etc. never leave this process --
# the child only ever gets thin proxy functions.
#
# This is a reliability boundary, not an adversarial security one: giving
# the sandboxed exec() a restricted __builtins__ dict blocks accidental
# `import`/`open`/`eval` use, but pure-Python builtin restriction is well
# known to be escapable by a sufficiently deliberate script (e.g. via
# __class__.__bases__ introspection reaching already-loaded classes). The
# real backstops are process isolation (a runaway or crashed script can't
# touch this process) and a hard wall-clock timeout enforced by killing
# the OS process outright, which a thread-based timeout can't guarantee.
# Bots have no write/exec tool access to begin with (BOT_ALLOWED_TOOL_NAMES),
# so the tool surface reachable from inside a script is exactly what a bot
# could already reach one call at a time.
_PTC_TIMEOUT_SECONDS = 90
_PTC_MAX_SUBCALLS = 30

_PTC_CHILD_STUB = r"""
import sys, json

def main():
    init = json.loads(sys.stdin.readline())
    code = init["code"]
    tool_names = init["tools"]

    def _call_tool(name, kwargs):
        sys.stdout.write(json.dumps({"type": "call", "tool": name, "args": kwargs}) + "\n")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("tool channel closed")
        resp = json.loads(line)
        if resp.get("type") == "error":
            raise RuntimeError(resp.get("message", "tool call failed"))
        return resp.get("value")

    def _make_proxy(name):
        def proxy(**kwargs):
            return _call_tool(name, kwargs)
        proxy.__name__ = name
        return proxy

    # stdout is the JSON protocol channel back to the parent -- a script's
    # own print() must never write there, or its output gets parsed as a
    # protocol message and crashes the whole exchange. Redirect to stderr
    # instead of dropping it, since the parent surfaces stderr on failure.
    def _sandboxed_print(*args, **kwargs):
        kwargs["file"] = sys.stderr
        print(*args, **kwargs)

    safe_builtins = {
        "len": len, "range": range, "enumerate": enumerate, "min": min, "max": max,
        "sum": sum, "sorted": sorted, "reversed": reversed, "list": list, "dict": dict,
        "set": set, "tuple": tuple, "str": str, "int": int, "float": float, "bool": bool,
        "print": _sandboxed_print, "abs": abs, "round": round, "zip": zip, "map": map, "filter": filter,
        "any": any, "all": all, "isinstance": isinstance, "True": True, "False": False, "None": None,
        "Exception": Exception, "ValueError": ValueError, "KeyError": KeyError, "TypeError": TypeError,
        "RuntimeError": RuntimeError, "StopIteration": StopIteration, "IndexError": IndexError,
    }
    g = {"__builtins__": safe_builtins}
    for name in tool_names:
        g[name] = _make_proxy(name)

    try:
        exec(compile(code, "<bot_program>", "exec"), g)
        sys.stdout.write(json.dumps({"type": "done"}) + "\n")
        sys.stdout.flush()
    except BaseException as e:
        import traceback
        sys.stdout.write(json.dumps({
            "type": "script_error",
            "message": str(e),
            "traceback": traceback.format_exc()[-2000:],
        }) + "\n")
        sys.stdout.flush()

main()
"""


def _ptc_signature_from_schema(schema):
    fn = schema.get("function", {})
    name = fn.get("name", "")
    params = fn.get("parameters", {}) or {}
    props = params.get("properties", {}) or {}
    required = set(params.get("required", []) or [])
    parts = [f"{pname}: {pschema.get('type', 'any')}" + ("" if pname in required else " (optional)")
             for pname, pschema in props.items()]
    return f"{name}({', '.join(parts)})"


def _build_run_tool_program_schema(tool_schemas):
    lines = [_ptc_signature_from_schema(s) for s in tool_schemas] + ["append_scratch_note(content: str)"]
    catalogue = "\n".join(f"  {l}" for l in lines)
    return {
        "type": "function",
        "function": {
            "name": "run_tool_program",
            "description": (
                "Write a short Python script that calls your tools directly as plain functions, all in one "
                "shot, instead of one tool call per round-trip -- use this when you already know the sequence "
                "of calls you need (e.g. read three specific files, or search then fetch several results), "
                "not when you're still deciding one call at a time. Each call is a real tool call, executed "
                "exactly like calling it individually, and returns the same result shape (inspect it with "
                "normal Python -- e.g. result.get(\"error\")). Only plain Python is available -- no imports, "
                "no file or network access beyond the functions below. Available functions (keyword arguments "
                "only):\n" + catalogue + "\n\nCall append_scratch_note(content=...) inside the script (or as a "
                "separate tool call afterward) to record your findings -- printed output and return values are "
                "not seen by Athena. The script has a limited number of tool calls and a time limit; going over "
                "either ends it early with an error explaining what happened."
            ),
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "The Python script to run."}},
                "required": ["code"],
            },
        },
    }


def _execute_bot_tool_program(code, workspace, bot, session_key, allowed_tools, tool_type_counts, tool_type_hard_stopped, seen_fingerprints):
    """Run one bot-authored script in an isolated subprocess (see the PTC
    block comment above), dispatching each tool call it makes through the
    exact same _execute_bot_tool_call used for individual tool calls, and
    feeding the same per-turn dedup/tool-type-budget state so a script
    can't be used to quietly exceed either."""
    effective = sorted(set(allowed_tools or []) & BOT_ALLOWED_TOOL_NAMES) + [BOT_SCRATCH_TOOL_NAME]
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", _PTC_CHILD_STUB],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except Exception as e:
        return {"error": f"Could not start the program sandbox: {e}"}

    tool_call_history = []
    sub_call_count = 0
    deadline = time.time() + _PTC_TIMEOUT_SECONDS

    def _cleanup():
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    try:
        proc.stdin.write(json.dumps({"code": code, "tools": effective}) + "\n")
        proc.stdin.flush()
    except Exception as e:
        _cleanup()
        return {"error": f"Could not send the program to the sandbox: {e}"}

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            _cleanup()
            return {
                "error": f"Program timed out after {_PTC_TIMEOUT_SECONDS}s -- write shorter scripts, or fewer tool calls per script.",
                "tool_call_history": tool_call_history,
            }
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            stderr_tail = ""
            try:
                stderr_tail = (proc.stderr.read() or "")[-1000:]
            except Exception:
                pass
            _cleanup()
            return {
                "error": "The program sandbox exited unexpectedly." + (f" stderr: {stderr_tail}" if stderr_tail else ""),
                "tool_call_history": tool_call_history,
            }
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_type = msg.get("type")

        if msg_type == "call":
            name = msg.get("tool", "")
            args = msg.get("args") or {}
            sub_call_count += 1
            group = _bot_tool_type_group(name)
            if sub_call_count > _PTC_MAX_SUBCALLS:
                blocked_msg = f"BLOCKED: this program has made more than {_PTC_MAX_SUBCALLS} tool calls -- stop and call append_scratch_note with what you have."
            elif group and group in tool_type_hard_stopped:
                blocked_msg = f"BLOCKED: '{group}' tools are no longer available this turn -- call append_scratch_note with what you have."
            else:
                blocked_msg = None
            if blocked_msg:
                tool_call_history.append(_tool_call_history_entry(name, args, {"error": blocked_msg}))
                try:
                    proc.stdin.write(json.dumps({"type": "error", "message": blocked_msg}) + "\n")
                    proc.stdin.flush()
                except Exception:
                    _cleanup()
                    return {"error": "Lost the sandbox's input pipe mid-program.", "tool_call_history": tool_call_history}
                continue
            if group:
                tool_type_counts[group] = tool_type_counts.get(group, 0) + 1
            fake_tc = {"function": {"name": name, "arguments": args}}
            fingerprint = _tool_call_fingerprint(fake_tc)
            if seen_fingerprints.count(fingerprint) >= 2:
                result = {"error": "BLOCKED: this exact tool call (same tool, same arguments) has already been made twice this turn. Use what you already have, or make a genuinely different call."}
            else:
                result = _execute_bot_tool_call(fake_tc, workspace, bot, session_key)
                seen_fingerprints.append(fingerprint)
                if len(seen_fingerprints) > 20:
                    seen_fingerprints.pop(0)
            tool_call_history.append(_tool_call_history_entry(name, args, result))
            try:
                proc.stdin.write(json.dumps({"type": "result", "value": result}) + "\n")
                proc.stdin.flush()
            except Exception:
                _cleanup()
                return {"error": "Lost the sandbox's input pipe mid-program.", "tool_call_history": tool_call_history}

        elif msg_type == "done":
            _cleanup()
            return {
                "status": "done",
                "detail": f"Program finished after {sub_call_count} tool call(s). Call append_scratch_note if you haven't already.",
                "tool_call_history": tool_call_history,
            }
        elif msg_type == "script_error":
            _cleanup()
            return {"error": f"The program raised an error: {msg.get('message', 'unknown error')}", "tool_call_history": tool_call_history}
        else:
            continue


def _bot_tool_schemas(allowed_tools, hard_stopped_groups=None, ptc_enabled=False):
    """Build real tool schemas for a bot's own Ollama request, filtered
    through BOT_ALLOWED_TOOL_NAMES regardless of what's stored -- a
    write tool name surviving in allowed_tools_json can never actually
    produce a usable schema.

    hard_stopped_groups drops every tool belonging to a tool-type group
    that's hit its per-turn hard-stop count (see _call_bot_endpoint) --
    append_scratch_note is never in a group, so it survives any of
    these cuts and stays the one way out. ptc_enabled adds run_tool_program
    (see the PTC block above) alongside the individual tools, never instead
    of them -- a bot can always fall back to calling tools one at a time."""
    pool = (
        BASH_TOOL_SCHEMAS + FILE_TOOL_SCHEMAS + RAG_TOOL_SCHEMAS + LSP_TOOL_SCHEMAS
        + WEB_TOOL_SCHEMAS + lcm_client.get_lcm_tools()
        + [s for s in BOT_DELEGATION_TOOL_SCHEMAS
           if s.get("function", {}).get("name") in ("list_bots", "list_rooms", "message_bot", "read_room_messages", "draft_bot_prompt")]
    )
    effective = set(allowed_tools or []) & BOT_ALLOWED_TOOL_NAMES
    tools = [s for s in pool if s.get("function", {}).get("name") in effective]
    if hard_stopped_groups:
        tools = [s for s in tools if _bot_tool_type_group(s.get("function", {}).get("name")) not in hard_stopped_groups]
    schemas = tools + [APPEND_SCRATCH_NOTE_SCHEMA]
    if ptc_enabled and tools:
        schemas = schemas + [_build_run_tool_program_schema(tools)]
    return schemas


def _execute_bot_tool_call(tool_call, workspace, bot, session_key):
    """Dispatch one tool call a bot actually made. Gated against
    BOT_ALLOWED_TOOL_NAMES a second time here, independent of what
    schemas it was even offered -- belt and suspenders."""
    fn = tool_call.get("function", {})
    name = fn.get("name", "")
    print(f"[BOT TOOL CALL] {bot.get('name', '?')} -> {name}", flush=True)
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if name == BOT_SCRATCH_TOOL_NAME:
        return _append_scratch_note(bot["name"], args.get("content", ""))
    if name not in BOT_ALLOWED_TOOL_NAMES:
        return {"error": f"'{name}' is not a tool this bot is permitted to use."}
    if name == "bash":
        return _execute_readonly_bash(args.get("command", ""), args.get("args", []), workspace)
    if name == "list_files":
        return _list_files(workspace, args.get("path", "."))
    if name == "read_file":
        return _read_file(workspace, args.get("path", ""), args.get("offset", 0))
    if name == "search_codebase":
        return _search_codebase(args.get("query", ""), args.get("limit", 3))
    if name == "find_definition":
        return _get_lsp_client(workspace).find_definition(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
    if name == "find_references":
        return _get_lsp_client(workspace).find_references(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
    if name == "type_info":
        return _get_lsp_client(workspace).type_info(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
    if name == "web_search":
        return _web_search(None, args.get("query", ""))
    if name == "web_fetch":
        return _web_fetch(args.get("url", ""))
    if name == "list_bots":
        return _list_bots_tool()
    if name == "list_rooms":
        return _list_rooms_tool()
    if name == "message_bot":
        return _message_bot_tool(args.get("bot_id"), args.get("content", ""))
    if name == "read_room_messages":
        return _read_room_messages_tool(args.get("room_id"))
    if name == "draft_bot_prompt":
        return _draft_bot_prompt(args.get("name", ""), args.get("job_scope", ""), args.get("tools", []), args.get("additional_constraints"))
    args["session_id"] = session_key
    try:
        resp = httpx.post(f"{lcm_client.LCM_URL}/tools/call", json={"name": name, "arguments": args}, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("result")
        return {"error": f"Tool call failed: HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"Tool call failed: {e}"}


TASKS_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks.db")

def _tasks_conn():
    conn = sqlite3.connect(TASKS_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt TEXT NOT NULL,
            session_id TEXT,
            session_label TEXT NOT NULL,
            model TEXT NOT NULL,
            endpoint_url TEXT,
            workspace TEXT,
            schedule_type TEXT NOT NULL,
            run_at REAL,
            recurrence_json TEXT,
            enabled_tools_json TEXT NOT NULL,
            enabled_skills_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT \'active\',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_run_at REAL,
            last_run_status TEXT,
            next_run_at REAL,
            created_at REAL NOT NULL
        )
    """)
    return conn

def _available_task_tools():
    names = []
    seen = set()
    for schema in BASH_TOOL_SCHEMAS + WEB_TOOL_SCHEMAS + FILE_TOOL_SCHEMAS + lcm_client.get_lcm_tools():
        fn = schema.get("function", {})
        name = fn.get("name")
        if name and name not in seen:
            seen.add(name)
            names.append({"name": name, "description": fn.get("description", "")})
    return names

# Read-only and low-risk, useful for the vast majority of "check
# something and tell me" tasks -- everything else (file writes, bash,
# LCM memory tools) starts off so a new task can never do more than
# intended just because a checkbox list was skipped over.
_DEFAULT_ENABLED_TASK_TOOLS = {"web_search", "web_fetch", "read_file"}

class TaskIn(BaseModel):
    prompt: str
    session_id: Optional[str] = None
    session_label: str
    model: str
    endpoint_url: Optional[str] = None
    workspace: Optional[str] = None
    schedule_type: str  # \'once\' | \'recurring\'
    run_at: Optional[float] = None
    recurrence: Optional[dict] = None
    enabled_tools: List[str] = []
    enabled_skills: List[str] = []

_TASK_COLUMNS = "id, prompt, session_id, session_label, model, endpoint_url, workspace, schedule_type, run_at, recurrence_json, enabled_tools_json, enabled_skills_json, status, consecutive_failures, last_run_at, last_run_status, next_run_at, created_at"

def _row_to_task(row):
    (id_, prompt, session_id, session_label, model, endpoint_url, workspace,
     schedule_type, run_at, recurrence_json, enabled_tools_json, enabled_skills_json,
     status, consecutive_failures, last_run_at, last_run_status, next_run_at, created_at) = row
    return {
        "id": id_, "prompt": prompt, "session_id": session_id, "session_label": session_label,
        "model": model, "endpoint_url": endpoint_url, "workspace": workspace,
        "schedule_type": schedule_type, "run_at": run_at,
        "recurrence": json.loads(recurrence_json) if recurrence_json else None,
        "enabled_tools": json.loads(enabled_tools_json), "enabled_skills": json.loads(enabled_skills_json),
        "status": status, "consecutive_failures": consecutive_failures,
        "last_run_at": last_run_at, "last_run_status": last_run_status,
        "next_run_at": next_run_at, "created_at": created_at,
    }

def _next_occurrence(recurrence, after_ts):
    """Next occurrence strictly after after_ts (the last run\'s
    timestamp, or now for a brand-new task) for {frequency, interval,
    days_of_week, day_of_month, time_of_day, end}. Covers daily/
    weekly/monthly with an interval and, for weekly, specific days of
    the week -- structured recurrence for realistic personal-
    automation schedules, not a full RFC 5545 RRULE implementation."""
    freq = recurrence.get("frequency", "daily")
    interval = max(1, int(recurrence.get("interval", 1)))
    time_of_day = recurrence.get("time_of_day") or "09:00"
    hh, mm = [int(x) for x in time_of_day.split(":")]
    days_of_week = recurrence.get("days_of_week") or []
    end = recurrence.get("end") or {"type": "never"}
    base = datetime.fromtimestamp(after_ts)

    if freq == "weekly" and days_of_week:
        dow_map = {"SU": 6, "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5}
        wanted = sorted(dow_map[d] for d in days_of_week if d in dow_map) or [base.weekday()]
        base_week_start = (base - timedelta(days=base.weekday())).date()
        result = None
        for offset in range(0, 7 * (interval + 2)):
            probe = (base + timedelta(days=offset)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if probe <= base:
                continue
            probe_week_start = (probe - timedelta(days=probe.weekday())).date()
            weeks_elapsed = (probe_week_start - base_week_start).days // 7
            if probe.weekday() in wanted and weeks_elapsed % interval == 0:
                result = probe
                break
        if result is None:
            result = base + timedelta(weeks=interval)
    elif freq == "monthly":
        day_of_month = recurrence.get("day_of_month") or base.day
        year, month = base.year, base.month
        result = None
        for _ in range(0, 61):
            month += interval
            year += (month - 1) // 12
            month = ((month - 1) % 12) + 1
            last_day = calendar.monthrange(year, month)[1]
            candidate = datetime(year, month, min(day_of_month, last_day), hh, mm)
            if candidate > base:
                result = candidate
                break
        if result is None:
            result = base + timedelta(days=30)
    else:  # daily, or weekly with no specific days picked (treated as "same weekday, every interval weeks")
        step = timedelta(days=interval) if freq == "daily" else timedelta(weeks=interval)
        candidate = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= base:
            candidate += step
        result = candidate

    if end.get("type") == "until" and end.get("until"):
        if result.timestamp() > float(end["until"]):
            return None
    return result.timestamp()

def _compute_initial_next_run(schedule_type, run_at, recurrence):
    if schedule_type == "once":
        return run_at
    if not recurrence:
        return None
    return _next_occurrence(recurrence, after_ts=time.time())

@app.get("/api/tasks/capabilities")
def task_capabilities():
    return {
        "tools": _available_task_tools(),
        "default_enabled_tools": sorted(_DEFAULT_ENABLED_TASK_TOOLS),
        "skills": [{"name": s.get("name"), "description": s.get("description", "")} for s in _scan_skills()],
    }

@app.get("/api/tasks")
def list_tasks():
    conn = _tasks_conn()
    try:
        rows = conn.execute(f"SELECT {_TASK_COLUMNS} FROM tasks ORDER BY created_at DESC").fetchall()
        return [_row_to_task(r) for r in rows]
    finally:
        conn.close()

@app.post("/api/tasks")
def create_task(req: TaskIn):
    next_run = _compute_initial_next_run(req.schedule_type, req.run_at, req.recurrence)
    conn = _tasks_conn()
    try:
        cur = conn.execute(f"""
            INSERT INTO tasks (prompt, session_id, session_label, model, endpoint_url, workspace,
                schedule_type, run_at, recurrence_json, enabled_tools_json, enabled_skills_json,
                status, consecutive_failures, last_run_at, last_run_status, next_run_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, \'active\', 0, NULL, NULL, ?, ?)
        """, (
            req.prompt, req.session_id, req.session_label, req.model, req.endpoint_url, req.workspace,
            req.schedule_type, req.run_at,
            json.dumps(req.recurrence) if req.recurrence else None,
            json.dumps(req.enabled_tools), json.dumps(req.enabled_skills),
            next_run, time.time(),
        ))
        conn.commit()
        row = conn.execute(f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_task(row)
    finally:
        conn.close()

@app.put("/api/tasks/{task_id}")
def update_task(task_id: int, req: TaskIn):
    next_run = _compute_initial_next_run(req.schedule_type, req.run_at, req.recurrence)
    conn = _tasks_conn()
    try:
        conn.execute("""
            UPDATE tasks SET prompt=?, session_id=?, session_label=?, model=?, endpoint_url=?, workspace=?,
                schedule_type=?, run_at=?, recurrence_json=?, enabled_tools_json=?, enabled_skills_json=?,
                next_run_at=?, consecutive_failures=0, status=\'active\'
            WHERE id=?
        """, (
            req.prompt, req.session_id, req.session_label, req.model, req.endpoint_url, req.workspace,
            req.schedule_type, req.run_at,
            json.dumps(req.recurrence) if req.recurrence else None,
            json.dumps(req.enabled_tools), json.dumps(req.enabled_skills),
            next_run, task_id,
        ))
        conn.commit()
        row = conn.execute(f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found"}
        return _row_to_task(row)
    finally:
        conn.close()

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    conn = _tasks_conn()
    try:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()

class TaskStatusIn(BaseModel):
    status: str  # \'active\' | \'paused\'

@app.post("/api/tasks/{task_id}/status")
def set_task_status(task_id: int, req: TaskStatusIn):
    """Pause/resume. Resuming clears consecutive_failures and
    recomputes next_run_at from right now -- so a recurring task that
    sat paused through several missed cycles doesn\'t immediately fire
    a burst of catch-up runs for each one."""
    conn = _tasks_conn()
    try:
        row = conn.execute("SELECT schedule_type, run_at, recurrence_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found"}
        if req.status == "active":
            schedule_type, run_at, recurrence_json = row
            recurrence = json.loads(recurrence_json) if recurrence_json else None
            next_run = _compute_initial_next_run(schedule_type, run_at, recurrence)
            conn.execute("UPDATE tasks SET status=?, consecutive_failures=0, next_run_at=? WHERE id=?", (req.status, next_run, task_id))
        else:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (req.status, task_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()

_task_runs_in_progress = {}  # task_id -> {"session_id": str, "message_count_before": int}

def _lcm_get_session(session_id):
    """Full session dict if it exists (id, label, pinned, created_at,
    last_active), else None. Used both to check existence and, when
    reusing an existing session for a task run, to preserve its real
    label/pinned state while bumping last_active -- the upsert below
    overwrites both of those from whatever gets sent, so a naive resend
    of a default label/pinned would silently clobber real values."""
    try:
        resp = httpx.get(f"{lcm_client.LCM_URL}/sessions", timeout=5)
        if resp.status_code == 200:
            for s in resp.json():
                if s.get("id") == session_id:
                    return s
    except Exception:
        pass
    return None

def _lcm_message_count(session_id):
    try:
        resp = httpx.get(f"{lcm_client.LCM_URL}/messages/{session_id}", timeout=5)
        if resp.status_code == 200:
            return len(resp.json())
    except Exception:
        pass
    return None

def _start_task_run(task):
    """Kicks off one task run through the exact same chat_stream a live
    chat message uses -- a scheduled task run is structurally identical
    to "a chat message whose browser tab was never opened", so this
    reuses the background-thread-plus-queue mechanism as-is rather
    than duplicating the generation loop. Recreates the task's session
    first if it's gone missing (e.g. deleted by the user) instead of
    erroring out looking for one that no longer exists, keeping the
    same session_label so the recreated session reads the same way."""
    task_id = task["id"]
    session_id = task["session_id"]
    existing = _lcm_get_session(session_id) if session_id else None
    now_ms = time.time() * 1000
    if not existing:
        session_id = str(uuid.uuid4())
        try:
            httpx.post(f"{lcm_client.LCM_URL}/sessions", json={
                "id": session_id, "label": task["session_label"],
                "pinned": False, "created_at": now_ms, "last_active": now_ms,
            }, timeout=5)
        except Exception as e:
            print(f"[TASKS] failed to (re)create session for task {task_id}: {e!r}", flush=True)
            return
        conn = _tasks_conn()
        try:
            conn.execute("UPDATE tasks SET session_id=? WHERE id=?", (session_id, task_id))
            conn.commit()
        finally:
            conn.close()
    else:
        # Reusing an existing session -- bump its last_active the same
        # way the frontend's own syncSession() does after a live
        # message, so it surfaces in the sidebar like any other chat
        # activity instead of sitting frozen at its original creation
        # time. created_at is ignored by the upsert on conflict, but
        # label/pinned are NOT, so both are read back and resent as-is
        # rather than risking a silent reset to defaults.
        try:
            httpx.post(f"{lcm_client.LCM_URL}/sessions", json={
                "id": session_id, "label": existing.get("label") or task["session_label"],
                "pinned": bool(existing.get("pinned")),
                "created_at": existing.get("created_at") or now_ms,
                "last_active": now_ms,
            }, timeout=5)
        except Exception as e:
            print(f"[TASKS] failed to bump last_active for task {task_id}: {e!r}", flush=True)

    message_count_before = _lcm_message_count(session_id) or 0
    enabled_tools = task["enabled_tools"]
    settings_search_url = (settings.load_settings() or {}).get("search_url") or ""
    req = ChatIn(
        session_id=session_id,
        message=task["prompt"],
        model=task["model"],
        workspace=task.get("workspace") or "",
        endpoint_url=task.get("endpoint_url") or "",
        search_url=settings_search_url if ("web_search" in enabled_tools or "web_fetch" in enabled_tools) else "",
        allowed_tools=enabled_tools,
        allowed_skills=task["enabled_skills"],
    )
    try:
        chat_stream(req)  # returned StreamingResponse is deliberately never read -- the background thread it starts keeps running regardless, same mechanism already proven for a closed browser tab
    except Exception as e:
        print(f"[TASKS] task {task_id} failed to start: {e!r}", flush=True)
        _record_task_run_result(task_id, task, success=False)
        return
    _task_runs_in_progress[task_id] = {"session_id": session_id, "message_count_before": message_count_before}

def _record_task_run_result(task_id, task, success):
    conn = _tasks_conn()
    try:
        if success:
            next_run = None
            if task["schedule_type"] == "recurring" and task["recurrence"]:
                next_run = _next_occurrence(task["recurrence"], after_ts=time.time())
            conn.execute(
                "UPDATE tasks SET consecutive_failures=0, last_run_at=?, last_run_status='success', next_run_at=? WHERE id=?",
                (time.time(), next_run, task_id)
            )
        else:
            new_failures = task["consecutive_failures"] + 1
            new_status = "paused" if new_failures >= 3 else "active"
            next_run = None
            if new_status == "active" and task["schedule_type"] == "recurring" and task["recurrence"]:
                next_run = _next_occurrence(task["recurrence"], after_ts=time.time())
            conn.execute(
                "UPDATE tasks SET consecutive_failures=?, last_run_at=?, last_run_status='error', status=?, next_run_at=? WHERE id=?",
                (new_failures, time.time(), new_status, next_run, task_id)
            )
        conn.commit()
    finally:
        conn.close()

def _task_scheduler_loop():
    """Wakes up periodically: checks any task runs already in progress
    for completion (a run is "done" once its session drops out of
    generation_streaming.active_generations, the same tracker the frontend's own
    reconnect-and-poll logic uses), then starts any newly-due tasks.
    Success/failure is inferred from whether a new assistant message
    actually landed in the session -- true on every real completion
    path in the generation loop, false if the run errored before ever
    reaching a model at all."""
    while True:
        try:
            for task_id in list(_task_runs_in_progress.keys()):
                info = _task_runs_in_progress[task_id]
                if info["session_id"] in generation_streaming.active_generations:
                    continue
                message_count_after = _lcm_message_count(info["session_id"])
                success = message_count_after is not None and message_count_after > info["message_count_before"]
                conn = _tasks_conn()
                try:
                    row = conn.execute(f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,)).fetchone()
                finally:
                    conn.close()
                if row:
                    _record_task_run_result(task_id, _row_to_task(row), success)
                del _task_runs_in_progress[task_id]

            conn = _tasks_conn()
            try:
                rows = conn.execute(
                    "SELECT " + _TASK_COLUMNS + " FROM tasks WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at <= ?",
                    (time.time(),)
                ).fetchall()
            finally:
                conn.close()
            for row in rows:
                task = _row_to_task(row)
                if task["id"] in _task_runs_in_progress:
                    continue
                print(f"[TASKS] starting task {task['id']!r}: {task['prompt'][:60]!r}", flush=True)
                _start_task_run(task)
        except Exception as e:
            print(f"[TASKS] scheduler loop error: {e!r}", flush=True)
        time.sleep(30)

if __name__ == "__main__":
    import uvicorn
    import threading

    parser = argparse.ArgumentParser(description="Athena -- chat/agent workspace")
    parser.add_argument("--reindex", action="store_true", help="Re-index the codebase for RAG and exit")
    args = parser.parse_args()

    if args.reindex:
        print("[Athena] Re-indexing codebase for RAG...", flush=True)
        db_path = os.environ.get("ATHENA_RAG_DB", "rag_index.db")
        # Clear existing index by deleting the old DB file
        if os.path.exists(db_path):
            os.remove(db_path)
            print(f"[Athena] Removed existing RAG index: {db_path}", flush=True)
        # Create fresh index
        fresh_rag = SimpleCodeRAG(db_path)
        fresh_rag.index_codebase(root_dir=".")
        # Count indexed chunks
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM chunks")
        count = cursor.fetchone()[0]
        conn.close()
        print(f"[Athena] Re-index complete: {count} chunks indexed in {db_path}", flush=True)
        sys.exit(0)

    _check_requirements()
    lcm_client.start_bundled_lcm()

    def _startup_reindex():
        # Warm the embedding model here, explicitly, before touching any
        # files -- so the potentially slow first-run model load (and its
        # own [RAG] log lines, see rag._get_embedder) happens at a known
        # point in the background at boot, decoupled from whichever thread
        # (this one or a live chat request's search_codebase call) would
        # otherwise have triggered it incidentally first.
        rag.warm_up()
        print("[Athena] Re-indexing codebase for RAG...", flush=True)
        try:
            rag.index_codebase(".")
        except Exception as e:
            print(f"[RAG] startup reindex failed: {e!r}", flush=True)
            return
        print("[Athena] RAG re-index complete.", flush=True)
    threading.Thread(target=_startup_reindex, daemon=True).start()

    threading.Thread(target=_memory_scan_loop, daemon=True).start()
    threading.Thread(target=_task_scheduler_loop, daemon=True).start()

    from mcp_manager import mcp_manager
    mcp_manager.start()
    for server_cfg in settings.load_settings().get("mcp_servers", []):
        try:
            tools = mcp_manager.connect_server(server_cfg["name"], server_cfg["command"], server_cfg.get("args", []))
            print(f"[Athena] Connected to MCP server '{server_cfg['name']}' -- {len(tools)} tools discovered", flush=True)
        except Exception as e:
            print(f"[Athena] Failed to connect to MCP server '{server_cfg['name']}': {e}", flush=True)

    port = int(os.environ.get("ATHENA_PORT", "9500"))
    uvicorn.run(app, host="0.0.0.0", port=port)
