"""Drives the real chat_stream loop with a scripted model, to pin down how it recovers when a
round goes wrong. These are the failure shapes seen in a real session (a Qwen3.6 round that ended
with only closing tool-call tags, and a repeat-block that fired after the page had changed)."""
import asyncio
import json
import logging_setup

import file_tools
import lcm_client
import main


def call(name, **args):
    return {"function": {"name": name, "arguments": args}}


def tool_round(*calls):
    return [{"message": {"tool_calls": list(calls)}, "done": True}]


def text_round(text):
    return [{"message": {"content": text}, "done": False}, {"message": {}, "done": True}]


LOST_CALL = [  # exactly what Ollama streamed in the real failure: thinking only, ending in closing tags, then stop
    {"message": {"thinking": "Let me write the complete file with both fixes applied."}, "done": False},
    {"message": {"thinking": "\n</parameter"}, "done": False}, {"message": {"thinking": ">"}, "done": False},
    {"message": {"thinking": "\n</function"}, "done": False}, {"message": {"thinking": ">"}, "done": False},
    {"message": {"thinking": "\n</tool_call>"}, "done": False},
    {"message": {}, "done": True},
]
BLANK = [{"message": {}, "done": True}]


def run_turn(monkeypatch, tmp_path, rounds, ctx_sizes=None, max_ctx=0, files=None):
    """-> (events, messages_seen_per_round, saved_assistant_replies, workspace)"""
    ws = tmp_path / "ws"
    ws.mkdir()
    for name, content in (files or {}).items():
        (ws / name).write_text(content)
    seen, saved, counter = [], [], [0]

    def fake_stream(req, target_url, messages, tools, ctx_size, cancel_flag, timeout=None):
        seen.append(list(messages))
        if ctx_sizes is not None:
            ctx_sizes.append(ctx_size)
        yield from rounds[min(len(seen) - 1, len(rounds) - 1)]

    def fake_send(session_id, role, content, **kw):
        counter[0] += 1
        if role == "assistant":
            saved.append(content)
        return counter[0]

    monkeypatch.setattr(main, "_stream_completion", fake_stream)
    monkeypatch.setattr(lcm_client, "send_to_lcm", fake_send)
    monkeypatch.setattr(lcm_client, "get_lcm_context", lambda sid: [{"role": "user", "content": "do the thing"}])
    monkeypatch.setattr(lcm_client, "get_lcm_tools", lambda: [])
    monkeypatch.setattr(main, "_get_memory_context", lambda: "")
    req = main.ChatIn(session_id="loop-test", message="do the thing", model="m", endpoint_url="http://127.0.0.1:1", workspace=str(ws), think="", max_ctx=max_ctx)
    response = main.chat_stream(req)

    async def drain():
        return [chunk async for chunk in response.body_iterator]

    events = []
    for chunk in asyncio.run(drain()):
        events += [json.loads(line[6:]) for line in chunk.split("\n") if line.startswith("data: ")]
    return events, seen, saved, ws


def trace():
    return [json.loads(line) for line in open(logging_setup.TRACE_LOG_PATH)]


def outcome():
    return [e for e in trace() if e["event"] == "turn_end"][-1]["outcome"]


# ---------- helpers ----------

def test_markup_detection_looks_at_the_tail_only():
    tail = "Let me write the complete file.\n</parameter>\n</function>\n</tool_call>"
    assert main._leaked_tool_markup("", tail)
    assert main._leaked_tool_markup("<tool_call><function=write_file><parameter=path>x")
    assert not main._leaked_tool_markup("All done, the page now has a working counter.")
    mid = "Tool calls end with </tool_call> in this format. " + "Then plain prose follows. " * 20
    assert not main._leaked_tool_markup(mid)


def test_a_successful_change_forgets_stale_look_and_read_repeats_but_not_repeated_writes():
    fps = [("preview", "a"), ("read_file", "b"), ("write_file", "c"), ("bash_exec", "d"), ("web_fetch", "e"), ("edit_file", "f")]
    assert main._forget_stale_repeats(fps) == [("write_file", "c"), ("edit_file", "f")]
    assert {"write_file", "edit_file", "bash_exec"} <= main._STATE_CHANGING_TOOL_NAMES and "preview" not in main._STATE_CHANGING_TOOL_NAMES


def test_write_file_can_append_so_a_big_file_goes_in_pieces(tmp_path):
    ws = str(tmp_path)
    assert file_tools.write_file(ws, "big.html", "<html>")["written"]
    second = file_tools.write_file(ws, "big.html", "<body></body>", append=True)
    file_tools.write_file(ws, "big.html", "</html>", True)
    assert second["appended"] and (tmp_path / "big.html").read_text() == "<html><body></body></html>"
    file_tools.write_file(ws, "big.html", "fresh")  # default still replaces
    assert (tmp_path / "big.html").read_text() == "fresh"
    schema = next(t for t in file_tools.FILE_TOOL_SCHEMAS if t["function"]["name"] == "write_file")
    assert "append" in schema["function"]["parameters"]["properties"] and "append" not in schema["function"]["parameters"]["required"]


