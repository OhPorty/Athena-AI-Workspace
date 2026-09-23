import json
import threading

import pytest

import main


class _FakeStreamResponse:
    def __init__(self, status_code=200, lines=None, error_body=""):
        self.status_code = status_code
        self._lines = lines or []
        self._error_body = error_body.encode()

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return self._error_body


class _FakeStreamContextManager:
    def __init__(self, captured, method, url, response, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *a):
        return False


def _capture_ollama_payload(monkeypatch, think, response=None):
    captured = {}
    monkeypatch.setattr(
        main.httpx, "stream",
        lambda method, url, **kwargs: _FakeStreamContextManager(captured, method, url, response or _FakeStreamResponse(), **kwargs),
    )
    req = main.ChatIn(session_id="s1", message="hi", model="test-model", think=think)
    chunks = list(main._stream_completion(req, "http://fake-ollama/api/chat", [], [], 4096, threading.Event()))
    return captured["json"], chunks


@pytest.mark.parametrize("level,expected", [
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
    ("max", "max"),
])
def test_think_level_passes_through_unchanged(monkeypatch, level, expected):
    payload, _ = _capture_ollama_payload(monkeypatch, level)
    assert payload["think"] == expected


def test_think_none_translates_to_boolean_false(monkeypatch):
    payload, _ = _capture_ollama_payload(monkeypatch, "none")
    assert payload["think"] is False


def test_think_unset_sends_no_think_field(monkeypatch):
    payload, _ = _capture_ollama_payload(monkeypatch, "")
    assert "think" not in payload


def test_think_does_not_affect_other_payload_fields(monkeypatch):
    payload, _ = _capture_ollama_payload(monkeypatch, "high")
    assert payload["model"] == "test-model"
    assert payload["stream"] is True


# --- Non-200 Ollama responses surface a real error instead of silently
# completing with nothing (the exact bug behind a 500 from an
# out-of-memory/unsupported-quantization model load looking like a
# blank reply) ---


def test_ollama_500_surfaces_friendly_error_chunk(monkeypatch):
    response = _FakeStreamResponse(status_code=500, error_body=json.dumps({"error": "model requires more system memory than is available"}))
    _, chunks = _capture_ollama_payload(monkeypatch, "", response=response)
    assert len(chunks) == 1
    content = chunks[0]["message"]["content"]
    assert chunks[0]["done"] is True
    assert "model requires more system memory" in content
    assert "Ollama itself returned an error" in content


def test_ollama_unknown_architecture_gets_specific_explanation_not_ram_guess(monkeypatch):
    response = _FakeStreamResponse(status_code=500, error_body=json.dumps({"error": "error loading model: unknown model architecture: 'spark2_5'"}))
    _, chunks = _capture_ollama_payload(monkeypatch, "", response=response)
    content = chunks[0]["message"]["content"]
    assert "isn't supported by llama.cpp" in content
    assert "not a RAM/VRAM/quantization issue" in content
    assert "spark2_5" in content
    assert "not enough RAM/VRAM for it" not in content  # the generic 500 guess must not also show


def test_ollama_404_surfaces_model_not_found_hint(monkeypatch):
    response = _FakeStreamResponse(status_code=404, error_body=json.dumps({"error": "model 'foo' not found"}))
    _, chunks = _capture_ollama_payload(monkeypatch, "", response=response)
    content = chunks[0]["message"]["content"]
    assert "doesn't have this model pulled" in content
    assert "model 'foo' not found" in content


def test_ollama_error_with_non_json_body_falls_back_to_raw_text(monkeypatch):
    response = _FakeStreamResponse(status_code=500, error_body="internal server error, not json")
    _, chunks = _capture_ollama_payload(monkeypatch, "", response=response)
    content = chunks[0]["message"]["content"]
    assert "internal server error, not json" in content


def test_ollama_success_does_not_trigger_error_path(monkeypatch):
    lines = [json.dumps({"message": {"content": "hi"}, "done": True})]
    response = _FakeStreamResponse(status_code=200, lines=lines)
    _, chunks = _capture_ollama_payload(monkeypatch, "", response=response)
    assert chunks == [{"message": {"content": "hi"}, "done": True}]
