"""Is this workspace a small new project being built, or an existing codebase being maintained?

The harness's search-first gate, RAG/LSP/backup tools and tight plan check-ins were
built for maintaining a codebase. In a small project they are pure overhead, so a
workspace with only a handful of files runs the lean "build" tool set instead.

Size, not "has a search index", is the signal: the index is updated whenever the
model edits files, so an index-based test would flip a project from build to
maintain partway through building it. A small project stays small until it isn't.
"""
import os

import settings

BUILD_FILE_LIMIT = 40
_SKIP_DIRS = {"node_modules", "venv", "__pycache__", "dist", "build", "target", "file_backups"}


def count_project_files(root, limit=BUILD_FILE_LIMIT):
    """Files under root, ignoring dot-dirs and dependency/build output; stops early once past limit."""
    count = 0
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        count += sum(1 for f in filenames if not f.startswith("."))
        if count > limit:
            break
    return count


def is_build_task(workspace, mode_setting=None):
    """mode_setting: 'build' / 'maintain' force the answer; anything else (the default) auto-detects."""
    mode_setting = mode_setting or settings.load_settings().get("workspace_mode") or "auto"
    if mode_setting == "build":
        return True
    if mode_setting == "maintain":
        return False
    if not workspace or not os.path.isdir(workspace):
        return False
    return count_project_files(workspace) <= BUILD_FILE_LIMIT
