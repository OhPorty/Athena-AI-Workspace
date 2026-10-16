"""The engines behind Athena's tools that are new in the rebuilt tool list: run, delete, search_files, ask_user, remember,
research, schedule, check, notify.

Each takes (args, ctx) and returns an ordinary result dict. A tool that needs the person (every removal, and ask_user) does not
wait here: it returns {"_approval": {...}} and the chat loop shows the request, waits for the answer, then calls the tool again
with ctx.approved set (or, for a question, uses the answer as the result).
"""
import json
import os
import re
import time
from datetime import datetime, timedelta

import httpx

import lcm_client
import ownership
import safety
import workspace_ops
from logging_setup import logger

NOT_AVAILABLE_NOW = "Nobody is here to answer, so this cannot be asked. Decide yourself and say what you assumed."


def _approval(title, summary="", items=None, reason="", danger=False, undo_note="", kind="approval", options=None):
    return {"_approval": {"kind": kind, "title": title, "summary": summary, "items": items or [], "reason": (reason or "")[:500],
                          "danger": danger, "undo_note": undo_note, "options": options or []}}


def _snapshot(ctx):
    return safety.ensure_turn_snapshot(getattr(ctx, "turn_state", None), ctx.workspace, ctx.session_id)


def _undo_note(ctx):
    return "A copy of the workspace is saved first, and you can undo this turn afterwards." if ctx.workspace else ""


# ---------- shell ----------

def run_tool(args, ctx):
    if args.get("check") is not None or args.get("stop") is not None:
        if args.get("stop") is not None:
            return workspace_ops.stop_process(args["stop"])
        return workspace_ops.check_process(args["check"])
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return {"error": "Give the command to run, for example command: \"npm install\". To look at a background command give check: <id>; to stop one, stop: <id>."}
    if not ctx.workspace:
        return {"error": "run needs a workspace. Ask the person to bind one."}
    reasons = workspace_ops.removal_reasons(command, ctx.workspace)
    if reasons and not ctx.approved:
        if not getattr(ctx, "interactive", True):
            return {"error": "That command removes files, and nobody is here to approve it, so it was not run."}
        targets = workspace_ops.removal_targets(command, ctx.workspace)
        items = targets if targets else [f"Command: {command}"]
        summary = "This command removes things: " + "; ".join(reasons) + "." + ("" if targets else " The exact files cannot be listed ahead of time.")
        return _approval("Athena wants to run a command that deletes files", summary, items, args.get("reason", ""), True, _undo_note(ctx))
    if not workspace_ops.is_read_only(command):
        _snapshot(ctx)
    if args.get("background"):
        return workspace_ops.start_background(command, ctx.workspace)
    before = workspace_ops.watch(ctx.workspace) if not workspace_ops.is_read_only(command) else None
    result = workspace_ops.run_command(command, ctx.workspace, args.get("timeout") or workspace_ops.DEFAULT_TIMEOUT)
    changed = workspace_ops.changes_since(before, ctx.workspace)
    if changed and isinstance(result, dict):
        result["files_changed"] = changed
    if safety.touches_athena(ctx.workspace):
        result = _with_syntax_check(result, ctx)
    return result


def _with_syntax_check(result, ctx):
    """A command inside Athena's own folder may have changed her Python files: compile them now."""
    bad = []
    for root, dirs, files in os.walk(safety.ATHENA_ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "venv", ".venv", "node_modules", "__pycache__", "file_backups", "logs")]
        for name in files:
            if name.endswith(".py"):
                problem = safety.check_edited_file(os.path.join(root, name))
                if problem:
                    bad.append({"file": os.path.relpath(os.path.join(root, name), safety.ATHENA_ROOT), **problem})
    if bad and isinstance(result, dict):
        result["athena_python_errors"] = bad[:10]
    return result


def delete_tool(args, ctx):
    plan = workspace_ops.plan_delete(args.get("paths"), ctx.workspace)
    if "error" in plan:
        return plan
    if not ctx.approved:
        if not getattr(ctx, "interactive", True):
            return {"error": "Nobody is here to approve a deletion, so nothing was deleted."}
        n = len(plan["items"])
        return _approval(f"Athena wants to delete {n} item{'s' if n != 1 else ''}", "These are moved to a trash folder inside the workspace, not destroyed.",
                         plan["items"], args.get("reason", ""), True, _undo_note(ctx))
    _snapshot(ctx)
    return workspace_ops.do_delete(plan["resolved"], ctx.workspace)


