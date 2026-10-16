"""Viewing a workspace's web page in its own browser tab.

A page a model wrote must never run on Athena's own address, where its scripts could act as the signed-in person. So the page is served
from a link with a long random token in it, and every response carries a sandbox: the page can run its scripts and use its own files
(stylesheets, scripts, images are served from the same link, so relative paths just work) but is treated as coming from nowhere, which
means it cannot read Athena's cookies, call her API as the person, or open her in a frame. The token is the only key, so the route needs
no login, and it stops working after a few hours. A page in the sandbox has no access to browser storage (localStorage and the like).
"""
import mimetypes
import os
import secrets
import threading
import time
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

import file_tools
import ownership
import safety

router = APIRouter()

LINK_SECONDS = 4 * 3600
HTML_SUFFIXES = (".html", ".htm")
_links = {}
_lock = threading.Lock()

SANDBOX_HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-modals allow-popups allow-pointer-lock allow-downloads; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def _forget_expired(now):
    for token in [t for t, e in _links.items() if e["expires"] < now]:
        del _links[token]


class ViewLinkIn(BaseModel):
    workspace: str
    path: str


@router.post("/api/workspace/view-link")
def make_view_link(req: ViewLinkIn):
    ownership.workspace_guard(req.workspace)
    try:
        target = file_tools.resolve_workspace_path(req.workspace, req.path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not os.path.isfile(target) or not target.lower().endswith(HTML_SUFFIXES):
        return JSONResponse({"error": "That is not an HTML file."}, status_code=400)
    if safety.classify(target) == "secret":
        return JSONResponse({"error": "That file is private."}, status_code=403)
    root = os.path.realpath(req.workspace)
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        _forget_expired(now)
        _links[token] = {"root": root, "expires": now + LINK_SECONDS}
    rel = os.path.relpath(target, root).replace(os.sep, "/")
    return {"url": f"/workspace-view/{token}/{quote(rel)}", "expires_in": LINK_SECONDS}


@router.get("/workspace-view/{token}/{path:path}")
def serve_workspace_file(token: str, path: str = ""):
    with _lock:
        entry = _links.get(token)
    if entry is None or entry["expires"] < time.time():
        return PlainTextResponse("This link has expired. Open the page again from Athena.", status_code=404, headers=SANDBOX_HEADERS)
    root = entry["root"]
    try:
        target = file_tools.resolve_workspace_path(root, path or ".")
        if os.path.isdir(target):
            target = file_tools.resolve_workspace_path(root, os.path.join(path or ".", "index.html"))
    except ValueError:
        return PlainTextResponse("Not found.", status_code=404, headers=SANDBOX_HEADERS)
    relative = os.path.relpath(target, root)
    hidden = any(part.startswith(".") for part in relative.split(os.sep))
    if hidden or not os.path.isfile(target) or safety.classify(target) == "secret":
        return PlainTextResponse("Not found.", status_code=404, headers=SANDBOX_HEADERS)
    media_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, headers=SANDBOX_HEADERS)
