"""What a model may touch, and how any change is undone.

Three ideas, all enforced in code rather than asked for in prose:

  1. Some things are never available to a model's tools: Athena's own settings, passwords, notes, databases, logs and backups
     (even when the folder a person binds as the workspace is Athena's own folder). A few more (.git, venv, node_modules) can be
     read but not changed.
  2. Before the first change of a turn, the workspace is snapshotted (content-addressed, so unchanged files cost nothing), and
     the person can undo that turn in one click. A standalone recover.py does the same when Athena will not start.
  3. A model may edit Athena herself. After it writes one of her Python files the file is compiled at once, so a syntax error is
     reported to the model immediately instead of being discovered at the next restart.
"""
import gzip
import json
import os
import shutil
import time
import uuid

import ownership
from fastapi import APIRouter, Depends
from pydantic import BaseModel

import backup_tools
from logging_setup import logger

router = APIRouter(dependencies=[Depends(ownership.workspace_guard)])

ATHENA_ROOT = os.path.dirname(os.path.abspath(__file__))
_BACKUP_ROOT = backup_tools._BACKUP_ROOT
TURNS_INDEX = os.path.join(_BACKUP_ROOT, "turns.jsonl")

_SECRET_FILES = {"athena_settings.json", "athena_auth.json", "athena_sessions.json", "athena_notes.json", "athena_vapid.pem"}
_SECRET_DIRS = {"file_backups", "logs"}
_SECRET_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3")
_READONLY_DIRS = {".git", "venv", ".venv", "node_modules"}
# The safety system itself: a model editing Athena may read these, never change them (it could not otherwise be trusted not to
# edit away its own guard rails, however small the mistake).
_READONLY_FILES = {"safety.py", "approvals.py", "workspace_ops.py", "recover.py", "auth.py", "auth_routes.py", "ownership.py", "accounts.py", "user_context.py", "prompt_gate.py", "workspace_view.py"}

_SNAPSHOT_SKIP_DIRS = set(backup_tools._BACKUP_SKIP_DIRS) | {".athena"}
_MAX_FILES = 8000
_MAX_BYTES = 400 * 1024 * 1024
_MAX_FILE_BYTES = 25 * 1024 * 1024
KEEP_SNAPSHOTS = 40

PRIVATE_MESSAGE = "That is private to Athena (her settings, passwords, notes, databases, logs or backups), so tools cannot use it."
READONLY_MESSAGE = "That can be read but not changed by tools (.git, venv, node_modules, and the files that make up Athena's safety system)."


# ---------- what is off limits ----------

def _real(path):
    return os.path.realpath(os.path.expanduser(str(path)))


def classify(path):
    """-> "secret" (no tool may read or change it), "readonly" (read only), or None."""
    p = _real(path)
    parent = os.path.dirname(ATHENA_ROOT)
    if p.startswith(parent + os.sep) and os.path.relpath(p, parent).split(os.sep)[0].startswith("athena_backup"):
        return "secret"                                  # a backup of Athena sitting next to her folder
    if p != ATHENA_ROOT and not p.startswith(ATHENA_ROOT + os.sep):
        return None
    parts = os.path.relpath(p, ATHENA_ROOT).split(os.sep)
    if parts[0] in _SECRET_DIRS:
        return "secret"
    if parts[0] == "user_data":
        # Each extra account's private folder. Only the project folders it works in are ordinary files; its notes and settings are private.
        return None if len(parts) >= 3 and parts[2] == "workspaces" else "secret"
    if any(part in _READONLY_DIRS for part in parts):
        return "readonly"
    if len(parts) == 1 and parts[0] in _SECRET_FILES:
        return "secret"
    if len(parts) == 1 and parts[0] in _READONLY_FILES:
        return "readonly"
    if parts[-1].lower().endswith(_SECRET_SUFFIXES):
        return "secret"
    return None


def check_model_path(path, write=False):
    """-> an error message if a model's tool may not use this path (for reading, or for changing it), else None."""
    kind = classify(path)
    if kind == "secret":
        return PRIVATE_MESSAGE
    if kind == "readonly" and write:
        return READONLY_MESSAGE
    return None