# ---------- the real loop ----------

def test_a_lost_tool_call_is_retried_instead_of_ending_the_turn(monkeypatch, tmp_path):
    rounds = [
        tool_round(call("list_files", path=".")),
        LOST_CALL,
        tool_round(call("write_file", path="a.txt", content="hi")),
        text_round("All done."),
    ]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds)
    assert (ws / "a.txt").read_text() == "hi"                                  # the retried call really ran
    nudge = seen[2][-1]
    assert nudge["role"] == "user" and "no tool call actually ran" in nudge["content"] and "append" in nudge["content"]
    assert saved[-1].endswith("All done.") and outcome() == "completed"
    assert events[-1].get("done") and not events[-1].get("generation_error")


def test_repeated_lost_calls_end_honestly_not_silently(monkeypatch, tmp_path):
    rounds = [tool_round(call("list_files", path="."))] + [LOST_CALL] * 5
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds)
    assert outcome() == "lost_tool_call_giveup"
    assert "never came through as a usable call" in saved[-1]
    assert len(seen) == 4  # first call + 3 lost attempts, then it stops


def test_a_blank_round_after_tool_use_is_not_a_finished_answer_even_if_earlier_rounds_talked(monkeypatch, tmp_path):
    rounds = [
        [{"message": {"content": "Looking at the folder now.", "tool_calls": [call("list_files", path=".")]}, "done": True}],
        BLANK,
        text_round("Here is what I found."),
    ]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds)
    assert "completely blank reply" in seen[2][-1]["content"]
    assert saved[-1].endswith("Here is what I found.") and outcome() == "completed"


def test_an_identical_check_is_allowed_again_after_a_successful_edit(monkeypatch, tmp_path):
    look = call("list_files", path=".")
    rounds = [tool_round(look), tool_round(look), tool_round(call("write_file", path="a.txt", content="x")), tool_round(look), text_round("done")]
    run_turn(monkeypatch, tmp_path, rounds)
    assert not [e for e in trace() if e["event"] == "tool_blocked"]


def test_an_identical_check_with_nothing_changed_is_still_blocked(monkeypatch, tmp_path):
    look = call("list_files", path=".")
    run_turn(monkeypatch, tmp_path, [tool_round(look)] * 3 + [text_round("done")])
    blocked = [e for e in trace() if e["event"] == "tool_blocked"]
    assert len(blocked) == 1 and "exact list_files call" in str(blocked[0]["output"])


def test_repeating_the_same_write_is_still_a_loop_even_though_writes_succeed(monkeypatch, tmp_path):
    same = call("write_file", path="a.txt", content="x")
    run_turn(monkeypatch, tmp_path, [tool_round(same)] * 3 + [text_round("done")])
    assert len([e for e in trace() if e["event"] == "tool_blocked"]) == 1


def test_a_successful_look_does_not_reset_the_repeat_count(monkeypatch, tmp_path):
    look = call("list_files", path=".")
    # look, look, (a different successful look), look: the third identical look must still be blocked
    run_turn(monkeypatch, tmp_path, [tool_round(look), tool_round(look), tool_round(call("list_files", path="x")), tool_round(look), text_round("done")])
    assert len([e for e in trace() if e["event"] == "tool_blocked"]) == 1


# ---------- context size ----------

def _ctx_for(monkeypatch, tmp_path, model_max, max_ctx):
    monkeypatch.setattr(main.model_profile, "max_context", lambda facts: model_max)
    sizes = []
    run_turn(monkeypatch, tmp_path, [text_round("ok")], ctx_sizes=sizes, max_ctx=max_ctx)
    return sizes[0]


def test_auto_context_stays_at_65k_for_a_bigger_model(monkeypatch, tmp_path):
    assert _ctx_for(monkeypatch, tmp_path, 262144, 0) <= 65536


def test_auto_context_never_exceeds_a_smaller_model(monkeypatch, tmp_path):
    assert _ctx_for(monkeypatch, tmp_path, 8192, 0) <= 8192


def test_chosen_context_can_use_the_whole_model(monkeypatch, tmp_path):
    assert _ctx_for(monkeypatch, tmp_path, 262144, 200000) == 200000


def test_chosen_context_is_clamped_to_the_model(monkeypatch, tmp_path):
    assert _ctx_for(monkeypatch, tmp_path, 32768, 200000) == 32768


