"""Thin wrapper around the laya package (convaiinnovations/laya-typed-decisions).

All policy -- thresholds, what to do with a result -- stays in main.py. This
module only answers Laya questions and normalizes any failure (load error,
inference error) to None so callers can gracefully skip the check rather
than crash a delegation step over an optional guardrail.

Device is hardcoded to CPU: a standalone benchmark showed CPU-only inference
is already fast enough (150-360ms/call) for this to run inline, and the local
GPU is already fully occupied by an existing Ollama process on this machine.
"""
import threading

_agent = None
_agent_lock = threading.Lock()
_load_failed = False  # cache a load failure so we don't retry every call


def _get_agent():
    global _agent, _load_failed
    if _agent is not None or _load_failed:
        return _agent
    with _agent_lock:
        if _agent is None and not _load_failed:
            try:
                import laya
                _agent = laya.load("convaiinnovations/laya-typed-decisions", device="cpu")
            except Exception as e:
                print(f"[LAYA] failed to load: {e!r}", flush=True)
                _load_failed = True
    return _agent


def check_single_topic(instruction: str):
    """Returns the probability (0-1) that `instruction` is about exactly one
    single, focused topic, or None if Laya is unavailable/erroring."""
    agent = _get_agent()
    if agent is None:
        return None
    try:
        q = {"single_topic": {"type": "noul", "instructions": "Is `instruction` about exactly one single, focused topic (not multiple separate asks)?"}}
        r = agent.predict({"instruction": instruction}, q)
        return r["answers"]["single_topic"]["noul"]
    except Exception as e:
        print(f"[LAYA] check_single_topic failed: {e!r}", flush=True)
        return None


def check_note_accurate(note: str, tool_call_history: list):
    """Returns the probability (0-1) that `note` accurately reflects
    `tool_call_history` with no invented claims, or None if unavailable/erroring."""
    agent = _get_agent()
    if agent is None:
        return None
    try:
        q = {"accurate": {"type": "noul", "instructions": "Does `note` accurately reflect what `tool_call_history` actually shows, without adding claims that aren't in it?"}}
        r = agent.predict({"tool_call_history": tool_call_history, "note": note}, q)
        return r["answers"]["accurate"]["noul"]
    except Exception as e:
        print(f"[LAYA] check_note_accurate failed: {e!r}", flush=True)
        return None
