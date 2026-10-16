import os
import re
import sys
import json
import time
import select
import hashlib
import subprocess
import threading
from typing import Optional, List
import contextvars
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter
from pydantic import BaseModel

import settings
from loop_guard import detect_text_loop as _detect_text_loop
import activity
import task_context
import lcm_client
import host_locks
from host_locks import OLLAMA_URL
import bash_tools
import file_tools
import lsp_tools
import web_tools
from logging_setup import logger, log_trace_event
import db
import ownership
from fastapi.responses import JSONResponse

router = APIRouter()


def _bots_conn():
    conn = db.get_conn()
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
            last_active REAL,
            is_everyone_room INTEGER NOT NULL DEFAULT 0
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
    ownership.ensure_owner_column(conn, "bots")
    ownership.ensure_owner_column(conn, "rooms")
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


@router.get("/api/bots")
def list_bots():
    where, params = ownership.owner_filter()
    conn = _bots_conn()
    try:
        rows = conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots WHERE {where} ORDER BY created_at ASC", params).fetchall()
        return [_row_to_bot(r) for r in rows]
    finally:
        conn.close()


@router.post("/api/bots")
def create_bot(req: BotIn):
    if ownership.endpoint_kind(req.provider, req.endpoint_url or "") is None:
        return JSONResponse(status_code=403, content={"error": "This account cannot use that model endpoint."})
    conn = _bots_conn()
    try:
        cur = conn.execute("""
            INSERT INTO bots (name, endpoint_url, provider, api_key, model, description, allowed_tools_json, unload_strategy, created_at, owner)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            req.name, req.endpoint_url, req.provider, req.api_key or "", req.model, req.description,
            json.dumps(effective_tools(req.allowed_tools)), req.unload_strategy, time.time(), ownership.stored_owner(),
        ))
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


@router.put("/api/bots/{bot_id}")
def update_bot(bot_id: int, req: BotIn):
    if ownership.endpoint_kind(req.provider, req.endpoint_url or "") is None:
        return JSONResponse(status_code=403, content={"error": "This account cannot use that model endpoint."})
    where, owner_params = ownership.owner_filter()
    conn = _bots_conn()
    try:
        conn.execute(f"""
            UPDATE bots SET name=?, endpoint_url=?, provider=?, api_key=?, model=?, description=?, allowed_tools_json=?, unload_strategy=?
            WHERE id=? AND {where}
        """, (
            req.name, req.endpoint_url, req.provider, req.api_key or "", req.model, req.description,
            json.dumps(effective_tools(req.allowed_tools)), req.unload_strategy, bot_id, *owner_params,
        ))
        conn.commit()
        return {"id": bot_id}
    finally:
        conn.close()


@router.delete("/api/bots/{bot_id}")
def delete_bot(bot_id: int):
    where, owner_params = ownership.owner_filter()
    conn = _bots_conn()
    try:
        conn.execute(f"DELETE FROM bots WHERE id=? AND {where}", (bot_id, *owner_params))
        conn.commit()
        return {"id": bot_id}
    finally:
        conn.close()



class _FakeReqForDispatch:
    """Minimal stand-in for ChatIn, carrying only the fields
    _stream_completion / _stream_openai_compatible actually read
    (provider, model, endpoint_url, api_key, think). Bot dispatch has no
    session/workspace/tool-allowlist concerns a real ChatIn carries,
    so building one of those would mean populating a dozen irrelevant
    fields just to satisfy the type -- this carries exactly what's
    needed and nothing else. think defaults to "none": a dispatched
    sub-agent turn should be fast and cheap by default, not spend
    tokens on extended reasoning the way Athena's own main-agent turns
    now do (ChatIn.think defaults to "high") -- override explicitly if
    a specific dispatched turn genuinely needs it."""
    def __init__(self, provider, model, endpoint_url, api_key, think="none"):
        self.provider = provider
        self.model = model
        self.endpoint_url = endpoint_url
        self.api_key = api_key
        self.think = think



_BOT_ALLOWED_PROVIDERS = {"", "openai", "openrouter", "custom"}  # never anthropic/google -- bots are local-inference only, enforced here as a backend guard even though the UI never offers those options



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
    from main import _stream_completion, _SUB_AGENT_TIMEOUT, _leaked_tool_markup, _TOOL_MARKUP  # deferred: main.py imports bots.py at module load
    provider = bot.get("provider") or ""
    if provider not in _BOT_ALLOWED_PROVIDERS:
        return {"error": f"Bots may not use provider '{provider}' -- local inference only (Ollama-native or OpenAI-compatible)."}

    endpoint_url = bot.get("endpoint_url") or ""
    fake_req = _FakeReqForDispatch(
        provider=provider,
        model=bot["model"],
        endpoint_url=endpoint_url,
        api_key=bot.get("api_key") or "",
        think="none",
    )
    target_url = (endpoint_url.rstrip("/") + "/api/chat") if endpoint_url and provider == "" else (OLLAMA_URL if provider == "" else "")
    _settings_snapshot = settings.load_effective()
    workspace = _settings_snapshot.get("workspace") or ""
    search_url = _settings_snapshot.get("search_url") or ""
    ptc_enabled = bool(_settings_snapshot.get("bot_ptc_enabled"))
    allowed_tools = effective_tools(bot.get("allowed_tools"))
    messages = list(chat_messages)
    session_key = f"bot-{bot.get('id', 'unknown')}"

    def _log_bot_turn_end(outcome, **extra):
        log_trace_event(session_key, "turn_end", bot=bot.get("name"), outcome=outcome, **extra)

    log_trace_event(session_key, "turn_start", bot=bot.get("name"), model=bot.get("model"))

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
    # Computed once for the whole turn, not per round -- a stable schema
    # keeps the model backend's prefix cache usable across every round
    # instead of resetting it whenever a group gets hard-stopped. Gate
    # enforcement for hard-stopped groups happens at call time below
    # instead (see the per-tc loop), the same way _execute_bot_tool_program
    # already enforces it for calls made inside a PTC script.
    tool_schemas = _bot_tool_schemas(allowed_tools, None, ptc_enabled)
    ctx_size = _bot_ctx_size(bot)
    recoveries = 0
    for _ in range(_BOT_MAX_ROUNDS):
        activity.touch()
        round_reply = ""
        round_tool_calls = []
        cancel_flag = threading.Event()
        try:
            round_thinking = ""
            for chunk in _stream_completion(fake_req, target_url, messages, tool_schemas if tool_schemas else None, ctx_size, cancel_flag, timeout=_SUB_AGENT_TIMEOUT):
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
                        logger.debug(f"bot={bot_name} round produced no content/tool_calls -- thinking was {len(round_thinking)} chars: {round_thinking[-500:]!r}")
                    break
        except Exception as e:
            _log_bot_turn_end("endpoint_error", error=str(e))
            return {"error": f"Bot endpoint call failed: {e}", "tool_call_history": tool_call_history}

        if not round_tool_calls:
            # Small models sometimes end a round with nothing, or with tool-call text that never became a call.
            # That is not an answer: nudge twice before letting an empty or garbled reply stand as the result.
            blank = not round_reply.strip()
            if (blank or _leaked_tool_markup(round_reply, round_thinking)) and recoveries < 2:
                recoveries += 1
                if round_reply.strip():
                    messages.append({"role": "assistant", "content": _TOOL_MARKUP.sub("", round_reply)})
                messages.append({"role": "user", "content": (
                    "You produced no output. Continue: make the tool call you need to answer the question, or if you already have "
                    "findings, call append_scratch_note with them now." if blank else
                    "Your last output ended with tool-call text, but no call actually ran (it was cut off or malformed). "
                    "Make that call again now as a real tool call."
                )})
                continue
            _log_bot_turn_end("completed")
            return {"content": round_reply, "tool_call_history": tool_call_history}
        recoveries = 0

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
            log_trace_event(session_key, "tool_call", tool=tool_name, args=fn_args)
            fingerprint = _tool_call_fingerprint(tc)
            _gate_error = _check_bot_tool_gate(group, _tool_type_hard_stopped)
            if _gate_error is not None:
                result = _gate_error
                tool_call_history.append(_tool_call_history_entry(tool_name, fn_args, result))
            elif seen_fingerprints.count(fingerprint) >= 2:
                result = {"error": "BLOCKED: this exact tool call (same tool, same arguments) has already been made twice this turn. Use what you already have, or make a genuinely different call."}
                tool_call_history.append(_tool_call_history_entry(tool_name, fn_args, result))
            elif tool_name == "run_tool_program":
                result = _execute_bot_tool_program(
                    fn_args.get("code", ""), workspace, bot, session_key, allowed_tools,
                    _tool_type_counts, _tool_type_hard_stopped, seen_fingerprints, search_url,
                )
                tool_call_history.append(_tool_call_history_entry(tool_name, {"code": fn_args.get("code", "")}, {"status": result.get("status") or "error", "detail": result.get("detail") or result.get("error")}))
                tool_call_history.extend(result.get("tool_call_history", []))
                seen_fingerprints.append(fingerprint)
                if len(seen_fingerprints) > 20:
                    seen_fingerprints.pop(0)
            else:
                result = _execute_bot_tool_call(tc, workspace, bot, session_key, search_url)
                tool_call_history.append(_tool_call_history_entry(tool_name, fn_args, result))
                seen_fingerprints.append(fingerprint)
                if len(seen_fingerprints) > 20:
                    seen_fingerprints.pop(0)
            _result_error = result.get("error") if isinstance(result, dict) else None
            log_trace_event(
                session_key, "tool_blocked" if isinstance(_result_error, str) and _result_error.startswith("BLOCKED:") else "tool_result",
                tool=tool_name, success=_result_error is None, output=result,
            )
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

    _log_bot_turn_end("max_rounds_reached")
    return {
        "content": round_reply,
        "error": "Hit the round limit without a final answer after repeated real tool calls -- this may genuinely be too large a job for one turn; consider a narrower job_scope.",
        "tool_call_history": tool_call_history,
    }



_BOT_TOOL_DESCRIPTIONS = {
    "bash_read_only": "read-only shell commands (ls, cat, grep, find, etc.) to look at files and search",
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
        for t in effective_tools(bot.get("allowed_tools"))
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

Your job: {job_scope}

You investigate and report. You never write files, run commands or change anything.

How to work:
- Say what you are about to look at before you call a tool, and say plainly when something is not what you expected instead of guessing.
- Orient first: use search_codebase or a targeted grep to find the right spot, then read_file with an offset for just that section. A targeted partial read that answers the question beats an unfinished read of a whole large file; you have a limited number of rounds and a limited context.
- Stay inside the question you were asked. If a tool fails, report the failure rather than retrying blindly or guessing.

Your tools:
{tool_lines}

When you are done, or genuinely stuck, call append_scratch_note with your findings: that is the only way they reach Athena, and a plain-text reply is discarded. If append_scratch_note is not one of your tools, reply with your findings directly.{constraints_block}"""



ATHENA_BOTS_SESSION_ID = "athena-bots-agent"


def _roster_lines():
    """The bots as the main agent needs to see them: id, what each can do, what it is for. Put in the prompt so
    the ids are never guessed (a real run called plan_delegation with bot ids 1 and 2 for bots 19 and 22)."""
    roster = _list_bots_for_model()
    if not roster:
        return "Your roster is empty: no bots exist yet."
    return "Your bots:\n" + "\n".join(
        f"- {b['name']} (id {b['id']}): can {b['can']}. {b['description'][:140].rstrip()}" for b in roster)


def _get_bot_delegation_prompt_section(workspace=None, async_enabled=False):
    base = """

## Stay On the Current Task
The person's most recent message is your current task. Earlier messages are background: something similar discussed before is not the same task.

## Bot Delegation
You have a roster of bots for read-only investigation. They read, search and report; they never write files or run commands, so you act on what they find. Their ids are listed below; use exactly those ids.

Delegate when you would otherwise have to explore to find where the answer lives. If you already know which file holds it, just read it yourself. Decide before your first tool call on a topic, and don't investigate it yourself and then delegate the same thing.

Ask each bot one focused question per step (use plan_delegation when you already have several). A bot starts with no memory of earlier steps, but it is automatically told what the person asked, so any restriction they gave reaches it without you restating it. Add `known` for what you have already ruled out and `report` for the shape of answer you need. A step's findings come back in its result, along with a check on whether the files and lines the bot cites really exist.

Treat a report as evidence to weigh, not as the answer. When a step comes back possibly_fabricated, no_scratch_note, rejected or error, it did not produce a usable finding: fix the step and run it again once, or tell the person plainly what could not be found (read_tool_call_history shows what a bot actually did). A rejected step says what to change. Once you call plan_delegation, correct the plan and resubmit it instead of falling back to single steps. message_bot is for a quick informal question, not for investigation.

When you need something from a large file, delegate that question: reading it yourself spends your context. Reading a small file you know is short is fine.

Bots should be broad specialists (for example web research, frontend code, backend code), not one-off bots for a single task. Check list_bots before creating one; a duplicate name is rejected, and if the roster is empty the first bot should be a general-purpose one. draft_bot_prompt shows how a bot's prompt is assembled."""
    base += "\n\n" + _roster_lines()
    if async_enabled:
        base += (
            "\n\n## Async Delegation\n"
            "run_delegation_step and plan_delegation no longer wait for the bot(s) to finish. They return "
            "immediately with {\"status\": \"pending\", \"handle\": <job id>} -- that is not a failure, and "
            "there is no findings content in that return value yet. You are free to keep working (answer the "
            "person, delegate another step, or just end your turn) while the bot works in the background. "
            "When it finishes, its findings arrive as a brand new message in this same conversation; react to "
            "it then. A 'pending' status is not a failure: don't retry, poll or wait on it, just continue your turn."
        )
    defaults = (settings.load_effective().get("bot_creation_defaults") or {})
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
    where, params = ownership.owner_filter()
    conn = _bots_conn()
    try:
        rows = conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots WHERE {where} ORDER BY created_at ASC", params).fetchall()
        return [_row_to_bot(r) for r in rows]
    finally:
        conn.close()



def _list_bots_for_model():
    """What list_bots shows the main agent: who each bot is and what it can actually do. The raw rows
    (endpoint, api key, a bare allowed_tools list) are for the app, not for a model's context."""
    out = []
    for b in _list_bots_tool():
        tools = effective_tools(b.get("allowed_tools"))
        out.append({
            "id": b["id"], "name": b["name"], "description": b.get("description") or "", "model": b["model"],
            "tools": tools, "can": ", ".join(_capabilities(tools)) or "reason over the instruction text only",
        })
    return out


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
    if isinstance(bot_id, str):                      # a bot named instead of numbered, or "19" as text
        wanted = bot_id.strip().lower()
        for b in _list_bots_tool():
            if b["name"].lower() == wanted or str(b["id"]) == wanted:
                return b
    return None


def _unknown_bot_message(bot_id):
    roster = _list_bots_for_model()
    listing = "; ".join(f"{b['name']} (id {b['id']})" for b in roster) or "none exist yet"
    return f"No bot with id {bot_id!r}. The bots are: {listing}. Use one of those ids."



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
    return False, ""

# Async delegation job tracking (see _run_delegation_step_async_tool /
# _plan_delegation_async_tool below) -- same shape as _bg_processes /
# _bg_processes_lock / _bg_process_counter, which already does "start a
# background unit of work, hand back an id immediately" for background
# bash processes. Not pruned, consistent with _bg_processes never being
# pruned either.

# ---------- what a step hands the bot, and what it hands back ----------
_FINDINGS_INLINE_LIMIT = 3500       # characters of findings returned with the step; the scratch file keeps everything
_DEFAULT_REPORT = ("A short answer, under 200 words. Back each claim with the file path and line number where you saw it, "
                   "and say plainly what you could not find.")


def _compose_step_message(instruction, known=None, report=None, task_text=""):
    """The bot knows nothing but this message, so it carries what a colleague arriving cold would need:
    what the person actually asked (their own words, so any restriction they gave travels with it), the
    one question, what is already known, and the shape of answer wanted."""
    parts = []
    if task_text and task_text.strip():
        parts.append("What the person asked (context only; your job is just the question below):\n" + task_text.strip()[:1200])
    parts.append("Your question:\n" + instruction.strip())
    if known and str(known).strip():
        parts.append("Already known or ruled out:\n" + str(known).strip()[:600])
    parts.append("Report back: " + (str(report).strip()[:300] if report and str(report).strip() else _DEFAULT_REPORT))
    parts.append("This is an isolated step. When you have your findings, call append_scratch_note with them: that is the only way "
                 "they reach Athena. Do not reply with plain text.")
    return "\n\n".join(parts)


def _read_new_findings(scratch_path, before_size):
    try:
        with open(scratch_path, "rb") as f:
            f.seek(before_size)
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


_CITATION_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|js|ts|tsx|jsx|vue|html|css|json|md|txt|ya?ml|toml|sql|sh|go|rs|java|cpp|c|h|rb|php))"
    r"(?::|[ \t]*,?[ \t]*\(?(?:lines?|L)[ \t]*)(\d+)(?:[ \t]*[-\u2013][ \t]*(\d+))?", re.I)
