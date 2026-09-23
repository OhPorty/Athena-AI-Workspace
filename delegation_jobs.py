import time
import threading

import task_context
import generation_streaming
import bots
from logging_setup import logger

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
    from main import chat_stream, ChatIn  # deferred: main.py imports delegation_jobs.py at module load
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
            logger.warning(f"failed to deliver async result for session {session_id!r}: {e!r}")



def _next_delegation_job_id():
    with _delegation_jobs_lock:
        _delegation_job_counter[0] += 1
        return str(_delegation_job_counter[0])


def _delegation_step_worker(job_id, task_hash, req_fields, bot_id, instruction):
    task_context.current_task_hash.set(task_hash)  # each thread starts with its own empty context -- see _plan_delegation_tool's own comment on this
    try:
        result = bots._run_delegation_step_tool(bot_id, instruction)
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
    is_compound, reason = bots._instruction_is_compound(instruction)
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
        plan_result = bots._plan_delegation_tool(steps)
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



