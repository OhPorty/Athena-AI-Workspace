import pyotp
import pytest

from auth import AuthManager, PASSWORD_MIN_LENGTH, password_problem


@pytest.fixture
def manager(tmp_path):
    return AuthManager(str(tmp_path / "athena_auth.json"))


def test_setup_creates_account(manager):
    assert manager.is_configured is False
    ok = manager.setup("Alice", "Correct-Horse-Battery-7")
    assert ok is True
    assert manager.is_configured is True
    assert manager.username == "alice"  # normalized lowercase


def test_setup_rejects_short_password(manager):
    ok = manager.setup("alice", "short")
    assert ok is False
    assert manager.is_configured is False


def test_setup_only_works_once(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    ok = manager.setup("bob", "another long password")
    assert ok is False
    assert manager.username == "alice"


def test_verify_password(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    assert manager.verify_password("alice", "Correct-Horse-Battery-7") is True
    assert manager.verify_password("alice", "wrong password") is False
    assert manager.verify_password("bob", "Correct-Horse-Battery-7") is False


def test_change_password(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    ok = manager.change_password("alice", "Correct-Horse-Battery-7", "New-Long-Phrase-8!")
    assert ok is True
    assert manager.verify_password("alice", "New-Long-Phrase-8!") is True
    assert manager.verify_password("alice", "Correct-Horse-Battery-7") is False


def test_change_password_requires_correct_current(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    ok = manager.change_password("alice", "wrong current", "New-Long-Phrase-8!")
    assert ok is False
    assert manager.verify_password("alice", "Correct-Horse-Battery-7") is True


def test_change_password_rejects_short_new_password(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    ok = manager.change_password("alice", "Correct-Horse-Battery-7", "short")
    assert ok is False


def test_totp_full_lifecycle(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    secret = manager.totp_generate_secret("alice")
    assert secret

    code = pyotp.TOTP(secret).now()
    backup_codes = manager.totp_confirm_enable("alice", code)
    assert backup_codes is not None
    assert len(backup_codes) == 8
    assert manager.totp_enabled_for("alice") is True

    fresh_code = pyotp.TOTP(secret).now()
    assert manager.totp_verify("alice", fresh_code) is True

    used_backup = backup_codes[0]
    assert manager.totp_verify("alice", used_backup) is True
    assert manager.totp_verify("alice", used_backup) is False  # single-use

    assert manager.totp_disable("alice", "Correct-Horse-Battery-7") is True
    assert manager.totp_enabled_for("alice") is False


def test_totp_confirm_enable_rejects_wrong_code(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    manager.totp_generate_secret("alice")
    assert manager.totp_confirm_enable("alice", "000000") is None
    assert manager.totp_enabled_for("alice") is False


def test_totp_verify_passes_when_2fa_disabled(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    assert manager.totp_verify("alice", "anything") is True


def test_totp_disable_requires_correct_password(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    secret = manager.totp_generate_secret("alice")
    manager.totp_confirm_enable("alice", pyotp.TOTP(secret).now())
    assert manager.totp_disable("alice", "wrong password") is False
    assert manager.totp_enabled_for("alice") is True


def test_session_token_lifecycle(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    token = manager.create_session("alice")
    assert manager.validate_token(token) is True
    manager.revoke_token(token)
    assert manager.validate_token(token) is False


def test_validate_token_rejects_unknown_or_empty(manager):
    assert manager.validate_token(None) is False
    assert manager.validate_token("not-a-real-token") is False


def test_expired_token_fails_validation(manager, monkeypatch):
    manager.setup("alice", "Correct-Horse-Battery-7")
    import time as time_module
    token = manager.create_session("alice")
    future = time_module.time() + 60 * 60 * 24 * 31  # past the 30-day TTL
    monkeypatch.setattr(time_module, "time", lambda: future)
    assert manager.validate_token(token) is False


def test_revoke_all_sessions(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    t1 = manager.create_session("alice")
    t2 = manager.create_session("alice")
    manager.revoke_all_sessions()
    assert manager.validate_token(t1) is False
    assert manager.validate_token(t2) is False


def test_pending_2fa_token_single_use(manager):
    token = manager.create_pending_2fa("alice")
    assert manager.consume_pending_2fa(token) == "alice"
    assert manager.consume_pending_2fa(token) is None


def test_policy_reports_min_length():
    assert PASSWORD_MIN_LENGTH == 10


# ---------- several accounts ----------

@pytest.fixture
def two(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    secret = manager.totp_generate_secret("alice")
    manager.totp_confirm_enable("alice", pyotp.TOTP(secret).now())
    return manager


def test_old_single_account_file_becomes_the_main_account(tmp_path):
    import json
    path = tmp_path / "athena_auth.json"
    path.write_text(json.dumps({"username": "me", "password_hash": __import__("auth")._hash_password("old password 1"), "created": 1.0}))
    m = AuthManager(str(path))
    assert m.main_user == "me" and m.is_main("me") and m.verify_password("me", "old password 1")
    assert m.onboarding("me") is None and m.can_use_whole_host("me")


def test_old_session_without_a_name_belongs_to_the_main_account(tmp_path):
    import json, time
    (tmp_path / "athena_auth.json").write_text(json.dumps({"username": "me", "password_hash": __import__("auth")._hash_password("old password 1")}))
    (tmp_path / "athena_sessions.json").write_text(json.dumps({"tok": {"expiry": time.time() + 999}}))
    assert AuthManager(str(tmp_path / "athena_auth.json")).session_user("tok") == "me"


def test_new_account_password_is_always_temporary(two):
    made = two.create_user("Bob", "bobs own pick 1")
    assert made["username"] == "bob" and made["temporary_password"] == "bobs own pick 1"
    assert two.verify_password("bob", "bobs own pick 1")
    assert two.onboarding("bob") == "change_password"
    assert two.change_password("bob", "bobs own pick 1", "Fresh#Secret-Lemon-2")
    assert two.onboarding("bob") == "setup_totp"          # its own authenticator comes next


def test_generated_temporary_password_when_none_given(two):
    made = two.create_user("carol")
    assert len(made["temporary_password"]) == 12 and two.verify_password("carol", made["temporary_password"])


def test_new_account_must_set_up_its_own_authenticator(two):
    two.create_user("bob")
    secret = two.totp_generate_secret("bob")
    assert secret != two._users()["alice"]["totp_secret"]
    assert two.totp_confirm_enable("bob", pyotp.TOTP(secret).now())
    assert two.totp_verify("bob", pyotp.TOTP(secret).now())
    assert two._users()["bob"]["totp_secret"] != two._users()["alice"]["totp_secret"]


def test_reusing_the_main_authenticator(two):
    made = two.create_user("bob", share_main_authenticator=True)
    assert len(made["backup_codes"]) == 8
    main_secret = two._users()["alice"]["totp_secret"]
    assert two.totp_enabled_for("bob") and two.totp_verify("bob", pyotp.TOTP(main_secret).now())
    assert two.public_user("bob")["authenticator_shared"] is True
    two.change_password("bob", made["temporary_password"], "Fresh#Secret-Lemon-2")
    assert two.onboarding("bob") is None                   # nothing more to set up


def test_reusing_needs_the_main_authenticator_to_exist(manager):
    manager.setup("alice", "Correct-Horse-Battery-7")
    assert "error" in manager.create_user("bob", share_main_authenticator=True)
    assert not manager.has_user("bob")


def test_usernames_are_checked_and_unique(two):
    assert "error" in two.create_user("Bad Name")
    assert "error" in two.create_user("x")
    assert two.create_user("bob")["username"] == "bob"
    assert "taken" in two.create_user("BOB")["error"]
    assert "error" in two.create_user("dave", "short")


def test_main_account_cannot_be_deleted_or_limited(two):
    assert two.delete_user("alice") is False
    assert two.set_full_access("alice", False) is False
    assert two.reset_authenticator("alice") is False
    assert two.can_use_whole_host("alice")


def test_full_access_is_per_account(two):
    two.create_user("bob")
    two.create_user("eve", full_access=True)
    assert not two.can_use_whole_host("bob") and two.can_use_whole_host("eve")
    two.set_full_access("bob", True)
    assert two.can_use_whole_host("bob")


def test_reset_password_signs_the_account_out_and_makes_it_temporary(two):
    made = two.create_user("bob")
    two.change_password("bob", made["temporary_password"], "Fresh#Secret-Lemon-2")
    token = two.create_session("bob")
    again = two.reset_password("bob")
    assert not two.validate_token(token)
    assert two.verify_password("bob", again["temporary_password"]) and two.onboarding("bob") == "change_password"


def test_delete_account_signs_it_out(two):
    two.create_user("bob")
    token = two.create_session("bob")
    assert two.delete_user("bob") and not two.validate_token(token) and not two.has_user("bob")


def test_session_knows_which_account_it_is_for(two):
    two.create_user("bob")
    assert two.session_user(two.create_session("bob")) == "bob"
    assert two.session_user(two.create_session("alice")) == "alice"
    assert two.status(two.create_session("bob"))["is_main"] is False


def test_pending_2fa_remembers_the_account(two):
    assert two.consume_pending_2fa(two.create_pending_2fa("bob")) == "bob"


def test_reset_authenticator_forces_a_new_setup(two):
    made = two.create_user("bob", share_main_authenticator=True)
    two.change_password("bob", made["temporary_password"], "Fresh#Secret-Lemon-2")
    assert two.reset_authenticator("bob")
    assert not two.totp_enabled_for("bob") and two.onboarding("bob") == "setup_totp"


# ---------- password rules ----------

@pytest.mark.parametrize("password,fragment", [
    ("123456789", "at least 10 characters"),
    ("1234567890", "lowercase"),
    ("alllowercase1!", "uppercase"),
    ("ALLUPPERCASE1!", "lowercase"),
    ("NoNumbersHere!", "a number"),
    ("NoSymbolsHere12", "a symbol"),
    ("Password123!", "common"),
    ("Qwerty#Zebra99", "common"),
    ("Xk9#aaaaZebra", "repeats"),
    ("Zebra#Lemon-alice9", "username"),
])
def test_weak_passwords_are_refused_with_a_reason(password, fragment):
    assert fragment in (password_problem(password, "alice") or "")


def test_a_good_password_passes():
    assert password_problem("Zebra#Lemon-Stone-47", "alice") is None
    assert password_problem("correct Horse 99 battery!", "alice") is None


def test_setup_and_change_enforce_the_rules(manager):
    assert manager.setup("alice", "12345678901") is False
    assert manager.setup("alice", "Zebra#Lemon-Stone-47") is True
    assert manager.change_password("alice", "Zebra#Lemon-Stone-47", "weakpassword") is False
    assert manager.change_password("alice", "Zebra#Lemon-Stone-47", "Another#Stone-Fruit-9") is True


def test_a_temporary_password_the_main_account_types_only_needs_to_be_8_long(manager):
    manager.setup("alice", "Zebra#Lemon-Stone-47")
    assert "error" not in manager.create_user("bob", "temp1234")
    assert "error" in manager.create_user("carol", "short")
