"""Which account the code is running for. The auth middleware sets it for each web request; code that runs outside a request
(a scheduled task, a background job) sets it explicitly with as_user()."""
import contextlib
import contextvars

_current = contextvars.ContextVar("athena_current_user", default="")


def current_user() -> str:
    return _current.get()


def set_current_user(name: str):
    return _current.set(name or "")


def reset_current_user(token):
    _current.reset(token)


@contextlib.contextmanager
def as_user(name: str):
    token = _current.set(name or "")
    try:
        yield
    finally:
        _current.reset(token)
