import os
import time
import shutil
import hashlib
import contextvars

# Public (no leading underscore) -- callers outside this module build
# scratch-file paths directly from this (os.path.join(BOT_TASK_FOLDER_ROOT,
# task_hash, ...)), so it's real API, not a private implementation detail.
BOT_TASK_FOLDER_ROOT = os.path.join(os.path.dirname(__file__), "bot_task_scratch")
_BOT_TASK_FOLDER_MAX = 50

# Scopes scratch-file/task-folder access to the current delegation task.
# Shared across main.py (chat_stream's own dispatch checks whether a
# delegation task is already active) and bots.py/delegation_jobs.py
# (every delegation tool reads/sets this) -- given its own home here
# instead of living in either of those two modules, so neither has to
# import the other just to reach a plain contextvars.ContextVar, which
# are safe to import and share across modules as-is.
current_task_hash = contextvars.ContextVar("current_task_hash", default=None)


def get_task_hash(session_id):
    h = current_task_hash.get()
    if h is not None:
        return h
    h = hashlib.md5(f"{session_id}:{time.time()}".encode()).hexdigest()[:12]
    current_task_hash.set(h)
    os.makedirs(os.path.join(BOT_TASK_FOLDER_ROOT, h), exist_ok=True)
    _evict_old_task_folders()
    return h


def _evict_old_task_folders():
    os.makedirs(BOT_TASK_FOLDER_ROOT, exist_ok=True)
    entries = [os.path.join(BOT_TASK_FOLDER_ROOT, d) for d in os.listdir(BOT_TASK_FOLDER_ROOT)]
    entries = [d for d in entries if os.path.isdir(d)]
    if len(entries) > _BOT_TASK_FOLDER_MAX:
        entries.sort(key=lambda d: os.path.getmtime(d))
        for old in entries[:len(entries) - _BOT_TASK_FOLDER_MAX]:
            shutil.rmtree(old, ignore_errors=True)