def touches_athena(workspace):
    """True when the workspace is Athena's own folder, a folder inside it, or one that contains it."""
    if not workspace:
        return False
    w = _real(workspace)
    return w == ATHENA_ROOT or w.startswith(ATHENA_ROOT + os.sep) or ATHENA_ROOT.startswith(w + os.sep)


def container_mounts(workspace):
    """Extra `docker run` arguments that hide Athena's private files from a command running in the sandbox and make her
    read-only folders read-only, when the mounted workspace includes them. Empty for any ordinary project folder."""
    if not touches_athena(workspace):
        return []
    args = []
    for name in sorted(_SECRET_FILES):
        path = os.path.join(ATHENA_ROOT, name)
        if os.path.isfile(path):
            args += ["-v", f"/dev/null:{path}:ro"]
    for name in sorted(_SECRET_DIRS):
        path = os.path.join(ATHENA_ROOT, name)
        if os.path.isdir(path):
            args += ["--mount", f"type=tmpfs,destination={path},tmpfs-size=4096,readonly"]
    for root, dirs, files in os.walk(ATHENA_ROOT):
        dirs[:] = [d for d in dirs if d not in _READONLY_DIRS and d not in _SECRET_DIRS and d != "__pycache__"]
        for name in files:
            if name.lower().endswith(_SECRET_SUFFIXES):
                args += ["-v", f"/dev/null:{os.path.join(root, name)}:ro"]
    users = os.path.join(ATHENA_ROOT, "user_data")
    if os.path.isdir(users):
        for account in os.listdir(users):
            for private in ("notes.json", "settings.json"):
                path = os.path.join(users, account, private)
                if os.path.isfile(path):
                    args += ["-v", f"/dev/null:{path}:ro"]
    for name in sorted(_READONLY_DIRS | _READONLY_FILES):
        path = os.path.join(ATHENA_ROOT, name)
        if os.path.exists(path):
            args += ["-v", f"{path}:{path}:ro"]
    return args


def check_edited_file(path):
    """After a write inside Athena's own folder: compile a Python file so a syntax error shows up now, not at the next start.
    -> None when fine or not applicable, else {"line", "message"}."""
    p = _real(path)
    if not p.endswith(".py") or not (p.startswith(ATHENA_ROOT + os.sep)):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            compile(f.read(), p, "exec")
    except SyntaxError as e:
        return {"line": e.lineno, "message": f"{e.msg} (line {e.lineno})"}
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return None


# ---------- snapshots ----------

def _walk_workspace(root):
    """Every file a snapshot would hold: (absolute path, size). Skips noise folders and anything private."""
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SNAPSHOT_SKIP_DIRS and classify(os.path.join(current, d)) != "secret"]
        for name in files:
            path = os.path.join(current, name)
            if os.path.islink(path) or classify(path) == "secret":
                continue
            try:
                yield path, os.path.getsize(path)
            except OSError:
                continue


