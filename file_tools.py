import hashlib
import os
import shutil
import tempfile
import threading
import zipfile

import ownership
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

import backup_tools
import binary_files
import rag
from logging_setup import logger

router = APIRouter(dependencies=[Depends(ownership.workspace_guard)])


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
            "description": "Read a file's contents. If you can view images, an image, SVG or PDF is also shown to you as a picture, so you can check what you made. Long files come in pieces of about 200 lines -- if the response has has_more: true, call again with offset set to the returned next_offset (or start_line to jump to a part) to continue, rather than assuming you've seen the whole file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace root."},
                    "offset": {"type": "integer", "description": "Character offset to start reading from. Omit or use 0 to start from the beginning."},
                    "start_line": {"type": "integer", "description": "Start at this line number (1 is the first) instead of using offset."},
                    "end_line": {"type": "integer", "description": "Stop after this line number. With start_line, reads exactly that range."},
                    "page": {"type": "integer", "description": "For a PDF: which page to look at as a picture (1 is the first). Only if you can view images."},
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
                    "content": {"type": "string", "description": "File content to write."},
                    "append": {"type": "boolean", "description": "Add to the end of the file instead of replacing it. Use this to write a large file in several calls."},
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
            "description": "Replace an exact range of lines in a file by line number, given the new content to put there -- use this instead of edit_file whenever you already know the exact line numbers (e.g. from grep -n or sed -n via bash_read_only), since it never requires reproducing old text byte-for-byte and so can't fail on a whitespace mismatch. Requires expected_content: what you believe is currently at that exact line range, used as a safety check before applying anything. If your line numbers turn out to be stale, this will try to find the expected content nearby and correct itself automatically, or fail safely and show you the real current content rather than corrupting the file.",
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
_VIEWER_MAX_CHARS = 2_000_000
# What a model gets from one read: about 200 lines. A long file arrives in pieces it can continue from, cut at a line end, instead of
# a head and a tail with the middle thrown away -- small context windows stay small and no part of the file is ever out of reach.
MODEL_READ_WINDOW = 8000


def read_file(workspace: str, rel_path: str, offset: int = 0, chunk_size: int = _READ_FILE_CHUNK_SIZE, start_line=None, end_line=None, line_aligned=False):
    try:
        target = resolve_workspace_path(workspace, rel_path)
        if not os.path.isfile(target):
            return {"error": f"Not a file: {rel_path}"}
        kind = None
        described = binary_files.describe(target)
        if described:
            kind, content = described
        else:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        offset = max(0, offset or 0)
        total_chars = len(content)
        line_starts = [0] + [i + 1 for i, ch in enumerate(content) if ch == "\n" and i + 1 < total_chars]
        total_lines = len(line_starts) if content else 0
        limit_end = total_chars
        if isinstance(start_line, int) and not isinstance(start_line, bool):
            offset = line_starts[min(max(start_line, 1), max(total_lines, 1)) - 1] if content else 0
        if isinstance(end_line, int) and not isinstance(end_line, bool) and 0 < end_line < total_lines:
            limit_end = line_starts[end_line]
        chunk = content[offset:min(offset + chunk_size, limit_end)]
        if line_aligned and offset + len(chunk) < limit_end:             # the window ends mid-way: cut it at the end of a line
            cut = chunk.rfind("\n")
            if cut > 0:
                chunk = chunk[:cut + 1]
        next_offset = offset + len(chunk)
        has_more = next_offset < limit_end
        first_line = content.count("\n", 0, offset) + 1 if content else 0
        last_line = first_line + chunk.count("\n") - (1 if chunk.endswith("\n") else 0) if chunk else first_line
        result = {"path": rel_path, "content": chunk, "total_chars": total_chars, "has_more": has_more}
        if line_aligned and content:
            result["lines"] = f"{first_line}-{last_line} of {total_lines}"
        if kind:
            result["binary"] = True
            result["kind"] = kind
        if has_more:
            result["next_offset"] = next_offset
            if line_aligned:
                result["note"] = (f"This is lines {first_line}-{last_line} of {total_lines}, not the whole file. Continue with offset {next_offset} "
                                  f"(or start_line {last_line + 1}), or jump to any part with start_line and end_line.")
        return result
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"read_file failed: {e}"}


def write_file(workspace: str, rel_path: str, file_content: str, append: bool = False):
    try:
        target = resolve_workspace_path(workspace, rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a" if append else "w", encoding="utf-8") as f:
            f.write(file_content)
        result = {"path": rel_path, "written": True, "bytes": len(file_content)}
        if append:
            result["appended"] = True
            result["file_bytes"] = os.path.getsize(target)
        return result
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"write_file failed: {e}"}


