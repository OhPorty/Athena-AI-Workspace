"""Athena's System 1 model: a small decision model (tev1, served by Ollama) that answers typed questions
about a piece of text with probabilities, fast and without generating free text.

It is built in, not a chat model: it is not picked from the model list and does not need enabling there.
Defaults to tev1:0.8b on the local Ollama; `system1_model` and `system1_endpoint_url` in settings override that.
Needs Ollama 0.35 or later (the /v1/systemone endpoint).
"""
import time

import httpx

import host_locks
import settings

DEFAULT_MODEL = "tev1:0.8b"
_PROBE_TTL = 60
_probe = {"at": 0.0, "ok": False}


def config():
    s = settings.load_settings()
    endpoint = (s.get("system1_endpoint_url") or host_locks.OLLAMA_URL.split("/api/")[0]).rstrip("/")
    return {"model": s.get("system1_model") or DEFAULT_MODEL, "endpoint_url": endpoint}


def decide(state, questions, cfg=None):
    """-> {question_name: answer}, where an answer carries "noul" (true/false probability) or "probabilities" (choice)."""
    cfg = cfg or config()
    with host_locks.get_host_lock(host_locks.endpoint_host(cfg["endpoint_url"])):
        resp = httpx.post(f"{cfg['endpoint_url']}/v1/systemone", json={"model": cfg["model"], "state": state, "questions": questions}, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"The System 1 model ({cfg['model']}) answered HTTP {resp.status_code}. {resp.text[:200]} (It needs Ollama 0.35 or later and `ollama pull {cfg['model']}`.)")
    return resp.json()["answers"]


def available():
    """True if the System 1 model answers right now (remembered briefly so a page of runs does not probe each time)."""
    if time.time() - _probe["at"] < _PROBE_TTL:
        return _probe["ok"]
    try:
        decide("ok", {"q": {"type": "noul", "instructions": "Is the state non-empty?"}})
        ok = True
    except Exception:
        ok = False
    _probe.update(at=time.time(), ok=ok)
    return ok
