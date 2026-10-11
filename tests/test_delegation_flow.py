"""How a delegated step is briefed, what it hands back, and how the bot loop copes with a weak model."""
import json
import os

import pytest

import bots
import delegation_jobs
import main
import model_profile
import task_context


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "static").mkdir(parents=True)
    (ws / "static" / "app.js").write_text("\n".join(f"line {n}" for n in range(1, 101)))
    (ws / "main.py").write_text("\n".join(f"x = {n}" for n in range(1, 31)))
    monkeypatch.setattr(bots.settings, "load_settings", lambda: {"workspace": str(ws)})
    monkeypatch.setattr(task_context, "BOT_TASK_FOLDER_ROOT", str(tmp_path / "scratch"))
    (tmp_path / "scratch" / "t1").mkdir(parents=True)
    task_context.current_task_hash.set("t1")
    return ws


def make_bot(name="Iris", tools=("read_file", "search_codebase")):
    created = bots.create_bot(bots.BotIn(name=name, model="m", endpoint_url="http://127.0.0.1:1", allowed_tools=list(tools)))
    return created["id"]


def scratch_for(name="Iris"):
    return os.path.join(task_context.BOT_TASK_FOLDER_ROOT, "t1", f"{name.lower()}.md")


class FakeBotLoop:
    """Stands in for _call_bot_endpoint: records the brief it was given and 'calls append_scratch_note'."""
    def __init__(self, monkeypatch, notes, name="Iris"):
        self.notes, self.calls, self.messages, self.name = list(notes), 0, [], name
        monkeypatch.setattr(bots, "_call_bot_endpoint", self)

    def __call__(self, bot, chat_messages):
        self.messages = [dict(m) for m in chat_messages]
        note = self.notes[min(self.calls, len(self.notes) - 1)]
        self.calls += 1
        if note:
            with open(scratch_for(self.name), "a", encoding="utf-8") as f:
                f.write(note)
        return {"content": "", "tool_call_history": [{"tool": "read_file", "args": {}, "outcome": "ok"}]}


# ---------- the brief ----------

def test_the_brief_carries_what_the_person_asked_the_question_and_the_shape_of_answer():
    msg = bots._compose_step_message("Where are tool pills rendered?", known="Not in app.css", report="file and line only",
                                     task_text="Fix the tool pill bug, but don't touch anything under legacy/")
    assert "don't touch anything under legacy/" in msg and "context only" in msg
    assert "Your question:\nWhere are tool pills rendered?" in msg
    assert "Already known or ruled out:\nNot in app.css" in msg and "Report back: file and line only" in msg
    assert "append_scratch_note" in msg


def test_the_brief_has_sensible_defaults_and_bounds():
    msg = bots._compose_step_message("What does run() do?")
    assert "What the person asked" not in msg and "Already known" not in msg
    assert "file path and line number" in msg                       # default report shape
    long = bots._compose_step_message("q", task_text="x" * 5000, known="k" * 5000, report="r" * 5000)
    assert len(long) < 3000


def test_the_bot_actually_receives_the_brief_and_the_task_text_travels_without_the_main_agent_restating_it(workspace, monkeypatch):
    loop = FakeBotLoop(monkeypatch, ["Found it at static/app.js:40."])
    bot_id = make_bot()
    bots._run_delegation_step_tool(bot_id, "Where is the toggle?", known="not in index.html", task_text="Fix the pills; ignore legacy/")
    user = loop.messages[1]["content"]
    assert "ignore legacy/" in user and "Where is the toggle?" in user and "not in index.html" in user
    assert "You are Iris" in loop.messages[0]["content"]


# ---------- the report comes back with the step ----------

def test_findings_come_back_in_the_step_result_with_a_clean_citation_check(workspace, monkeypatch):
    FakeBotLoop(monkeypatch, ["Toggle is at static/app.js:40 and the state at app.js line 12-20."])
    out = bots._run_delegation_step_tool(make_bot(), "Where is the toggle?")
    assert out["status"] == "done" and "static/app.js:40" in out["findings"]
    assert out["citations"] == {"checked": 2, "not_found": []}
    assert "read_scratch_file" not in out["detail"] and "findings_truncated" not in out


def test_only_this_steps_findings_come_back_not_earlier_ones(workspace, monkeypatch):
    open(scratch_for(), "w").write("EARLIER STEP NOTE\n")
    FakeBotLoop(monkeypatch, ["second note"])
    out = bots._run_delegation_step_tool(make_bot(), "Another question")
    assert out["findings"] == "second note"


