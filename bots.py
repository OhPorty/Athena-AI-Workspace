import os
import re
import sys
import json
import time
import select
import sqlite3
import hashlib
import subprocess
import threading
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

import settings
import activity
import task_context
import laya_gate
import lcm_client
import host_locks
from host_locks import OLLAMA_URL
import bash_tools
import file_tools
import lsp_tools
import web_tools

router = APIRouter()


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


@router.get("/api/bots")
def list_bots():
    conn = _bots_conn()
    try:
        rows = conn.execute(f"SELECT {_BOT_COLUMNS} FROM bots ORDER BY created_at ASC").fetchall()
        return [_row_to_bot(r) for r in rows]
    finally:
        conn.close()


@router.post("/api/bots")
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


@router.put("/api/bots/{bot_id}")
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


@router.delete("/api/bots/{bot_id}")
def delete_bot(bot_id: int):
    conn = _bots_conn()
    try:
        conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))
        conn.commit()
        return {"id": bot_id}
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
    from main import _stream_completion, _SUB_AGENT_TIMEOUT  # deferred: main.py imports bots.py at module load
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
    host_lock = host_locks.get_host_lock(host_locks.endpoint_host(bot.get("endpoint_url")))
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



def _message_bot_tool(bot_id, content):
    import rooms  # deferred: rooms.py imports bots.py at module load
    conn = _bots_conn()
    try:
        room_row = conn.execute(
            "SELECT " + rooms._ROOM_COLUMNS + " FROM rooms WHERE kind = 'dm' AND human_party = 'athena' AND member_bot_ids_json = ?",
            (json.dumps([bot_id]),)
        ).fetchone()
        if room_row:
            room = rooms._row_to_room(room_row)
        else:
            now = time.time()
            cur = conn.execute(
                "INSERT INTO rooms (kind, human_party, label, member_bot_ids_json, created_at, last_active) VALUES ('dm', 'athena', ?, ?, ?, ?)",
                (f"Athena & bot #{bot_id}", json.dumps([bot_id]), now, now)
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
    current_settings = settings.load_settings()
    defaults = current_settings.get("bot_creation_defaults") or {}
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
    from main import _search_codebase  # deferred: main.py imports bots.py at module load
    import rooms  # deferred: rooms.py imports bots.py at module load
    if name == "bash":
        return bash_tools.execute_readonly_bash(args.get("command", ""), args.get("args", []), workspace)
    if name == "list_files":
        return file_tools.list_files(workspace, args.get("path", "."))
    if name == "read_file":
        return file_tools.read_file(workspace, args.get("path", ""), args.get("offset", 0))
    if name == "search_codebase":
        return _search_codebase(args.get("query", ""), args.get("limit", 3))
    if name == "find_definition":
        return lsp_tools.get_lsp_client(workspace).find_definition(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
    if name == "find_references":
        return lsp_tools.get_lsp_client(workspace).find_references(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
    if name == "type_info":
        return lsp_tools.get_lsp_client(workspace).type_info(args.get("path", ""), args.get("line", 0), args.get("symbol", ""))
    if name == "web_search":
        return web_tools.web_search(None, args.get("query", ""))
    if name == "web_fetch":
        return web_tools.web_fetch(args.get("url", ""))
    if name == "list_bots":
        return _list_bots_tool()
    if name == "list_rooms":
        return rooms._list_rooms_tool()
    if name == "message_bot":
        return _message_bot_tool(args.get("bot_id"), args.get("content", ""))
    if name == "read_room_messages":
        return rooms._read_room_messages_tool(args.get("room_id"))
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
