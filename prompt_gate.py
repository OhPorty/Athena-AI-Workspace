"""One prompt at a time, for small machines. When the main account turns it on, a prompt that would run on a model on this network waits
for the one running now (first come, first served) and the person is told, instead of two models loading at once. A prompt to an online
model never waits and never holds the line: it does not run on this machine.
"""
import collections
import json
import threading
import time

import ownership
import settings

_cond = threading.Condition()
_running = []                      # slots now generating
_waiting = collections.deque()     # slots in line
_STALE_AFTER = 6 * 3600            # a slot nobody released (a bug, never a normal run) stops blocking after this


class Slot:
    def __init__(self, user, session_id):
        self.user, self.session_id, self.since = user, session_id, time.time()


def enabled() -> bool:
    return bool(settings.load_settings().get("one_prompt_at_a_time"))


def applies(provider: str, endpoint_url: str) -> bool:
    return enabled() and not ownership.is_online(provider, endpoint_url)


def status(user: str) -> dict:
    with _cond:
        _drop_stale()
        return {"enabled": enabled(), "busy": bool(_running), "busy_with_other": any(s.user != user for s in _running),
                "waiting": len(_waiting)}


def _drop_stale():
    now = time.time()
    for slot in [s for s in _running if now - s.since > _STALE_AFTER]:
        _running.remove(slot)


def _event(kind, **data):
    return f"data: {json.dumps({'type': kind, **data})}\n\n"


def wait_turn(user, session_id, cancel_flag):
    """Generator: yields events while the prompt waits; returns the slot to release afterwards, or None if it was stopped while waiting."""
    slot = Slot(user, session_id)
    with _cond:
        _drop_stale()
        if not _running and not _waiting:
            _running.append(slot)
            return slot
        _waiting.append(slot)
    last_position = None
    while True:
        with _cond:
            _drop_stale()
            if not _running and _waiting and _waiting[0] is slot:
                _waiting.popleft()
                _running.append(slot)
                break
            if cancel_flag.is_set():
                _waiting.remove(slot)
                _cond.notify_all()
                return None
            position = list(_waiting).index(slot) + 1
            someone_else = any(s.user != user for s in list(_running) + list(_waiting)[:position - 1])
            if position != last_position:
                last_position = position
                event = _event("queued", position=position, someone_else=someone_else,
                               message=("Someone else is in the middle of a prompt. " if someone_else else "Another of your prompts is still running. ")
                                       + ("Yours is queued and starts next." if position == 1 else f"Yours is queued, number {position} in line."))
            else:
                event = None
            _cond.wait(timeout=3)
        if event:
            yield event
        else:
            yield ": waiting\n\n"
    yield _event("queue_started")
    return slot


def release(slot):
    with _cond:
        if slot in _running:
            _running.remove(slot)
        _cond.notify_all()


def blocking_turn(user, label, cancel_flag):
    """For work with nobody streaming (a research run): wait for the turn and return the slot, or None if stopped while waiting."""
    turn = wait_turn(user, label, cancel_flag)
    try:
        while True:
            next(turn)
    except StopIteration as done:
        return done.value