def _append_index(entry):
    os.makedirs(_BACKUP_ROOT, exist_ok=True)
    with open(TURNS_INDEX, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _read_index():
    if not os.path.isfile(TURNS_INDEX):
        return []
    out = []
    with open(TURNS_INDEX, "r", encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return out


def take_snapshot(workspace, session_id="", note=""):
    """Copy every file of the workspace into the content-addressed store. -> {"snapshot_id", "files", "bytes", "skipped"} or
    {"error"}. Too large a workspace is skipped (and said so) rather than made slow."""
    root = _real(workspace)
    if not os.path.isdir(root):
        return {"error": "No such workspace."}
    files = list(_walk_workspace(root))
    total = sum(size for _, size in files)
    if len(files) > _MAX_FILES or total > _MAX_BYTES:
        return {"error": f"The workspace is too large to snapshot ({len(files)} files, {total // (1024 * 1024)} MB).", "skipped": True}
    snapshot_id = uuid.uuid4().hex
    lines, skipped = [], 0
    for path, size in files:
        if size > _MAX_FILE_BYTES:
            skipped += 1
            continue
        try:
            backup_tools._backup_one_file(path, snapshot_id, lines)
        except OSError:
            skipped += 1
    os.makedirs(_BACKUP_ROOT, exist_ok=True)
    with open(os.path.join(_BACKUP_ROOT, "manifest.jsonl"), "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    info = {"snapshot_id": snapshot_id, "workspace": root, "session_id": session_id, "ts": time.time(), "files": len(lines),
            "bytes": total, "skipped": skipped, "note": note}
    _append_index(info)
    _prune(root)
    return info


def ensure_turn_snapshot(turn_state, workspace, session_id=""):
    """Called before any tool that changes things: takes the turn's one snapshot the first time. Never raises."""
    if not workspace or turn_state is None or "snapshot" in turn_state:
        return turn_state.get("snapshot") if turn_state else None
    try:
        info = take_snapshot(workspace, session_id, "before this turn's changes")
    except Exception as e:
        logger.warning(f"snapshot before changes failed: {e!r}")
        info = {"error": str(e)}
    turn_state["snapshot"] = info
    return info


def snapshots_for(workspace):
    root = _real(workspace)
    return [e for e in reversed(_read_index()) if e.get("workspace") == root]


def _prune(root):
    entries = _read_index()
    mine = [e for e in entries if e.get("workspace") == root]
    if len(mine) <= KEEP_SNAPSHOTS:
        return
    drop = {e["snapshot_id"] for e in mine[:-KEEP_SNAPSHOTS]}
    with open(TURNS_INDEX, "w", encoding="utf-8") as f:
        for e in entries:
            if e.get("snapshot_id") not in drop:
                f.write(json.dumps(e) + "\n")


def _snapshot_entries(snapshot_id):
    out = []
    manifest = os.path.join(_BACKUP_ROOT, "manifest.jsonl")
    if not os.path.isfile(manifest):
        return out
    with open(manifest, "r", encoding="utf-8") as f:
        for line in f:
            if snapshot_id in line:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("snapshot_id") == snapshot_id:
                    out.append(entry)
    return out


def undo(workspace, snapshot_id=""):
    """Put the workspace back as it was in a snapshot (the latest when none is named). Files made since are moved aside into
    .athena/trash, never deleted, and a snapshot of the current state is taken first, so an undo can itself be undone."""
    root = _real(workspace)
    mine = snapshots_for(root)
    chosen = next((e for e in mine if e["snapshot_id"] == snapshot_id), None) if snapshot_id else (mine[0] if mine else None)
    if chosen is None:
        return {"error": "There is no saved snapshot of this workspace to go back to."}
    entries = _snapshot_entries(chosen["snapshot_id"])
    if not entries:
        return {"error": "That snapshot has no saved files."}
    before = take_snapshot(root, chosen.get("session_id", ""), "before undoing to an earlier snapshot")
    restored = 0
    wanted = {}
    for entry in entries:
        wanted[entry["path"]] = entry
    for path, entry in wanted.items():
        object_path = os.path.join(_BACKUP_ROOT, "objects", entry["hash"][:2], entry["hash"][2:] + ".gz")
        if not os.path.isfile(object_path):
            continue
        with gzip.open(object_path, "rb") as f:
            content = f.read()
        try:
            with open(path, "rb") as f:
                if f.read() == content:
                    continue
        except OSError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        restored += 1
    aside = os.path.join(root, ".athena", "trash", f"undo-{time.strftime('%Y%m%d-%H%M%S')}")
    moved = []
    for path, _ in list(_walk_workspace(root)):
        if path not in wanted:
            target = os.path.join(aside, os.path.relpath(path, root))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(path, target)
            moved.append(os.path.relpath(path, root))
    return {"restored": restored, "moved_aside": moved[:50], "moved_aside_count": len(moved), "snapshot_id": chosen["snapshot_id"],
            "taken_at": chosen["ts"], "kept_aside_in": os.path.relpath(aside, root) if moved else None, "undo_snapshot": before.get("snapshot_id")}


# ---------- routes ----------

class UndoIn(BaseModel):
    workspace: str
    snapshot_id: str = ""


@router.get("/api/workspace/snapshots")
def api_snapshots(workspace: str):
    if not workspace or not os.path.isdir(_real(workspace)):
        return {"snapshots": []}
    return {"snapshots": [{k: e.get(k) for k in ("snapshot_id", "ts", "files", "note")} for e in snapshots_for(workspace)[:20]]}


@router.post("/api/workspace/undo")
def api_undo(req: UndoIn):
    ownership.workspace_guard(req.workspace)
    if not req.workspace or not os.path.isdir(_real(req.workspace)):
        return {"error": "No such workspace."}
    return undo(req.workspace, req.snapshot_id)