def search_files_tool(args, ctx):
    return workspace_ops.search_files(ctx.workspace, args.get("pattern", ""), args.get("path") or ".", bool(args.get("names_only")), min(int(args.get("limit") or 40), 100))


# ---------- asking the person ----------

def ask_user_tool(args, ctx):
    if not getattr(ctx, "interactive", True):
        return {"error": NOT_AVAILABLE_NOW}
    question = str(args.get("question") or "").strip()
    if not question:
        return {"error": "Give the question to ask."}
    options = args.get("options")
    options = [str(o).strip()[:120] for o in options if str(o).strip()][:6] if isinstance(options, list) else []
    return _approval(question[:300], str(args.get("detail") or "")[:600], kind="question", options=options)


# ---------- memory ----------

def remember_tool(args, ctx):
    fact = re.sub(r"\s+", " ", str(args.get("fact") or "")).strip()
    if len(fact) < 6:
        return {"error": "Give the fact to remember as a short, complete sentence."}
    fact = fact[:400]
    try:
        existing = ownership.own_facts(httpx.get(f"{lcm_client.LCM_URL}/facts", timeout=10).json())
        if any(re.sub(r"\s+", " ", (f.get("content") or "")).strip().lower() == fact.lower() for f in existing):
            return {"saved": False, "note": "That is already remembered."}
        resp = httpx.post(f"{lcm_client.LCM_URL}/facts", json={"content": fact, "source_session_id": ctx.session_id, "manual": False}, timeout=10)
        if resp.status_code == 200:
            ownership.record_fact(resp.json()["id"])
            return {"saved": True, "fact": fact, "note": "It will be part of what Athena knows in every chat, and the person can remove it on the Memory page."}
        return {"error": f"The memory service answered HTTP {resp.status_code}."}
    except Exception as e:
        return {"error": f"The memory service is unreachable: {e}"}


# ---------- research ----------

def research_tool(args, ctx):
    import research
    question = str(args.get("question") or "").strip()
    if not question:
        return {"error": "Give the question to research."}
    depth = args.get("depth") if args.get("depth") in research.DEPTHS else "quick"
    req = ctx.req
    if req is None or not getattr(req, "model", ""):
        return {"error": "research needs the current model to be known."}
    model = {"model": req.model, "label": req.model, "provider": getattr(req, "provider", "") or "", "endpoint_url": getattr(req, "endpoint_url", "") or "",
             "api_key": getattr(req, "api_key", "") or ""}
    started = research.start_run(question, depth, model, wait=True)
    if "error" in started:
        return started
    run = research.get_run(started["id"])
    if run is None or run["status"] != "done":
        return {"error": (run or {}).get("error") or "The research did not finish.", "run_id": started["id"]}
    verified = [c for c in run["claims"] if c.get("supported")]
    report = run["report"]
    return {"report": report[:7000] + ("\n[report shortened; the full one is on the Research page]" if len(report) > 7000 else ""),
            "pages_read": len([s for s in run["sources"] if s["status"] == "read"]), "verified_claims": len(verified),
            "saved_as": "Also saved on the Research page, where the person can read it with the evidence.", "run_id": started["id"]}


# ---------- scheduling ----------

_DAYS = {"mon": "MO", "tue": "TU", "wed": "WE", "thu": "TH", "fri": "FR", "sat": "SA", "sun": "SU"}


def _parse_when(text):
    text = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        target = datetime.now().replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
        return target if target > datetime.now() else target + timedelta(days=1)
    return None