_SKIP_WALK = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", ".athena", "file_backups"}


def _locate_cited_file(workspace, cited):
    direct = os.path.join(workspace, cited)
    if os.path.isfile(direct):
        return direct
    base, matches = os.path.basename(cited), []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_WALK]
        if base in files:
            matches.append(os.path.join(root, base))
            if len(matches) > 1:
                break
    return matches[0] if len(matches) == 1 else (matches[0] if matches else None)


def verify_citations(findings, workspace, limit=20):
    """Check that the places a bot says it looked exist: the file is in the workspace and has that line.
    A report that cites files and lines which are not there was not written from looking at them."""
    if not findings or not workspace or not os.path.isdir(workspace):
        return {"checked": 0, "not_found": []}
    seen, missing = set(), []
    for match in _CITATION_RE.finditer(findings):
        cited, line, end = match.group(1), int(match.group(2)), int(match.group(3) or match.group(2))
        if (cited, line) in seen:
            continue
        seen.add((cited, line))
        if len(seen) > limit:
            seen.discard((cited, line))
            break
        path = _locate_cited_file(workspace, cited)
        if path is None:
            missing.append(f"{cited}:{line} (no such file)")
            continue
        try:
            with open(path, "rb") as f:
                total = sum(1 for _ in f)
        except OSError:
            continue
        if max(line, end) > total:
            missing.append(f"{cited}:{line} (the file has only {total} lines)")
    return {"checked": len(seen), "not_found": missing}


