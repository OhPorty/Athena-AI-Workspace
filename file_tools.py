import os

from fastapi import APIRouter

import rag
from logging_setup import logger

router = APIRouter()


FILE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories at a path within the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to workspace root. Use '.' for the root."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents. Large files are capped at 20000 characters per call -- if the response has has_more: true, call again with offset set to the returned next_offset to continue reading from where you left off, rather than assuming you've seen the whole file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace root."},
                    "offset": {"type": "integer", "description": "Character offset to start reading from. Omit or use 0 to start from the beginning."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file, or overwrite an existing one entirely. Use edit_file instead if you only need to change part of an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace root."},
                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one exact occurrence of text in an existing file. old_text must match uniquely -- include enough surrounding context if the text could appear more than once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace root."},
                    "old_text": {"type": "string", "description": "Exact text to find and replace. Must appear exactly once in the file."},
                    "new_text": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_lines",
            "description": "Replace an exact range of lines in a file by line number, given the new content to put there -- use this instead of edit_file whenever you already know the exact line numbers (e.g. from grep -n or sed -n via bash), since it never requires reproducing old text byte-for-byte and so can't fail on a whitespace mismatch. Requires expected_content: what you believe is currently at that exact line range, used as a safety check before applying anything. If your line numbers turn out to be stale, this will try to find the expected content nearby and correct itself automatically, or fail safely and show you the real current content rather than corrupting the file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace root."},
                    "start_line": {"type": "integer", "description": "First line to replace (1-indexed)."},
                    "end_line": {"type": "integer", "description": "Last line to replace, inclusive (1-indexed). Same as start_line to replace a single line."},
                    "new_content": {"type": "string", "description": "The new text to put in place of that line range. This completely replaces the range, it is not inserted alongside it."},
                    "expected_content": {"type": "string", "description": "What you believe is currently at lines start_line-end_line, exactly as you last saw it. Used to verify your line numbers are still accurate before making any change."},
                },
                "required": ["path", "start_line", "end_line", "new_content", "expected_content"],
            },
        },
    },
]


def resolve_workspace_path(workspace: str, rel_path: str) -> str:
    """Resolve a model-supplied relative path against the workspace root,
    refusing to ever resolve outside it. This is the actual safety
    boundary for file tools -- without it, a path like '../../etc/passwd'
    or an absolute path would let the model read/write anywhere on the
    filesystem the Athena process has access to, not just the intended
    workspace directory."""
    workspace_root = os.path.realpath(workspace)
    candidate = os.path.realpath(os.path.join(workspace_root, rel_path or "."))
    if candidate != workspace_root and not candidate.startswith(workspace_root + os.sep):
        raise ValueError(f"Path '{rel_path}' resolves outside the workspace, refusing.")
    return candidate


def list_files(workspace: str, rel_path: str):
    try:
        target = resolve_workspace_path(workspace, rel_path)
        if not os.path.isdir(target):
            return {"error": f"Not a directory: {rel_path}"}
        entries = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            entries.append({"name": name, "type": "dir" if os.path.isdir(full) else "file"})
        return {"path": rel_path, "entries": entries}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"list_files failed: {e}"}


_READ_FILE_CHUNK_SIZE = 20000


def read_file(workspace: str, rel_path: str, offset: int = 0):
    try:
        target = resolve_workspace_path(workspace, rel_path)
        if not os.path.isfile(target):
            return {"error": f"Not a file: {rel_path}"}
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        offset = max(0, offset or 0)
        total_chars = len(content)
        chunk = content[offset:offset + _READ_FILE_CHUNK_SIZE]
        next_offset = offset + len(chunk)
        has_more = next_offset < total_chars
        result = {"path": rel_path, "content": chunk, "total_chars": total_chars, "has_more": has_more}
        if has_more:
            result["next_offset"] = next_offset
        return result
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"read_file failed: {e}"}


def write_file(workspace: str, rel_path: str, file_content: str):
    try:
        target = resolve_workspace_path(workspace, rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(file_content)
        return {"path": rel_path, "written": True, "bytes": len(file_content)}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"write_file failed: {e}"}


def edit_file(workspace: str, rel_path: str, old_text: str, new_text: str):
    try:
        target = resolve_workspace_path(workspace, rel_path)
        if not os.path.isfile(target):
            return {"error": f"Not a file: {rel_path}"}
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        count = content.count(old_text)
        if count == 0:
            return {"error": "old_text not found in file"}
        if count > 1:
            return {"error": f"old_text appears {count} times -- must be unique, add more context"}
        content = content.replace(old_text, new_text)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            rag.index_codebase(workspace)
        except Exception as e:
            logger.warning(f"auto-reindex after write failed: {e}")
        return {"path": rel_path, "edited": True}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"edit_file failed: {e}"}


