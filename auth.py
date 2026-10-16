"""
Athena authentication: a main account plus any number of extra accounts, each with its own password and its own authenticator
(TOTP 2FA), or -- when the main account chooses at creation -- the main account's authenticator. Adapted from Odysseus's core/auth.py.
Config stored in athena_auth.json; sessions in athena_sessions.json. bcrypt for passwords, pyotp for TOTP.

The first account ever created is the main account. Accounts the main account creates always start with a temporary password that
must be changed at first login (whatever password the main account typed), and an account with its own authenticator must also set
that up before it can use anything else.
"""

import json
import os
import re
import secrets
import threading
import time
from typing import Optional, Dict, Any, List

import bcrypt
import pyotp

TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days -- self-hosted tool, favor convenience
PASSWORD_MIN_LENGTH = 10
TEMP_PASSWORD_MIN_LENGTH = 8       # a temporary password the main account types; the account replaces it at first sign-in
PASSWORD_RULES = [
    f"At least {PASSWORD_MIN_LENGTH} characters",
    "An uppercase letter and a lowercase letter",
    "A number",
    "A symbol, such as ! ? # $ % & *",
    "Nothing easy to guess: no common words like \"password\" or runs like 12345 or aaaa, and not your username",
]
_COMMON_FRAGMENTS = ("password", "passw0rd", "qwerty", "letmein", "welcome", "iloveyou", "admin", "athena", "12345", "abcde", "11111", "00000", "monkey", "dragon", "football", "baseball")


def password_problem(password: str, username: str = "") -> Optional[str]:
    """Why this is not an acceptable password for a person to choose, in words they can act on, or None when it is fine."""
    missing = []
    if len(password) < PASSWORD_MIN_LENGTH:
        missing.append(f"at least {PASSWORD_MIN_LENGTH} characters")
    if not re.search(r"[a-z]", password):
        missing.append("a lowercase letter")
    if not re.search(r"[A-Z]", password):
        missing.append("an uppercase letter")
    if not re.search(r"[0-9]", password):
        missing.append("a number")
    if not re.search(r"[^A-Za-z0-9\s]", password):
        missing.append("a symbol (such as ! ? # $ %)")
    if missing:
        return "The password needs " + ", ".join(missing) + "."
    lowered = password.lower()
    if any(fragment in lowered for fragment in _COMMON_FRAGMENTS):
        return "That password contains something very common (like \"password\" or 12345). Pick something less guessable."
    if re.search(r"(.)\1{3,}", password):
        return "That password repeats the same character four or more times in a row. Pick something less guessable."
    name = (username or "").strip().lower()
    if len(name) >= 3 and name in lowered:
        return "The password must not contain the username."
    return None
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
_TEMP_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, AttributeError):
        return False


def _temp_password() -> str:
    return "".join(secrets.choice(_TEMP_PASSWORD_ALPHABET) for _ in range(12))


def _atomic_write_json(path: str, data: dict):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


