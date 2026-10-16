import json
import threading
import time
import types

import pyotp
import pytest
from fastapi.testclient import TestClient

import auth_routes
import ownership
import prompt_gate
import settings
import task_scheduler
import user_context

manager = auth_routes.auth_manager
MAIN_PW = "Entrance#Lemon-4821"


def _client():
    import main
    return TestClient(main.app, base_url="https://testserver", raise_server_exceptions=False)


@pytest.fixture
def main_client():
    c = _client()
    assert c.post("/api/auth/setup", json={"username": "boss", "password": MAIN_PW}).status_code == 200
    return c


@pytest.fixture(autouse=True)
def chat_service_knows_no_sessions(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, **k: types.SimpleNamespace(status_code=200, json=lambda: []))


def make_user(name, full_access=False, password="temp password 1", share=False):
    """An account that has finished first-login setup (so tests can use it straight away) and a client signed in as it."""
    made = manager.create_user(name, password, full_access=full_access, share_main_authenticator=share)
    assert "error" not in made, made
    manager.change_password(name, password, "Own#Phrase-Zebra-22")
    manager._users()[name]["must_setup_totp"] = False
    c = _client()
    assert c.post("/api/auth/login", json={"username": name, "password": "Own#Phrase-Zebra-22"}).status_code == 200
    return c


# ---------- signing in as an extra account ----------

def test_temporary_password_has_to_be_changed_then_an_authenticator_set_up_before_anything_else(main_client):
    made = main_client.post("/api/accounts", json={"username": "bob", "password": "chosen by boss"}).json()
    assert made["temporary_password"] == "chosen by boss"
    bob = _client()
    login = bob.post("/api/auth/login", json={"username": "bob", "password": "chosen by boss"}).json()
    assert login["onboarding"] == "change_password"
    blocked = bob.get("/api/bots")
    assert blocked.status_code == 403 and blocked.json()["onboarding"] == "change_password"
    assert bob.get("/api/auth/status").json()["onboarding"] == "change_password"
    assert bob.post("/api/auth/change-password", json={"current_password": "chosen by boss", "new_password": "chosen by boss"}).status_code == 400
    assert bob.post("/api/auth/change-password", json={"current_password": "chosen by boss", "new_password": "Real#Choice-Zebra-33"}).json()["onboarding"] == "setup_totp"
    assert bob.get("/api/bots").status_code == 403
    secret = bob.post("/api/auth/2fa/setup").json()["secret"]
    assert bob.post("/api/auth/2fa/confirm", json={"code": pyotp.TOTP(secret).now()}).status_code == 200
    assert bob.get("/api/bots").status_code == 200
    again = _client()
    step = again.post("/api/auth/login", json={"username": "bob", "password": "Real#Choice-Zebra-33"}).json()
    assert step["requires_totp"] is True
    ok = again.post("/api/auth/verify-2fa", json={"pending_token": step["pending_token"], "code": pyotp.TOTP(secret).now()})
    assert ok.status_code == 200 and again.get("/api/bots").status_code == 200


def test_an_account_reusing_the_main_authenticator_skips_that_step(main_client):
    secret = main_client.post("/api/auth/2fa/setup").json()["secret"]
    main_client.post("/api/auth/2fa/confirm", json={"code": pyotp.TOTP(secret).now()})
    made = main_client.post("/api/accounts", json={"username": "bob", "share_authenticator": True}).json()
    assert len(made["backup_codes"]) == 8
    bob = _client()
    step = bob.post("/api/auth/login", json={"username": "bob", "password": made["temporary_password"]}).json()
    assert step["requires_totp"] is True
    bob.post("/api/auth/verify-2fa", json={"pending_token": step["pending_token"], "code": pyotp.TOTP(secret).now()})
    assert bob.post("/api/auth/change-password", json={"current_password": made["temporary_password"], "new_password": "Real#Choice-Zebra-33"}).json()["onboarding"] is None
    assert bob.get("/api/bots").status_code == 200


def test_only_the_main_account_manages_accounts(main_client):
    bob = make_user("bob")
    assert bob.get("/api/accounts").status_code == 403
    assert bob.post("/api/accounts", json={"username": "eve"}).status_code == 403
    assert bob.delete("/api/accounts/boss").status_code == 403
    assert bob.put("/api/accounts/settings", json={"one_prompt_at_a_time": True}).status_code == 403
    assert main_client.get("/api/accounts").status_code == 200


def test_main_creates_resets_and_removes_accounts(main_client):
    assert main_client.post("/api/accounts", json={"username": "BAD NAME"}).status_code == 400
    overview = main_client.post("/api/accounts", json={"username": "bob", "full_access": True}).json()
    assert [u["username"] for u in overview["users"]] == ["boss", "bob"] and overview["users"][1]["full_access"] is True
    assert main_client.put("/api/accounts/bob/access", json={"full_access": False}).json()["users"][1]["full_access"] is False
    reset = main_client.post("/api/accounts/bob/reset-password").json()
    assert len(reset["temporary_password"]) == 12
    assert main_client.post("/api/accounts/boss/reset-authenticator").status_code == 400
    assert main_client.delete("/api/accounts/boss").status_code == 400
    after = main_client.delete("/api/accounts/bob").json()
    assert [u["username"] for u in after["users"]] == ["boss"]


def test_removing_an_account_signs_it_out_and_erases_its_data(main_client):
    bob = make_user("bob")
    bob.post("/api/bots", json={"name": "Bobbot", "model": "m", "allowed_tools": []})
    bob.post("/api/notes", json={"notes": [{"id": "1", "title": "mine"}]})
    assert ownership.session_allowed("bob-chat", "bob", claim=True)
    main_client.delete("/api/accounts/bob")
    assert bob.get("/api/bots").status_code == 401
    assert ownership.session_owner("bob-chat") is None
    import bots
    conn = bots._bots_conn()
    assert conn.execute("SELECT COUNT(*) FROM bots WHERE owner = 'bob'").fetchone()[0] == 0
    conn.close()


# ---------- every account has its own data ----------

def test_bots_rooms_and_tasks_are_private(main_client):
    bob = make_user("bob")
    main_client.post("/api/bots", json={"name": "Mainbot", "model": "m", "allowed_tools": []})
    bob.post("/api/bots", json={"name": "Bobbot", "model": "m", "allowed_tools": []})
    assert [b["name"] for b in main_client.get("/api/bots").json()] == ["Mainbot"]
    assert [b["name"] for b in bob.get("/api/bots").json()] == ["Bobbot"]
    main_bot = main_client.get("/api/bots").json()[0]["id"]
    bob.delete(f"/api/bots/{main_bot}")
    assert len(main_client.get("/api/bots").json()) == 1
    room = main_client.post("/api/rooms/everyone").json()
    assert bob.get("/api/rooms").json() == []
    assert bob.get(f"/api/rooms/{room['id']}/messages").json() == []
    assert "error" in bob.post(f"/api/rooms/{room['id']}/messages", json={"sender_type": "user", "content": "hi"}).json()
    assert bob.post("/api/rooms/everyone").json()["id"] != room["id"]
    task = {"prompt": "p", "session_label": "l", "model": "m", "schedule_type": "once", "run_at": time.time() + 999}
    made = main_client.post("/api/tasks", json=task).json()
    assert bob.get("/api/tasks").json() == [] and len(main_client.get("/api/tasks").json()) == 1
    bob.delete(f"/api/tasks/{made['id']}")
    assert len(main_client.get("/api/tasks").json()) == 1
    assert "error" in bob.put(f"/api/tasks/{made['id']}", json={**task, "prompt": "hijack"}).json()


def test_notes_are_private_and_old_notes_stay_the_main_accounts(main_client):
    main_client.post("/api/notes", json={"notes": [{"id": "m", "title": "main note"}]})
    bob = make_user("bob")
    assert bob.get("/api/notes").json()["notes"] == []
    bob.post("/api/notes", json={"notes": [{"id": "b", "title": "bob note"}]})
    assert main_client.get("/api/notes").json()["notes"][0]["title"] == "main note"
    bob.post("/api/notes", json={"notes": []})                       # the clear-data button: only this account's notes
    assert main_client.get("/api/notes").json()["notes"][0]["title"] == "main note"


def test_chats_belong_to_the_account_that_started_them(main_client, monkeypatch):
    import httpx
    sessions = [{"id": "old-main-chat", "label": "x"}]
    monkeypatch.setattr(httpx, "get", lambda url, **k: types.SimpleNamespace(status_code=200, json=lambda: list(sessions) if url.endswith("/sessions") else []))
    bob = make_user("bob")
    assert ownership.session_allowed("bobs", "bob", claim=True)        # a new chat: the service does not know it yet
    sessions.append({"id": "bobs", "label": "y"})
    assert {s["id"] for s in ownership.own_sessions(sessions, "bob")} == {"bobs"}
    assert {s["id"] for s in ownership.own_sessions(sessions, "boss")} == {"old-main-chat"}
    assert ownership.session_allowed("bobs", "boss") is False and ownership.session_allowed("old-main-chat", "bob") is False
    assert bob.get("/api/history/old-main-chat").json() == []
    assert bob.post("/api/chat/cancel", json={"session_id": "old-main-chat"}).json() == {"cancelled": False}
    assert "error" in bob.delete("/api/history/old-main-chat").json()
    assert "error" in bob.post("/api/sessions", json={"id": "old-main-chat", "label": "mine now", "created_at": 1}).json()


def test_memory_facts_are_private(main_client, monkeypatch):
    import httpx
    facts = [{"id": 1, "content": "main fact"}, {"id": 2, "content": "bob fact"}]
    monkeypatch.setattr(httpx, "get", lambda url, **k: types.SimpleNamespace(status_code=200, json=lambda: list(facts)))
    bob = make_user("bob")
    ownership.record_fact(2, "bob")
    assert [f["id"] for f in main_client.get("/api/memory").json()] == [1]
    assert [f["id"] for f in bob.get("/api/memory").json()] == [2]
    assert "error" in bob.delete("/api/memory/1").json()


def test_each_account_has_its_own_delegation_chat(main_client):
    assert ownership.resolve_session("athena-bots-agent", "boss") == "athena-bots-agent"
    manager.create_user("bob", "temp password 1")
    assert ownership.resolve_session("athena-bots-agent", "bob") == "athena-bots-agent--bob"
    assert ownership.is_bots_session("athena-bots-agent--bob") and ownership.owner_of_session("athena-bots-agent--bob") == "bob"
    assert ownership.resolve_session("some-other-chat", "bob") == "some-other-chat"


def test_research_runs_and_ratings_and_push_are_private(main_client):
    import research, annotations, push_notify
    research.save_run({"id": "r1", "question": "q", "status": "done", "created_at": 1, "owner": "boss", "claims": [], "sources": []})
    research.save_run({"id": "r2", "question": "q2", "status": "done", "created_at": 2, "owner": "bob", "claims": [], "sources": []})
    bob = make_user("bob")
    assert [r["id"] for r in main_client.get("/api/research").json()["runs"]] == ["r1"]
    assert [r["id"] for r in bob.get("/api/research").json()["runs"]] == ["r2"]
    assert "error" in bob.get("/api/research/r1").json()
    bob.delete("/api/research/r1")
    assert research.get_run("r1") is not None
    with user_context.as_user("bob"):
        push_notify.save_subscription("https://push.example/b", "k", "a", "bob phone")
    push_notify.save_subscription("https://push.example/m", "k", "a", "main phone")
    assert [d["label"] for d in push_notify.subscriptions()] == ["main phone"]
    with user_context.as_user("bob"):
        assert [d["label"] for d in push_notify.subscriptions()] == ["bob phone"]
        push_notify.remove_subscription("https://push.example/m")
    assert len(push_notify.subscriptions()) == 1


# ---------- settings, models and endpoints ----------

LOCAL = {"id": "L1", "name": "Ollama", "type": "local", "url": "http://192.168.1.50:11434", "provider": "", "apiKey": "", "detectedModels": ["qwen"], "enabledModels": ["qwen"]}
MAIN_ONLINE = {"id": "O1", "name": "Mine", "type": "online", "url": "", "provider": "openai", "apiKey": "sk-main-secret", "detectedModels": ["gpt"], "enabledModels": ["gpt"]}


def test_another_account_never_sees_the_main_accounts_keys(main_client):
    main_client.post("/api/settings", json={"endpoints": [LOCAL, MAIN_ONLINE], "search_url": "http://searx", "athena_agent_model": {"model": "gpt", "api_key": "sk-main-secret"}})
    bob = make_user("bob")
    seen = bob.get("/api/settings").json()
    assert "sk-main-secret" not in json.dumps(seen)
    assert [e["id"] for e in seen["endpoints"]] == ["L1"] and seen["endpoints"][0]["enabledModels"] == ["qwen"]        # the models the main account ticked
    assert seen["endpoints"][0]["hidden"] is True and seen["endpoints"][0]["detectedModels"] == []
    assert "athena_agent_model" not in seen and seen["mcp_servers"] == []
    assert "sk-main-secret" in json.dumps(main_client.get("/api/settings").json())


def test_another_account_changes_only_its_own_settings(main_client):
    main_client.post("/api/settings", json={"endpoints": [LOCAL], "search_url": "http://searx", "theme": {"seedColor": "red"}})
    bob = make_user("bob")
    own = {"id": "B1", "name": "Bob's", "type": "online", "url": "", "provider": "openai", "apiKey": "sk-bob", "enabledModels": ["gpt"]}
    bob.post("/api/settings", json={"search_url": "http://evil", "theme": {"seedColor": "blue"}, "workspace_mode": "build",
                                    "endpoints": [{**LOCAL, "enabledModels": ["qwen"], "url": "http://evil:1"}, own]})
    shared = settings.load_settings()
    assert shared["search_url"] == "http://searx" and shared["theme"] == {"seedColor": "red"} and "workspace_mode" not in shared
    assert shared["endpoints"][0]["url"] == "http://192.168.1.50:11434" and shared["endpoints"][0]["enabledModels"] == ["qwen"]
    seen = bob.get("/api/settings").json()
    assert seen["theme"] == {"seedColor": "blue"}
    assert [e["id"] for e in seen["endpoints"]] == ["L1", "B1"] and seen["endpoints"][0]["enabledModels"] == ["qwen"]
    assert "hidden" not in seen["endpoints"][1]
    assert main_client.get("/api/settings").json()["theme"] == {"seedColor": "red"}
    assert all(e["id"] != "B1" for e in main_client.get("/api/settings").json()["endpoints"])


def test_another_account_cannot_add_a_local_endpoint_disguised_as_online(main_client):
    bob = make_user("bob")
    sneaky = {"id": "S1", "name": "x", "type": "online", "url": "http://192.168.1.99:8080", "provider": "custom", "apiKey": "k", "enabledModels": ["m"]}
    sneaky_local = {"id": "S2", "name": "y", "type": "local", "url": "http://192.168.1.98:11434", "provider": "", "enabledModels": ["m"]}
    bob.post("/api/settings", json={"endpoints": [sneaky, sneaky_local]})
    assert bob.get("/api/settings").json()["endpoints"] == []


def test_endpoint_rules():
    settings.save_settings({"endpoints": [LOCAL]})
    kind = lambda p, u, who: ownership.endpoint_kind(p, u, who)
    manager.setup("boss", MAIN_PW)
    manager.create_user("bob", "temp password 1")
    assert kind("openai", "", "bob") == "online" and kind("anthropic", "", "bob") == "online" and kind("openrouter", "https://openrouter.ai/api/v1", "bob") == "online"
    assert kind("", "http://192.168.1.50:11434", "bob") == "local" and kind("", "http://192.168.1.50:11434/", "bob") == "local"
    assert kind("", "", "bob") == "local"
    assert kind("", "http://192.168.1.77:11434", "bob") is None and kind("custom", "http://localhost:8080/v1", "bob") is None
    assert kind("openai", "http://10.0.0.5:9000/v1", "bob") is None
    assert kind("", "http://192.168.1.77:11434", "boss") == "local" and kind("custom", "http://localhost:8080/v1", "boss") == "local"
    assert ownership.is_online("openai", "") and not ownership.is_online("", "http://192.168.1.50:11434")


def test_models_and_endpoints_can_only_be_added_by_main(main_client, monkeypatch):
    bob = make_user("bob")
    assert "Only the main account" in bob.post("/api/detect-models", json={"type": "local", "url": "http://192.168.1.50:11434"}).json()["error"]
    assert "not an online provider" in bob.post("/api/detect-models", json={"type": "online", "url": "http://localhost:9", "provider": "custom", "api_key": "k"}).json()["error"]
    assert "error" in bob.post("/api/model-profile", json={"model": "m", "url": "http://192.168.1.77:11434"}).json()
    assert bob.post("/api/settings/memory-model", json={"model": "m"}).status_code == 403
    assert bob.post("/api/bots", json={"name": "x", "model": "m", "endpoint_url": "http://192.168.1.77:11434"}).status_code == 403


# ---------- the host: workspaces, MCP, skills ----------

def test_restricted_account_is_kept_to_its_own_workspaces_folder(main_client, tmp_path):
    bob = make_user("bob")
    root = ownership.workspace_root("bob")
    assert ownership.workspace_allowed(root + "/proj", "bob") and ownership.workspace_allowed("", "bob")
    assert not ownership.workspace_allowed(str(tmp_path), "bob") and not ownership.workspace_allowed(root + "/../..", "bob")
    first = bob.get("/api/workspace/browse").json()
    assert first["path"] == root and first["parent"] is None
    assert "error" in bob.get("/api/workspace/browse", params={"path": str(tmp_path)}).json()
    assert "error" in bob.post("/api/workspace/mkdir", json={"path": str(tmp_path), "name": "x"}).json()
    assert bob.post("/api/workspace/mkdir", json={"path": root, "name": "proj"}).json()["path"].endswith("proj")
    assert bob.get("/api/workspace/files", params={"workspace": str(tmp_path)}).status_code == 403
    assert bob.get("/api/workspace/files", params={"workspace": root + "/proj"}).status_code == 200
    assert bob.put("/api/workspace/file", json={"workspace": str(tmp_path), "path": "a", "content": "x"}).status_code == 403
    assert bob.post("/api/workspace/undo", json={"workspace": str(tmp_path)}).status_code == 403
    assert "error" in bob.post("/api/rag/index", json={"workspace": str(tmp_path)}).json()
    assert bob.post("/api/tasks", json={"prompt": "p", "session_label": "l", "model": "m", "schedule_type": "once", "run_at": 1, "workspace": str(tmp_path)}).status_code == 403


def test_full_access_account_may_use_any_folder_and_mcp(main_client, tmp_path):
    eve = make_user("eve", full_access=True)
    assert ownership.workspace_allowed(str(tmp_path), "eve")
    assert eve.get("/api/workspace/browse", params={"path": str(tmp_path)}).json()["path"] == str(tmp_path)
    assert eve.get("/api/mcp/servers").status_code == 200
    assert eve.get("/api/accounts").status_code == 403         # never account management


def test_restricted_account_has_no_mcp_or_skill_editing(main_client):
    bob = make_user("bob")
    assert bob.get("/api/mcp/servers").status_code == 403
    assert bob.post("/api/mcp/servers", json={"name": "x", "command": "y"}).status_code == 403
    assert bob.delete("/api/mcp/servers/x").status_code == 403
    assert bob.post("/api/skills", json={"name": "s", "description": "d", "body": "b"}).status_code == 403
    assert bob.delete("/api/skills/s").status_code == 403
    assert bob.get("/api/skills").status_code == 200


# ---------- the chat itself ----------

def _chat(client, **body):
    payload = {"session_id": "s1", "message": "hi", **body}
    return client.post("/api/chat", json=payload).text


def test_chat_refuses_another_accounts_chat_unknown_endpoints_and_outside_workspaces(main_client, monkeypatch, tmp_path):
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: types.SimpleNamespace(status_code=500, json=lambda: {}))
    bob = make_user("bob")
    ownership.session_allowed("boss-chat", "boss", claim=True)
    assert "belongs to another account" in _chat(bob, session_id="boss-chat")
    assert "cannot use that model endpoint" in _chat(bob, endpoint_url="http://192.168.1.77:11434")
    assert "inside its own folder" in _chat(bob, workspace=str(tmp_path))
    assert "cannot use that model endpoint" in _chat(bob, provider="custom", endpoint_url="http://localhost:9/v1", api_key="k")


def test_chat_never_trusts_a_client_supplied_owner(main_client, monkeypatch):
    import main, httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: types.SimpleNamespace(status_code=500, json=lambda: {}))
    bob = make_user("bob")
    seen = {}
    real = main._account_refusal
    monkeypatch.setattr(main, "_account_refusal", lambda req: (seen.setdefault("owner", req.owner), real(req))[1])
    _chat(bob, owner="boss")
    assert seen["owner"] == "bob"


# ---------- one prompt at a time ----------

def _turn(user, label="s", cancel=None):
    flag = cancel or threading.Event()
    steps = prompt_gate.wait_turn(user, label, flag)
    events, slot = [], None
    try:
        while True:
            events.append(next(steps))
    except StopIteration as done:
        slot = done.value
    return slot, events


def _reset_gate():
    with prompt_gate._cond:
        prompt_gate._running.clear()
        prompt_gate._waiting.clear()


def test_gate_applies_only_to_local_models_when_switched_on():
    settings.save_settings({"endpoints": [LOCAL], "one_prompt_at_a_time": False})
    assert not prompt_gate.applies("", "http://192.168.1.50:11434")
    settings.save_settings({"endpoints": [LOCAL], "one_prompt_at_a_time": True})
    assert prompt_gate.applies("", "http://192.168.1.50:11434") and prompt_gate.applies("", "")
    assert not prompt_gate.applies("openai", "") and not prompt_gate.applies("anthropic", "https://api.anthropic.com")


def test_second_prompt_waits_is_told_and_goes_next():
    _reset_gate()
    first, events = _turn("boss")
    assert first is not None and events == []
    box = {}

    def second():
        box["slot"], box["events"] = _turn("bob")
    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.5)
    assert t.is_alive() and prompt_gate.status("bob") == {"enabled": prompt_gate.enabled(), "busy": True, "busy_with_other": True, "waiting": 1}
    prompt_gate.release(first)
    t.join(10)
    assert not t.is_alive() and box["slot"] is not None
    first_event = json.loads(box["events"][0][6:])
    assert first_event["type"] == "queued" and first_event["someone_else"] is True and first_event["position"] == 1
    assert json.loads(box["events"][-1][6:])["type"] == "queue_started"
    prompt_gate.release(box["slot"])
    assert prompt_gate.status("boss")["busy"] is False


def test_queue_is_first_come_first_served():
    _reset_gate()
    holder, _ = _turn("boss")
    order, threads = [], []

    def waiter(name):
        slot, _ = _turn(name)
        order.append(name)
        time.sleep(0.1)
        prompt_gate.release(slot)
    for name in ("a", "b", "c"):
        t = threading.Thread(target=waiter, args=(name,))
        t.start()
        threads.append(t)
        time.sleep(0.4)
    prompt_gate.release(holder)
    for t in threads:
        t.join(15)
    assert order == ["a", "b", "c"]


def test_stopping_while_queued_leaves_the_line_without_blocking_others():
    _reset_gate()
    holder, _ = _turn("boss")
    flag = threading.Event()
    box = {}
    t = threading.Thread(target=lambda: box.update(result=_turn("bob", cancel=flag)))
    t.start()
    time.sleep(0.5)
    flag.set()
    t.join(10)
    assert box["result"][0] is None and prompt_gate.status("boss")["waiting"] == 0
    prompt_gate.release(holder)
    slot, _ = _turn("eve")
    assert slot is not None
    prompt_gate.release(slot)


def test_own_second_prompt_gets_its_own_wording():
    _reset_gate()
    holder, _ = _turn("boss")
    box = {}
    t = threading.Thread(target=lambda: box.update(result=_turn("boss", "another")))
    t.start()
    time.sleep(0.5)
    prompt_gate.release(holder)
    t.join(10)
    event = json.loads(box["result"][1][0][6:])
    assert event["someone_else"] is False and "Another of your prompts" in event["message"]
    prompt_gate.release(box["result"][0])


def test_queue_status_route_and_toggle(main_client):
    bob = make_user("bob")
    assert bob.get("/api/queue/status").json()["enabled"] is False
    assert main_client.put("/api/accounts/settings", json={"one_prompt_at_a_time": True}).json()["one_prompt_at_a_time"] is True
    assert bob.get("/api/queue/status").json()["enabled"] is True
    assert settings.load_settings()["one_prompt_at_a_time"] is True


def test_a_scheduled_task_runs_as_the_account_that_made_it(main_client, monkeypatch):
    seen = {}
    import main
    monkeypatch.setattr(main, "chat_stream", lambda req: seen.update(owner=req.owner, acting=ownership.acting_user(), session=req.session_id))
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: types.SimpleNamespace(status_code=200, json=lambda: []))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: types.SimpleNamespace(status_code=200, json=lambda: {}))
    bob = make_user("bob")
    made = bob.post("/api/tasks", json={"prompt": "p", "session_label": "l", "model": "m", "schedule_type": "once", "run_at": time.time() + 999}).json()
    row = task_scheduler._row_to_task(task_scheduler._tasks_conn().execute(f"SELECT {task_scheduler._TASK_COLUMNS} FROM tasks WHERE id = ?", (made["id"],)).fetchone())
    task_scheduler._start_task_run(row)
    assert seen["acting"] == "bob" and ownership.session_owner(seen["session"]) == "bob"


def test_account_folders_are_private_to_tools_except_the_workspaces(main_client):
    import safety
    base = ownership.USERS_ROOT
    import os
    os.makedirs(os.path.join(base, "bob", "workspaces", "proj"), exist_ok=True)
    # the test data dir is outside Athena's folder, so point the classifier at it as if inside
    assert safety.classify(os.path.join(safety.ATHENA_ROOT, "user_data", "bob", "notes.json")) == "secret"
    assert safety.classify(os.path.join(safety.ATHENA_ROOT, "user_data", "bob", "settings.json")) == "secret"
    assert safety.classify(os.path.join(safety.ATHENA_ROOT, "user_data", "bob", "workspaces", "proj", "a.py")) is None
    for name in ("auth.py", "auth_routes.py", "ownership.py", "accounts.py", "prompt_gate.py", "user_context.py"):
        assert safety.classify(os.path.join(safety.ATHENA_ROOT, name)) == "readonly"


# ---------- a whole chat turn through the line ----------

def _scripted_chat(monkeypatch, user, **req_fields):
    """Start a real chat_stream turn as `user` with a scripted model. -> (events list that fills as it runs, thread, started flag)"""
    import asyncio
    import lcm_client
    import main
    started = threading.Event()

    def fake_stream(req, target_url, messages, tools, ctx_size, cancel_flag, timeout=None):
        started.set()
        yield {"message": {"content": "hello"}, "done": False}
        yield {"message": {}, "done": True}
    saved = [0]
    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    monkeypatch.setattr(lcm_client, "send_to_lcm", lambda *a, **k: (saved.__setitem__(0, saved[0] + 1), saved[0])[1])
    monkeypatch.setattr(lcm_client, "get_lcm_context", lambda sid: [{"role": "user", "content": "hi"}])
    monkeypatch.setattr(lcm_client, "get_lcm_tools", lambda: [])
    monkeypatch.setattr(main, "_get_memory_context", lambda: "")
    events = []

    def run():
        with user_context.as_user(user):
            response = main.chat_stream(main.ChatIn(session_id=f"{user}-chat", message="hi", model="m", think="", **req_fields))

            async def drain():
                async for chunk in response.body_iterator:
                    events.extend(json.loads(line[6:]) for line in chunk.split("\n") if line.startswith("data: "))
            asyncio.run(drain())
    thread = threading.Thread(target=run)
    thread.start()
    return events, thread, started


def test_a_whole_prompt_waits_behind_another_accounts_and_then_runs(main_client, monkeypatch):
    manager.create_user("bob", "temp password 1")
    settings.save_settings({"endpoints": [LOCAL], "one_prompt_at_a_time": True})
    _reset_gate()
    holder, _ = _turn("boss")
    events, thread, started = _scripted_chat(monkeypatch, "bob", endpoint_url="http://192.168.1.50:11434")
    deadline = time.time() + 10
    while time.time() < deadline and not any(e.get("type") == "queued" for e in events):
        time.sleep(0.1)
    queued = next(e for e in events if e.get("type") == "queued")
    assert queued["someone_else"] is True and "Someone else" in queued["message"]
    assert not started.is_set()                                   # the model has not been asked anything yet
    prompt_gate.release(holder)
    thread.join(20)
    kinds = [e.get("type") for e in events]
    assert "queue_started" in kinds and kinds.index("queue_started") > kinds.index("queued")
    assert started.is_set() and any(e.get("delta") == "hello" for e in events) and events[-1].get("done") is True
    assert prompt_gate.status("bob")["busy"] is False             # the line is free again


def test_a_prompt_to_an_online_model_never_waits_or_holds_the_line(main_client, monkeypatch):
    manager.create_user("bob", "temp password 1")
    settings.save_settings({"endpoints": [LOCAL], "one_prompt_at_a_time": True})
    _reset_gate()
    holder, _ = _turn("boss")
    events, thread, started = _scripted_chat(monkeypatch, "bob", provider="openai", api_key="k")
    thread.join(20)
    assert started.is_set() and not any(e.get("type") == "queued" for e in events) and events[-1].get("done") is True
    assert prompt_gate.status("bob")["waiting"] == 0
    prompt_gate.release(holder)


def test_with_the_switch_off_prompts_run_at_the_same_time(main_client, monkeypatch):
    manager.create_user("bob", "temp password 1")
    settings.save_settings({"endpoints": [LOCAL], "one_prompt_at_a_time": False})
    _reset_gate()
    holder, _ = _turn("boss")
    events, thread, started = _scripted_chat(monkeypatch, "bob", endpoint_url="http://192.168.1.50:11434")
    thread.join(20)
    assert started.is_set() and not any(e.get("type") == "queued" for e in events)
    prompt_gate.release(holder)


def test_limited_account_cannot_choose_local_models_but_full_access_can(main_client):
    main_client.post("/api/settings", json={"endpoints": [{**LOCAL, "detectedModels": ["qwen", "llama"], "enabledModels": ["qwen"]}]})
    bob = make_user("bob")
    eve = make_user("eve", full_access=True)
    pick = {**LOCAL, "detectedModels": ["qwen", "llama"], "enabledModels": ["llama"]}
    bob.post("/api/settings", json={"endpoints": [pick]})
    eve.post("/api/settings", json={"endpoints": [pick]})
    seen_bob, seen_eve = bob.get("/api/settings").json()["endpoints"][0], eve.get("/api/settings").json()["endpoints"][0]
    assert seen_bob["enabledModels"] == ["qwen"] and seen_bob["hidden"] is True
    assert seen_eve["enabledModels"] == ["llama"] and "hidden" not in seen_eve and seen_eve["detectedModels"] == ["qwen", "llama"]
    assert main_client.get("/api/settings").json()["endpoints"][0]["enabledModels"] == ["qwen"]


def test_the_setup_and_change_routes_explain_what_is_wrong(main_client):
    fresh = _client()
    manager._config = {}
    weak = fresh.post("/api/auth/setup", json={"username": "boss", "password": "123456789"})
    assert weak.status_code == 400 and "at least 10 characters" in weak.json()["error"]
    assert "common" in fresh.post("/api/auth/setup", json={"username": "boss", "password": "Password123!"}).json()["error"]
    assert fresh.post("/api/auth/setup", json={"username": "boss", "password": MAIN_PW}).status_code == 200
    assert "username" in fresh.post("/api/auth/change-password", json={"current_password": MAIN_PW, "new_password": "Xx#boss-Lemon-99"}).json()["error"]
    assert "uppercase" in fresh.post("/api/auth/change-password", json={"current_password": MAIN_PW, "new_password": "all#lower-case-99"}).json()["error"]
    assert "not right" in fresh.post("/api/auth/change-password", json={"current_password": "nope", "new_password": "Good#Lemon-Stone-47"}).json()["error"]
    assert fresh.get("/api/auth/status").json()["password_rules"][0].startswith("At least 10")


def test_zip_download_route_serves_a_zip_and_keeps_limited_accounts_inside_their_folder(main_client, tmp_path):
    import io, zipfile
    mine = tmp_path / "proj"
    mine.mkdir()
    (mine / "a.txt").write_text("hello")
    got = main_client.get("/api/workspace/zip", params={"workspace": str(mine)})
    assert got.status_code == 200 and got.headers["content-type"] == "application/zip" and "proj.zip" in got.headers["content-disposition"]
    assert zipfile.ZipFile(io.BytesIO(got.content)).namelist() == ["a.txt"] and got.headers["x-files-zipped"] == "1"
    bob = make_user("bob")
    assert bob.get("/api/workspace/zip", params={"workspace": str(mine)}).status_code == 403                  # outside his own folder
    own = ownership.workspace_root("bob") + "/site"
    import os
    os.makedirs(own)
    open(own + "/i.html", "w").write("<p>")
    assert zipfile.ZipFile(io.BytesIO(bob.get("/api/workspace/zip", params={"workspace": own}).content)).namelist() == ["i.html"]
    assert main_client.get("/api/workspace/zip", params={"workspace": ""}).status_code == 400
