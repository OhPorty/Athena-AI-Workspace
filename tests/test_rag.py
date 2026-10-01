import os

import numpy as np
import pytest

import main
import rag


def test_search_codebase_description_has_read_file_off_ramp():
    schema = next(s for s in main.RAG_TOOL_SCHEMAS if s["function"]["name"] == "search_codebase")
    description = schema["function"]["description"]
    assert "switch to read_file or bash" in description


def test_score_confidence_label_bands():
    assert main._score_confidence_label(0.9) == "high"
    assert main._score_confidence_label(main._RAG_HIGH_CONFIDENCE_FLOOR) == "high"
    assert main._score_confidence_label(main._RAG_HIGH_CONFIDENCE_FLOOR - 0.01) == "uncertain"
    assert main._score_confidence_label(main._RAG_LOW_CONFIDENCE_CEILING) == "uncertain"
    assert main._score_confidence_label(main._RAG_LOW_CONFIDENCE_CEILING - 0.01) == "low"
    assert main._score_confidence_label(0.0) == "low"


def test_search_codebase_labels_results_and_notes_low_confidence(monkeypatch):
    monkeypatch.setattr(main.rag, "search", lambda query, limit=3, workspace=None: [
        {"filepath": "a.py", "content": "x", "score": 0.65},
        {"filepath": "b.py", "content": "y", "score": 0.61},
    ])
    result = main._search_codebase("something")
    assert [r["confidence"] for r in result["results"]] == ["uncertain", "uncertain"]
    assert "note" in result
    assert "high-confidence" in result["note"]


def test_search_codebase_omits_note_when_a_result_is_high_confidence(monkeypatch):
    monkeypatch.setattr(main.rag, "search", lambda query, limit=3, workspace=None: [
        {"filepath": "a.py", "content": "x", "score": 0.9},
    ])
    result = main._search_codebase("something")
    assert result["results"][0]["confidence"] == "high"
    assert "note" not in result


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    """index_codebase/search only need *some* deterministic vector per
    chunk of text -- avoid loading the real ~130MB fastembed model."""
    def _fake_embed(texts):
        return np.ones((len(texts), rag.EMBEDDING_DIM), dtype=np.float32)

    monkeypatch.setattr(rag, "_embed", _fake_embed)


def test_index_codebase_does_not_duplicate_same_file_under_different_path_forms(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.py").write_text("def foo():\n    return 1\n")
    (src_dir / "b.py").write_text("def bar():\n    return 2\n")

    db_path = tmp_path / "test_index.db"
    r = rag.SimpleCodeRAG(db_path=str(db_path))

    absolute_form = str(src_dir)
    relative_form = os.path.relpath(src_dir, os.getcwd())

    r.index_codebase(absolute_form)
    r.index_codebase(relative_form)

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT filepath) FROM chunks")
    distinct_filepaths = cur.fetchone()[0]
    conn.close()

    assert distinct_filepaths == 2  # a.py and b.py, not 4


def test_index_codebase_stores_canonical_realpath(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.py").write_text("def foo():\n    return 1\n")

    db_path = tmp_path / "test_index.db"
    r = rag.SimpleCodeRAG(db_path=str(db_path))
    r.index_codebase(os.path.relpath(src_dir, os.getcwd()))

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT filepath FROM chunks")
    stored = [row[0] for row in cur.fetchall()]
    conn.close()

    assert stored
    assert all(p == os.path.realpath(p) for p in stored)


def test_search_scoped_to_workspace_excludes_other_indexed_projects(tmp_path):
    """The index accumulates chunks from every project Athena has ever
    touched (her own root at startup, plus any workspace a user has edited
    a file in) in one shared table with no partitioning of its own -- a
    session whose workspace is project A must never get project B's chunks
    back just because both happen to be in the same physical index file."""
    project_a = tmp_path / "project_a"
    project_a.mkdir()
    (project_a / "a.py").write_text("def alpha():\n    return 1\n")

    project_b = tmp_path / "project_b"
    project_b.mkdir()
    (project_b / "b.py").write_text("def beta():\n    return 2\n")

    db_path = tmp_path / "test_index.db"
    r = rag.SimpleCodeRAG(db_path=str(db_path))
    r.index_codebase(str(project_a))
    r.index_codebase(str(project_b))

    unscoped = r.search("anything", limit=10)
    assert {os.path.basename(res["filepath"]) for res in unscoped} == {"a.py", "b.py"}

    scoped_to_a = r.search("anything", limit=10, workspace=str(project_a))
    assert {os.path.basename(res["filepath"]) for res in scoped_to_a} == {"a.py"}

    scoped_to_b = r.search("anything", limit=10, workspace=str(project_b))
    assert {os.path.basename(res["filepath"]) for res in scoped_to_b} == {"b.py"}
