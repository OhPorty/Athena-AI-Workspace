import pytest

import db
import logging_setup
import rag
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


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return str(ws)