def test_long_findings_are_cut_with_a_pointer_to_the_full_text(workspace, monkeypatch):
    FakeBotLoop(monkeypatch, ["y" * 9000])
    out = bots._run_delegation_step_tool(make_bot(), "Big question")
    assert len(out["findings"]) == bots._FINDINGS_INLINE_LIMIT and out["findings_truncated"]
    assert 'read_scratch_file with bot_name "Iris"' in out["detail"]       # says how to get the rest, including the name it needs
    assert len(open(scratch_for()).read()) == 9000                                   # nothing lost on disk


def test_a_report_citing_places_that_do_not_exist_is_treated_as_fabricated(workspace, monkeypatch):
    FakeBotLoop(monkeypatch, ["See static/render.js:210 and components/pill.vue:33, also app.js:5000."])
    out = bots._run_delegation_step_tool(make_bot(), "Where?")
    assert out["status"] == "possibly_fabricated" and "do not exist" in out["detail"] and out["citations"]["checked"] == 3
    assert "findings" not in out


def test_a_report_with_one_bad_citation_among_good_ones_is_kept_but_flagged(workspace, monkeypatch):
    FakeBotLoop(monkeypatch, ["Real: static/app.js:10, main.py:3. Doubtful: nowhere/ghost.py:9."])
    out = bots._run_delegation_step_tool(make_bot(), "Where?")
    assert out["status"] == "done" and out["citations"]["checked"] == 3 and len(out["citations"]["not_found"]) == 1


def test_the_internal_retry_still_works_and_is_reported(workspace, monkeypatch):
    loop = FakeBotLoop(monkeypatch, ["", "Found: main.py:2"])
    out = bots._run_delegation_step_tool(make_bot(), "Where?")
    assert loop.calls == 2 and out["status"] == "done" and "after one internal retry" in out["detail"] and "main.py:2" in out["findings"]


def test_no_note_even_after_the_retry_is_still_a_clear_failure(workspace, monkeypatch):
    FakeBotLoop(monkeypatch, [""])
    assert bots._run_delegation_step_tool(make_bot(), "Where?")["status"] == "no_scratch_note"


def test_a_plan_returns_every_steps_findings_with_a_bounded_share_each(workspace, monkeypatch):
    FakeBotLoop(monkeypatch, ["z" * 4000])
    bot_id = make_bot()
    out = bots._plan_delegation_tool([{"bot_id": bot_id, "instruction": "A?", "known": "k1", "report": "r1"},
                                      {"bot_id": bot_id, "instruction": "B?"}, {"bot_id": bot_id, "instruction": "C?"},
                                      {"bot_id": bot_id, "instruction": "D?"}], task_text="the request")
    assert [r["status"] for r in out["plan_results"]] == ["done"] * 4 and "note" not in out
    assert all(len(r["findings"]) <= 1500 for r in out["plan_results"]) and all(r["findings_truncated"] for r in out["plan_results"])


def test_a_plan_passes_known_report_and_task_text_to_each_bot(workspace, monkeypatch):
    loop = FakeBotLoop(monkeypatch, ["ok"])
    bots._plan_delegation_tool([{"bot_id": make_bot(), "instruction": "A?", "known": "ruled out X", "report": "one line"}], task_text="the whole request")
    user = loop.messages[1]["content"]
    assert "the whole request" in user and "ruled out X" in user and "Report back: one line" in user


# ---------- the citation check itself ----------

@pytest.mark.parametrize("text,checked,missing", [
    ("static/app.js:50", 1, 0), ("app.js, line 50", 1, 0), ("see app.js line 10-20", 1, 0), ("main.py:30", 1, 0),
    ("main.py:31", 1, 1), ("static/app.js:101", 1, 1), ("static/gone.js:3", 1, 1),
    ("app.js:5 and app.js:5", 1, 0),                                  # the same place twice counts once
    ("version 1.2.3 and example.com:8080 and http://x.io/a.html:80", 0, 0),
    ("", 0, 0),
])
def test_citation_checking(workspace, text, checked, missing):
    out = bots.verify_citations(text, str(workspace))
    assert (out["checked"], len(out["not_found"])) == (checked, missing)


def test_the_citation_check_is_bounded_and_survives_a_missing_workspace(workspace):
    many = " ".join(f"main.py:{n}" for n in range(1, 60))
    assert bots.verify_citations(many, str(workspace))["checked"] == 20
    assert bots.verify_citations("main.py:3", "") == {"checked": 0, "not_found": []}
    assert bots.verify_citations("main.py:3", "/definitely/not/here") == {"checked": 0, "not_found": []}


# ---------- remembering what was asked ----------