def replace_lines(workspace: str, rel_path: str, start_line, end_line, new_content: str, expected_content: str):
    """Replaces an exact line range by number, sidestepping edit_file's
    fragile requirement to reproduce old text byte-for-byte. Requires
    expected_content -- a sanity check against what's actually at that
    line range right now, not an exact-match requirement like
    old_text. If it genuinely doesn't match (most often because an
    earlier edit shifted the file's line numbers and these ones are
    now stale), this does NOT blindly apply the change: it searches a
    window around the given range for the expected content and, if
    found once and unambiguously, applies the edit there instead and
    reports the correction. If it's not found nearby, or found more
    than once, it fails safely and returns the real current content at
    that location, rather than corrupting the file the way a blind
    line-number replacement could."""
    try:
        target = resolve_workspace_path(workspace, rel_path)
        if not os.path.isfile(target):
            return {"error": f"Not a file: {rel_path}"}
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        lines = content.split("\n")

        try:
            start_line = int(start_line)
            end_line = int(end_line)
        except (TypeError, ValueError):
            return {"error": "start_line and end_line must be numbers."}
        if start_line < 1 or start_line > len(lines) or end_line < start_line:
            return {"error": f"Invalid range for a file with {len(lines)} lines (start_line={start_line}, end_line={end_line})."}

        end = min(len(lines), end_line)
        actual_block = "\n".join(lines[start_line - 1:end])
        expected_stripped = (expected_content or "").strip()

        if actual_block.strip() != expected_stripped:
            window_start = max(0, start_line - 1 - 25)
            window_end = min(len(lines), end_line + 25)
            window_lines = lines[window_start:window_end]
            expected_lines = expected_stripped.split("\n") if expected_stripped else []

            matches = []
            if expected_lines:
                for offset in range(len(window_lines) - len(expected_lines) + 1):
                    candidate = "\n".join(window_lines[offset:offset + len(expected_lines)]).strip()
                    if candidate == expected_stripped:
                        matches.append(window_start + offset + 1)

            if len(matches) == 1:
                real_start = matches[0]
                real_end = real_start + len(expected_lines) - 1
                new_lines = new_content.split("\n")
                updated_lines = lines[:real_start - 1] + new_lines + lines[real_end:]
                with open(target, "w", encoding="utf-8") as f:
                    f.write("\n".join(updated_lines))
                try:
                    rag.index_codebase(workspace)
                except Exception as e:
                    logger.warning(f"auto-reindex after write failed: {e}")
                return {
                    "path": rel_path,
                    "edited": True,
                    "note": f"Your line numbers ({start_line}-{end_line}) were stale, but the expected content was found unambiguously nearby at lines {real_start}-{real_end} and the edit was applied there instead. Re-check line numbers for this file before your next edit, since they may have shifted again.",
                }

            reason = "did not match" if not matches else f"was found {len(matches)} times nearby, which is ambiguous"
            return {
                "error": f"expected_content {reason} at or near lines {start_line}-{end_line}. Actual current content at that exact range right now:\n{actual_block}\n\nRe-verify the real content and line numbers before retrying, rather than guessing again."
            }

        new_lines = new_content.split("\n")
        updated_lines = lines[:start_line - 1] + new_lines + lines[end:]
        with open(target, "w", encoding="utf-8") as f:
            f.write("\n".join(updated_lines))
        try:
            rag.index_codebase(workspace)
        except Exception as e:
            logger.warning(f"auto-reindex after write failed: {e}")

        delta = len(new_lines) - (end - start_line + 1)
        result = {"path": rel_path, "edited": True, "lines_replaced": f"{start_line}-{end_line}"}
        if delta != 0:
            result["warning"] = f"This changed the file's line count by {delta:+d}. Any other line numbers you had for this file are now stale -- re-check before another replace_lines call."
        return result
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"replace_lines failed: {e}"}


@router.get("/api/workspace/files")
def list_workspace_files(workspace: str, path: str = ""):
    """List files and directories (with sizes) at a path relative to the
    given workspace root, for the file-browser panel. Reuses
    resolve_workspace_path so the UI browser shares the same confinement
    boundary as the agent's list_files/read_file/write_file/edit_file tools."""
    if not workspace:
        return {"error": "No workspace set."}
    try:
        target = resolve_workspace_path(workspace, path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(target):
        return {"error": f"Not a directory: {path}"}
    try:
        entries = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            is_dir = os.path.isdir(full)
            try:
                size = None if is_dir else os.path.getsize(full)
            except OSError:
                size = None
            entries.append({"name": name, "type": "dir" if is_dir else "file", "size": size})
        return {"path": path, "entries": entries}
    except PermissionError:
        return {"error": f"Permission denied: {path}"}


@router.get("/api/workspace/read")
def read_workspace_file(workspace: str, path: str = "", offset: int = 0):
    """Read a file's content for the file-browser panel's preview,
    reusing the same read_file implementation the agent's read_file
    tool uses."""
    if not workspace:
        return {"error": "No workspace set."}
    return read_file(workspace, path, offset)


@router.delete("/api/workspace/delete")
def delete_workspace_entry(workspace: str, path: str = ""):
    """Deletes a file or directory (recursively) at a path within the
    given workspace, for the file browser panel's delete action --
    reuses the same resolve_workspace_path confinement check every
    other file tool uses, so this can never delete anything outside
    the workspace regardless of what path is passed."""
    if not workspace:
        return {"error": "No workspace set."}
    if not path:
        return {"error": "Refusing to delete the workspace root itself."}
    try:
        target = resolve_workspace_path(workspace, path)
        if target == os.path.realpath(workspace):
            return {"error": "Refusing to delete the workspace root itself."}
        if os.path.isdir(target):
            import shutil
            shutil.rmtree(target)
        elif os.path.isfile(target):
            os.remove(target)
        else:
            return {"error": f"Not found: {path}"}
        return {"ok": True}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"delete failed: {e}"}