def schedule_tool(args, ctx):
    import task_scheduler
    action = str(args.get("action") or "create").lower()
    if action == "list":
        return {"tasks": [{k: t[k] for k in ("id", "prompt", "schedule_type", "status", "next_run_at", "recurrence")} for t in task_scheduler.list_tasks()]}
    if action in ("cancel", "delete"):
        if not isinstance(args.get("id"), int):
            return {"error": "Give the id of the task to cancel (see action: list)."}
        task_scheduler.delete_task(args["id"])
        return {"cancelled": args["id"]}
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"error": "Give prompt: what Athena should do each time the task runs."}
    repeat = str(args.get("repeat") or "").lower()
    when = None
    if isinstance(args.get("in_minutes"), (int, float)) and args["in_minutes"] > 0:
        when = datetime.now() + timedelta(minutes=float(args["in_minutes"]))
    elif args.get("at"):
        when = _parse_when(args["at"])
        if when is None:
            return {"error": "at must look like 2026-10-20 09:30, or 09:30 for the next 9:30."}
    req = ctx.req
    base = {"prompt": prompt[:2000], "session_label": str(args.get("label") or prompt)[:60], "model": getattr(req, "model", "") or "",
            "endpoint_url": getattr(req, "endpoint_url", "") or None, "enabled_tools": ["web_search", "web_fetch", "read_file"]}
    if repeat in ("daily", "weekly", "monthly"):
        time_of_day = (when or datetime.now().replace(hour=9, minute=0)).strftime("%H:%M")
        days = [_DAYS[d[:3].lower()] for d in (args.get("days") or []) if str(d)[:3].lower() in _DAYS]
        task = task_scheduler.create_task(task_scheduler.TaskIn(
            schedule_type="recurring", recurrence={"frequency": repeat, "interval": 1, "days_of_week": days, "time_of_day": time_of_day, "end": {"type": "never"}}, **base))
    elif when is not None:
        task = task_scheduler.create_task(task_scheduler.TaskIn(schedule_type="once", run_at=when.timestamp(), **base))
    else:
        return {"error": "Say when: in_minutes, or at (a date and time), or repeat (daily, weekly or monthly)."}
    if not isinstance(task, dict):
        return {"error": "This account cannot schedule that (the model or folder is not available to it)."}
    return {"scheduled": task["id"], "next_run": datetime.fromtimestamp(task["next_run_at"]).strftime("%Y-%m-%d %H:%M") if task.get("next_run_at") else None,
            "note": "It runs with read-only tools (web search, web fetch, read file). The person can change that on the Tasks page."}


# ---------- checking ----------

def _python_syntax_errors(workspace):
    bad = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in workspace_ops._NOISE_DIRS and safety.classify(os.path.join(root, d)) != "secret"]
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        compile(f.read(), path, "exec")
                except SyntaxError as e:
                    bad.append({"file": os.path.relpath(path, workspace), "line": e.lineno, "message": e.msg})
                except (OSError, UnicodeDecodeError):
                    continue
    return bad


def check_tool(args, ctx):
    ws = ctx.workspace
    if not ws:
        return {"error": "check needs a workspace."}
    what = str(args.get("what") or "tests").lower()
    if what == "syntax" or safety.touches_athena(ws) and what == "tests":
        errors = _python_syntax_errors(os.path.realpath(ws))
        result = {"checked": "Python syntax of every .py file", "syntax_errors": errors, "ok": not errors}
        if safety.touches_athena(ws):
            result["note"] = ("This folder is Athena herself. Her test suite needs her own Python environment, which the sandbox does not have, so only syntax "
                              "was checked. Ask the person to run `venv/bin/python -m pytest -q` before restarting Athena.")
        return result
    root = os.path.realpath(ws)
    command = None
    pkg = os.path.join(root, "package.json")
    if os.path.isfile(pkg):
        try:
            if "test" in (json.load(open(pkg)).get("scripts") or {}):
                command = "npm test --silent"
        except (OSError, ValueError):
            pass
    if command is None and (os.path.isdir(os.path.join(root, "tests")) or os.path.isfile(os.path.join(root, "pytest.ini")) or os.path.isfile(os.path.join(root, "pyproject.toml"))):
        command = "python3 -m pytest -q -x --no-header 2>&1 | tail -40"
    if command is None:
        return {"error": "No tests were found here (no package.json test script and no tests/ folder). Tell me the command to use with run, or check what: \"syntax\"."}
    _snapshot(ctx)
    result = workspace_ops.run_command(command, ws, 300)
    if isinstance(result, dict) and "exit_code" in result:
        result["passed"] = result["exit_code"] == 0
    return result


# ---------- notifying ----------

def notify_tool(args, ctx):
    import push_notify
    message = str(args.get("message") or "").strip()
    if not message:
        return {"error": "Give the message to send."}
    return push_notify.send(str(args.get("title") or "Athena")[:80], message[:300])


HANDLERS = {"run": run_tool, "delete": delete_tool, "search_files": search_files_tool, "ask_user": ask_user_tool, "remember": remember_tool,
            "research": research_tool, "schedule": schedule_tool, "check": check_tool, "notify": notify_tool}


def handle(name, args, ctx):
    """-> (True, result) when this module owns the tool, else (False, None)."""
    fn = HANDLERS.get(name)
    if fn is None:
        return False, None
    try:
        return True, fn(args if isinstance(args, dict) else {}, ctx)
    except Exception as e:
        logger.error(f"tool {name} failed: {e!r}")
        return True, {"error": f"{name} failed: {e}"}