def _step_result(bot, instruction, step_history, scratch_path, before_size, retried, findings_limit):
    findings = _read_new_findings(scratch_path, before_size)
    workspace = settings.load_effective().get("workspace") or ""
    cites = verify_citations(findings, workspace)
    base = {"bot": bot["name"], "instruction": instruction, "tool_calls_recorded": len(step_history)}
    if cites["checked"] >= 2 and len(cites["not_found"]) * 2 >= cites["checked"]:
        return {**base, "status": "possibly_fabricated", "citations": cites,
                "detail": f"{len(cites['not_found'])} of the {cites['checked']} places this report cites do not exist ({'; '.join(cites['not_found'][:4])}). "
                          "Treat it as a failed step, not a finding; read_tool_call_history shows what the bot actually looked at."}
    result = {**base, "status": "done", "detail": "Step complete" + (" after one internal retry." if retried else "."),
              "findings": findings[:findings_limit]}
    if len(findings) > findings_limit:
        result["findings_truncated"] = True
        result["detail"] += f" Findings are cut off here at {findings_limit} characters; read_scratch_file with bot_name \"{bot['name']}\" has all of it."
    if cites["checked"]:
        result["citations"] = cites
    return result


def _run_delegation_step_tool(bot_id, instruction, known=None, report=None, task_text="", findings_limit=_FINDINGS_INLINE_LIMIT):
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
        return {"error": _unknown_bot_message(bot_id), "instruction": instruction}
    gap = capability_gap(bot, instruction)
    if gap:
        return {"status": "rejected", "error": gap, "bot": bot["name"], "instruction": instruction}
    task_hash = task_context.current_task_hash.get()
    if task_hash is None:
        return {"error": "No active task context -- this should only be called mid-delegation.", "instruction": instruction}

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", bot["name"].lower())
    scratch_path = os.path.join(task_context.BOT_TASK_FOLDER_ROOT, task_hash, f"{safe_name}.md")
    before_size = os.path.getsize(scratch_path) if os.path.exists(scratch_path) else 0

    chat_messages = [
        {"role": "system", "content": _build_bot_system_prompt(bot)},
        {"role": "user", "content": _compose_step_message(instruction, known, report, task_text)},
    ]
    # Steps can run in parallel threads (plan_delegation's concurrency setting), but two of them landing
    # on the same physical inference host must still never generate at the same time.
    host_lock = host_locks.get_host_lock(host_locks.endpoint_host(bot.get("endpoint_url")))
    with host_lock:
        result = _call_bot_endpoint(bot, chat_messages)
    # One entry for the whole step, covering the first attempt and the retry if there is one.
    step_history = list(result.get("tool_call_history", []))

    def failed(detail):
        _append_tool_call_history(bot["name"], instruction, step_history)
        return {"status": "error", "detail": detail, "bot": bot["name"], "instruction": instruction, "tool_calls_recorded": len(step_history)}

    if result.get("error"):
        return failed(result["error"])
    size = lambda: os.path.getsize(scratch_path) if os.path.exists(scratch_path) else 0
    retried = False
    if size() <= before_size:
        # One nudge in the SAME isolated conversation before ever surfacing a failure: usually all it takes.
        retried = True
        chat_messages.append({"role": "assistant", "content": result.get("content", "")})
        chat_messages.append({"role": "user", "content": (
            "You did not call append_scratch_note. That is the only way your findings reach Athena -- a plain text reply "
            "is discarded. Call append_scratch_note now with your findings from above. Do not reply with plain text again.")})
        with host_lock:
            retry_result = _call_bot_endpoint(bot, chat_messages)
        step_history += retry_result.get("tool_call_history", [])
        if retry_result.get("error"):
            return failed(retry_result["error"])
        if size() <= before_size:
            _append_tool_call_history(bot["name"], instruction, step_history)
            return {
                "status": "no_scratch_note",
                "detail": "The bot replied without calling append_scratch_note, even after one internal retry -- no findings were recorded for this step.",
                "bot": bot["name"], "instruction": instruction, "tool_calls_recorded": len(step_history),
            }
    _append_tool_call_history(bot["name"], instruction, step_history)
    result = _step_result(bot, instruction, step_history, scratch_path, before_size, retried, findings_limit)
    return result