def test_the_first_real_request_is_kept_and_machine_messages_never_replace_it():
    task_context.current_task_text.set("")
    assert task_context.remember_task_text("Fix the pill bug") == "Fix the pill bug"
    assert task_context.remember_task_text("a different message") == "Fix the pill bug"
    task_context.current_task_text.set("")
    assert task_context.remember_task_text("[Async delegation job 3 complete] Iris ...") == ""      # not what the person asked


def test_dispatch_hands_the_request_known_and_report_to_the_step(monkeypatch):
    task_context.current_task_hash.set(None)
    task_context.current_task_text.set("")
    seen = {}
    monkeypatch.setattr(bots, "_run_delegation_step_tool", lambda *a, **k: seen.update(args=a) or {"status": "done"})
    ctx = main.ToolContext(session_id=bots.ATHENA_BOTS_SESSION_ID, workspace="", search_url="", bot=None, session_key=None,
                           req=type("R", (), {"message": "Fix the pill bug, avoid legacy/"})(), plan_state=None,
                           passthrough_session_id=None, require_athena_bots_session=True)
    main.dispatch_tool("run_delegation_step", {"bot_id": 1, "instruction": "Where?", "known": "k", "report": "r"}, ctx)
    assert seen["args"] == (1, "Where?", "k", "r", "Fix the pill bug, avoid legacy/")


def test_an_async_result_arrives_with_its_findings_and_failed_citations(monkeypatch):
    sent = []
    monkeypatch.setattr(delegation_jobs, "_deliver_delegation_result", lambda fields, content: sent.append(content))
    monkeypatch.setattr(bots, "_run_delegation_step_tool", lambda *a, **k: {
        "status": "done", "bot": "Iris", "detail": "Step complete.", "findings": "It is at app.js:40.",
        "citations": {"checked": 2, "not_found": ["ghost.py:9 (no such file)"]}})
    delegation_jobs._delegation_step_worker("9", "t1", {"session_id": "s"}, 1, "Where?", None, None, "the request")
    assert "It is at app.js:40." in sent[0] and "ghost.py:9" in sent[0] and "read_scratch_file" not in sent[0]


# ---------- the bot loop with a weak model ----------

def run_loop(monkeypatch, rounds, tools=None):
    seen = {"messages": []}

    def fake_stream(req, url, messages, tool_schemas, ctx, cancel, timeout=None):
        seen["messages"] = [dict(m) for m in messages]
        seen["ctx"] = ctx
        seen["rounds"] = seen.get("rounds", 0) + 1
        yield from rounds[min(seen["rounds"] - 1, len(rounds) - 1)]

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    bot = {"id": 1, "name": "Iris", "model": "m", "endpoint_url": "", "provider": "", "allowed_tools": tools or ["read_file"]}
    return bots._call_bot_endpoint(bot, [{"role": "user", "content": "q"}]), seen


BLANK = [{"message": {}, "done": True}]
ANSWER = [{"message": {"content": "the answer"}, "done": True}]
LOST = [{"message": {"content": "Let me read it.\n</parameter>\n</function>\n</tool_call>"}, "done": True}]


def test_a_blank_round_is_nudged_instead_of_returned_as_an_empty_answer(monkeypatch):
    out, seen = run_loop(monkeypatch, [BLANK, ANSWER])
    assert out["content"] == "the answer" and seen["rounds"] == 2
    assert "You produced no output" in seen["messages"][-1]["content"]


def test_two_nudges_at_most_then_the_empty_result_stands(monkeypatch):
    out, seen = run_loop(monkeypatch, [BLANK])
    assert out["content"] == "" and seen["rounds"] == 3


def test_a_lost_tool_call_is_nudged_to_be_made_again(monkeypatch):
    out, seen = run_loop(monkeypatch, [LOST, ANSWER])
    assert out["content"] == "the answer" and "no call actually ran" in seen["messages"][-1]["content"]
    assert "</tool_call>" not in "".join(m.get("content", "") for m in seen["messages"] if m["role"] == "assistant")


