"""The main agent hands investigation to bots; a bot with no way to look at anything just guesses.
These pin down that bots really have tools, that the main agent can see what each bot can do, and
that a step needing files or the web is refused up front for a bot that has neither."""
import json

import bots
import main
from bots import BotIn


def make(name="Bot", tools=None, **kw):
    created = bots.create_bot(BotIn(name=name, model="m", endpoint_url="http://127.0.0.1:1", allowed_tools=tools or [], **kw))
    return next(b for b in bots._list_bots_tool() if b["id"] == created["id"])


def legacy(name, tools):
    """A row the way the old Bots page wrote it: saved straight to the database with whatever list it sent."""
    conn = bots._bots_conn()
    try:
        cur = conn.execute("INSERT INTO bots (name, endpoint_url, provider, api_key, model, description, allowed_tools_json, unload_strategy, created_at) VALUES (?, 'http://x', '', 'sekrit', 'm', 'does things', ?, 'none', 1)",
                           (name, json.dumps(tools)))
        conn.commit()
        return next(b for b in bots._list_bots_tool() if b["id"] == cur.lastrowid)
    finally:
        conn.close()


def test_no_tools_chosen_means_the_standard_set_and_unknown_or_write_tools_never_count():
    assert bots.effective_tools([]) == bots.DEFAULT_BOT_TOOLS == ["bash_read_only", "list_files", "read_file", "search_codebase"]
    assert bots.effective_tools(None) == bots.DEFAULT_BOT_TOOLS
    assert bots.effective_tools(["write_file", "bash_exec"]) == bots.DEFAULT_BOT_TOOLS      # a stored write tool is not a tool choice
    assert bots.effective_tools(["web_fetch", "write_file"]) == ["web_fetch"]


def test_an_old_bot_with_an_empty_list_can_actually_use_tools(monkeypatch):
    bot = legacy("Iris", [])
    assert bot["allowed_tools"] == []                                  # as stored
    seen = {}

    def fake_stream(req, url, messages, tools, *a, **k):
        seen["tools"] = {t["function"]["name"] for t in (tools or [])}
        yield {"message": {"content": "done"}, "done": True}

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    bots._call_bot_endpoint(bot, [{"role": "user", "content": "look"}])
    assert {"read_file", "list_files", "bash_read_only", "search_codebase", "append_scratch_note"} <= seen["tools"]
    assert "read_file" in bots._build_bot_system_prompt(bot)


def test_list_bots_tells_the_main_agent_what_each_bot_can_do_and_hides_keys_and_endpoints():
    legacy("Iris", [])
    make("Webby", ["web_fetch", "web_search"])
    roster = {b["name"]: b for b in bots._list_bots_for_model()}
    assert "read files" in roster["Iris"]["can"] and "search code" in roster["Iris"]["can"] and "web" not in roster["Iris"]["can"]
    assert roster["Webby"]["can"] == "search the web, fetch web pages" and roster["Webby"]["tools"] == ["web_fetch", "web_search"]
    for entry in roster.values():
        assert "api_key" not in entry and "endpoint_url" not in entry
        assert "sekrit" not in json.dumps(entry)


def test_dispatch_list_bots_uses_the_model_facing_view():
    legacy("Iris", [])
    ctx = main.ToolContext(session_id=bots.ATHENA_BOTS_SESSION_ID, workspace="", search_url="", bot=None, session_key=None,
                           req=None, plan_state=None, passthrough_session_id=None, require_athena_bots_session=True)
    out = main.dispatch_tool("list_bots", {}, ctx)
    assert out[0]["name"] == "Iris" and "can" in out[0] and "api_key" not in out[0]


def test_a_web_step_for_a_bot_without_web_tools_is_refused_and_names_who_could_do_it():
    iris, webby = make("Iris", ["read_file"]), make("Webby", ["web_fetch"])
    gap = bots.capability_gap(iris, "Fetch the three newest videos from https://www.youtube.com/@someone")
    assert "Iris cannot do this" in gap and "web access" in gap and "Webby" in gap
    assert bots.capability_gap(webby, "Fetch the three newest videos from https://www.youtube.com/@someone") is None


def test_a_file_step_for_a_bot_with_only_web_tools_is_refused():
    webby = make("Webby", ["web_search"])
    gap = bots.capability_gap(webby, "Find where tool pills are rendered in static/index.html")
    assert gap and "file access" in gap and "do this yourself" in gap          # nobody else in the roster either


def test_plain_instructions_and_ordinary_words_are_not_flagged():
    webby = make("Webby", ["web_search"])
    for text in ("Summarise the tradeoffs between tabs and spaces", "Decide and/or explain which option is cheaper", "Think about naming"):
        assert bots.capability_gap(webby, text) is None, text
    iris = make("Iris", ["read_file"])
    assert bots.capability_gap(iris, "Read main.py and report the function that builds the system prompt") is None
    assert bots.capability_gap(iris, "See the docs at https://example.com/guide.html") and True    # url is web, not a file


def test_the_step_is_refused_before_any_bot_is_run(monkeypatch):
    iris = make("Iris", ["read_file"])
    monkeypatch.setattr(bots, "_call_bot_endpoint", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the bot should not have been run")))
    out = bots._run_delegation_step_tool(iris["id"], "Look at https://example.com and report the headline")
    assert out["status"] == "rejected" and "cannot do this" in out["error"] and out["bot"] == "Iris"


def test_a_plan_reports_the_refused_step_without_stopping_the_others(monkeypatch):
    iris = make("Iris", ["read_file"])
    out = bots._plan_delegation_tool([{"bot_id": iris["id"], "instruction": "Open https://example.com and report the title"}])
    assert out["plan_results"][0]["status"] == "rejected"
