"""remember, research, schedule, notify, check, push, and calling a rarely used tool by name."""
import base64
import json
import os
import types

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import agent_tools
import http_ece
import push_notify
import research
import task_scheduler
import workspace_ops
from test_chat_loop_recovery import call, run_turn, text_round, tool_round


def ctx(tmp_path=None, **kw):
    return types.SimpleNamespace(session_id="s1", workspace=str(tmp_path) if tmp_path else "", turn_state={}, approved=False, interactive=True,
                                 req=types.SimpleNamespace(model="qwen", provider="", endpoint_url="http://127.0.0.1:11434", api_key="", message="hi"), **kw)


# ---------- remember ----------

def test_remember_saves_a_fact_once_and_says_so(monkeypatch):
    facts, posted = [], []
    monkeypatch.setattr(httpx, "get", lambda url, **k: types.SimpleNamespace(json=lambda: facts))
    monkeypatch.setattr(httpx, "post", lambda url, json=None, **k: (posted.append(json), facts.append({"content": json["content"]}), types.SimpleNamespace(status_code=200, json=lambda: {"id": len(facts)}))[2])
    first = agent_tools.remember_tool({"fact": "The person keeps bees and sells honey at a weekend market."}, ctx())
    assert first["saved"] is True and posted[0]["source_session_id"] == "s1" and posted[0]["manual"] is False
    again = agent_tools.remember_tool({"fact": "the person keeps bees and sells honey at a weekend market."}, ctx())
    assert again["saved"] is False and len(posted) == 1
    assert "error" in agent_tools.remember_tool({"fact": "hi"}, ctx())