def _old_text_not_found(content, old_text):
    """Point at the nearest real text, so the model fixes its old_text instead of re-reading the file."""
    import difflib
    file_lines, want = content.split("\n"), old_text.strip("\n").split("\n")
    n = max(1, min(len(want), 40))
    first = next((w.strip() for w in want if w.strip()), "")
    starts = sorted(range(max(1, len(file_lines) - n + 1)),
                    key=lambda i: -difflib.SequenceMatcher(None, first, file_lines[i].strip()).quick_ratio())[:8]
    best = max(((difflib.SequenceMatcher(None, old_text.strip(), "\n".join(file_lines[i:i + n]).strip()).ratio(), i) for i in starts), default=(0, 0))
    ratio, i = best
    error = "old_text not found in file"
    if ratio < 0.5:
        return {"error": error}
    text = "\n".join(file_lines[i:i + n])
    return {"error": f"{error}. The closest text is at lines {i + 1}-{i + n} ({ratio:.0%} similar); copy old_text from it exactly.",
            "closest_match": {"lines": f"{i + 1}-{i + n}", "text": text[:1500]}}


def edit_file(workspace: str, rel_path: str, old_text: str, new_text: str):
    try:
        target = resolve_workspace_path(workspace, rel_path)
        if not os.path.isfile(target):
            return {"error": f"Not a file: {rel_path}"}
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        count = content.count(old_text)
        if count == 0:
            return _old_text_not_found(content, old_text)
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


_EDIT_MAX_BYTES = 2 * 1024 * 1024


