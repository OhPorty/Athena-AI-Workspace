import json
import os
import threading
import time

from loguru import logger

# Two independent, purpose-built sinks -- kept apart at the storage level
# on purpose. `logger` (below) is for "is Athena healthy": human-readable,
# leveled, timestamped operational output. `log_trace_event` is for "what
# did this turn actually do": one structured JSON line per tool call/gate
# decision/turn boundary, meant to be grepped/jq'd by session_id, not read
# top to bottom like a log file. Mixing the two would make both harder to
# use for what they're actually for.
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
ATHENA_LOG_PATH = os.path.join(LOGS_DIR, "athena.log")
TRACE_LOG_PATH = os.path.join(LOGS_DIR, "agent_trace.jsonl")

os.makedirs(LOGS_DIR, exist_ok=True)

logger.remove()  # drop loguru's default stderr handler so ours (below) is the only one, not a duplicate
logger.add(
    lambda msg: print(msg, end="", flush=True),
    level="DEBUG",
    format="<green>{time:HH:mm:ss}</green> <level>{level: <8}</level> <cyan>{name}</cyan> - <level>{message}</level>",
)
logger.add(ATHENA_LOG_PATH, level="DEBUG", rotation="10 MB", retention="7 days", enqueue=True)

_trace_lock = threading.Lock()


def log_trace_event(session_id: str, event_type: str, **fields):
    """Appends one JSON line to the agent trace log -- an append-only,
    per-turn record of what happened (tool calls, results, gate
    rejections, turn boundaries), the DeepSeek-Harness-style event log
    this session has referenced repeatedly. Never raises: a trace-logging
    failure should never take down the actual generation it's observing."""
    ts = time.time()
    record = {"ts": ts, "session_id": session_id, "event": event_type, **fields}
    try:
        line = json.dumps(record, default=str)
    except Exception:
        try:
            line = json.dumps({"ts": ts, "session_id": session_id, "event": event_type, "error": "unserializable fields"})
        except Exception as e:
            logger.warning(f"log_trace_event couldn't even serialize its own fallback record (event={event_type!r} session_id={session_id!r}): {e}")
            return
    try:
        with _trace_lock:
            os.makedirs(os.path.dirname(TRACE_LOG_PATH), exist_ok=True)
            with open(TRACE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        logger.warning(f"log_trace_event failed to write (event={event_type!r} session_id={session_id!r}): {e}")