def _plan_delegation_tool(steps, task_text=""):
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
        runnable.append((i, bot_id, instruction, step.get("known"), step.get("report")))

    # Sequential (1) by default -- Athena targets hardware of all sizes,
    # including old/cheap machines that shouldn't pay for concurrency
    # they can't afford. A person running stronger hardware can raise
    # this in settings; the per-host lock in _run_delegation_step_tool
    # still serializes any two steps that land on the same physical
    # inference host regardless of what this is set to.
    concurrency = max(1, int(settings.load_settings().get("delegation_concurrency") or 1))

    per_step_limit = max(1200, 6000 // max(1, len(runnable)))      # a whole plan's findings come back in one result; keep it bounded

    if concurrency <= 1 or len(runnable) <= 1:
        for i, bot_id, instruction, known, report in runnable:
            r = _run_delegation_step_tool(bot_id, instruction, known, report, task_text, per_step_limit)
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
            i, bot_id, instruction, known, report = item
            task_context.current_task_hash.set(task_hash)
            r = _run_delegation_step_tool(bot_id, instruction, known, report, task_text, per_step_limit)
            return i, {"step": i, "bot_id": bot_id, **r}

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for i, r in executor.map(lambda item: contextvars.copy_context().run(_run, item), runnable):
                results[i] = r

    return {"plan_results": results}



def _message_bot_tool(bot_id, content):
    import rooms  # deferred: rooms.py imports bots.py at module load
    if task_context.current_task_hash.get() is not None:
        return {"error": "A delegation task is already active this turn -- message_bot cannot be used mid-delegation, including as a workaround for a failed or rejected step. Use run_delegation_step or plan_delegation instead, even if a prior step failed."}
    conn = _bots_conn()
    try:
        where, owner_params = ownership.owner_filter()
        room_row = conn.execute(
            "SELECT " + rooms._ROOM_COLUMNS + " FROM rooms WHERE kind = 'dm' AND human_party = 'athena' AND member_bot_ids_json = ? AND " + where,
            (json.dumps([bot_id]), *owner_params)
        ).fetchone()
        if room_row:
            room = rooms._row_to_room(room_row)
        else:
            now = time.time()
            cur = conn.execute(
                "INSERT INTO rooms (kind, human_party, label, member_bot_ids_json, created_at, last_active, owner) VALUES ('dm', 'athena', ?, ?, ?, ?, ?)",
                (f"Athena & bot #{bot_id}", json.dumps([bot_id]), now, now, ownership.stored_owner())
            )
            conn.commit()
            room = rooms._row_to_room(conn.execute(f"SELECT {rooms._ROOM_COLUMNS} FROM rooms WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()
    result = rooms.send_room_message(room["id"], rooms.RoomSendIn(sender_type="athena", sender_bot_id=None, content=content))
    return result



def _create_bot_tool(name, description, allowed_tools):
    """Deliberately takes no model/endpoint from the caller -- Athena
    could easily hallucinate a model name that isn't actually
    installed on any registered endpoint, since she has no way to see
    what's real. Every bot she creates uses the person's own
    pre-configured default (set via Athena2's Agent Defaults menu)
    instead, removing the guess entirely rather than just warning
    about it."""
    current_settings = settings.load_effective()
    defaults = current_settings.get("bot_creation_defaults") or {}
    model = defaults.get("model")
    endpoint_url = defaults.get("endpoint_url")
    provider = defaults.get("provider", "")
    if model and ownership.endpoint_kind(provider, endpoint_url or "") is None:
        return {"error": "This account cannot use the endpoint set as the default for new bots. Choose a different default in Athena's Agent Defaults menu."}
    if not model:
        return {"error": "No default model is configured for bot creation yet. Ask the person to set one in Athena's Agent Defaults menu before creating a bot."}
    if not allowed_tools:
        allowed_tools = _bot_prompt_tool_cache.get(name.strip().lower(), [])
    allowed_tools = [t for t in allowed_tools if t in BOT_ALLOWED_TOOL_NAMES]
    if not allowed_tools:
        return {"error": "No valid read-only tools were provided for this bot. Call draft_bot_prompt first with a real tools list (or pass allowed_tools directly) -- a bot can't be created with no tools."}
    conn = _bots_conn()
    try:
        own_where, own_params = ownership.owner_filter()
        existing_names = [r[0] for r in conn.execute(f"SELECT name FROM bots WHERE {own_where}", own_params).fetchall()]
        if name.strip().lower() in (n.lower() for n in existing_names):
            return {"error": f"A bot named '{name}' already exists. Existing bots: {', '.join(existing_names)}. Choose a genuinely different name, not a numbered variant."}
        cur = conn.execute("""
            INSERT INTO bots (name, endpoint_url, provider, api_key, model, description, allowed_tools_json, unload_strategy, created_at, owner)
            VALUES (?, ?, ?, '', ?, ?, ?, 'ollama_keep_alive', ?, ?)
        """, (name, endpoint_url, provider, model, description, json.dumps(allowed_tools), time.time(), ownership.stored_owner()))
        conn.commit()
        return _row_to_bot(conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()



def _update_bot_tool(bot_id, description=None, allowed_tools=None):
    """Deliberately narrow -- Athena can refine a bot's own instructions
    and tool access as it learns what works, but identity fields
    (name, model, endpoint) are left to the person managing the
    roster via the UI, not something Athena rewrites on its own."""
    where, owner_params = ownership.owner_filter()
    conn = _bots_conn()
    try:
        existing = conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots WHERE id = ? AND {where}", (bot_id, *owner_params)).fetchone()
        if not existing:
            return {"error": _unknown_bot_message(bot_id)}
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
        "description": "Ask one bot one focused question and get its findings back. The bot starts fresh with no memory of earlier steps, and is automatically told what the person asked; it investigates read-only and the report comes back in this result, with a check that the files and lines it cites exist.",
        "parameters": {"type": "object", "properties": {
            "bot_id": {"type": "integer", "description": "The bot to ask."},
            "instruction": {"type": "string", "description": "The question: one topic, self-contained."},
            "known": {"type": "string", "description": "Optional: what is already known or ruled out, so the bot does not redo it."},
            "report": {"type": "string", "description": "Optional: the shape of answer you need. Default: short, with file paths and line numbers."},
        }, "required": ["bot_id", "instruction"]},
    }},
    {"type": "function", "function": {
        "name": "read_scratch_file",
        "description": "Read a bot's complete scratch file for this task: everything it reported across all steps. A step's own findings already come back with it (cut off only when very long), so this is for the full text or an earlier step.",
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
        "description": "Run several focused questions in order, one result per step, each with its findings (see run_delegation_step). Use it when you already know all the questions.",
        "parameters": {"type": "object", "properties": {
            "steps": {
                "type": "array",
                "description": "One entry per question; split a bundled question into separate steps.",
                "items": {
                    "type": "object",
                    "properties": {
                        "bot_id": {"type": "integer"},
                        "instruction": {"type": "string", "description": "One self-contained question."},
                        "known": {"type": "string", "description": "Optional: already known or ruled out."},
                        "report": {"type": "string", "description": "Optional: the shape of answer you need."},
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
            "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "Read-only tool names this bot can use, e.g. ['bash_read_only', 'search_codebase']."},
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
    "Ask one bot one focused question. Runs in the background and returns immediately with "
    "{\"status\": \"pending\", \"handle\": <job id>}. When the bot is done, its findings arrive as a new message "
    "in this same conversation; you don't need to poll or wait, and can keep working in the meantime."
)
_ASYNC_PLAN_DELEGATION_DESCRIPTION = (
    "Run several focused questions in order. Runs in the background and returns immediately with "
    "{\"status\": \"pending\", \"handle\": <job id>}. Once every step is done, ONE message with all the findings "
    "arrives in this same conversation; you don't need to poll or wait."
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
    "bash_read_only", "list_files", "read_file", "search_codebase",
    "find_definition", "find_references", "type_info",
    "web_search", "web_fetch",
    "list_bots", "list_rooms", "message_bot", "read_room_messages", "draft_bot_prompt",
    "lcm_recall_search", "lcm_recall_expand", "lcm_recall_range",
}


# What a bot gets when no tools were chosen. The Bots page used to save every bot with an empty list, so
# bots described as investigators could not read a file or fetch a page; "nothing chosen" now means this set.
DEFAULT_BOT_TOOLS = ["bash_read_only", "list_files", "read_file", "search_codebase"]


def effective_tools(allowed):
    chosen = [t for t in (allowed or []) if t in BOT_ALLOWED_TOOL_NAMES]
    return chosen or list(DEFAULT_BOT_TOOLS)


def _capabilities(tools):
    tools = set(tools)
    caps = []
    if tools & {"read_file", "list_files", "bash_read_only"}:
        caps.append("read files")
    if tools & {"search_codebase", "find_definition", "find_references", "type_info"}:
        caps.append("search code")
    if "web_search" in tools:
        caps.append("search the web")
    if "web_fetch" in tools:
        caps.append("fetch web pages")
    return caps


_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.I)
_WEB_WORDS_RE = re.compile(r"\b(?:search the web|web search|google|look (?:it )?up online|online)\b", re.I)
_FILE_WORDS_RE = re.compile(
    r"\b[\w-]+\.(?:py|js|ts|tsx|jsx|html|css|json|md|txt|ya?ml|toml|sql|sh|go|rs|java|cpp|c|h)\b"
    r"|\b(?:directory|directories|folder|codebase|repository|repo|source files?|files?)\b", re.I)
_FILE_TOOLS = {"read_file", "list_files", "bash_read_only", "search_codebase"}
_WEB_TOOLS = {"web_fetch", "web_search"}


def capability_gap(bot, instruction):
    """-> a refusal message when this instruction needs files or the web and the bot has neither, else None.
    A bot that cannot look at anything just answers from its own guesses, so the main agent is told at once
    (and which bots could do it) instead of discovering it from an empty report a whole step later."""
    tools = set(effective_tools(bot.get("allowed_tools")))
    text = instruction or ""
    needs_web = bool(_URL_RE.search(text) or _WEB_WORDS_RE.search(text))
    needs_files = bool(_FILE_WORDS_RE.search(_URL_RE.sub(" ", text)))
    missing = []
    if needs_web and not tools & _WEB_TOOLS:
        missing.append("web")
    if needs_files and not tools & _FILE_TOOLS:
        missing.append("file")
    if not missing:
        return None
    others = [b["name"] for b in _list_bots_tool()
              if b["id"] != bot.get("id") and (("web" not in missing) or set(effective_tools(b.get("allowed_tools"))) & _WEB_TOOLS)
              and (("file" not in missing) or set(effective_tools(b.get("allowed_tools"))) & _FILE_TOOLS)]
    can = ", ".join(_capabilities(tools)) or "nothing but reasoning over the instruction text"
    return (f"{bot['name']} cannot do this: it can only {can}, and this step needs {' and '.join(missing)} access. "
            + (f"Bots that can: {', '.join(others)}." if others else "No bot in the roster can, so do this yourself."))


_BOT_CTX_SIZE = 32768


def _bot_ctx_size(bot):
    """The context a bot's turn asks for: the default, or less when its model cannot take that much."""
    try:
        import model_profile
        cap = model_profile.max_context(model_profile.get_facts(bot.get("provider") or "", bot.get("endpoint_url") or "", bot.get("api_key") or "", bot["model"]))
    except Exception:
        cap = None
    return min(_BOT_CTX_SIZE, cap) if cap else _BOT_CTX_SIZE

_BOT_MAX_ROUNDS = 200  # absolute outer safety net; per-tool-type limits below are what actually catches an exploration binge in practice

# Grouped, independently-budgeted counters -- the flat MAX_ROUNDS ceiling
# let a bot burn dozens of bash_read_only/list_files calls without ever reaching
# append_scratch_note, since nothing distinguished "still investigating"
# from "stuck re-exploring." Each group gets its own warn/hard-stop
# count instead, same structural pattern as Athena2's own exploration
# gate (_get_mode_tools) -- append_scratch_note itself is never in a
# group, so it's always reachable regardless of what else got throttled.

_BOT_TOOL_TYPE_GROUPS = {
    "explore": {"bash_read_only", "list_files", "read_file", "search_codebase", "find_definition", "find_references", "type_info"},
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


def _check_bot_tool_gate(group, tool_type_hard_stopped):
    """Hard-stop enforcement for the outer (non-PTC) tool-call loop in
    _call_bot_endpoint. Used to happen by omitting a hard-stopped group's
    tools from the schema sent to the model each round -- but the schema
    is now built once for the whole turn (see tool_schemas above), so
    this checks at call time instead, matching the same message
    _execute_bot_tool_program already uses for calls made inside a PTC
    script. Returns an error dict if `group` is currently hard-stopped,
    else None. A standalone function so it's independently testable."""
    if group and group in tool_type_hard_stopped:
        return {"error": f"BLOCKED: '{group}' tools are no longer available this turn -- call append_scratch_note with what you have."}
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



def _execute_bot_tool_program(code, workspace, bot, session_key, allowed_tools, tool_type_counts, tool_type_hard_stopped, seen_fingerprints, search_url=""):
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
            log_trace_event(session_key, "tool_call", tool=name, args=args, via="run_tool_program")
            sub_call_count += 1
            group = _bot_tool_type_group(name)
            if sub_call_count > _PTC_MAX_SUBCALLS:
                blocked_msg = f"BLOCKED: this program has made more than {_PTC_MAX_SUBCALLS} tool calls -- stop and call append_scratch_note with what you have."
            elif group and group in tool_type_hard_stopped:
                blocked_msg = f"BLOCKED: '{group}' tools are no longer available this turn -- call append_scratch_note with what you have."
            else:
                blocked_msg = None
            if blocked_msg:
                log_trace_event(session_key, "tool_blocked", tool=name, success=False, output={"error": blocked_msg}, via="run_tool_program")
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
                result = _execute_bot_tool_call(fake_tc, workspace, bot, session_key, search_url)
                seen_fingerprints.append(fingerprint)
                if len(seen_fingerprints) > 20:
                    seen_fingerprints.pop(0)
            _result_error = result.get("error") if isinstance(result, dict) else None
            log_trace_event(
                session_key, "tool_blocked" if isinstance(_result_error, str) and _result_error.startswith("BLOCKED:") else "tool_result",
                tool=name, success=_result_error is None, output=result, via="run_tool_program",
            )
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
    from main import RAG_TOOL_SCHEMAS  # deferred: main.py imports bots.py at module load
    pool = (
        bash_tools.BASH_TOOL_SCHEMAS + file_tools.FILE_TOOL_SCHEMAS + RAG_TOOL_SCHEMAS + lsp_tools.LSP_TOOL_SCHEMAS
        + web_tools.WEB_TOOL_SCHEMAS + lcm_client.get_lcm_tools()
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



def _execute_bot_tool_call(tool_call, workspace, bot, session_key, search_url=""):
    """Dispatch one tool call a bot actually made -- a thin wrapper over
    the shared main.dispatch_tool (see the plan-first/dispatch-unification
    work), gated by BOT_ALLOWED_TOOL_NAMES. append_scratch_note is handled
    here directly, before that gate, since it's bot-specific and was never
    part of BOT_ALLOWED_TOOL_NAMES to begin with."""
    from main import dispatch_tool, ToolContext  # deferred: main.py imports bots.py at module load
    fn = tool_call.get("function", {})
    name = fn.get("name", "")
    logger.debug(f"bot tool call: {bot.get('name', '?')} -> {name}")
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if name == BOT_SCRATCH_TOOL_NAME:
        return _append_scratch_note(bot["name"], args.get("content", ""))
    ctx = ToolContext(
        workspace=workspace,
        search_url=search_url,
        bot=bot,
        session_key=session_key,
        passthrough_session_id=session_key,
        require_athena_bots_session=False,
    )
    return dispatch_tool(name, args, ctx, allowed_names=BOT_ALLOWED_TOOL_NAMES)
