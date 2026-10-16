#!/usr/bin/env python3
"""Put Athena (or any workspace Athena has edited) back the way it was, without needing Athena to start.

Before a turn changes anything, Athena saves a snapshot of the workspace under file_backups/. This script reads those snapshots using
only the Python standard library, so it works even when an edit broke Athena herself.

    python3 recover.py                    list the latest snapshots of Athena's own folder
    python3 recover.py --workspace PATH   list the snapshots of another workspace
    python3 recover.py ID                 restore the snapshot whose id starts with ID (the newest one: use "latest")

Files the snapshot does not have are moved into .athena/trash, not deleted. Files that are overwritten are copied to
file_backups/recovery-<time>/ first, so a restore can be undone too.
"""
import argparse
import gzip
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUPS = os.path.join(HERE, "file_backups")
INDEX = os.path.join(BACKUPS, "turns.jsonl")
MANIFEST = os.path.join(BACKUPS, "manifest.jsonl")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build", ".next", ".athena"}


def read_index(workspace):
    if not os.path.isfile(INDEX):
        return []
    out = []
    with open(INDEX, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("workspace") == workspace:
                out.append(entry)
    return list(reversed(out))


def entries_of(snapshot_id):
    found = {}
    with open(MANIFEST, encoding="utf-8") as f:
        for line in f:
            if snapshot_id in line:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("snapshot_id") == snapshot_id:
                    found[entry["path"]] = entry
    return found


def walk(root):
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                yield path


def restore(workspace, snapshot):
    wanted = entries_of(snapshot["snapshot_id"])
    if not wanted:
        sys.exit("That snapshot has no saved files.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safety_copy = os.path.join(BACKUPS, f"recovery-{stamp}")
    restored = 0
    for path, entry in wanted.items():
        object_path = os.path.join(BACKUPS, "objects", entry["hash"][:2], entry["hash"][2:] + ".gz")
        if not os.path.isfile(object_path):
            print(f"  missing from the store, skipped: {path}")
            continue
        with gzip.open(object_path, "rb") as f:
            content = f.read()
        if os.path.isfile(path):
            with open(path, "rb") as f:
                if f.read() == content:
                    continue
            target = os.path.join(safety_copy, os.path.relpath(path, workspace))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(path, target)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        restored += 1
    aside = os.path.join(workspace, ".athena", "trash", f"recover-{stamp}")
    moved = 0
    for path in list(walk(workspace)):
        if path not in wanted:
            target = os.path.join(aside, os.path.relpath(path, workspace))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(path, target)
            moved += 1
    print(f"Restored {restored} file(s); moved {moved} newer file(s) aside into {os.path.relpath(aside, workspace) if moved else '(none)'}.")
    if restored:
        print(f"The versions that were replaced are in {os.path.relpath(safety_copy, HERE)}.")


def main():
    parser = argparse.ArgumentParser(description="Restore a workspace from Athena's snapshots.")
    parser.add_argument("snapshot", nargs="?", help="start of a snapshot id, or 'latest'")
    parser.add_argument("--workspace", default=HERE, help="the folder to restore (default: Athena's own folder)")
    args = parser.parse_args()
    workspace = os.path.realpath(args.workspace)
    snapshots = read_index(workspace)
    if not snapshots:
        sys.exit(f"No snapshots of {workspace} were found in {BACKUPS}.")
    if not args.snapshot:
        print(f"Snapshots of {workspace}, newest first:\n")
        for entry in snapshots[:20]:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["ts"]))
            print(f"  {entry['snapshot_id'][:10]}  {when}  {entry.get('files', '?'):>5} files  {entry.get('note', '')}")
        print("\nTo go back to one:  python3 recover.py <id>      (or: python3 recover.py latest)")
        return
    chosen = snapshots[0] if args.snapshot == "latest" else next((e for e in snapshots if e["snapshot_id"].startswith(args.snapshot)), None)
    if chosen is None:
        sys.exit("No snapshot starts with that id. Run without arguments to list them.")
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(chosen["ts"]))
    answer = input(f"Restore {workspace} to how it was at {when}? [y/N] ")
    if answer.strip().lower() != "y":
        sys.exit("Nothing changed.")
    restore(workspace, chosen)


if __name__ == "__main__":
    main()