class AuthManager:
    """Password + session-token auth for the main account and the accounts it creates, with per-account TOTP 2FA."""

    def __init__(self, auth_path: str):
        self.auth_path = auth_path
        self._sessions_path = os.path.join(os.path.dirname(auth_path), "athena_sessions.json")
        self._config: Dict[str, Any] = {}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._pending_2fa: Dict[str, Any] = {}
        self._sessions_lock = threading.RLock()
        self._config_lock = threading.RLock()
        self._setup_lock = threading.Lock()
        self._load()
        self._load_sessions()

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _load(self):
        try:
            if os.path.exists(self.auth_path):
                with open(self.auth_path, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
            else:
                self._config = {}
        except Exception:
            self._config = {}
        if "username" in self._config and "users" not in self._config:
            self._migrate_single_user()

    def _migrate_single_user(self):
        """The file used to hold exactly one account at its top level; that account becomes the main account."""
        old = self._config
        name = old["username"]
        record = {k: v for k, v in old.items() if k != "username"}
        record.update({"role": "main", "full_access": True, "must_change_password": False, "must_setup_totp": False})
        self._config = {"users": {name: record}, "main": name}
        self._save()

    def _save(self):
        _atomic_write_json(self.auth_path, self._config)

    def _users(self) -> Dict[str, Dict[str, Any]]:
        return self._config.setdefault("users", {})

    def _load_sessions(self):
        try:
            if os.path.exists(self._sessions_path):
                with open(self._sessions_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                now = time.time()
                self._sessions = {k: v for k, v in data.items() if v.get("expiry", 0) > now}
                if len(self._sessions) != len(data):
                    self._save_sessions()
        except Exception:
            self._sessions = {}

    def _save_sessions(self):
        try:
            with self._sessions_lock:
                snapshot = dict(self._sessions)
            _atomic_write_json(self._sessions_path, snapshot)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Who exists
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        main = self._config.get("main")
        return bool(main) and bool(self._users().get(main, {}).get("password_hash"))

    @property
    def main_user(self) -> Optional[str]:
        return self._config.get("main")

    @property
    def username(self) -> Optional[str]:
        """The main account's name (kept for callers from when there was one account)."""
        return self.main_user

    def is_main(self, username: Optional[str]) -> bool:
        return bool(username) and username == self.main_user

    def has_user(self, username: str) -> bool:
        return (username or "").strip().lower() in self._users()

    def can_use_whole_host(self, username: Optional[str]) -> bool:
        """Full access: any folder as a workspace, Athena's own files, MCP servers. The main account always has it."""
        record = self._users().get(username or "")
        return bool(record) and (self.is_main(username) or bool(record.get("full_access")))

    def main_has_authenticator(self) -> bool:
        main = self._users().get(self.main_user or "", {})
        return bool(main.get("totp_enabled") and main.get("totp_secret"))

    def policy(self) -> dict:
        return {"password_min_length": PASSWORD_MIN_LENGTH, "password_rules": PASSWORD_RULES, "session_days": TOKEN_TTL // 86400}

    def public_user(self, username: str) -> Optional[dict]:
        record = self._users().get(username)
        if record is None:
            return None
        return {
            "username": username,
            "is_main": self.is_main(username),
            "full_access": self.can_use_whole_host(username),
            "totp_enabled": bool(record.get("totp_enabled")),
            "authenticator_shared": bool(record.get("authenticator_shared")),
            "must_change_password": bool(record.get("must_change_password")),
            "must_setup_totp": bool(record.get("must_setup_totp")),
            "created": record.get("created"),
        }

    def list_users(self) -> List[dict]:
        order = sorted(self._users(), key=lambda n: (not self.is_main(n), self._users()[n].get("created", 0)))
        return [self.public_user(n) for n in order]

    def onboarding(self, username: Optional[str]) -> Optional[str]:
        """What a signed-in account must finish before using anything else, in order."""
        record = self._users().get(username or "")
        if not record:
            return None
        if record.get("must_change_password"):
            return "change_password"
        if record.get("must_setup_totp") and not record.get("totp_enabled"):
            return "setup_totp"
        return None

    # ------------------------------------------------------------------
    # First-run setup, creating and managing accounts
    # ------------------------------------------------------------------

    def setup(self, username: str, password: str) -> bool:
        """First-run creation of the main account. Only works if nothing is configured yet."""
        with self._setup_lock:
            if self.is_configured:
                return False
            username = username.strip().lower()
            if not USERNAME_RE.match(username) or password_problem(password, username):
                return False
            with self._config_lock:
                self._config = {"main": username, "users": {username: {
                    "password_hash": _hash_password(password), "created": time.time(), "role": "main", "full_access": True,
                    "must_change_password": False, "must_setup_totp": False}}}
                self._save()
            return True

    def create_user(self, username: str, password: Optional[str] = None, full_access: bool = False,
                    share_main_authenticator: bool = False) -> Dict[str, Any]:
        """Create an account. The password is always temporary (the account must change it at first login), whether the main
        account typed one or it is generated. Returns {"temporary_password", "backup_codes"} or {"error"}."""
        username = (username or "").strip().lower()
        if not USERNAME_RE.match(username):
            return {"error": "Usernames are 2 to 32 characters: lowercase letters, digits, dot, dash or underscore, starting with a letter or digit."}
        if password is not None and password != "" and len(password) < TEMP_PASSWORD_MIN_LENGTH:
            return {"error": f"A temporary password needs at least {TEMP_PASSWORD_MIN_LENGTH} characters (or leave it empty to have one made). The account chooses its own at first sign-in."}
        temp = password or _temp_password()
        with self._config_lock:
            if username in self._users():
                return {"error": "That username is already taken."}
            main = self._users().get(self.main_user or "", {})
            record = {"password_hash": _hash_password(temp), "created": time.time(), "role": "user", "full_access": bool(full_access),
                      "must_change_password": True, "must_setup_totp": True}
            backup: List[str] = []
            if share_main_authenticator:
                if not (main.get("totp_enabled") and main.get("totp_secret")):
                    return {"error": "Turn on two-factor authentication for the main account first, then its authenticator can be reused."}
                backup = [secrets.token_hex(4) for _ in range(8)]
                record.update({"totp_secret": main["totp_secret"], "totp_enabled": True, "totp_backup_codes": backup,
                               "authenticator_shared": True, "must_setup_totp": False})
            self._users()[username] = record
            self._save()
        return {"username": username, "temporary_password": temp, "backup_codes": backup}

    def reset_password(self, username: str) -> Dict[str, Any]:
        """A new temporary password; the account must change it at its next login and every signed-in device is signed out."""
        with self._config_lock:
            record = self._users().get(username)
            if record is None:
                return {"error": "No such account."}
            temp = _temp_password()
            record["password_hash"] = _hash_password(temp)
            record["must_change_password"] = True
            self._save()
        self.revoke_user_sessions(username)
        return {"username": username, "temporary_password": temp}

    def reset_authenticator(self, username: str) -> bool:
        """Remove the account's authenticator; it must set up its own at next login. Not for the main account (that is Security)."""
        with self._config_lock:
            record = self._users().get(username)
            if record is None or self.is_main(username):
                return False
            for key in ("totp_secret", "totp_secret_pending", "totp_backup_codes", "authenticator_shared"):
                record.pop(key, None)
            record["totp_enabled"] = False
            record["must_setup_totp"] = True
            self._save()
        self.revoke_user_sessions(username)
        return True

    def set_full_access(self, username: str, full_access: bool) -> bool:
        with self._config_lock:
            record = self._users().get(username)
            if record is None or self.is_main(username):
                return False
            record["full_access"] = bool(full_access)
            self._save()
        return True

    def delete_user(self, username: str) -> bool:
        with self._config_lock:
            if username not in self._users() or self.is_main(username):
                return False
            del self._users()[username]
            self._save()
        self.revoke_user_sessions(username)
        return True

    def verify_password(self, username: str, password: str) -> bool:
        record = self._users().get((username or "").strip().lower())
        if not record:
            return False
        return _verify_password(password, record.get("password_hash", ""))

    def change_password(self, username: str, current_password: str, new_password: str) -> bool:
        record = self._users().get(username or "")
        if not record or not _verify_password(current_password, record.get("password_hash", "")):
            return False
        if password_problem(new_password, username) or new_password == current_password:
            return False
        with self._config_lock:
            record["password_hash"] = _hash_password(new_password)
            record["must_change_password"] = False
            self._save()
        return True

    # ------------------------------------------------------------------
    # TOTP two-factor authentication (each account has its own record)
    # ------------------------------------------------------------------

    def totp_enabled_for(self, username: str) -> bool:
        return bool(self._users().get(username or "", {}).get("totp_enabled"))

    def totp_generate_secret(self, username: str) -> Optional[str]:
        """A new pending TOTP secret for this account (not enabled until confirmed with a code)."""
        record = self._users().get(username or "")
        if record is None:
            return None
        secret = pyotp.random_base32()
        with self._config_lock:
            record["totp_secret_pending"] = secret
            self._save()
        return secret

    def totp_provisioning_uri(self, username: str, secret: str) -> str:
        return pyotp.TOTP(secret).provisioning_uri(name=username or "athena", issuer_name="Athena")

    def totp_confirm_enable(self, username: str, code: str) -> Optional[list]:
        """Check a code against the pending secret; if right, turn 2FA on and return fresh backup codes. None on failure."""
        record = self._users().get(username or "")
        secret = (record or {}).get("totp_secret_pending")
        if not secret or not pyotp.TOTP(secret).verify(code, valid_window=1):
            return None
        backup = [secrets.token_hex(4) for _ in range(8)]
        with self._config_lock:
            record.update({"totp_secret": secret, "totp_enabled": True, "totp_backup_codes": backup, "must_setup_totp": False})
            record.pop("totp_secret_pending", None)
            record.pop("authenticator_shared", None)
            self._save()
        return backup

    def totp_verify(self, username: str, code: str) -> bool:
        """Check a TOTP code (or consume a backup code) at login. True straight away if this account has no 2FA."""
        record = self._users().get(username or "")
        if record is None:
            return False
        if not record.get("totp_enabled"):
            return True
        secret = record.get("totp_secret")
        if not secret:
            return False  # marked enabled but the secret is missing -- fail closed
        backup = record.get("totp_backup_codes", [])
        if code in backup:
            with self._config_lock:
                backup.remove(code)
                record["totp_backup_codes"] = backup
                self._save()
            return True
        return pyotp.TOTP(secret).verify(code, valid_window=1)

    def totp_disable(self, username: str, password: str) -> bool:
        """Turn 2FA off for this account. Needs the account's password. An account that must have its own authenticator is not
        allowed to remove it (it would just be asked to set one up again)."""
        record = self._users().get(username or "")
        if record is None or not _verify_password(password, record.get("password_hash", "")):
            return False
        with self._config_lock:
            for key in ("totp_secret", "totp_secret_pending", "totp_backup_codes", "authenticator_shared"):
                record.pop(key, None)
            record["totp_enabled"] = False
            if not self.is_main(username):
                record["must_setup_totp"] = True
            self._save()
        return True

    # ------------------------------------------------------------------
    # Session tokens
    # ------------------------------------------------------------------

    def create_pending_2fa(self, username: str) -> str:
        """A short-lived, single-use token proving the password step already passed for this account; the TOTP step must
        present it with the code. In memory only -- a 5 minute window surviving a restart is not worth the complexity."""
        token = secrets.token_hex(24)
        with self._sessions_lock:
            self._pending_2fa[token] = (username, time.time() + 300)
        return token

    def consume_pending_2fa(self, token: Optional[str]) -> Optional[str]:
        """Single use. Returns the account name the password step passed for, or None."""
        if not token:
            return None
        with self._sessions_lock:
            entry = self._pending_2fa.pop(token, None)
        if entry is None or time.time() > entry[1]:
            return None
        return entry[0]

    def create_session(self, username: str) -> str:
        token = secrets.token_hex(32)
        with self._sessions_lock:
            self._sessions[token] = {"expiry": time.time() + TOKEN_TTL, "username": username}
        self._save_sessions()
        return token

    def session_user(self, token: Optional[str]) -> Optional[str]:
        """The account a valid token belongs to, else None. A token for an account that no longer exists is dropped."""
        if not token:
            return None
        with self._sessions_lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if time.time() > session["expiry"]:
                self._sessions.pop(token, None)
                self._save_sessions()
                return None
            name = session.get("username") or self.main_user      # a session from before there were several accounts is the main account's
        if name not in self._users():
            self.revoke_token(token)
            return None
        return name

    def validate_token(self, token: Optional[str]) -> bool:
        return self.session_user(token) is not None

    def revoke_token(self, token: str):
        with self._sessions_lock:
            self._sessions.pop(token, None)
        self._save_sessions()

    def revoke_user_sessions(self, username: str):
        with self._sessions_lock:
            self._sessions = {t: s for t, s in self._sessions.items() if (s.get("username") or self.main_user) != username}
        self._save_sessions()

    def revoke_all_sessions(self):
        with self._sessions_lock:
            self._sessions = {}
        self._save_sessions()

    def status(self, token: Optional[str]) -> Dict[str, Any]:
        user = self.session_user(token)
        info = self.public_user(user) if user else None
        return {
            "configured": self.is_configured,
            "authenticated": user is not None,
            "username": user,
            "is_main": bool(info and info["is_main"]),
            "full_access": bool(info and info["full_access"]),
            "totp_enabled": bool(info and info["totp_enabled"]),
            "authenticator_shared": bool(info and info["authenticator_shared"]),
            "onboarding": self.onboarding(user),
            "password_rules": PASSWORD_RULES,
        }
