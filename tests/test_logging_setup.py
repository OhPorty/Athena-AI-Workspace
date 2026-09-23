import json
import os
import threading

import logging_setup
from logging_setup import log_trace_event


def _read_trace_lines():
    if not os.path.exists(logging_setup.TRACE_LOG_PATH):
        return []
    with open(logging_setup.TRACE_LOG_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def test_log_trace_event_writes_isolated_path(tmp_path):
    assert logging_setup.TRACE_LOG_PATH.startswith(str(tmp_path))


def test_log_trace_event_writes_well_formed_json_line():
    log_trace_event("sess-1", "turn_start", mode="workspace", model="llama3")
    lines = _read_trace_lines()
    assert len(lines) == 1
    record = lines[0]
    assert record["session_id"] == "sess-1"
    assert record["event"] == "turn_start"
    assert record["mode"] == "workspace"
    assert record["model"] == "llama3"
    assert isinstance(record["ts"], (int, float))


def test_log_trace_event_appends_multiple_lines():
    log_trace_event("sess-1", "turn_start")
    log_trace_event("sess-1", "tool_call", tool="bash")
    log_trace_event("sess-1", "turn_end", outcome="completed")
    lines = _read_trace_lines()
    assert [r["event"] for r in lines] == ["turn_start", "tool_call", "turn_end"]


def test_log_trace_event_stringifies_otherwise_unserializable_fields():
    # json.dumps(..., default=str) means most non-JSON-native objects
    # (here, a plain object()) get stringified rather than crashing the
    # whole write -- confirms fields don't need to be pre-sanitized by
    # every call site.
    log_trace_event("sess-1", "tool_result", output=object())
    lines = _read_trace_lines()
    assert len(lines) == 1
    assert "object at 0x" in lines[0]["output"]


def test_log_trace_event_gives_up_quietly_when_totally_unserializable(monkeypatch):
    # Simulates even the minimal fallback record failing to serialize --
    # the true last-resort path. Must not raise; no line gets written
    # since there's genuinely nothing safe left to write.
    monkeypatch.setattr(logging_setup.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(TypeError("nope")))
    log_trace_event("sess-1", "tool_result", output="anything")
    assert _read_trace_lines() == []


def test_log_trace_event_never_raises_on_write_failure(monkeypatch):
    monkeypatch.setattr(logging_setup, "TRACE_LOG_PATH", "/nonexistent-dir-xyz/cannot-write-here.jsonl")
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("no permission")))
    log_trace_event("sess-1", "turn_start")  # must not raise


def test_log_trace_event_thread_safe_concurrent_writes():
    def write_many(n, tag):
        for i in range(n):
            log_trace_event(f"sess-{tag}", "tool_call", tool=f"tool-{tag}-{i}")

    threads = [threading.Thread(target=write_many, args=(50, tag)) for tag in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = _read_trace_lines()
    assert len(lines) == 8 * 50  # every line parsed cleanly -- no interleaved/corrupted writes
    seen = {(r["session_id"], r["tool"]) for r in lines}
    assert len(seen) == 8 * 50  # every individual write is distinct and present, none lost or duplicated
