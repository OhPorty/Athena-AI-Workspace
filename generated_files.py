"""Files Athena's tools produce for the user to open: PDFs, preview screenshots.

Stored under an unguessable token in generated_files/ and served by main.py's
/api/generated-files/{token}. The token is the only user-controlled input that
reaches the filesystem, so lookup is confined to this one directory.
"""
import mimetypes
import os
import time
import uuid

ROOT = os.environ.get("ATHENA_GENERATED_DIR") or os.environ.get("ATHENA_PDF_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_files")
_INLINE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


def store(data, filename):
    """-> token. filename keeps its extension; it is what the user sees on download."""
    os.makedirs(ROOT, exist_ok=True)
    token = f"{uuid.uuid4().hex}__{filename}"
    with open(os.path.join(ROOT, token), "wb") as f:
        f.write(data)
    return token


def url_for(token):
    return f"/api/generated-files/{token}"


def resolve(token):
    """os.path.basename strips any directory component (../, absolute paths), the same
    confinement guarantee file_tools.resolve_workspace_path gives workspace paths."""
    safe = os.path.basename(token or "")
    path = os.path.join(ROOT, safe)
    if not safe or not os.path.isfile(path):
        return None
    return path


def display_name(token):
    safe = os.path.basename(token or "")
    if "__" in safe:
        return safe.split("__", 1)[1]
    return safe or "download"


def media_type(token):
    return mimetypes.guess_type(display_name(token))[0] or "application/octet-stream"


def shown_inline(token):
    """Images display in the page; everything else (PDFs) downloads."""
    return os.path.splitext(display_name(token))[1].lower() in _INLINE_EXTENSIONS


def prune(name_prefix, max_age_days):
    """Delete old files whose display name starts with name_prefix (e.g. preview screenshots,
    which pile up one set per preview call and are only interesting for a while)."""
    if not os.path.isdir(ROOT):
        return
    cutoff = time.time() - max_age_days * 86400
    for entry in os.listdir(ROOT):
        path = os.path.join(ROOT, entry)
        try:
            if display_name(entry).startswith(name_prefix) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass
