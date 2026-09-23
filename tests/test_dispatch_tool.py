import bots
import file_tools
import main
import web_tools


def make_ctx(**overrides):
    fields = dict(
        session_id="s1", workspace="/tmp", search_url="", bot=None, session_key=None,
        req=None, plan_state=None, passthrough_session_id=None,
        require_athena_bots_session=False,
    )
    fields.update(overrides)
    return main.ToolContext(**fields)


def test_allowed_names_gating_rejects_disallowed_name():
    ctx = make_ctx()
    result = main.dispatch_tool("bash_exec", {}, ctx, allowed_names={"list_files"})
    assert "error" in result
    assert "not a tool available" in result["error"]


def test_allowed_names_gating_permits_allowed_name(monkeypatch):
    called = {}

    def fake_list_files(ws, path):
        called["args"] = (ws, path)
        return {"entries": []}

    monkeypatch.setattr(file_tools, "list_files", fake_list_files)
    ctx = make_ctx(workspace="/some/ws")
    result = main.dispatch_tool("list_files", {"path": "."}, ctx, allowed_names={"list_files"})
    assert result == {"entries": []}
    assert called["args"] == ("/some/ws", ".")


def test_custom_unknown_tool_message_used_for_disallowed_name():
    ctx = make_ctx(unknown_tool_message_template="Unknown tool for X: {name}")
    result = main.dispatch_tool("nope", {}, ctx, allowed_names={"list_files"})
    assert result == {"error": "Unknown tool for X: nope"}


def test_routes_read_file_to_file_tools(monkeypatch):
    monkeypatch.setattr(file_tools, "read_file", lambda ws, path, offset: {"path": path, "offset": offset})
    ctx = make_ctx(workspace="/ws")
    result = main.dispatch_tool("read_file", {"path": "a.py", "offset": 5}, ctx)
    assert result == {"path": "a.py", "offset": 5}


def test_routes_bash_to_bash_tools(monkeypatch):
    import bash_tools
    monkeypatch.setattr(bash_tools, "execute_readonly_bash", lambda cmd, args, cwd: {"cmd": cmd, "cwd": cwd})
    ctx = make_ctx(workspace="/ws")
    result = main.dispatch_tool("bash", {"command": "ls", "args": []}, ctx)
    assert result == {"cmd": "ls", "cwd": "/ws"}


def test_web_search_without_search_url_configured():
    ctx = make_ctx(search_url="")
    result = main.dispatch_tool("web_search", {"query": "test"}, ctx)
    assert "error" in result
    assert "not configured" in result["error"] or "No search engine" in result["error"]


def test_web_search_with_search_url_configured(monkeypatch):
    monkeypatch.setattr(web_tools, "web_search", lambda url, query: {"url": url, "query": query})
    ctx = make_ctx(search_url="http://search.example")
    result = main.dispatch_tool("web_search", {"query": "test"}, ctx)
    assert result == {"url": "http://search.example", "query": "test"}


def test_bot_delegation_cluster_blocked_when_session_required():
    ctx = make_ctx(session_id="not-the-bots-session", require_athena_bots_session=True)
    result = main.dispatch_tool("list_bots", {}, ctx)
    assert "error" in result
    assert "dedicated Athena Bots session" in result["error"]


def test_bot_delegation_cluster_allowed_in_correct_session(monkeypatch):
    monkeypatch.setattr(bots, "_list_bots_tool", lambda: {"bots": []})
    ctx = make_ctx(session_id=bots.ATHENA_BOTS_SESSION_ID, require_athena_bots_session=True)
    result = main.dispatch_tool("list_bots", {}, ctx)
    assert result == {"bots": []}


def test_write_plan_unavailable_without_plan_state():
    ctx = make_ctx(plan_state=None)
    result = main.dispatch_tool("write_plan", {"steps": ["a"]}, ctx)
    assert "error" in result


def test_write_plan_and_update_plan_step():
    plan_state = []
    ctx = make_ctx(plan_state=plan_state)
    write_result = main.dispatch_tool("write_plan", {"steps": ["step one", "step two"]}, ctx)
    assert "plan" in write_result
    assert len(plan_state) == 2

    update_result = main.dispatch_tool(
        "update_plan_step", {"index": 0, "status": "done", "note": "finished"}, ctx
    )
    assert plan_state[0]["status"] == "done"
    assert plan_state[0]["note"] == "finished"
    assert "plan" in update_result


def test_update_plan_step_rejects_bad_index():
    plan_state = [{"text": "a", "status": "pending", "note": ""}]
    ctx = make_ctx(plan_state=plan_state)
    result = main.dispatch_tool("update_plan_step", {"index": 5, "status": "done"}, ctx)
    assert "error" in result


def test_unknown_tool_falls_through_to_lcm_passthrough(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"result": {"ok": True}}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr(main.httpx, "post", fake_post)
    ctx = make_ctx(passthrough_session_id="real-session-id")
    result = main.dispatch_tool("some_lcm_tool", {"x": 1}, ctx)
    assert result == {"ok": True}
    assert captured["json"]["arguments"]["session_id"] == "real-session-id"


def test_unknown_tool_errors_when_passthrough_disabled():
    ctx = make_ctx(passthrough_session_id=None)
    result = main.dispatch_tool("totally_unknown_tool", {}, ctx)
    assert "error" in result
