"""
Athena single-user authentication -- adapted from Odysseus's proven
core/auth.py, stripped down from multi-user/admin/privilege management
to a single account, since Athena is a single-user self-hosted tool.
Config stored in athena_auth.json; sessions in athena_sessions.json.
Uses bcrypt for password hashing and pyotp for TOTP 2FA, same as the
reference implementation.
"""

import json
import os
import secrets
import threading
import time
from typing import Optional, Dict, Any

import bcrypt
import pyotp

TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days -- single-user tool, favor convenience
PASSWORD_MIN_LENGTH = 8


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def _atomic_write_json(path: str, data: dict):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


class AuthManager:
    """Single-user password + session-token auth, with optional TOTP 2FA."""

    def __init__(self, auth_path: str):
        self.auth_path = auth_path
        self._sessions_path = os.path.join(os.path.dirname(auth_path), "athena_sessions.json")
        self._config: Dict[str, Any] = {}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._pending_2fa: Dict[str, float] = {}
        self._sessions_lock = threading.RLock()
        self._config_lock = threading.Lock()
        self._setup_lock = threading.Lock()
        self._load()
        self._load_sessions()

    def _load(self):
        try:
            if os.path.exists(self.auth_path):
                with open(self.auth_path, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
            else:
                self._config = {}
        except Exception:
            self._config = {}

    def _save(self):
        _atomic_write_json(self.auth_path, self._config)

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

    @property
    def is_configured(self) -> bool:
        return bool(self._config.get("username")) and bool(self._config.get("password_hash"))

    @property
    def username(self) -> Optional[str]:
        return self._config.get("username")

    def policy(self) -> dict:
        return {
            "password_min_length": PASSWORD_MIN_LENGTH,
            "session_days": TOKEN_TTL // 86400,
        }

    # ------------------------------------------------------------------
    # First-run setup / password
    # ------------------------------------------------------------------

    def setup(self, username: str, password: str) -> bool:
        """First-run account creation. Only works if not already configured."""
        with self._setup_lock:
            if self.is_configured:
                return False
            username = username.strip().lower()
            if not username or len(password) < PASSWORD_MIN_LENGTH:
                return False
            with self._config_lock:
                self._config = {
                    "username": username,
                    "password_hash": _hash_password(password),
                    "created": time.time(),
                }
                self._save()
            return True

    def verify_password(self, username: str, password: str) -> bool:
        if not self.is_configured:
            return False
        if username.strip().lower() != self._config.get("username"):
            return False
        return _verify_password(password, self._config["password_hash"])

    def change_password(self, current_password: str, new_password: str) -> bool:
        if not self.is_configured:
            return False
        if not _verify_password(current_password, self._config["password_hash"]):
            return False
        if len(new_password) < PASSWORD_MIN_LENGTH:
            return False
        with self._config_lock:
            self._config["password_hash"] = _hash_password(new_password)
            self._save()
        return True

    # ------------------------------------------------------------------
    # TOTP two-factor authentication
    # ------------------------------------------------------------------

    @property
    def totp_enabled(self) -> bool:
        return bool(self._config.get("totp_enabled"))

    def totp_generate_secret(self) -> Optional[str]:
        """Generate a new pending TOTP secret (not yet enabled until confirmed)."""
        if not self.is_configured:
            return None
        secret = pyotp.random_base32()
        with self._config_lock:
            self._config["totp_secret_pending"] = secret
            self._save()
        return secret

    def totp_provisioning_uri(self, secret: str) -> str:
        totp = pyotp.TOTP(secret)
        return totp.provisioning_uri(name=self.username or "athena", issuer_name="Athena")

    def totp_confirm_enable(self, code: str) -> Optional[list]:
        """Verify a code against the pending secret; if correct, enable 2FA
        and return freshly generated backup codes. Returns None on failure."""
        secret = self._config.get("totp_secret_pending")
        if not secret:
            return None
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=1):
            return None
        backup = [secrets.token_hex(4) for _ in range(8)]
        with self._config_lock:
            self._config["totp_secret"] = secret
            self._config["totp_enabled"] = True
            self._config.pop("totp_secret_pending", None)
            self._config["totp_backup_codes"] = backup
            self._save()
        return backup

    def totp_verify(self, code: str) -> bool:
        """Verify a TOTP code (or consume a backup code) at login time.
        Returns True immediately if 2FA isn't enabled at all."""
        if not self.totp_enabled:
            return True
        secret = self._config.get("totp_secret")
        if not secret:
            return False  # 2FA marked enabled but secret missing -- fail closed
        backup = self._config.get("totp_backup_codes", [])
        if code in backup:
            with self._config_lock:
                backup.remove(code)
                self._config["totp_backup_codes"] = backup
                self._save()
            return True
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=1)

    def totp_disable(self, password: str) -> bool:
        """Disable 2FA. Requires password confirmation."""
        if not _verify_password(password, self._config.get("password_hash", "")):
            return False
        with self._config_lock:
            self._config.pop("totp_secret", None)
            self._config.pop("totp_secret_pending", None)
            self._config.pop("totp_backup_codes", None)
            self._config["totp_enabled"] = False
            self._save()
        return True

    # ------------------------------------------------------------------
    # Session tokens
    # ------------------------------------------------------------------

    def create_pending_2fa(self) -> str:
        """Issue a short-lived, single-use token proving the password step
        already passed, which the TOTP-verify step must present alongside
        the code. Without this, the 2FA endpoint would accept a guessed
        code from anyone who never actually knew the password. Kept in
        memory only (not persisted) since a 5-minute window surviving a
        restart isn't worth the complexity."""
        token = secrets.token_hex(24)
        with self._sessions_lock:
            self._pending_2fa[token] = time.time() + 300  # 5 minutes
        return token

    def consume_pending_2fa(self, token: Optional[str]) -> bool:
        """Single-use: valid only once, and only within its window."""
        if not token:
            return False
        with self._sessions_lock:
            expiry = self._pending_2fa.pop(token, None)
        return expiry is not None and time.time() <= expiry

    def create_session(self) -> str:
        """Issue a new session token for the (already-verified) user."""
        token = secrets.token_hex(32)
        with self._sessions_lock:
            self._sessions[token] = {"expiry": time.time() + TOKEN_TTL}
        self._save_sessions()
        return token

    def validate_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._sessions_lock:
            session = self._sessions.get(token)
            if session is None:
                return False
            if time.time() > session["expiry"]:
                self._sessions.pop(token, None)
                self._save_sessions()
                return False
        return True

    def revoke_token(self, token: str):
        with self._sessions_lock:
            self._sessions.pop(token, None)
        self._save_sessions()

    def revoke_all_sessions(self):
        with self._sessions_lock:
            self._sessions = {}
        self._save_sessions()

    def status(self, token: Optional[str]) -> Dict[str, Any]:
        return {
            "configured": self.is_configured,
            "authenticated": self.validate_token(token),
            "username": self.username if self.is_configured else None,
            "totp_enabled": self.totp_enabled,
        }
