import pyotp
import pytest

from auth import AuthManager, PASSWORD_MIN_LENGTH


@pytest.fixture
def manager(tmp_path):
    return AuthManager(str(tmp_path / "athena_auth.json"))


def test_setup_creates_account(manager):
    assert manager.is_configured is False
    ok = manager.setup("Alice", "correct horse battery")
    assert ok is True
    assert manager.is_configured is True
    assert manager.username == "alice"  # normalized lowercase


def test_setup_rejects_short_password(manager):
    ok = manager.setup("alice", "short")
    assert ok is False
    assert manager.is_configured is False


def test_setup_only_works_once(manager):
    manager.setup("alice", "correct horse battery")
    ok = manager.setup("bob", "another long password")
    assert ok is False
    assert manager.username == "alice"


def test_verify_password(manager):
    manager.setup("alice", "correct horse battery")
    assert manager.verify_password("alice", "correct horse battery") is True
    assert manager.verify_password("alice", "wrong password") is False
    assert manager.verify_password("bob", "correct horse battery") is False


def test_change_password(manager):
    manager.setup("alice", "correct horse battery")
    ok = manager.change_password("correct horse battery", "new long password")
    assert ok is True
    assert manager.verify_password("alice", "new long password") is True
    assert manager.verify_password("alice", "correct horse battery") is False


def test_change_password_requires_correct_current(manager):
    manager.setup("alice", "correct horse battery")
    ok = manager.change_password("wrong current", "new long password")
    assert ok is False
    assert manager.verify_password("alice", "correct horse battery") is True


def test_change_password_rejects_short_new_password(manager):
    manager.setup("alice", "correct horse battery")
    ok = manager.change_password("correct horse battery", "short")
    assert ok is False


def test_totp_full_lifecycle(manager):
    manager.setup("alice", "correct horse battery")
    secret = manager.totp_generate_secret()
    assert secret

    code = pyotp.TOTP(secret).now()
    backup_codes = manager.totp_confirm_enable(code)
    assert backup_codes is not None
    assert len(backup_codes) == 8
    assert manager.totp_enabled is True

    fresh_code = pyotp.TOTP(secret).now()
    assert manager.totp_verify(fresh_code) is True

    used_backup = backup_codes[0]
    assert manager.totp_verify(used_backup) is True
    assert manager.totp_verify(used_backup) is False  # single-use

    assert manager.totp_disable("correct horse battery") is True
    assert manager.totp_enabled is False


def test_totp_confirm_enable_rejects_wrong_code(manager):
    manager.setup("alice", "correct horse battery")
    manager.totp_generate_secret()
    assert manager.totp_confirm_enable("000000") is None
    assert manager.totp_enabled is False


def test_totp_verify_passes_when_2fa_disabled(manager):
    manager.setup("alice", "correct horse battery")
    assert manager.totp_verify("anything") is True


def test_totp_disable_requires_correct_password(manager):
    manager.setup("alice", "correct horse battery")
    secret = manager.totp_generate_secret()
    manager.totp_confirm_enable(pyotp.TOTP(secret).now())
    assert manager.totp_disable("wrong password") is False
    assert manager.totp_enabled is True


def test_session_token_lifecycle(manager):
    token = manager.create_session()
    assert manager.validate_token(token) is True
    manager.revoke_token(token)
    assert manager.validate_token(token) is False


def test_validate_token_rejects_unknown_or_empty(manager):
    assert manager.validate_token(None) is False
    assert manager.validate_token("not-a-real-token") is False


def test_expired_token_fails_validation(manager, monkeypatch):
    import time as time_module
    token = manager.create_session()
    future = time_module.time() + 60 * 60 * 24 * 31  # past the 30-day TTL
    monkeypatch.setattr(time_module, "time", lambda: future)
    assert manager.validate_token(token) is False


def test_revoke_all_sessions(manager):
    t1 = manager.create_session()
    t2 = manager.create_session()
    manager.revoke_all_sessions()
    assert manager.validate_token(t1) is False
    assert manager.validate_token(t2) is False


def test_pending_2fa_token_single_use(manager):
    token = manager.create_pending_2fa()
    assert manager.consume_pending_2fa(token) is True
    assert manager.consume_pending_2fa(token) is False


def test_policy_reports_min_length():
    assert PASSWORD_MIN_LENGTH == 8
