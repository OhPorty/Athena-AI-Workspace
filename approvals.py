"""Asking the person, and waiting for the answer.

Two kinds of request share one mechanism:
  approval  "Athena wants to do something that cannot be quietly undone" (every removal): Approve / Don't allow.
  question  "Athena needs a decision": pick one of the offered options, or type an answer.

The chat loop registers a request, streams it to the page, and waits (sending keep-alives) until the person answers, the turn is
stopped, or the time runs out. Running out of time means "no" for an approval and "no answer" for a question, never "yes". The
request lives here, not in the browser, so closing the tab and coming back shows the same question still waiting.
"""
import threading
import time
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

import ownership

router = APIRouter()

WAIT_SECONDS = 30 * 60
_lock = threading.Lock()
_requests = {}


class Request:
    def __init__(self, session_id, kind, title, summary="", items=None, reason="", options=None, danger=False, undo_note=""):
        self.id = uuid.uuid4().hex[:12]
        self.session_id = session_id
        self.kind = kind
        self.title = title
        self.summary = summary
        self.items = items or []
        self.reason = reason
        self.options = options or []
        self.danger = danger
        self.undo_note = undo_note
        self.created_at = time.time()
        self.decision = None
        self.answer = ""
        self.event = threading.Event()

    def public(self):
        return {"id": self.id, "session_id": self.session_id, "kind": self.kind, "title": self.title, "summary": self.summary,
                "items": self.items, "reason": self.reason, "options": self.options, "danger": self.danger,
                "undo_note": self.undo_note, "created_at": self.created_at}


def register(session_id, kind, title, **fields):
    request = Request(session_id, kind, title, **fields)
    with _lock:
        _requests[request.id] = request
    return request


def get(request_id):
    with _lock:
        return _requests.get(request_id)


def wait(request, seconds):
    """Waits up to `seconds` for an answer. -> the decision, or None if there is still none."""
    request.event.wait(seconds)
    return request.decision


def answer(request_id, decision, text=""):
    request = get(request_id)
    if request is None or request.decision is not None:
        return False
    if request.kind in ("approval", "plan") and decision not in ("approved", "denied"):
        return False
    request.decision = decision
    request.answer = str(text or "")[:2000]
    request.event.set()
    return True


def finish(request, decision):
    """The turn ended or timed out before an answer: close it so it stops showing as waiting."""
    if request.decision is None:
        request.decision = decision
        request.event.set()


def forget(request_id):
    with _lock:
        _requests.pop(request_id, None)


def pending(session_id):
    with _lock:
        return [r.public() for r in _requests.values() if r.session_id == session_id and r.decision is None]


class AnswerIn(BaseModel):
    decision: str
    text: str = ""


@router.get("/api/approvals/pending")
def api_pending(session_id: str):
    if not ownership.session_allowed(session_id):
        return {"requests": []}
    return {"requests": pending(session_id)}


@router.post("/api/approvals/{request_id}")
def api_answer(request_id: str, req: AnswerIn):
    asked = get(request_id)
    if asked is not None and not ownership.session_allowed(asked.session_id):
        return {"ok": False}
    return {"ok": answer(request_id, req.decision, req.text)}
