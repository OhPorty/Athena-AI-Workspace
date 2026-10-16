"""MCP: connecting local and web servers, losing and reviving them, and offering their tools to models
only when the person has switched them on."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import main
import mcp_manager as mm
import mcp_tools
import settings
import test_chat_loop_recovery as loop

PROBE = os.path.join(os.path.dirname(__file__), "mcp_probe_server.py")
manager = mm.mcp_manager
manager.start()


def local(name="probe", **extra):
    return {"name": name, "command": sys.executable, "args": [PROBE], **extra}


@pytest.fixture
def probe():
    """A connected local probe server; model access is switched on in the saved settings."""
    cfg = mm.normalize_config(local(model_access=True))
    settings.save_settings({**settings.load_settings(), "mcp_servers": [cfg]})
    manager.connect_server(cfg)
    yield cfg
    manager.disconnect_server("probe")


def _kill_probe_processes():
    for pid in os.listdir("/proc"):
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                cmd = open(f"/proc/{pid}/cmdline").read()
            except OSError:
                continue
            if PROBE in cmd:
                os.kill(int(pid), signal.SIGKILL)


# ---------- configuration ----------

def test_old_configs_still_load_and_new_fields_have_safe_defaults():
    cfg = mm.normalize_config({"name": "myserver", "command": "python3", "args": ["/x/server.py"]})
    assert cfg["transport"] == "stdio" and cfg["model_access"] is False and cfg["enabled_tools"] is None
    assert cfg["timeout"] == mm.DEFAULT_TIMEOUT and cfg["env"] == {} and cfg["cwd"] == ""


def test_config_is_validated_with_plain_messages():
    bad = [({}, "name"), ({"name": "x"}, "command"), ({"name": "x", "transport": "http", "url": "ftp://a"}, "http"),
           ({"name": "x", "transport": "carrier-pigeon", "command": "c"}, "transport"), ({"name": "x", "command": "c", "env": "NOEQUALS"}, "NAME=value"), ({"name": "x", "url": "http://a", "headers": "no colon"}, "header"), ("nope", "name")]
    for raw, word in bad:
        with pytest.raises(ValueError, match=word):
            mm.normalize_config(raw)


def test_env_headers_args_and_timeout_are_parsed_and_clamped():
    cfg = mm.normalize_config({"name": "x", "command": "c", "args": "a b", "env": "A=1\n# note\nB = two words ", "timeout": 99999})
    assert cfg["args"] == ["a", "b"] and cfg["env"] == {"A": "1", "B": "two words"} and cfg["timeout"] == 600
    web = mm.normalize_config({"name": "w", "url": "https://x.example/mcp", "headers": {"Authorization": "Bearer s3cret"}, "timeout": 1})
    assert web["transport"] == "http" and web["headers"] == {"Authorization": "Bearer s3cret"} and web["timeout"] == 5


def test_the_ui_never_sees_secret_values():
    view = mm.public_view({"name": "x", "command": "c", "env": {"API_KEY": "sk-secret"}, "headers": {"Authorization": "Bearer t0ken"}})
    assert view["env_names"] == ["API_KEY"] and view["header_names"] == ["Authorization"]
    assert "sk-secret" not in json.dumps(view) and "t0ken" not in json.dumps(view)


# ---------- the manager ----------

def test_a_local_server_gets_its_env_and_working_folder_and_its_tools_are_found(tmp_path):
    try:
        tools = manager.connect_server(local("envtest", env={"PROBE_TOKEN": "abc123"}, cwd=str(tmp_path)))
        assert {"add", "whoami", "boom"} <= {t.name for t in tools}
        assert manager.call_tool("envtest", "whoami", {}).content[0].text == f"abc123|{tmp_path}"
        assert manager.call_tool("envtest", "add", {"a": 2, "b": 3}).content[0].text == "5"
    finally:
        manager.disconnect_server("envtest")


def test_a_tools_own_failure_is_a_result_not_an_exception(probe):
    result = manager.call_tool("probe", "boom", {})
    assert result.is_error and "always fails" in result.content[0].text


def test_a_server_that_dies_mid_call_is_reported_honestly_and_is_back_for_the_next_call(probe):
    with pytest.raises(ConnectionError, match="may or may not have run"):
        manager.call_tool("probe", "crash", {})
    assert manager.call_tool("probe", "add", {"a": 1, "b": 2}).content[0].text == "3"
    assert manager.list_servers()[0]["status"] == "connected"


def test_a_server_that_died_while_idle_is_revived_by_the_next_call(probe):
    _kill_probe_processes()
    time.sleep(0.7)
    assert manager.call_tool("probe", "add", {"a": 7, "b": 7}).content[0].text == "14"


def test_a_slow_tool_times_out_with_a_clear_message_and_the_server_stays_usable(probe):
    with pytest.raises(TimeoutError, match="did not answer within 1 seconds"):
        manager.call_tool("probe", "slow", {"seconds": 5}, timeout=1)
    assert manager.call_tool("probe", "add", {"a": 1, "b": 1}).content[0].text == "2"


def test_an_unreachable_server_is_an_error_status_with_a_readable_reason():
    with pytest.raises(RuntimeError):
        manager.connect_server({"name": "ghost", "command": "/no/such/program"})
    entry = next(s for s in manager.list_servers() if s["name"] == "ghost")
    assert entry["status"] == "error" and entry["connected"] is False and "No such file" in entry["error"]
    manager.disconnect_server("ghost")
    assert not [s for s in manager.list_servers() if s["name"] == "ghost"]


def test_reconnect_and_disconnect(probe):
    assert len(manager.reconnect("probe")) >= 5
    assert manager.list_servers()[0]["status"] == "connected"
    manager.disconnect_server("probe")
    assert manager.list_tools("probe") == []
    with pytest.raises(RuntimeError, match="not set up"):
        manager.call_tool("probe", "add", {"a": 1, "b": 1})


def test_startup_connects_servers_in_the_background_and_reports_each(tmp_path):
    seen = {}
    manager.connect_all([local("bg1"), {"name": "bg2", "command": "/no/such"}, {"name": "", "command": "x"}], on_result=lambda n, t, e: seen.update({n: (t, e)}))
    deadline = time.time() + 20
    while len(seen) < 3 and time.time() < deadline:
        time.sleep(0.2)
    try:
        assert seen["bg1"][1] is None and len(seen["bg1"][0]) >= 5
        assert seen["bg2"][0] is None and "No such file" in seen["bg2"][1]
        assert any(e and "name" in e for t, e in seen.values())                 # the nameless one was refused, not crashed on
    finally:
        for name in ("bg1", "bg2"):
            manager.disconnect_server(name)


def test_a_server_at_a_web_address_works_too():
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    proc = subprocess.Popen([sys.executable, PROBE, "streamable-http", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
                break
            except OSError:
                time.sleep(0.25)
        tools = manager.connect_server({"name": "web", "transport": "http", "url": f"http://127.0.0.1:{port}/mcp"})
        assert "add" in {t.name for t in tools}
        assert manager.call_tool("web", "add", {"a": 4, "b": 4}).content[0].text == "8"
        with pytest.raises(RuntimeError):
            manager.connect_server({"name": "webbad", "transport": "http", "url": "http://127.0.0.1:1/mcp"})
    finally:
        manager.disconnect_server("web"); manager.disconnect_server("webbad"); proc.kill(); proc.wait()


# ---------- what models are offered ----------

def test_nothing_is_offered_until_the_person_switches_a_server_on(probe):
    settings.save_settings({**settings.load_settings(), "mcp_servers": [{**probe, "model_access": False}]})
    assert mcp_tools.schemas() == []
    settings.save_settings({**settings.load_settings(), "mcp_servers": [probe]})
    names = {s["function"]["name"] for s in mcp_tools.schemas()}
    assert {"mcp__probe__add", "mcp__probe__whoami", "mcp__probe__boom"} <= names


def test_only_the_tools_the_person_left_enabled_are_offered(probe):
    settings.save_settings({**settings.load_settings(), "mcp_servers": [{**probe, "enabled_tools": ["add"]}]})
    assert [s["function"]["name"] for s in mcp_tools.schemas()] == ["mcp__probe__add"]
    settings.save_settings({**settings.load_settings(), "mcp_servers": [{**probe, "enabled_tools": []}]})
    assert mcp_tools.schemas() == []


def test_a_schema_carries_the_tools_description_and_real_parameters(probe):
    add = next(s for s in mcp_tools.schemas() if s["function"]["name"] == "mcp__probe__add")["function"]
    assert "Add two numbers" in add["description"] and "probe MCP server" in add["description"]
    assert add["parameters"]["type"] == "object" and set(add["parameters"]["properties"]) == {"a", "b"} and "$schema" not in add["parameters"]


def test_a_server_that_is_not_connected_offers_nothing(probe):
    manager.disconnect_server("probe")
    assert mcp_tools.schemas() == []


def test_names_are_made_safe_and_unique():
    assert mcp_tools._slug("My Server (v2)!") == "my_server_v2" and mcp_tools._tool_part("do it/now") == "do_it_now"
    assert mcp_tools.is_mcp_tool("mcp__a__b") and not mcp_tools.is_mcp_tool("read_file")


# ---------- what comes back ----------

def test_a_call_through_the_model_layer_returns_an_ordinary_result(probe):
    assert mcp_tools.call("mcp__probe__add", {"a": 2, "b": 3}) == {"result": 5}
    assert mcp_tools.call("mcp__probe__info", {"topic": "rust"}) == {"topic": "rust", "letters": 4}
    out = mcp_tools.call("mcp__probe__big", {})
    assert out["result"].startswith("lorem")


def test_failures_are_errors_in_the_normal_shape_never_exceptions(probe):
    assert "always fails" in mcp_tools.call("mcp__probe__boom", {})["error"]
    assert "not an available MCP tool" in mcp_tools.call("mcp__probe__nonexistent", {})["error"]
    assert "not an available MCP tool" in mcp_tools.call("mcp__other__add", {})["error"]
    settings.save_settings({**settings.load_settings(), "mcp_servers": [{**probe, "timeout": 5}]})
    manager.set_config({**probe, "timeout": 5})
    assert "did not answer within 5 seconds" in mcp_tools.call("mcp__probe__slow", {"seconds": 8})["error"]
    assert "dropped during the call" in mcp_tools.call("mcp__probe__crash", {})["error"]
    assert mcp_tools.call("mcp__probe__add", {"a": 1, "b": 1}) == {"result": 2}


def _block(kind, **kw):
    return SimpleNamespace(type=kind, **kw)


def test_result_conversion_handles_text_json_images_and_empty():
    conv = mcp_tools._result_to_dict
    ok = lambda *blocks, **kw: SimpleNamespace(content=list(blocks), is_error=False, structured_content=None, **kw)
    assert conv(ok(_block("text", text="plain words"))) == {"result": "plain words"}
    assert conv(ok(_block("text", text='{"a": 1}'))) == {"a": 1}
    assert conv(ok(_block("text", text="[1, 2]"))) == {"result": [1, 2]}
    assert conv(ok()) == {"result": "(the tool returned nothing)"}
    image = conv(ok(_block("text", text="chart"), _block("image", data="QUJD", mimeType="image/png")))
    assert image["_images"] == ["QUJD"] and image["result"] == "chart" and "1 image" in image["images"]
    assert conv(ok(_block("image", data="QUJD", mimeType="image/png"))) == {"_images": ["QUJD"], "images": "1 image(s) returned"}
    err = conv(SimpleNamespace(content=[_block("text", text="it broke")], is_error=True, structured_content=None))
    assert err == {"error": "it broke"}
    assert "without saying why" in conv(SimpleNamespace(content=[], is_error=True, structured_content=None))["error"]


# ---------- inside Athena ----------

def _req(allowed=None):
    return SimpleNamespace(search_url="", allowed_tools=allowed)


def test_the_tools_reach_workspace_and_chat_modes_but_not_the_delegation_agent(probe):
    names = lambda tools: {t["function"]["name"] for t in tools}
    for mode, kwargs in (("workspace", {"build_task": True}), ("workspace", {}), ("casual", {})):
        assert "mcp__probe__add" in names(main._get_mode_tools(mode, _req(), **kwargs)), mode
    assert "mcp__probe__add" not in names(main._get_mode_tools("athena_delegation", _req()))
    settings.save_settings({**settings.load_settings(), "mcp_servers": [{**probe, "model_access": False}]})
    assert "mcp__probe__add" not in names(main._get_mode_tools("workspace", _req()))


def test_dispatch_runs_an_mcp_tool_and_a_success_counts_as_progress(probe):
    ctx = main.ToolContext(session_id="s", workspace="", search_url="", bot=None, session_key=None, req=None, plan_state=None,
                           passthrough_session_id=None, require_athena_bots_session=False)
    out = main.dispatch_tool("mcp__probe__add", {"a": 20, "b": 22}, ctx)
    assert out == {"result": 42} and main._result_is_progress("mcp__probe__add", out)
    assert not main._result_is_progress("mcp__probe__boom", {"error": "x"})
    assert main._credit_progress("mcp__probe__add", out, 5, 5) == (0, 4)             # it never counts against a model's gates


def test_a_model_can_call_an_mcp_tool_in_a_real_chat_turn(probe, monkeypatch, tmp_path):
    offered = []
    rounds = [loop.tool_round(loop.call("mcp__probe__add", a=19, b=23)), loop.text_round("The sum is 42.")]
    events, seen, saved, ws = loop.run_turn(monkeypatch, tmp_path, rounds, tools_seen=offered)
    assert "mcp__probe__add" in offered[0]
    tool_messages = [m for m in seen[1] if m["role"] == "tool"]
    assert tool_messages and '"result": 42' in tool_messages[-1]["content"]
    assert loop.outcome() == "completed"


def test_a_failing_mcp_tool_in_a_turn_is_just_a_tool_error_the_model_can_answer_from(probe, monkeypatch, tmp_path):
    rounds = [loop.tool_round(loop.call("mcp__probe__boom")), loop.text_round("That tool failed, so I could not do it.")]
    _, seen, saved, _ = loop.run_turn(monkeypatch, tmp_path, rounds)
    assert any("always fails" in m.get("content", "") for m in seen[1] if m["role"] == "tool")
    assert loop.outcome() == "completed" and len(seen) == 2


# ---------- the routes the UI uses ----------

def test_adding_a_server_saves_it_connects_and_never_echoes_secrets_back(tmp_path):
    try:
        out = main.add_mcp_server(main.MCPServerIn(name="routeprobe", command=sys.executable, args=[PROBE], env="PROBE_TOKEN=s3cret-value", cwd=str(tmp_path)))
        assert out["connected"] is True and out["tool_count"] >= 5
        listing = main.list_mcp_servers()["servers"]
        view = next(s for s in listing if s["name"] == "routeprobe")
        assert view["status"] == "connected" and view["model_access"] is False and view["env_names"] == ["PROBE_TOKEN"]
        assert "s3cret-value" not in json.dumps(listing)
        assert {t["name"] for t in view["tools"]} >= {"add", "whoami"} and all(t["enabled"] for t in view["tools"])
        assert manager.call_tool("routeprobe", "whoami", {}).content[0].text.startswith("s3cret-value|")      # it did reach the server
    finally:
        main.remove_mcp_server("routeprobe")
    assert not [s for s in main.list_mcp_servers()["servers"] if s["name"] == "routeprobe"]


def test_a_bad_config_is_refused_plainly_and_not_saved():
    out = main.add_mcp_server(main.MCPServerIn(name="nope", transport="http", url="not-a-url"))
    assert out["connected"] is False and "http" in out["error"]
    assert main.list_mcp_servers()["servers"] == []


def test_a_server_that_cannot_start_is_saved_so_it_can_be_fixed_and_reconnected(tmp_path):
    out = main.add_mcp_server(main.MCPServerIn(name="later", command="/no/such/program"))
    assert out["connected"] is False and "No such file" in out["error"]
    view = main.list_mcp_servers()["servers"][0]
    assert view["status"] == "error" and "No such file" in view["error"]
    main.remove_mcp_server("later")


def test_switching_model_access_and_choosing_tools_takes_effect_for_models(probe):
    settings.save_settings({**settings.load_settings(), "mcp_servers": [{**probe, "model_access": False}]})
    assert mcp_tools.schemas() == []
    out = main.set_mcp_access("probe", main.MCPAccessIn(model_access=True, enabled_tools=["add", "info"]))
    assert out["server"]["model_access"] is True
    assert {t["name"]: t["enabled"] for t in out["server"]["tools"]}["boom"] is False
    assert sorted(s["function"]["name"] for s in mcp_tools.schemas()) == ["mcp__probe__add", "mcp__probe__info"]
    off = main.set_mcp_access("probe", main.MCPAccessIn(model_access=False, enabled_tools=None))
    assert mcp_tools.schemas() == [] and off["server"]["model_access"] is False
    assert "No MCP server named" in main.set_mcp_access("ghost", main.MCPAccessIn(model_access=True))["error"]


def test_reconnect_brings_a_dropped_server_back(probe):
    manager.disconnect_server("probe")
    assert main.list_mcp_servers()["servers"][0]["status"] != "connected"
    out = main.reconnect_mcp_server("probe")
    assert out["connected"] is True and main.list_mcp_servers()["servers"][0]["status"] == "connected"
    assert main.reconnect_mcp_server("ghost")["connected"] is False


# ---------- editing a saved server ----------

def _saved(name):
    return next(s for s in settings.load_settings()["mcp_servers"] if s["name"] == name)


def test_editing_keeps_secrets_you_did_not_retype_and_carries_over_model_access(tmp_path):
    names = ["editme", "renamed"]
    try:
        main.add_mcp_server(main.MCPServerIn(name="editme", command=sys.executable, args=[PROBE], env="PROBE_TOKEN=first-secret", cwd=str(tmp_path)))
        main.set_mcp_access("editme", main.MCPAccessIn(model_access=True, enabled_tools=["add"]))
        out = main.update_mcp_server("editme", main.MCPServerIn(name="editme", command=sys.executable, args=[PROBE], cwd=str(tmp_path)))
        assert out["connected"] is True
        saved = _saved("editme")
        assert saved["env"] == {"PROBE_TOKEN": "first-secret"}                               # not sent, so not lost
        assert saved["model_access"] is True and saved["enabled_tools"] == ["add"]            # editing is not the place those change
        assert manager.call_tool("editme", "whoami", {}).content[0].text.startswith("first-secret|")
        main.update_mcp_server("editme", main.MCPServerIn(name="editme", command=sys.executable, args=[PROBE], env="PROBE_TOKEN=second-secret", cwd=str(tmp_path)))
        assert _saved("editme")["env"] == {"PROBE_TOKEN": "second-secret"}                    # retyped, so replaced
        assert manager.call_tool("editme", "whoami", {}).content[0].text.startswith("second-secret|")
        renamed = main.update_mcp_server("editme", main.MCPServerIn(name="renamed", command=sys.executable, args=[PROBE], cwd=str(tmp_path)))
        assert renamed["connected"] is True
        assert [s["name"] for s in settings.load_settings()["mcp_servers"]] == ["renamed"]
        assert "editme" not in [s["name"] for s in manager.list_servers()] and any(s["name"] == "renamed" and s["connected"] for s in manager.list_servers())
    finally:
        for n in names:
            main.remove_mcp_server(n)


def test_a_bad_edit_is_refused_and_the_saved_server_is_untouched(tmp_path):
    try:
        main.add_mcp_server(main.MCPServerIn(name="keepme", command=sys.executable, args=[PROBE], cwd=str(tmp_path)))
        main.add_mcp_server(main.MCPServerIn(name="other", command=sys.executable, args=[PROBE], cwd=str(tmp_path)))
        before = _saved("keepme")
        assert "full address" in main.update_mcp_server("keepme", main.MCPServerIn(name="keepme", transport="http", url="nope"))["error"]
        assert "already a server named" in main.update_mcp_server("keepme", main.MCPServerIn(name="other", command=sys.executable))["error"]
        assert "No MCP server named" in main.update_mcp_server("ghost", main.MCPServerIn(name="ghost", command="x"))["error"]
        assert _saved("keepme") == before
    finally:
        main.remove_mcp_server("keepme")
        main.remove_mcp_server("other")


def test_an_edit_that_cannot_connect_is_still_saved_so_it_can_be_fixed(tmp_path):
    try:
        main.add_mcp_server(main.MCPServerIn(name="fixme", command=sys.executable, args=[PROBE], cwd=str(tmp_path)))
        out = main.update_mcp_server("fixme", main.MCPServerIn(name="fixme", command="/no/such/program"))
        assert out["connected"] is False and "No such file" in out["error"]
        assert _saved("fixme")["command"] == "/no/such/program"
        assert main.update_mcp_server("fixme", main.MCPServerIn(name="fixme", command=sys.executable, args=[PROBE], cwd=str(tmp_path)))["connected"] is True
    finally:
        main.remove_mcp_server("fixme")
