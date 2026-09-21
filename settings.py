import os
import json
import threading

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "athena_settings.json")
# Not underscore-prefixed -- callers doing a read-modify-write sequence
# (load_settings(), mutate, save_settings()) need to hold this across both
# calls to avoid a lost update from a concurrent request, so it's real
# public API, not a private implementation detail of this module.
settings_lock = threading.Lock()


def load_settings():
    try:
        with open(_SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(settings):
    """Atomic write (temp file + os.replace) so a crash or a
    concurrent write from another device mid-write can never leave
    behind a half-written, corrupted JSON file -- which previously
    silently made load_settings() return {} (an unparseable file is
    caught by its broad except and treated as 'no settings yet'),
    making real saved data look like it had vanished entirely."""
    tmp_path = _SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(settings, f)
    os.replace(tmp_path, _SETTINGS_PATH)


notes_lock = threading.Lock()  # public, same reasoning as settings_lock above
_NOTES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "athena_notes.json")


def load_notes():
    try:
        with open(_NOTES_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def save_notes(notes):
    """Atomic write (temp file + os.replace), same pattern as
    settings, so a crash or a concurrent write from another device
    mid-write can never leave a corrupted notes file behind. This
    replaces the old localStorage-only storage, which never synced
    notes across devices at all -- a note saved on one device was
    simply invisible everywhere else."""
    tmp_path = _NOTES_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(notes, f)
    os.replace(tmp_path, _NOTES_PATH)