def test_the_bots_context_is_capped_to_what_its_model_supports(monkeypatch):
    monkeypatch.setattr(model_profile, "get_facts", lambda *a, **k: {**model_profile.new_facts(), "engine": "ollama", "context_train": 8192})
    _, seen = run_loop(monkeypatch, [ANSWER])
    assert seen["ctx"] == 8192
    monkeypatch.setattr(model_profile, "get_facts", lambda *a, **k: {**model_profile.new_facts(), "engine": "ollama", "context_train": 262144})
    assert run_loop(monkeypatch, [ANSWER])[1]["ctx"] == bots._BOT_CTX_SIZE
    monkeypatch.setattr(model_profile, "get_facts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("probe failed")))
    assert run_loop(monkeypatch, [ANSWER])[1]["ctx"] == bots._BOT_CTX_SIZE


# ---------- the main agent's prompt ----------

def test_the_delegation_prompt_is_short_positive_and_describes_the_new_flow():
    text = bots._get_bot_delegation_prompt_section(workspace="/x")
    assert len(text.split()) < 500
    for must in ("`known`", "`report`", "automatically told what the person asked", "come back in its result", "possibly_fabricated"):
        assert must in text, must
    for gone in ("Under no circumstances", "NEVER", "Do NOT", "call read_scratch_file for that bot"):
        assert gone not in text, gone


def test_prompt_extras_still_apply():
    assert "No workspace is currently bound" in bots._get_bot_delegation_prompt_section(workspace=None)
    assert "findings arrive as a brand new message" in bots._get_bot_delegation_prompt_section(workspace="/x", async_enabled=True)
    assert "failed an automatic check" in bots._get_bot_delegation_prompt_section(workspace="/x", laya_gating_enabled=True)


# ---------- bot ids are never guessed ----------

def test_the_prompt_lists_every_bot_with_its_id_and_what_it_can_do():
    iris = make_bot("Iris", ("read_file",))
    webby = make_bot("Webby", ("web_fetch",))
    text = bots._get_bot_delegation_prompt_section(workspace="/x")
    assert f"- Iris (id {iris}): can read files" in text and f"- Webby (id {webby}): can fetch web pages" in text
    assert "use exactly those ids" in text.lower()


def test_an_empty_roster_is_stated_plainly():
    assert "roster is empty" in bots._get_bot_delegation_prompt_section(workspace="/x")


def test_a_wrong_id_says_which_bots_exist_so_one_retry_is_enough(workspace):
    iris = make_bot("Iris")
    out = bots._run_delegation_step_tool(99, "Where?")
    assert "No bot with id 99" in out["error"] and f"Iris (id {iris})" in out["error"]
    plan = bots._plan_delegation_tool([{"bot_id": 98, "instruction": "A?"}])
    assert f"Iris (id {iris})" in plan["plan_results"][0]["error"]


def test_a_bot_can_be_named_instead_of_numbered(workspace, monkeypatch):
    FakeBotLoop(monkeypatch, ["Found: main.py:2"])
    make_bot("Iris")
    assert bots._run_delegation_step_tool("iris", "Where?")["status"] == "done"
    assert bots._get_bot_row("nobody") is None


def test_delegation_results_are_not_paged_away_by_the_large_output_layer(workspace, monkeypatch):
    import tool_output
    FakeBotLoop(monkeypatch, ["w" * 3400])
    out = bots._plan_delegation_tool([{"bot_id": make_bot(), "instruction": "A?"}, {"bot_id": make_bot("Second"), "instruction": "B?"}])
    assert len(json.dumps(out)) > tool_output._LARGE_TOOL_OUTPUT_THRESHOLD           # would have been stored and previewed
    for name in ("run_delegation_step", "plan_delegation", "read_scratch_file"):
        assert name in tool_output.UNCOMPRESSED_TOOL_NAMES
    assert len(json.dumps(out)) < 9000                                               # and the delegation code keeps it bounded


def test_a_report_whose_citations_check_out_is_not_rejected_by_the_scoring_model(workspace, monkeypatch):
    """A real run: 29 tool calls, a report the optional scorer put at 0.43 against a 0.45 line, so a good report was
    treated as failed. Citations that verify outrank that estimate; the scorer is only for reports with too little to check."""
    monkeypatch.setattr(bots, "_maybe_flag_fabrication", lambda *a, **k: {"status": "possibly_fabricated", "detail": "scorer says no", "bot": "Iris"})
    FakeBotLoop(monkeypatch, ["The route is static/app.js:40 and the state is main.py:3."])
    assert bots._run_delegation_step_tool(make_bot(), "Where?")["status"] == "done"


def test_the_scoring_model_still_decides_when_the_report_cites_nothing_checkable(workspace, monkeypatch):
    monkeypatch.setattr(bots, "_maybe_flag_fabrication", lambda *a, **k: {"status": "possibly_fabricated", "detail": "scorer says no", "bot": "Iris"})
    FakeBotLoop(monkeypatch, ["It is somewhere in the frontend, I think."])
    assert bots._run_delegation_step_tool(make_bot(), "Where?")["status"] == "possibly_fabricated"
