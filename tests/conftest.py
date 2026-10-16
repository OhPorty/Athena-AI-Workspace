import pytest

import db
import generated_files
import logging_setup
import model_profile
import auth_routes
import backup_tools
import ownership
import rag
import safety
import settings


@pytest.fixture(autouse=True)
def isolated_data_files(tmp_path, monkeypatch):
    """Points every module-level data-file path constant at a fresh file
    under pytest's tmp_path before each test, so no test ever reads or
    writes Athena's real production settings/bots/tasks/ratings data.
    Each *_conn() function creates its own tables on first connect, so
    pointing the constant at a not-yet-existing path is enough."""
    monkeypatch.setattr(settings, "_SETTINGS_PATH", str(tmp_path / "athena_settings.json"))
    monkeypatch.setattr(settings, "_NOTES_PATH", str(tmp_path / "athena_notes.json"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "athena.db"))
    monkeypatch.setattr(logging_setup, "TRACE_LOG_PATH", str(tmp_path / "agent_trace.jsonl"))
    # RAG indexing needs a real embedding model and is out of scope for
    # this phase -- edit_file/replace_lines call it as a side effect, so
    # it's neutered here rather than actually running on every test.
    monkeypatch.setattr(rag, "index_codebase", lambda *a, **k: None)
    # Model-profile probing makes real HTTP calls to the model's endpoint; no test
    # should reach the network (tests that exercise the probes pass a mock transport).
    monkeypatch.setattr(model_profile, "PROBING_ENABLED", False)
    # Tools that produce files (PDFs, preview screenshots) must never write into the real generated_files/.
    monkeypatch.setattr(generated_files, "ROOT", str(tmp_path / "generated_files"))
    # Snapshots and backups of test workspaces must never land in Athena's real file_backups/.
    monkeypatch.setattr(backup_tools, "_BACKUP_ROOT", str(tmp_path / "file_backups"))
    monkeypatch.setattr(safety, "_BACKUP_ROOT", str(tmp_path / "file_backups"))
    monkeypatch.setattr(safety, "TURNS_INDEX", str(tmp_path / "file_backups" / "turns.jsonl"))
    # Accounts and their sessions live in files too; no test may touch the real ones.
    manager = auth_routes.auth_manager
    monkeypatch.setattr(manager, "auth_path", str(tmp_path / "athena_auth.json"))
    monkeypatch.setattr(manager, "_sessions_path", str(tmp_path / "athena_sessions.json"))
    monkeypatch.setattr(manager, "_config", {})
    monkeypatch.setattr(manager, "_sessions", {})
    monkeypatch.setattr(manager, "_pending_2fa", {})
    monkeypatch.setattr(settings, "_USERS_ROOT", str(tmp_path / "user_data"))
    monkeypatch.setattr(ownership, "USERS_ROOT", str(tmp_path / "user_data"))
    model_profile.clear_cache()


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return str(ws)
