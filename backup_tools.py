import os
import json
import gzip
import time
import uuid
import hashlib

import rag

# Stored next to Athena's own app data (pins.db, tasks.db, etc.), never
# inside a workspace -- so a backup can never be accidentally
# git-committed, wiped if the workspace gets reset/deleted, or clutter
# the actual project directory the user is working in.
_BACKUP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "file_backups")

# Skipped when backing up a whole directory -- either already under
# their own version control, or fully regeneratable, so backing them
# up would just burn storage and time for no real recovery value.
_BACKUP_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build", ".next"}


def _backup_one_file(abs_path, snapshot_id, manifest_lines):
    """The actual per-file content-addressable backup step, factored
    out so both a single-file and a whole-directory backup share the
    exact same logic and guarantees, rather than the directory case
    reimplementing it separately. Appends one manifest line (not yet
    written to disk) to manifest_lines and returns a small per-file
    result dict; the caller decides when to actually flush the batch."""
    with open(abs_path, "rb") as f:
        raw = f.read()
    content_hash = hashlib.sha256(raw).hexdigest()
    objects_dir = os.path.join(_BACKUP_ROOT, "objects", content_hash[:2])
    object_path = os.path.join(objects_dir, content_hash[2:] + ".gz")
    already_existed = os.path.isfile(object_path)
    if not already_existed:
        os.makedirs(objects_dir, exist_ok=True)
        with gzip.open(object_path, "wb") as f:
            f.write(raw)
    manifest_lines.append(json.dumps({
        "path": abs_path,
        "hash": content_hash,
        "timestamp": time.time(),
        "bytes": len(raw),
        "snapshot_id": snapshot_id,
    }))
    return {"path": abs_path, "hash": content_hash, "deduplicated": already_existed, "bytes": len(raw)}


def backup_file(path: str):
    """Content-addressable backup, the same underlying idea as Git's
    own object store: each file's content is hashed (SHA-256) and
    stored at a path derived entirely from that hash, gzip-compressed,
    with a separate append-only manifest recording which path and
    timestamp each hash belongs to. This gives two properties for
    free, as a structural consequence of the design rather than
    special-cased logic to enforce: (1) identical content is only
    ever stored once no matter how many times it gets backed up, and
    (2) a backup taken after a real edit can never collide with and
    silently overwrite one taken before it, since different content
    mathematically produces a different hash and therefore a
    different storage path.

    Not scoped to any workspace -- unlike the other file tools, a
    backup is read-only with respect to its source (it only ever
    copies content into Athena's own storage, never touches the
    original), so the usual workspace-boundary safety check doesn't
    apply the same way here. Works on either a single file or a whole
    directory. For a directory, every file underneath it (skipping
    common noise dirs) is backed up individually using the exact same
    logic, but all tagged with one shared snapshot_id, so a future
    restore tool can recognize them as one coherent snapshot and
    restore the whole directory back to that exact point in time at
    once, rather than as a pile of unrelated individual file backups."""
    try:
        target = os.path.realpath(os.path.expanduser(path))
        manifest_lines = []
        if os.path.isfile(target):
            result = _backup_one_file(target, None, manifest_lines)
        elif os.path.isdir(target):
            snapshot_id = uuid.uuid4().hex
            files_backed_up = []
            for root, dirs, files in os.walk(target):
                dirs[:] = [d for d in dirs if d not in _BACKUP_SKIP_DIRS]
                for name in files:
                    abs_path = os.path.join(root, name)
                    files_backed_up.append(_backup_one_file(abs_path, snapshot_id, manifest_lines))
            result = {
                "path": target,
                "snapshot_id": snapshot_id,
                "files_backed_up": len(files_backed_up),
                "files_deduplicated": sum(1 for f in files_backed_up if f["deduplicated"]),
                "total_bytes": sum(f["bytes"] for f in files_backed_up),
            }
        else:
            return {"error": f"Not a file or directory: {path}"}
        os.makedirs(_BACKUP_ROOT, exist_ok=True)
        manifest_path = os.path.join(_BACKUP_ROOT, "manifest.jsonl")
        with open(manifest_path, "a", encoding="utf-8") as f:
            for line in manifest_lines:
                f.write(line + "\n")
        return result
    except Exception as e:
        return {"error": f"backup_file failed: {e}"}