def test_chosen_context_is_used_as_is_when_the_limit_is_unknown(monkeypatch, tmp_path):
    assert _ctx_for(monkeypatch, tmp_path, None, 200000) == 200000


# ---------- a long final answer is not "ungrounded"; invented files are ----------

ANSWER_TEXT = ("The size limit is 40 project files. the build-task check counts the real files in the workspace, "
               "skipping node_modules, venv and build output, and compares the total with BUILD_FILE_LIMIT. An explicit "
               "workspace_mode setting wins over the count. Small projects therefore get the lean tool set, and a project that grows "
               "past the limit is treated as an existing codebase with the full harness. ") * 2


def test_a_long_final_answer_after_tool_calls_is_accepted_without_any_nudge(monkeypatch, tmp_path):
    rounds = [tool_round(call("read_file", path="notes.md")), text_round(ANSWER_TEXT + " See notes.md.")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, files={"notes.md": "limit is 40"})
    assert outcome() == "completed" and len(seen) == 2 and len(saved) == 1
    assert "failure" not in saved[0].lower() and "wasn't able" not in saved[0]


def test_a_long_final_answer_that_names_no_files_is_accepted(monkeypatch, tmp_path):
    rounds = [tool_round(call("list_files", path=".")), text_round(ANSWER_TEXT)]
    _, seen, saved, _ = run_turn(monkeypatch, tmp_path, rounds, files={"a.txt": "x"})
    assert outcome() == "completed" and len(seen) == 2 and len(saved) == 1


def test_naming_a_workspace_file_it_never_opened_is_not_flagged(monkeypatch, tmp_path):
    rounds = [tool_round(call("list_files", path=".")), text_round(ANSWER_TEXT + " Also see real_module.py.")]
    _, seen, saved, _ = run_turn(monkeypatch, tmp_path, rounds, files={"real_module.py": "x"})
    assert outcome() == "completed" and len(seen) == 2          # the file exists in the workspace


def test_an_invented_file_is_nudged_by_name_and_a_corrected_answer_then_completes(monkeypatch, tmp_path):
    rounds = [tool_round(call("list_files", path=".")), text_round(ANSWER_TEXT + " I read ghost_config.py and found it."), text_round(ANSWER_TEXT)]
    _, seen, saved, _ = run_turn(monkeypatch, tmp_path, rounds, files={"a.txt": "x"})
    assert outcome() == "completed" and len(seen) == 3
    nudge = seen[2][-1]["content"]
    assert "ghost_config.py" in nudge and "no tool call or result this turn" in nudge


def test_invented_files_repeated_after_the_nudges_end_the_turn_honestly_naming_them(monkeypatch, tmp_path):
    rounds = [tool_round(call("list_files", path="."))] + [text_round(f"Take {n}: " + ANSWER_TEXT.replace("limit", "cap" * n) + " I read ghost_config.py and it says so.") for n in (1, 2, 3)]
    _, seen, saved, _ = run_turn(monkeypatch, tmp_path, rounds, files={"a.txt": "x"})
    assert outcome() == "ungrounded_claims_giveup" and len(seen) == 4
    assert "ghost_config.py" in saved[-1] and "never actually looked at" in saved[-1]
    assert [e for e in trace() if e["event"] == "turn_end"][-1]["files"] == ["ghost_config.py"]


def test_ungrounded_paths_unit_rules(tmp_path):
    check = lambda reply, messages, base=0, ws="": main._ungrounded_paths(reply, messages, base, ws)
    tool_msgs = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "static/app.js"}}}]},
                 {"role": "tool", "content": json.dumps({"path": "static/app.js", "content": "x"})}]
    assert check("static/app.js has it", tool_msgs) == [] and check("app.js has it", tool_msgs) == []                 # by path or name
    assert check("ghost.py has it", tool_msgs) == ["ghost.py"]
    assert check("see main.py", [{"role": "user", "content": "please look at main.py"}], base=1) == []              # the person named it
    assert check("see main.py", [{"role": "system", "content": "main.py is huge"}]) == ["main.py"]                    # system prompt is not evidence
    earlier = [{"role": "assistant", "content": "I changed styles.css last time"}]
    assert check("styles.css is updated", earlier, base=1) == []                                                    # an earlier turn's words count
    this_turn = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "I read ghost.py"}, {"role": "user", "content": "ghost.py was never shown"}]
    assert check("ghost.py again", this_turn, base=1) == ["ghost.py"]                                               # this turn's own words, and the nudges, never vouch
    (tmp_path / "real.py").write_text("x")
    assert check("real.py", [], ws=str(tmp_path)) == [] and check("version 1.2.3 and example.com", []) == []
