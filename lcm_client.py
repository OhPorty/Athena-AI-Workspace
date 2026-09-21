import os
import sys
import time
import signal
import atexit
import ctypes
import subprocess

import httpx

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


def stop_bundled_lcm():
    global _lcm_process
    if _lcm_process and _lcm_process.poll() is None:
        print("[Athena] Stopping bundled LCM...", flush=True)
        _lcm_process.terminate()
        try:
            _lcm_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _lcm_process.kill()


def _handle_shutdown_signal(signum, frame):
    stop_bundled_lcm()
    sys.exit(0)


def start_bundled_lcm():
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
    atexit.register(stop_bundled_lcm)
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


def send_to_lcm(session_id: str, role: str, content: str, model: str = None, has_image: bool = False, thinking: str = None, tool_calls: list = None):
    """Returns the real LCM message ID on success, or None on failure --
    the ID is what ratings attach to, since it's the one stable
    identifier that survives across page reloads and session history
    reloads (unlike a frontend array index, which is meaningless once
    messages get re-fetched from LCM in a different order/subset).

    thinking/tool_calls are optional and only meaningful for an
    assistant message -- passing them lets history reloads (this
    session or the Bots/Athena2 screens) show the same thinking and
    tool-call detail a live stream shows, instead of losing it the
    moment the tab closes.

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
            "thinking": thinking,
            "tool_calls": tool_calls,
        }, timeout=30)
        if resp.status_code == 200:
            return resp.json().get("id")
        print(f"[Athena] send_to_lcm got HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
    except Exception as e:
        print(f"[Athena] send_to_lcm failed: {e}", flush=True)
    return None