def _read_backup_manifest():
    manifest_path = os.path.join(_BACKUP_ROOT, "manifest.jsonl")
    if not os.path.isfile(manifest_path):
        return []
    entries = []
    with open(manifest_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _restore_one_entry(entry):
    """Restore a single manifest entry back to its original path,
    backing up whatever's currently there first (via the same
    content-addressable backup_file) so a restore is never a
    one-way, unrecoverable action -- if it turns out to be the wrong
    version, the pre-restore state is itself just another backup to
    restore from."""
    target_path = entry["path"]
    content_hash = entry["hash"]
    object_path = os.path.join(_BACKUP_ROOT, "objects", content_hash[:2], content_hash[2:] + ".gz")
    if not os.path.isfile(object_path):
        return {"path": target_path, "error": f"Backup object missing on disk for hash {content_hash}."}
    pre_restore_backup = None
    if os.path.isfile(target_path):
        pre_restore_backup = _backup_one_file(target_path, None, [])
    os.makedirs(os.path.dirname(target_path), exist_ok=True) if os.path.dirname(target_path) else None
    with gzip.open(object_path, "rb") as f:
        content = f.read()
    with open(target_path, "wb") as f:
        f.write(content)
    try:
        rag.index_codebase(".")
    except Exception as e:
        print(f"[RAG] auto-reindex after restore failed: {e}", flush=True)
    return {
        "path": target_path,
        "restored_hash": content_hash,
        "restored_from_timestamp": entry["timestamp"],
        "bytes": len(content),
        "pre_restore_backup_hash": pre_restore_backup["hash"] if pre_restore_backup else None,
    }


def restore_file(path: str = "", snapshot_id: str = "", restore_hash: str = "", restore_timestamp: float = None):
    if bool(path) == bool(snapshot_id):
        return {"error": "Provide exactly one of 'path' or 'snapshot_id', not both and not neither."}

    entries = _read_backup_manifest()

    if snapshot_id:
        matches = [e for e in entries if e.get("snapshot_id") == snapshot_id]
        if not matches:
            return {"error": f"No backups found for snapshot_id '{snapshot_id}'."}
        results = [_restore_one_entry(e) for e in matches]
        return {"snapshot_id": snapshot_id, "files_restored": len(results), "results": results}

    matches = [e for e in entries if e.get("path") == path]
    if not matches:
        return {"error": f"No backups found for path '{path}'."}
    if restore_hash:
        chosen = next((e for e in matches if e.get("hash") == restore_hash), None)
        if not chosen:
            return {"error": f"No backup found for path '{path}' with hash '{restore_hash}'."}
    elif restore_timestamp is not None:
        eligible = [e for e in matches if e["timestamp"] <= restore_timestamp]
        if not eligible:
            return {"error": f"No backup found for path '{path}' at or before timestamp {restore_timestamp}."}
        chosen = max(eligible, key=lambda e: e["timestamp"])
    else:
        chosen = max(matches, key=lambda e: e["timestamp"])

    return _restore_one_entry(chosen)


BACKUP_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "backup_file",
            "description": "Save a copy of a file's (or an entire directory's) current content before making changes to it, so it can be recovered later if something goes wrong. Works on a single file or a whole directory -- pass a directory path to back up everything inside it (recursively, skipping .git/node_modules/build-output-style directories) as one coherent snapshot. Deduplicated by content -- backing up something whose content hasn't actually changed since a previous backup costs no extra storage. Use this before editing anything whose current state matters and hasn't already been captured (e.g. by version control) this turn.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Absolute path to the file or directory to back up (e.g. /home/user/project or /home/user/project/main.py)."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restore_file",
            "description": "Restore a previously backed-up file (or an entire directory snapshot) back to disk, undoing a change by bringing back an earlier version. Provide EITHER 'path' (restores that one file's most recent backup, or a specific one if 'restore_hash' or 'restore_timestamp' is also given) OR 'snapshot_id' (restores every file from that whole-directory backup at once) -- never both. Whatever is currently at the target path is itself backed up first, automatically, before being overwritten, so a restore is never a one-way action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the single file to restore. Omit if using snapshot_id instead."},
                    "snapshot_id": {"type": "string", "description": "Restore an entire directory snapshot (from a directory backup_file call) back to that exact point in time. Omit if using path instead."},
                    "restore_hash": {"type": "string", "description": "Optional: restore this exact backed-up version of 'path' by its content hash, instead of the most recent one."},
                    "restore_timestamp": {"type": "number", "description": "Optional: restore the most recent backup of 'path' that was taken at or before this unix timestamp, instead of the latest one overall."},
                },
                "required": [],
            },
        },
    },
]