def test_remember_reports_an_unreachable_memory_service(monkeypatch):
    def down(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", down)
    assert "unreachable" in agent_tools.remember_tool({"fact": "A fact long enough to save."}, ctx())["error"]


# ---------- research ----------

def test_research_runs_the_verified_pipeline_with_the_current_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(research, "start_run", lambda q, depth, model, check=None, wait=False, parent_id=None: (seen.update(q=q, depth=depth, model=model, wait=wait), {"id": "r1"})[1])
    monkeypatch.setattr(research, "get_run", lambda i: {"status": "done", "report": "The tower is in Paris. [1](https://a.example)", "claims": [{"supported": True}, {"supported": False}],
                                                       "sources": [{"status": "read"}, {"status": "failed"}]})
    out = agent_tools.research_tool({"question": "Where is the Eiffel Tower?", "depth": "thorough"}, ctx())
    assert seen == {"q": "Where is the Eiffel Tower?", "depth": "thorough", "model": {"model": "qwen", "label": "qwen", "provider": "", "endpoint_url": "http://127.0.0.1:11434", "api_key": ""}, "wait": True}
    assert out["pages_read"] == 1 and out["verified_claims"] == 1 and "Paris" in out["report"] and out["run_id"] == "r1" and "Research page" in out["saved_as"]
    assert agent_tools.research_tool({"question": "x", "depth": "enormous"}, ctx()) and seen["depth"] == "quick"


def test_research_that_fails_says_why(monkeypatch):
    monkeypatch.setattr(research, "start_run", lambda *a, **k: {"id": "r2"})
    monkeypatch.setattr(research, "get_run", lambda i: {"status": "error", "error": "No search engine is set up", "report": ""})
    assert agent_tools.research_tool({"question": "x"}, ctx())["error"] == "No search engine is set up"


# ---------- schedule ----------

def test_schedule_creates_lists_and_cancels_tasks_with_read_only_tools():
    made = agent_tools.schedule_tool({"prompt": "Check the weather and tell me", "in_minutes": 30}, ctx())
    assert made["scheduled"] and "read-only tools" in made["note"]
    daily = agent_tools.schedule_tool({"prompt": "Summarise AI news", "repeat": "daily", "at": "07:30"}, ctx())
    listed = agent_tools.schedule_tool({"action": "list"}, ctx())["tasks"]
    assert {t["id"] for t in listed} == {made["scheduled"], daily["scheduled"]}
    task = next(t for t in task_scheduler.list_tasks() if t["id"] == made["scheduled"])
    assert task["enabled_tools"] == ["web_search", "web_fetch", "read_file"] and task["model"] == "qwen"      # a model cannot hand itself write tools
    recurring = next(t for t in listed if t["id"] == daily["scheduled"])
    assert recurring["recurrence"]["time_of_day"] == "07:30" and recurring["schedule_type"] == "recurring"
    assert agent_tools.schedule_tool({"action": "cancel", "id": made["scheduled"]}, ctx())["cancelled"] == made["scheduled"]
    assert [t["id"] for t in agent_tools.schedule_tool({"action": "list"}, ctx())["tasks"]] == [daily["scheduled"]]


def test_schedule_asks_for_what_is_missing():
    assert "when" in agent_tools.schedule_tool({"prompt": "x"}, ctx())["error"].lower() or "Say when" in agent_tools.schedule_tool({"prompt": "x"}, ctx())["error"]
    assert "prompt" in agent_tools.schedule_tool({"in_minutes": 5}, ctx())["error"]
    assert "at must look like" in agent_tools.schedule_tool({"prompt": "x", "at": "next tuesday"}, ctx())["error"]


# ---------- check ----------

def test_check_finds_the_projects_test_command(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(workspace_ops, "run_command", lambda command, ws, timeout=120: ran.append(command) or {"command": command, "output": "ok", "exit_code": 0})
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    assert agent_tools.check_tool({}, ctx(tmp_path))["passed"] is True and ran == ["npm test --silent"]
    (tmp_path / "package.json").unlink()
    (tmp_path / "tests").mkdir()
    agent_tools.check_tool({"what": "tests"}, ctx(tmp_path))
    assert "pytest" in ran[-1]
    (tmp_path / "tests").rmdir()
    assert "No tests were found" in agent_tools.check_tool({}, ctx(tmp_path))["error"]
    (tmp_path / "bad.py").write_text("def x(:\n")
    syntax = agent_tools.check_tool({"what": "syntax"}, ctx(tmp_path))
    assert syntax["ok"] is False and syntax["syntax_errors"][0]["file"] == "bad.py"


# ---------- notify and push ----------

def test_notify_goes_through_push(monkeypatch):
    sent = []
    monkeypatch.setattr(push_notify, "send", lambda title, body, **k: (sent.append((title, body)), {"sent": 1, "failed": 0, "devices": 1})[1])
    assert agent_tools.notify_tool({"message": "The PDF is ready", "title": "Athena"}, ctx())["sent"] == 1 and sent == [("Athena", "The PDF is ready")]
    assert "error" in agent_tools.notify_tool({}, ctx())


def test_push_messages_are_encrypted_for_the_device_signed_and_expired_devices_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(push_notify, "VAPID_PATH", str(tmp_path / "vapid.pem"))
    client = ec.generate_private_key(ec.SECP256R1())
    pub = client.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    assert push_notify.send("a", "b")["sent"] == 0                                         # no device yet: says how to add one
    assert push_notify.save_subscription("https://fcm.googleapis.com/fcm/send/abc", push_notify._b64(pub), push_notify._b64(auth), "phone")
    assert not push_notify.save_subscription("http://insecure.example/x", "k", "a")        # only https endpoints
    captured = {}
    ok = types.SimpleNamespace(status_code=201)
    out = push_notify.send("Athena", "Done.", post=lambda e, c, h: (captured.update(endpoint=e, content=c, headers=h), ok)[1])
    assert out == {"sent": 1, "failed": 0, "devices": 1}
    plain = http_ece.decrypt(captured["content"], private_key=client, auth_secret=auth, version="aes128gcm")
    assert json.loads(plain) == {"title": "Athena", "body": "Done.", "url": "/"}              # only the device can read it
    header = captured["headers"]["Authorization"]
    claims = json.loads(base64.urlsafe_b64decode(header.split("t=")[1].split(",")[0].split(".")[1] + "=="))
    assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"].startswith("mailto:") and header.split("k=")[1] == push_notify.public_key()
    assert oct(os.stat(push_notify.VAPID_PATH).st_mode)[-3:] == "600"
    gone = push_notify.send("a", "b", post=lambda e, c, h: types.SimpleNamespace(status_code=410))
    assert gone["failed"] == 1 and push_notify.subscriptions() == []


# ---------- calling a rarely used tool by name, in a real turn ----------

def test_a_rare_tool_called_with_the_right_arguments_just_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_tools, "remember_tool", lambda args, c: {"saved": True, "fact": args["fact"]})
    monkeypatch.setitem(agent_tools.HANDLERS, "remember", agent_tools.remember_tool)
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(call("remember", fact="The person prefers dark themes.")), text_round("noted")])
    output = json.loads([m for m in seen[1] if m["role"] == "tool"][0]["content"])
    assert output == {"saved": True, "fact": "The person prefers dark themes."}
    assert any(e.get("type") == "tool_start" and e["tool"] == "remember" for e in events)


def test_a_rare_tool_called_wrongly_gets_its_usage_back_and_can_try_again(monkeypatch, tmp_path):
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(call("make", kind="pdf")), text_round("I see what it needs")])
    guide = json.loads([m for m in seen[1] if m["role"] == "tool"][0]["content"])
    assert guide["kind"] == "pdf" and "template" in guide["spec_fields"]["properties"] and "how_to_call" in guide


def test_make_runs_the_matching_engine_when_given_a_spec(monkeypatch, tmp_path):
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(call("make", kind="scaffold", spec={"kind": "page", "path": "site"})), text_round("done")])
    result = json.loads([m for m in seen[1] if m["role"] == "tool"][0]["content"])
    assert "error" not in result and (ws / "site").exists()
