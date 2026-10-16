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
_USERS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_data")


def _account_file(name, legacy_path):
    """The main account keeps the original file; every other account has its own under user_data/<name>/."""
    from user_context import current_user
    user = current_user()
    if user:
        from auth_routes import auth_manager
        if user != auth_manager.main_user:
            folder = os.path.join(_USERS_ROOT, "".join(c if c.isalnum() or c in "._-" else "_" for c in user.lower()))
            os.makedirs(folder, exist_ok=True)
            return os.path.join(folder, name)
    return legacy_path


def _notes_path():
    return _account_file("notes.json", _NOTES_PATH)


def load_user_settings():
    """The settings only this account has (its own online endpoints and keys, its default model, theme ...); the main account's are
    in the shared file."""
    try:
        with open(_account_file("settings.json", None) or os.devnull) as f:
            return json.load(f)
    except Exception:
        return {}


def save_user_settings(data):
    path = _account_file("settings.json", None)
    if path is None:
        raise ValueError("The main account's settings live in the shared file.")
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.replace(tmp_path, path)


def load_notes():
    try:
        with open(_notes_path()) as f:
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
    path = _notes_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(notes, f)
    os.replace(tmp_path, path)


# ---------- what each account sees and may change ----------

# Settings every account has for itself. The rest are shared and only the main account changes them.
PER_ACCOUNT_KEYS = ("default_model", "workspace", "theme", "model_aliases", "bot_creation_defaults", "athena_agent_model", "plan_approval_all")


def _acting_other_account():
    """The signed-in account's name when it is not the main account, else None."""
    from user_context import current_user
    user = current_user()
    if not user:
        return None
    from auth_routes import auth_manager
    return None if user == auth_manager.main_user else user


def load_effective():
    """The settings as the acting account sees them. The main account sees the shared file as it is. Another account sees the shared
    settings except everything private to the main account (its online endpoints and keys, its MCP servers), its own value for each
    per-account setting, the local engines the main account set up, and its own online endpoints."""
    shared = load_settings()
    user = _acting_other_account()
    if not user:
        return shared
    mine = load_user_settings()
    out = {k: v for k, v in shared.items() if k not in PER_ACCOUNT_KEYS and k not in ("endpoints", "mcp_servers", "memory_extraction_model")}
    for key in PER_ACCOUNT_KEYS:
        if key in mine:
            out[key] = mine[key]
    from auth_routes import auth_manager
    full = auth_manager.can_use_whole_host(user)
    choices = (mine.get("local_choices") or {}) if full else {}
    endpoints = []
    for ep in shared.get("endpoints", []):
        if ep.get("type") == "local":
            copy = {k: v for k, v in ep.items() if k != "apiKey"}
            # Everyone starts with the models the main account ticked. An account with full access may pick its own subset; a limited
            # account just uses those, and never sees the endpoint itself (hidden: the Models page skips it, the model picker does not).
            copy["enabledModels"] = choices.get(ep.get("id"), ep.get("enabledModels") or [])
            if not full:
                copy["hidden"] = True
                copy["detectedModels"] = []
            endpoints.append(copy)
    out["endpoints"] = endpoints + list(mine.get("endpoints") or [])
    out["mcp_servers"] = shared.get("mcp_servers", []) if full else []
    return out


def apply_update(patch):
    """Merge a settings change into the right place and return what the account now sees. The main account writes the shared file.
    Another account may change only its own settings: its theme, default model, workspace, aliases, bot defaults, agent model, which
    models of the shared local engines it uses, and its own online endpoints. Anything else in the request is ignored."""
    user = _acting_other_account()
    if not user:
        with settings_lock:
            current = load_settings()
            current.update(patch)
            save_settings(current)
        return load_effective()
    import ownership
    from auth_routes import auth_manager
    full = auth_manager.can_use_whole_host(user)
    shared_local = {e.get("id"): e for e in load_settings().get("endpoints", []) if e.get("type") == "local"}
    with settings_lock:
        mine = load_user_settings()
        for key in PER_ACCOUNT_KEYS:
            if key in patch:
                mine[key] = patch[key]
        if "workspace" in patch and not ownership.workspace_allowed(patch.get("workspace") or "", user):
            mine["workspace"] = ""
        if isinstance(patch.get("endpoints"), list):
            own, choices = [], dict(mine.get("local_choices") or {})
            for ep in patch["endpoints"]:
                if not isinstance(ep, dict):
                    continue
                if ep.get("id") in shared_local:
                    if full:
                        choices[ep["id"]] = [m for m in (ep.get("enabledModels") or []) if isinstance(m, str)]
                elif ep.get("type") == "online" and ownership.endpoint_kind(ep.get("provider", ""), ep.get("url", ""), user) == "online":
                    own.append(ep)
            mine["endpoints"], mine["local_choices"] = own, choices
        save_user_settings(mine)
    return load_effective()