def _version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_for_edit(workspace: str, rel_path: str):
    """The WHOLE file for the editor, unlike read_file's capped, lossy chunk (which would
    truncate a big file or mangle non-UTF-8 bytes if saved back). Anything that can't be edited
    safely as text says so instead. Line endings are normalized to \\n for the editor and
    remembered, so save_edit can put them back exactly as they were."""
    try:
        target = resolve_workspace_path(workspace, rel_path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isfile(target):
        return {"error": f"Not a file: {rel_path}"}
    size = os.path.getsize(target)
    if size > _EDIT_MAX_BYTES:
        return {"error": f"{rel_path} is {size // 1024} KB; files over {_EDIT_MAX_BYTES // 1024} KB can't be edited here.", "uneditable": True}
    with open(target, "rb") as f:
        data = f.read()
    try:
        if b"\x00" in data:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary")
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": f"{rel_path} isn't plain UTF-8 text, so it can't be edited here.", "uneditable": True}
    eol = "crlf" if "\r\n" in text else "lf"
    return {"path": rel_path, "content": text.replace("\r\n", "\n"), "eol": eol, "version": _version(data), "bytes": len(data)}


def save_edit(workspace: str, rel_path: str, content: str, eol: str = "lf", base_version=None):
    """Overwrite an existing file with the editor's text. Refuses if the file changed on disk since
    it was opened (base_version) -- the model may be editing it too -- unless base_version is None
    (the caller chose to overwrite). The version being replaced is snapshotted first so a manual
    edit can always be undone, and the write is atomic so a crash can't leave half a file."""
    try:
        target = resolve_workspace_path(workspace, rel_path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isfile(target):
        return {"error": f"Not a file: {rel_path}"}
    with open(target, "rb") as f:
        current = f.read()
    if base_version is not None and base_version != _version(current):
        return {"error": f"{rel_path} changed on disk since you opened it.", "conflict": True}
    text = content.replace("\r\n", "\n")
    if eol == "crlf":
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    if len(data) > _EDIT_MAX_BYTES:
        return {"error": f"That's over the {_EDIT_MAX_BYTES // 1024} KB editing limit."}
    if data == current:
        return {"path": rel_path, "saved": True, "unchanged": True, "version": _version(data), "bytes": len(data)}
    backup_tools.backup_file(target)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".athena-edit-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return {"error": f"Couldn't save {rel_path}: {e}"}

    def reindex():
        try:
            rag.index_codebase(workspace)
        except Exception as e:
            logger.warning(f"re-index after manual edit of {rel_path} failed: {e}")

    threading.Thread(target=reindex, daemon=True).start()
    return {"path": rel_path, "saved": True, "version": _version(data), "bytes": len(data)}


class WorkspaceFileIn(BaseModel):
    workspace: str
    path: str
    content: str
    eol: str = "lf"
    base_version: str | None = None


@router.get("/api/workspace/file")
def get_workspace_file(workspace: str, path: str = ""):
    if not workspace:
        return {"error": "No workspace set."}
    return read_for_edit(workspace, path)


@router.put("/api/workspace/file")
def put_workspace_file(req: WorkspaceFileIn):
    if not req.workspace:
        return {"error": "No workspace set."}
    ownership.workspace_guard(req.workspace)
    return save_edit(req.workspace, req.path, req.content, req.eol, req.base_version)


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
    """The whole file for the file-browser preview (a person is reading it, so it is not cut to the
    model's 20k-character chunk); only a very large file is cut, and has_more says so."""
    if not workspace:
        return {"error": "No workspace set."}
    return read_file(workspace, path, offset, _VIEWER_MAX_CHARS)


_INLINE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".wav", ".mp3", ".ogg", ".oga", ".m4a", ".flac", ".opus"}


@router.get("/api/workspace/download")
def download_workspace_file(workspace: str, path: str = "", inline: bool = False):
    """Hand a workspace file to the browser as-is: how a generated .docx, .xlsx, .zip or image
    gets from the workspace to the user. Confined by resolve_workspace_path like every file tool."""
    if not workspace or not path:
        return JSONResponse({"error": "A workspace and a file path are required."}, status_code=400)
    try:
        target = resolve_workspace_path(workspace, path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not os.path.isfile(target):
        return JSONResponse({"error": f"Not a file: {path}"}, status_code=404)
    # Only raster images and audio may display in the page. Anything that can carry script (html, svg)
    # is always a download, so a file a model wrote can never run on Athena's own origin.
    shown = os.path.splitext(target)[1].lower() in _INLINE_EXTENSIONS
    kind = "inline" if inline and shown else "attachment"
    return FileResponse(target, filename=os.path.basename(target), content_disposition_type=kind)


# What a workspace zip leaves out: folders that are rebuilt by one command or only hold Athena's own scratch (previews, trash).
_ZIP_SKIP_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".athena"}
_ZIP_MAX_BYTES = 1024 ** 3


def zip_workspace(workspace: str):
    """Zip the whole workspace into a temporary file. -> (path of the zip, number of files, names left out).
    Symlinks are never followed (a link cannot pull in a file from outside), Athena's own private files are never included, and a workspace
    over 1 GB is refused rather than filling the disk."""
    import safety
    root = resolve_workspace_path(workspace, "")
    if not os.path.isdir(root):
        raise ValueError("The workspace folder does not exist.")
    handle, zip_path = tempfile.mkstemp(prefix="athena_workspace_", suffix=".zip")
    os.close(handle)
    count, total, left_out = 0, 0, []
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for current, dirs, files in os.walk(root):
                kept = []
                for d in dirs:
                    full = os.path.join(current, d)
                    if d in _ZIP_SKIP_DIRS or os.path.islink(full) or safety.classify(full) == "secret":
                        left_out.append(os.path.relpath(full, root))
                    else:
                        kept.append(d)
                dirs[:] = sorted(kept)
                for name in sorted(files):
                    full = os.path.join(current, name)
                    if os.path.islink(full) or safety.classify(full) == "secret" or not os.path.isfile(full):
                        left_out.append(os.path.relpath(full, root))
                        continue
                    try:
                        total += os.path.getsize(full)
                        if total > _ZIP_MAX_BYTES:
                            raise ValueError("This workspace is over 1 GB, which is too big to zip here.")
                        archive.write(full, os.path.relpath(full, root))
                        count += 1
                    except OSError:
                        left_out.append(os.path.relpath(full, root))
            if count == 0:
                archive.writestr(".empty", "")           # a zip with no entries is not a valid download for most tools
    except Exception:
        os.remove(zip_path)
        raise
    return zip_path, count, left_out


@router.get("/api/workspace/zip")
def download_workspace_zip(workspace: str):
    """The whole workspace as one .zip, for taking a project away. Confined to the workspace by resolve_workspace_path, like every file route."""
    if not workspace:
        return JSONResponse({"error": "No workspace set."}, status_code=400)
    try:
        zip_path, count, left_out = zip_workspace(workspace)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    name = (os.path.basename(os.path.realpath(workspace).rstrip(os.sep)) or "workspace") + ".zip"
    return FileResponse(zip_path, media_type="application/zip", filename=name, background=BackgroundTask(os.remove, zip_path),
                        headers={"X-Files-Zipped": str(count), "X-Files-Left-Out": str(len(left_out))})


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
