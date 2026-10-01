import json
import time
import uuid
import calendar
from datetime import datetime, timedelta
from typing import Optional, List

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

import settings
import skills
import lcm_client
import bash_tools
import file_tools
import web_tools
import generation_streaming
from logging_setup import logger
import db

router = APIRouter()


# ==================== Tasks (scheduled automations) ====================
# A task is a prompt that runs on its own, on a schedule, and posts its
# result into a normal chat session -- unattended, so it needs real,
# explicit handicapping rather than the broad tool access a live,
# supervised chat gets. Every list below is read fresh on each call, so
# newly added tools/skills show up automatically without a restart,
# and (per the safety default) start disabled until explicitly opted
# into on a given task.

def _tasks_conn():
    conn = db.get_conn()
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
    for schema in bash_tools.BASH_TOOL_SCHEMAS + web_tools.WEB_TOOL_SCHEMAS + file_tools.FILE_TOOL_SCHEMAS + lcm_client.get_lcm_tools():
        fn = schema.get("function", {})
        name = fn.get("name")
        if name and name not in seen:
            seen.add(name)
            names.append({"name": name, "description": fn.get("description", "")})
    return names

# Read-only and low-risk, useful for the vast majority of "check
# something and tell me" tasks -- everything else (file writes, bash_read_only,
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

@router.get("/api/tasks/capabilities")
def task_capabilities():
    return {
        "tools": _available_task_tools(),
        "default_enabled_tools": sorted(_DEFAULT_ENABLED_TASK_TOOLS),
        "skills": [{"name": s.get("name"), "description": s.get("description", "")} for s in skills.scan_skills()],
    }

@router.get("/api/tasks")
def list_tasks():
    conn = _tasks_conn()
    try:
        rows = conn.execute(f"SELECT {_TASK_COLUMNS} FROM tasks ORDER BY created_at DESC").fetchall()
        return [_row_to_task(r) for r in rows]
    finally:
        conn.close()

@router.post("/api/tasks")
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

@router.put("/api/tasks/{task_id}")
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

@router.delete("/api/tasks/{task_id}")
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

@router.post("/api/tasks/{task_id}/status")
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
    from main import chat_stream, ChatIn  # deferred: main.py imports task_scheduler.py at module load
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
            logger.warning(f"failed to (re)create session for task {task_id}: {e!r}")
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
            logger.warning(f"failed to bump last_active for task {task_id}: {e!r}")

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
        logger.warning(f"task {task_id} failed to start: {e!r}")
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
                logger.info(f"starting task {task['id']!r}: {task['prompt'][:60]!r}")
                _start_task_run(task)
        except Exception as e:
            logger.warning(f"scheduler loop error: {e!r}")
        time.sleep(30)

