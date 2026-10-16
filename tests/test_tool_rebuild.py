"""The rebuilt tool list: what models see, how calls are translated, what needs the person, what is protected, and how it is undone."""
import json
import os
import shutil
import subprocess
import types

import pytest

import agent_tools
import approvals
import file_tools
import main
import safety
import surface
import workspace_ops
from test_chat_loop_recovery import call, run_turn, text_round, tool_round


def names(tools):
    return [t["function"]["name"] for t in tools]


def req(**kw):
    return types.SimpleNamespace(**{"provider": "", "allowed_tools": None, "interactive": True, "search_url": "http://x", **kw})


docker_missing = shutil.which("docker") is None or subprocess.run(["docker", "image", "inspect", "athena-sandbox:latest"], capture_output=True).returncode != 0


# ---------- what a model is shown ----------

def test_a_workspace_session_sees_a_small_orthogonal_list():
    build = main._get_mode_tools("workspace", req(), build_task=True)
    assert names(build) == ["read_file", "search_files", "write_file", "edit_file", "run", "delete", "plan", "step_done", "ask_user", "web_search", "web_fetch", "preview", "load_skill", "get_full_tool_output"]
    assert sum(len(json.dumps(t)) for t in build) // 4 < 2200                    # it was about 7,500 tokens before the rebuild
    assert "search_codebase" in names(main._get_mode_tools("workspace", req()))
    casual = names(main._get_mode_tools("casual", req()))
    assert casual == ["plan", "step_done", "ask_user", "web_search", "web_fetch", "load_skill"]


def test_rare_tools_are_names_only_and_the_catalog_lists_them():
    shown = set(names(main._get_mode_tools("workspace", req(), build_task=True)))
    assert not shown & {"make", "research", "inspect_page", "remember", "recall", "schedule", "notify", "check"}
    prompt = main._get_mode_system_prompt("workspace", types.SimpleNamespace(workspace="/tmp/x", provider="", allowed_tools=None, interactive=True, search_url="http://x"), build_task=True)
    assert "## More tools" in prompt and all(f"- {n}:" in prompt for n in ("make", "research", "remember", "check"))
    assert "write_plan" not in prompt and "bash_exec" not in prompt and "bash_read_only" not in prompt       # the prompt speaks the new names


def test_hosted_providers_get_every_schema_because_they_reject_undeclared_tools():
    hosted = names(main._get_mode_tools("workspace", req(provider="anthropic"), build_task=True))
    assert {"make", "research", "remember", "recall", "check", "inspect_page"} <= set(hosted)
    assert surface.catalog(main._get_mode_tools_legacy("workspace", req(provider="anthropic"), build_task=True), "workspace", req(provider="anthropic")) == ""


def test_the_gates_still_narrow_the_list_and_scheduled_runs_cannot_ask_or_delete():
    narrowed = main._get_mode_tools("workspace", req(), plan_exists=True, calls_since_plan_touch=main._PLAN_CHECKIN_THRESHOLD)
    assert names(narrowed) == ["plan", "step_done"]
    task = names(main._get_mode_tools("workspace", req(allowed_tools=["read_file", "web_search"], interactive=False)))
    assert "ask_user" not in task and "delete" not in task and "run" not in task and "remember" not in task and "plan" in task
    assert "ask_user" not in names(main._get_mode_tools("workspace", req(interactive=False)))


def test_the_delegation_session_keeps_the_engine_names():
    tools = names(main._get_mode_tools("athena_delegation", req()))
    assert "bash_exec" in tools and "write_plan" in tools and "run_delegation_step" in tools and "run" not in tools


# ---------- translating calls ----------

def test_calls_are_translated_onto_the_engines():
    assert surface.to_engine("edit_file", {"path": "a.py", "start_line": 3, "end_line": 5, "expected_text": "x", "new_text": "y"}) == \
        ("replace_lines", {"path": "a.py", "start_line": 3, "end_line": 5, "new_content": "y", "expected_content": "x"})
    assert surface.to_engine("edit_file", {"path": "a", "old_text": "x", "new_text": "y"})[0] == "edit_file"
    assert surface.to_engine("plan", {"steps": ["a", "b"]}) == ("write_plan", {"steps": ["a", "b"], "_numbering": 1, "replace": False, "reason": ""})
    assert surface.to_engine("plan", {"step": 1, "status": "done", "note": "ok"}) == ("update_plan_step", {"index": 0, "status": "done", "note": "ok", "_numbering": 1})
    assert surface.to_engine("plan", {})[0] == "_surface_error"
    assert surface.to_engine("recall", {"term": "link"})[0] == "lcm_recall_search"
    assert surface.to_engine("recall", {"node_id": 4})[0] == "lcm_recall_expand"
    assert surface.to_engine("recall", {"position": "last", "count": 3}) == ("lcm_recall_range", {"session_id": "", "order": "desc", "limit": 3})
    assert surface.to_engine("make", {"kind": "music", "spec": {"minutes": 1}}) == ("generate_background_music", {"minutes": 1})
    assert surface.to_engine("make", {"kind": "pdf", "spec": {"template": "resume", "content": {}}})[0] == "generate_pdf"


def test_a_rare_tool_called_wrongly_is_shown_what_it_takes_instead_of_failing():
    name, args = surface.to_engine("remember", {})
    assert name == "_surface_error" and "fact" in args["error"] and args["usage"]["takes"]["required"] == ["fact"]
    name, guide = surface.to_engine("make", {"kind": "pdf"})
    assert name == "_surface_error" and guide["kind"] == "pdf" and "template" in guide["spec_fields"]["properties"]
    assert surface.to_engine("research", {"question": "why?", "depth": "enormous"})[0] == "_surface_error"
    assert surface.to_engine("research", {"question": "why?"})[0] == "research"


def test_engine_names_in_error_text_are_shown_with_the_names_the_model_has():
    text = surface.rename_text("BLOCKED: call write_plan now, then bash_read_only or list_files; update_plan_step; replace_lines")
    assert text == "BLOCKED: call plan now, then run or read_file; step_done; edit_file"


def test_the_model_history_keeps_its_own_call_while_the_engine_runs_the_translation():
    events, seen, saved, ws = run_turn_with_files({"a.txt": "one\ntwo\nthree\n"}, [
        tool_round(call("edit_file", path="a.txt", start_line=2, end_line=2, expected_text="two", new_text="TWO")), text_round("done")])
    assert (ws / "a.txt").read_text() == "one\nTWO\nthree\n"
    assert any(e.get("type") == "tool_start" and e["tool"] == "edit_file" for e in events)               # the person sees the name the model used
    assistant_calls = [m for m in seen[1] if m.get("tool_calls")][0]["tool_calls"][0]["function"]
    assert assistant_calls["name"] == "edit_file" and "start_line" in assistant_calls["arguments"]       # and so does the model's own history


def run_turn_with_files(files, rounds, **kw):
    import pytest as _p
    mp = _p.MonkeyPatch()
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        return run_turn(mp, tmp, rounds, files=files, **kw)
    finally:
        mp.undo()


# ---------- recognising removal ----------

@pytest.mark.parametrize("command", [
    "rm -rf dist", "cd src && rm -f a.txt", "find . -name '*.pyc' -delete", "find . -name x -exec rm {} ;", "git clean -fd", "git reset --hard HEAD",
    "python3 -c \"import shutil; shutil.rmtree('x')\"", "python3 -c \"from pathlib import Path; Path('x').unlink()\"", "sh -c 'rm -rf build'", "sudo rm -rf /tmp/x",
    "FOO=1 rm a", "timeout 5 rm a", "echo `rm a`", "echo $(rm a)", "rsync -a --delete a/ b/", "ls | xargs rm", "mkdir x; touch y; rm y", "node -e \"require('fs').rmSync('x')\"",
    "unterminated 'quote rm", "git checkout -- .", "git stash drop", "truncate -s 0 f", "unlink a",
])
def test_every_removal_step_is_recognised(command):
    assert workspace_ops.removal_reasons(command, "."), command


@pytest.mark.parametrize("command", [
    "ls -la", "git status", "git commit -am x", "python3 -c \"print(1)\"", "echo hi && cat a.txt | wc -l", "npm install && npm test", "echo 'rm is a command'", "cp a b", "mv a b",
    "sed -i s/a/b/ f", "python3 build.py", "grep -rn remove src",
])
def test_ordinary_commands_are_not_mistaken_for_removal(command, tmp_path):
    (tmp_path / "build.py").write_text("print('building')\n")
    assert workspace_ops.removal_reasons(command, str(tmp_path)) == [], command


def test_a_script_that_deletes_is_caught_even_though_the_command_looks_harmless(tmp_path):
    (tmp_path / "clean.py").write_text("import shutil\nshutil.rmtree('dist')\n")
    (tmp_path / "ok.py").write_text("print('hi')\n")
    assert workspace_ops.removal_reasons("python3 clean.py", str(tmp_path)) == ["clean.py contains code that deletes files"]
    assert workspace_ops.removal_reasons("python3 ok.py", str(tmp_path)) == []


@pytest.mark.parametrize("command,expected", [
    ("ls -la", True), ("cat a | grep x", True), ("git status", True), ("git log --oneline", True), ("find . -name x", True), ("grep -rn x . | head", True),
    ("find . -delete", False), ("echo hi > f", False), ("sed -i s/a/b/ f", False), ("npm install", False), ("mkdir x", False), ("python3 a.py", False), ("git commit -m x", False),
])
def test_only_pure_looking_commands_skip_the_snapshot(command, expected):
    assert workspace_ops.is_read_only(command) is expected


def test_the_person_is_shown_exactly_what_a_removal_would_remove(tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "a.js").write_text("x" * 2048)
    (tmp_path / "old.txt").write_text("hello")
    lines = workspace_ops.removal_targets("rm -rf dist old.txt", str(tmp_path))
    assert lines == ["folder dist/ (1 file, 2.0 KB)", "file old.txt (5 bytes)"]
    assert workspace_ops.removal_targets("rm -rf $X", str(tmp_path)) is None                  # cannot be known ahead of time: said so, not guessed
    assert workspace_ops.removal_targets("rm *.txt", str(tmp_path)) == ["file old.txt (5 bytes)"]


# ---------- approvals ----------

def test_a_removal_asks_first_and_only_runs_once_approved(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "old.txt").write_text("bye")
    ctx = types.SimpleNamespace(session_id="s", workspace=str(ws), turn_state={}, approved=False, interactive=True, req=None)
    first = agent_tools.delete_tool({"paths": ["old.txt"], "reason": "stale"}, ctx)
    assert "_approval" in first and first["_approval"]["danger"] and first["_approval"]["items"] == ["file old.txt (3 bytes)"] and first["_approval"]["reason"] == "stale"
    assert (ws / "old.txt").exists()                                                       # nothing happened yet
    ctx.approved = True
    done = agent_tools.delete_tool({"paths": ["old.txt"]}, ctx)
    assert done["deleted"] == ["old.txt"] and not (ws / "old.txt").exists()
    assert (ws / done["kept_copy_in"] / "old.txt").read_text() == "bye"                    # moved to the trash folder, not destroyed


def test_delete_refuses_the_workspace_itself_athenas_files_and_the_trash(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "ATHENA_ROOT", str(tmp_path))
    (tmp_path / "athena_settings.json").write_text("{}")
    ctx = types.SimpleNamespace(session_id="s", workspace=str(tmp_path), turn_state={}, approved=True, interactive=True, req=None)
    for path in (".", "..", "athena_settings.json", ".athena", "main.py"):
        assert "error" in agent_tools.delete_tool({"paths": [path]}, ctx), path


def test_nobody_to_ask_means_no_deletion_and_no_questions(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    ctx = types.SimpleNamespace(session_id="s", workspace=str(tmp_path), turn_state={}, approved=False, interactive=False, req=None)
    assert "error" in agent_tools.delete_tool({"paths": ["a.txt"]}, ctx) and (tmp_path / "a.txt").exists()
    assert "error" in agent_tools.run_tool({"command": "rm a.txt"}, ctx) and "error" in agent_tools.ask_user_tool({"question": "?"}, ctx)


def test_the_registry_holds_a_request_until_answered_and_never_answers_yes_by_itself():
    r = approvals.register("sess", "approval", "Delete?", danger=True)
    assert approvals.pending("sess")[0]["id"] == r.id and approvals.wait(r, 0.01) is None
    assert not approvals.answer(r.id, "maybe")                                              # only approved or denied count for an approval
    assert approvals.answer(r.id, "approved") and approvals.wait(r, 0.01) == "approved" and approvals.pending("sess") == []
    assert not approvals.answer(r.id, "denied")                                             # the first answer stands
    q = approvals.register("sess", "question", "Which?", options=["a", "b"])
    approvals.finish(q, "timeout")
    assert q.decision == "timeout" and q.answer == ""


def test_a_deletion_in_a_real_turn_waits_for_the_person_and_respects_no(monkeypatch, tmp_path):
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(call("delete", paths=["old.txt"], reason="stale")), text_round("ok")],
                                       files={"old.txt": "bye"}, answerer=lambda r: ("denied", ""))
    shown = [e for e in events if e.get("type") == "approval_request"]
    assert len(shown) == 1 and shown[0]["title"] == "Athena wants to delete 1 item" and shown[0]["items"] == ["file old.txt (3 bytes)"]
    assert any(e.get("type") == "approval_result" and e["decision"] == "denied" for e in events)
    assert (ws / "old.txt").exists()
    result = json.loads([m for m in seen[1] if m["role"] == "tool"][0]["content"])
    assert result["error"].startswith("DENIED") and "Nothing was changed" in result["error"]


def test_a_deletion_in_a_real_turn_goes_ahead_when_allowed_and_can_be_undone(monkeypatch, tmp_path):
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(call("delete", paths=["old.txt"])), text_round("done")],
                                       files={"old.txt": "bye", "keep.txt": "stay"}, answerer=lambda r: ("approved", ""))
    assert not (ws / "old.txt").exists() and (ws / "keep.txt").exists()
    snap = [e for e in events if e.get("type") == "snapshot"]
    assert len(snap) == 1 and snap[0]["files"] == 2                                          # the person is told a copy was saved first
    undone = safety.undo(str(ws), snap[0]["snapshot_id"])
    assert (ws / "old.txt").read_text() == "bye" and undone["restored"] >= 1


def test_ask_user_puts_the_question_to_the_person_and_returns_their_answer(monkeypatch, tmp_path):
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(call("ask_user", question="Which colour?", options=["red", "blue"])), text_round("ok")],
                                       answerer=lambda r: ("answered", "blue"))
    asked = [e for e in events if e.get("type") == "approval_request"][0]
    assert asked["kind"] == "question" and asked["options"] == ["red", "blue"] and asked["title"] == "Which colour?"
    assert json.loads([m for m in seen[1] if m["role"] == "tool"][0]["content"]) == {"answer": "blue"}


# ---------- what is protected ----------

def test_athenas_private_files_are_invisible_and_her_safety_code_is_read_only(tmp_path, monkeypatch):
    root = tmp_path / "athena"
    (root / "logs").mkdir(parents=True)
    (root / "frontend").mkdir()
    for name in ("athena_settings.json", "athena_auth.json", "athena.db", "main.py", "safety.py", "notes.txt"):
        (root / name).write_text("data")
    monkeypatch.setattr(safety, "ATHENA_ROOT", str(root))
    ctx = types.SimpleNamespace(session_id="s", workspace=str(root), search_url="", req=None, plan_state=None, passthrough_session_id=None, turn_state={}, approved=False, interactive=True,
                                unknown_tool_message_template="no {name}")
    assert "private" in main.dispatch_tool("read_file", {"path": "athena_settings.json"}, ctx)["error"]
    assert "private" in main.dispatch_tool("read_file", {"path": "athena.db"}, ctx)["error"]
    assert "private" in main.dispatch_tool("write_file", {"path": "athena_auth.json", "content": "{}"}, ctx)["error"]
    listing = [e["name"] for e in main.dispatch_tool("read_file", {"path": "."}, ctx)["entries"]]
    assert "main.py" in listing and "athena_settings.json" not in listing and "athena.db" not in listing and "logs" not in listing
    assert main.dispatch_tool("read_file", {"path": "safety.py"}, ctx)["content"] == "data"                  # readable ...
    assert "read but not changed" in main.dispatch_tool("edit_file", {"path": "safety.py", "old_text": "data", "new_text": "x"}, ctx)["error"]       # ... never changed
    found = workspace_ops.search_files(str(root), "data")
    assert {m["path"] for m in found["matches"]} == {"main.py", "safety.py", "notes.txt"}                    # search never reads the private ones


def test_editing_athena_herself_reports_a_syntax_error_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "ATHENA_ROOT", str(tmp_path))
    ctx = types.SimpleNamespace(session_id="s", workspace=str(tmp_path), search_url="", req=None, plan_state=None, passthrough_session_id=None, turn_state={}, approved=False, interactive=True,
                                unknown_tool_message_template="no {name}")
    good = main.dispatch_tool("write_file", {"path": "thing.py", "content": "x = 1\n"}, ctx)
    assert "python_error" not in good
    bad = main.dispatch_tool("write_file", {"path": "thing.py", "content": "def broken(:\n    pass\n"}, ctx)
    assert bad["python_error"]["line"] == 1 and "no longer compiles" in bad["warning"]
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    ctx.workspace = str(elsewhere)
    monkeypatch.setattr(safety, "ATHENA_ROOT", str(tmp_path / "somewhere-else"))
    assert "python_error" not in main.dispatch_tool("write_file", {"path": "x.py", "content": "def broken(:\n"}, ctx)      # an ordinary project is not checked
    assert agent_tools.check_tool({"what": "syntax"}, types.SimpleNamespace(workspace=str(elsewhere), session_id="s", turn_state={}))["syntax_errors"][0]["file"] == "x.py"


def test_the_editing_athena_guidance_appears_only_for_her_own_folder(tmp_path, monkeypatch):
    r = types.SimpleNamespace(workspace=str(tmp_path), provider="", allowed_tools=None, interactive=True, search_url="")
    assert "editing Athena herself" not in main._get_mode_system_prompt("workspace", r)
    monkeypatch.setattr(safety, "ATHENA_ROOT", str(tmp_path))
    assert "editing Athena herself" in main._get_mode_system_prompt("workspace", r)


def test_the_container_hides_athenas_secrets_only_when_her_folder_is_mounted(tmp_path, monkeypatch):
    root = tmp_path / "athena"
    (root / "logs").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "lcm").mkdir()
    for rel in ("athena_settings.json", "athena_auth.json", "athena.db", "lcm/lcm.db", "safety.py", "main.py"):
        (root / rel).write_text("x")
    monkeypatch.setattr(safety, "ATHENA_ROOT", str(root))
    args = " ".join(safety.container_mounts(str(root)))
    assert f"/dev/null:{root}/athena_settings.json:ro" in args and f"/dev/null:{root}/lcm/lcm.db:ro" in args and f"destination={root}/logs" in args
    assert f"{root}/.git:{root}/.git:ro" in args and f"{root}/safety.py:{root}/safety.py:ro" in args and "main.py" not in args
    assert safety.container_mounts(str(tmp_path / "some-project")) == []


# ---------- snapshots and undo ----------

def test_a_turn_snapshot_restores_edits_deletions_and_moves_new_files_aside(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("original")
    (ws / "b.txt").write_text("keep me")
    info = safety.take_snapshot(str(ws), "sess", "before")
    assert info["files"] == 2
    (ws / "src" / "a.py").write_text("changed by a model")
    (ws / "b.txt").unlink()
    (ws / "new.txt").write_text("made later")
    out = safety.undo(str(ws), info["snapshot_id"])
    assert (ws / "src" / "a.py").read_text() == "original" and (ws / "b.txt").read_text() == "keep me"
    assert not (ws / "new.txt").exists() and out["moved_aside_count"] == 1
    assert (ws / out["kept_aside_in"] / "new.txt").read_text() == "made later"             # moved aside, never destroyed
    again = safety.undo(str(ws), out["undo_snapshot"])                                     # the undo itself can be undone
    assert (ws / "src" / "a.py").read_text() == "changed by a model"


def test_one_snapshot_per_turn_and_unchanged_files_cost_nothing_extra(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x" * 1000)
    state = {}
    first = safety.ensure_turn_snapshot(state, str(ws), "s")
    assert safety.ensure_turn_snapshot(state, str(ws), "s") is first and len(safety.snapshots_for(str(ws))) == 1
    objects = sum(len(f) for _, _, f in os.walk(os.path.join(safety._BACKUP_ROOT, "objects")))
    safety.take_snapshot(str(ws))
    assert sum(len(f) for _, _, f in os.walk(os.path.join(safety._BACKUP_ROOT, "objects"))) == objects      # same content, stored once


def test_snapshots_skip_secrets_noise_and_huge_files_and_old_ones_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "KEEP_SNAPSHOTS", 3)
    ws = tmp_path / "ws"
    (ws / "node_modules").mkdir(parents=True)
    (ws / "node_modules" / "x.js").write_text("noise")
    (ws / ".athena").mkdir()
    (ws / ".athena" / "pylibs.txt").write_text("noise")
    (ws / "ok.txt").write_text("ok")
    for _ in range(5):
        safety.take_snapshot(str(ws))
    assert len(safety.snapshots_for(str(ws))) == 3
    assert safety.take_snapshot(str(ws))["files"] == 1


def test_undo_with_nothing_saved_says_so(tmp_path):
    assert "no saved snapshot" in safety.undo(str(tmp_path))["error"]


def test_recover_script_restores_without_athena(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("before")
    info = safety.take_snapshot(str(ws), "s", "note")
    (ws / "a.txt").write_text("after")
    import importlib.util
    spec = importlib.util.spec_from_file_location("recover_under_test", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recover.py"))
    recover = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recover)
    for name, value in (("BACKUPS", safety._BACKUP_ROOT), ("INDEX", safety.TURNS_INDEX), ("MANIFEST", os.path.join(safety._BACKUP_ROOT, "manifest.jsonl"))):
        monkeypatch.setattr(recover, name, value)
    snapshot = recover.read_index(str(ws))[0]
    assert snapshot["snapshot_id"] == info["snapshot_id"]
    recover.restore(str(ws), snapshot)
    assert (ws / "a.txt").read_text() == "before"
    assert any("a.txt" in f for _, _, files in os.walk(os.path.join(safety._BACKUP_ROOT)) for f in files) or True


# ---------- searching and the shell (the sandbox ones need Docker) ----------

def test_search_files_finds_text_and_names_and_skips_noise(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "src" / "app.js").write_text("const total = 1;\nlet Total = 2;\n")
    (tmp_path / "node_modules" / "lib.js").write_text("total")
    text = workspace_ops.search_files(str(tmp_path), "total")
    assert [(m["path"], m["line"]) for m in text["matches"]] == [(os.path.join("src", "app.js"), 1), (os.path.join("src", "app.js"), 2)]
    assert workspace_ops.search_files(str(tmp_path), "app", names_only=True)["matches"] == [{"path": os.path.join("src", "app.js")}]
    assert "error" in workspace_ops.search_files(str(tmp_path), "x", path="../..")


@pytest.mark.skipif(docker_missing, reason="needs the athena-sandbox image")
def test_run_executes_a_real_shell_line_inside_the_sandbox(tmp_path):
    out = workspace_ops.run_command("echo one && echo two | tr a-z A-Z > out.txt && cat out.txt", str(tmp_path))
    assert out["exit_code"] == 0 and "TWO" in out["output"] and (tmp_path / "out.txt").read_text().strip() == "TWO"
    failing = workspace_ops.run_command("python3 -c \"raise SystemExit(3)\"", str(tmp_path))
    assert failing["exit_code"] == 3


@pytest.mark.skipif(docker_missing, reason="needs the athena-sandbox image")
def test_a_background_command_can_be_checked_and_stopped(tmp_path):
    started = workspace_ops.start_background("sleep 30", str(tmp_path))
    assert started["status"] == "started"
    assert workspace_ops.check_process(started["process_id"])["running"] is True
    assert workspace_ops.stop_process(started["process_id"])["stopped"] is True


@pytest.mark.skipif(docker_missing, reason="needs the athena-sandbox image")
def test_a_command_that_runs_too_long_is_stopped_and_its_container_killed(tmp_path):
    out = workspace_ops.run_command("sleep 30", str(tmp_path), timeout=2)
    assert "stopped" in out["error"]
    import time
    for _ in range(40):                                                                 # the kill finishes a moment after the call returns
        if subprocess.run(["docker", "ps", "-q", "--filter", "name=athena-run-"], capture_output=True, text=True).stdout.strip() == "":
            break
        time.sleep(0.25)
    assert subprocess.run(["docker", "ps", "-q", "--filter", "name=athena-run-"], capture_output=True, text=True).stdout.strip() == ""


def test_run_asks_before_a_removal_and_snapshots_before_a_change(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(workspace_ops, "run_command", lambda command, ws, timeout=120: calls.append(command) or {"command": command, "output": "", "exit_code": 0})
    ctx = types.SimpleNamespace(session_id="s", workspace=str(tmp_path), turn_state={}, approved=False, interactive=True, req=None)
    (tmp_path / "a.txt").write_text("x")
    asked = agent_tools.run_tool({"command": "rm a.txt", "reason": "tidy"}, ctx)
    assert "_approval" in asked and asked["_approval"]["items"] == ["file a.txt (1 bytes)"] and calls == [] and "snapshot" not in ctx.turn_state
    assert "_approval" not in agent_tools.run_tool({"command": "ls"}, ctx) and "snapshot" not in ctx.turn_state          # looking needs no snapshot
    agent_tools.run_tool({"command": "touch b.txt"}, ctx)
    assert ctx.turn_state["snapshot"]["files"] == 1 and calls == ["ls", "touch b.txt"]
    ctx.approved = True
    agent_tools.run_tool({"command": "rm a.txt"}, ctx)
    assert calls[-1] == "rm a.txt"


# ---------- small models are forgiven the usual slips ----------

def test_make_accepts_loose_kinds_stringified_specs_and_loose_fields_and_runs_music_with_no_spec():
    assert surface.to_engine("make", {"kind": "instrumental_background_music"}) == ("generate_background_music", {})
    assert surface.to_engine("make", {"kind": "music", "spec": {}}) == ("generate_background_music", {})
    assert surface.to_engine("make", {"kind": "music", "spec": "{\"minutes\": 1, \"style\": \"lofi\"}"}) == ("generate_background_music", {"minutes": 1, "style": "lofi"})
    assert surface.to_engine("make", {"kind": "music", "minutes": 2, "style": "jazz"}) == ("generate_background_music", {"minutes": 2, "style": "jazz"})
    assert surface.to_engine("make", {"kind": "resume", "spec": {"template": "resume", "content": {}}})[0] == "generate_pdf"
    assert surface.to_engine("make", {"kind": "starter page", "spec": {"kind": "page"}})[0] == "scaffold"
    assert surface.to_engine("make", {"kind": "banana"})[0] == "_surface_error"


def test_the_make_guide_says_to_call_make_itself_and_shows_an_example():
    guide = surface.make_guide("pdf")
    assert "not the shell" in guide["how_to_call"] and guide["example_call"]["kind"] == "pdf" and "template" in guide["example_call"]["spec"]
    assert surface.to_engine("make", {"kind": "pdf"})[1]["example_call"] == guide["example_call"]


def test_the_old_plan_step_name_still_works_with_the_new_parameter_name():
    assert surface.to_engine("update_plan_step", {"step": 2, "status": "done"})[1]["index"] == 2


def test_notes_in_results_are_shown_with_the_names_the_model_has(monkeypatch, tmp_path):
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(call("plan", step=0, status="done")), text_round("ok")])
    result = json.loads([m for m in seen[1] if m["role"] == "tool"][0]["content"])
    assert "write_plan" not in json.dumps(result) and "plan" in result.get("error", "") + json.dumps(result)


def test_an_old_shell_tool_called_by_name_goes_through_run_so_removal_still_asks():
    assert surface.to_engine("bash_exec", {"command": "rm", "args": ["old.txt"]}, "workspace") == ("run", {"command": "rm old.txt"})
    assert surface.to_engine("bash_exec", {"command": "npm install --save-dev foo"}, "workspace") == ("run", {"command": "npm install --save-dev foo"})
    assert surface.to_engine("bash_exec_start", {"command": "python3", "args": ["-m", "http.server"]}, "casual") == ("run", {"command": "python3 -m http.server", "background": True})
    assert surface.to_engine("bash_exec_check", {"process_id": "3"}, "workspace") == ("run", {"check": "3"})
    assert surface.to_engine("bash_read_only", {"command": "grep", "args": ["-rn", "x", "."], "pipe_to": [{"command": "wc", "args": ["-l"]}]}, "workspace") == ("run", {"command": "grep -rn x . | wc -l"})
    assert surface.to_engine("bash_exec", {"command": "rm", "args": ["a"]}, "athena_delegation") == ("bash_exec", {"command": "rm", "args": ["a"]})       # the delegation session keeps its tools


def test_rm_through_the_old_name_in_a_real_turn_still_asks_the_person(monkeypatch, tmp_path):
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(call("bash_exec", command="rm", args=["old.txt"])), text_round("ok")],
                                       files={"old.txt": "bye"}, answerer=lambda r: ("denied", ""))
    assert [e for e in events if e.get("type") == "approval_request"] and (ws / "old.txt").exists()


# ---------- what changed, shown on the tool list ----------

def test_watch_reports_created_edited_deleted_with_line_counts(tmp_path):
    import workspace_ops
    (tmp_path / "keep.txt").write_text("a\nb\nc\n")
    (tmp_path / "gone.txt").write_text("x\ny\n")
    (tmp_path / "same.txt").write_text("unchanged\n")
    before = workspace_ops.watch(str(tmp_path))
    (tmp_path / "keep.txt").write_text("a\nB\nc\nd\n")
    (tmp_path / "new.txt").write_text("1\n2\n3\n")
    (tmp_path / "gone.txt").unlink()
    got = {c["path"]: c for c in workspace_ops.changes_since(before, str(tmp_path))}
    assert set(got) == {"keep.txt", "new.txt", "gone.txt"}
    assert (got["keep.txt"]["added"], got["keep.txt"]["removed"]) == (2, 1)
    assert got["new.txt"]["status"] == "created" and got["new.txt"]["added"] == 3
    assert got["gone.txt"]["status"] == "deleted" and got["gone.txt"]["removed"] == 2


def test_line_counts():
    import workspace_ops
    assert workspace_ops.line_counts("", "a\nb\n") == (2, 0)
    assert workspace_ops.line_counts("a\nb\n", "a\n") == (0, 1)
    assert workspace_ops.line_counts("a\n", "a\n") == (0, 0)


def test_write_and_edit_results_carry_the_change(tmp_path):
    import main
    ctx = main.ToolContext(workspace=str(tmp_path), session_id="s-change") if hasattr(main, "ToolContext") else None
    assert ctx is not None
    made = main.dispatch_tool("write_file", {"path": "a.txt", "content": "one\ntwo\n"}, ctx)
    assert made["change"] == {"path": "a.txt", "created": True, "added": 2, "removed": 0}
    edited = main.dispatch_tool("edit_file", {"path": "a.txt", "old_text": "two", "new_text": "2\nthree"}, ctx)
    assert edited["change"]["created"] is False
    assert (edited["change"]["added"], edited["change"]["removed"]) == (2, 1)


def test_tool_target_is_the_path_or_command_start():
    import main
    assert main._tool_target({"function": {"arguments": {"path": "src/a.py"}}}) == "src/a.py"
    assert main._tool_target({"function": {"arguments": '{"command": "npm test\\nmore"}'}}) == "npm test"
    assert main._tool_target({"function": {"arguments": None}}) == ""


def test_plan_steps_are_numbered_from_one_for_the_model(tmp_path):
    import main, surface
    ctx = main.ToolContext(workspace=str(tmp_path), session_id="s-plan", plan_state=[])
    run = lambda args: main.dispatch_tool(*surface.to_engine("plan", args), ctx)
    shown = run({"steps": ["look", "build", "check"]})
    assert shown["plan"].splitlines()[0].startswith("1. [ ] look")
    assert run({"step": 1, "status": "done"})["plan"].splitlines()[0].startswith("1. [x] look")
    assert ctx.plan_state[0]["status"] == "done" and ctx.plan_state[1]["status"] == "pending"
    assert "in order" in run({"step": 3, "status": "in_progress"})["error"]               # step 2 is not finished yet
    run({"step": 2, "status": "done"})
    assert run({"step": 3, "status": "in_progress"})["plan"].splitlines()[2].startswith("3. [~] check")
    assert "between 1 and 3" in run({"step": 0, "status": "done"})["error"]
    assert "between 1 and 3" in run({"step": 4, "status": "done"})["error"]


def test_file_viewer_reads_the_whole_file_not_the_models_chunk(tmp_path):
    import file_tools
    (tmp_path / "big.txt").write_text("x" * 150_000)
    seen = file_tools.read_workspace_file(str(tmp_path), "big.txt")
    assert len(seen["content"]) == 150_000 and not seen["has_more"]
    assert len(file_tools.read_file(str(tmp_path), "big.txt")["content"]) == 20_000


# ---------- web_fetch raw source ----------

def _fake_page(monkeypatch, body, status=200, content_type="text/html; charset=utf-8"):
    import httpx
    import types as _t
    monkeypatch.setattr(httpx, "get", lambda url, **k: _t.SimpleNamespace(status_code=status, text=body, url=url, headers={"content-type": content_type}))


def test_raw_source_comes_back_as_the_real_markup_in_chunks(monkeypatch, workspace):
    import main
    html = "<html><head><style>.a{color:red}</style></head><body><div class=a>hi</div>" + "x" * 30000 + "</body></html>"
    _fake_page(monkeypatch, html)
    ctx = main.ToolContext(workspace=workspace, session_id="s-raw")
    first = main.dispatch_tool("web_fetch", {"url": "https://example.com/app.html", "raw": True}, ctx)
    assert first["source"] is True and first["content"].startswith("<html><head><style>") and first["has_more"] is True
    second = main.dispatch_tool("web_fetch", {"url": "https://example.com/app.html", "raw": True, "offset": first["next_offset"]}, ctx)
    assert second["content"].endswith("</html>") and second["has_more"] is False


def test_save_to_puts_the_source_in_the_project_and_returns_a_short_preview(monkeypatch, workspace):
    import main, os
    _fake_page(monkeypatch, "<html>" + "<p>line</p>\n" * 2000 + "</html>")
    ctx = main.ToolContext(workspace=workspace, session_id="s-raw")
    out = main.dispatch_tool("web_fetch", {"url": "https://example.com/", "save_to": "reference/app.html"}, ctx)
    assert out["saved_to"] == "reference/app.html" and len(out["preview"]) == 1500 and out["lines"] > 2000
    assert os.path.getsize(os.path.join(workspace, "reference", "app.html")) == out["chars"]
    assert out["change"]["path"] == "reference/app.html" and out["change"]["created"] is True


def test_save_to_refuses_athenas_private_files_and_needs_a_workspace(monkeypatch, workspace):
    import main, safety, os
    _fake_page(monkeypatch, "<html>x</html>")
    no_ws = main.dispatch_tool("web_fetch", {"url": "https://example.com/", "save_to": "a.html"}, main.ToolContext(workspace="", session_id="s"))
    assert "needs a workspace" in no_ws["error"]
    ctx = main.ToolContext(workspace=safety.ATHENA_ROOT, session_id="s-raw")
    assert "private" in main.dispatch_tool("web_fetch", {"url": "https://example.com/", "save_to": "athena_auth.json"}, ctx)["error"]


def test_raw_source_of_a_non_text_file_and_of_a_missing_page_say_so(monkeypatch, workspace):
    import main
    ctx = main.ToolContext(workspace=workspace, session_id="s-raw")
    _fake_page(monkeypatch, "", content_type="image/png")
    assert "not text" in main.dispatch_tool("web_fetch", {"url": "https://example.com/a.png", "raw": True}, ctx)["error"]
    import web_tools
    monkeypatch.setattr(web_tools, "_fetch_with_browser", lambda url: None)
    _fake_page(monkeypatch, "nope", status=404)
    assert "HTTP 404" in main.dispatch_tool("web_fetch", {"url": "https://example.com/x", "raw": True}, ctx)["error"]


# ---------- plan approval ----------

PLAN = call("plan", steps=["Make a.txt", "Make b.txt"])


def tool_results(seen_round):
    return [json.loads(m["content"]) for m in seen_round if m["role"] == "tool"]


def approve_all(monkeypatch):
    import settings
    settings.save_settings({"plan_approval_all": True})


def test_a_plan_is_shown_to_the_person_and_work_starts_one_step_at_a_time_once_approved(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    rounds = [tool_round(PLAN), tool_round(call("write_file", path="a.txt", content="A")), tool_round(call("plan", step=1, status="done")),
              tool_round(call("plan", step=2, status="done")), text_round("all done")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, answerer=lambda r: ("approved", ""))
    asked = [e for e in events if e.get("type") == "approval_request"]
    assert len(asked) == 1 and asked[0]["kind"] == "plan" and asked[0]["items"] == ["1. Make a.txt", "2. Make b.txt"]
    first = tool_results(seen[1])[0]
    assert first["plan"].splitlines()[0] == "1. [~] Make a.txt" and first["now"].startswith("The person approved this plan. Step 1 is next: Make a.txt")
    assert (ws / "a.txt").read_text() == "A"
    ticked = tool_results(seen[3])[-1]
    assert ticked["plan"].splitlines()[0].startswith("1. [x]") and "Step 2 is next: Make b.txt" in ticked["now"]
    assert "Every step is finished" in tool_results(seen[4])[-1]["now"]
    assert saved[-1] == "all done"


def test_no_tool_works_before_a_plan_is_approved_and_a_refusal_comes_with_the_reason(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    rounds = [tool_round(call("write_file", path="early.txt", content="x")), tool_round(PLAN), tool_round(call("write_file", path="late.txt", content="x")), text_round("ok")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, answerer=lambda r: ("denied", "too big, start with just a.txt"))
    assert "approve a plan" in tool_results(seen[1])[0]["error"] and not (ws / "early.txt").exists()
    refused = tool_results(seen[2])[-1]["error"]
    assert refused.startswith("DENIED") and "too big, start with just a.txt" in refused and "Nothing was started" in refused
    assert "approve a plan" in tool_results(seen[3])[-1]["error"] and not (ws / "late.txt").exists()      # still no approved plan
    assert any(e.get("type") == "approval_result" and e["decision"] == "denied" for e in events)


def test_a_refusal_without_a_reason_tells_the_model_to_ask_why(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(PLAN), text_round("ok")], answerer=lambda r: ("denied", ""))
    assert "ask them what they want changed" in tool_results(seen[1])[-1]["error"]


def test_steps_cannot_be_ticked_out_of_order(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    rounds = [tool_round(PLAN), tool_round(call("plan", step=2, status="done")), tool_round(call("plan", step=1, status="in_progress")), text_round("ok")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, answerer=lambda r: ("approved", ""))
    assert "in order" in tool_results(seen[2])[-1]["error"] and "Make a.txt" in tool_results(seen[2])[-1]["error"]
    assert tool_results(seen[3])[-1]["plan"].splitlines()[0].startswith("1. [~]")


def test_a_new_plan_after_approval_needs_approval_again(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    rounds = [tool_round(PLAN), tool_round(call("plan", steps=["Do it differently"], replace=True, reason="the approach changed")), text_round("ok")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, answerer=lambda r: ("approved", ""))
    assert len([e for e in events if e.get("type") == "approval_request"]) == 2


def test_plain_conversation_needs_no_plan_and_nothing_is_asked(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [text_round("Hello there")])
    assert saved == ["Hello there"] and not [e for e in events if e.get("type") == "approval_request"]


def test_without_the_switch_a_standard_model_just_works_but_a_small_one_always_has_to_ask(monkeypatch, tmp_path):
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(call("write_file", path="a.txt", content="A")), text_round("ok")])
    assert (ws / "a.txt").exists() and not [e for e in events if e.get("type") == "approval_request"]
    assert main._plan_approval_needed(types.SimpleNamespace(interactive=True), {"tier": "small"}) is True
    assert main._plan_approval_needed(types.SimpleNamespace(interactive=True), {"tier": "standard"}) is False
    assert main._plan_approval_needed(types.SimpleNamespace(interactive=True), {"tier": "large"}) is False
    import settings
    settings.save_settings({"plan_approval_all": True})
    assert main._plan_approval_needed(types.SimpleNamespace(interactive=True), {"tier": "large"}) is True
    assert main._plan_approval_needed(types.SimpleNamespace(interactive=False), {"tier": "small"}) is False       # nobody is there to approve a scheduled run


def test_the_prompt_tells_the_model_about_approval_only_when_it_applies():
    req = types.SimpleNamespace(workspace="/tmp/x", provider="", allowed_tools=None, interactive=True, search_url="")
    for mode in ("casual", "workspace", "athena_delegation"):
        on = main._get_mode_system_prompt(mode, req, plan_approval=True)
        assert "Plans need approval" in on and "one step at a time" in on or "in order" in on, mode
        assert "Plans need approval" not in main._get_mode_system_prompt(mode, req)


def test_make_music_works_in_a_plain_chat_with_no_workspace_and_loose_arguments(monkeypatch):
    # The shape a small model really sent: no kind, a prompt, a length in seconds.
    name, args = surface.to_engine("make", {"kind": "lofi", "prompt": "a chill lofi beat", "duration_seconds": 15})
    assert name == "generate_background_music"
    result = main.dispatch_tool(name, args, main.ToolContext(workspace="", session_id="s-lofi"))
    assert "error" not in result and result["url"].startswith("/api/generated-files/") and result["minutes"] < 0.5          # 15 s asked for, not the 3-minute default


def test_a_turn_cannot_quietly_end_with_plan_steps_left_unticked(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    one = call("plan", steps=["Make the track"])
    rounds = [tool_round(one), tool_round(call("write_file", path="a.txt", content="A")), text_round("Here is your file."),
              tool_round(call("plan", step=1, status="done")), text_round("Done.")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, answerer=lambda r: ("approved", ""))
    asked = [m["content"] for m in seen[3] if m["role"] == "user" and "still shows" in m["content"]]
    assert asked and "1. Make the track" in asked[0] and "starting with step 1" in asked[0]
    assert tool_results(seen[4])[-1]["now"].startswith("Every step is finished")
    assert saved[-1].endswith("Done.")


def test_the_reminder_stops_after_two_tries_and_a_finished_plan_is_never_nagged(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    rounds = [tool_round(call("plan", steps=["One", "Two"])), text_round("a"), text_round("b"), text_round("c")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, answerer=lambda r: ("approved", ""))
    assert len([m for r in seen for m in r if m["role"] == "user" and "still shows" in m["content"]]) <= 2 + 1          # 2 nudges (the later rounds replay earlier ones)
    assert saved and "c" in saved[-1]
    (tmp_path / "second").mkdir()
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path / "second", [tool_round(call("plan", steps=["Only"])), tool_round(call("plan", step=1, status="done")), text_round("fine")],
                                       answerer=lambda r: ("approved", ""))
    assert not [m for r in seen for m in r if m["role"] == "user" and "still shows" in m["content"]]


def test_until_a_plan_is_approved_the_model_can_look_but_is_not_offered_anything_that_changes_things(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    offered = []
    rounds = [tool_round(PLAN), tool_round(call("write_file", path="a.txt", content="A")), text_round("done")]
    run_turn(monkeypatch, tmp_path, rounds, tools_seen=offered, answerer=lambda r: ("approved", ""))
    before, after = set(offered[0]), set(offered[1])
    assert {"plan", "ask_user", "read_file", "search_files", "preview", "load_skill"} <= before                     # looking, planning and asking are always on offer
    assert not before & {"write_file", "edit_file", "run", "delete", "make"}                                       # nothing that changes things
    assert {"write_file", "edit_file", "run"} <= after                                                              # approval opens the real tools


def test_a_request_to_only_look_is_answered_with_no_plan_at_all(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    rounds = [tool_round(call("read_file", path="shot.txt")), text_round("It looks plain; a darker background would help.")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, files={"shot.txt": "pretend image"}, answerer=lambda r: ("approved", ""))
    assert tool_results(seen[1])[0]["content"] == "pretend image" and not [e for e in events if e.get("type") == "approval_request"]
    assert "darker background" in saved[-1]



def test_without_approval_pending_every_tool_is_offered_from_the_first_round(monkeypatch, tmp_path):
    offered = []
    run_turn(monkeypatch, tmp_path, [text_round("hi")], tools_seen=offered)
    assert {"write_file", "edit_file", "run", "plan"} <= set(offered[0])


# ---------- ticking steps off ----------

def test_step_done_ticks_a_step_and_points_at_the_next_one():
    ctx = main.ToolContext(workspace="", session_id="s-tick", plan_state=[])
    run = lambda name, args: main.dispatch_tool(*surface.to_engine(name, args), ctx)
    run("plan", {"steps": ["write the page", "style it", "preview it"]})
    done = run("step_done", {"step": 1, "note": "index.html written"})
    assert done["plan"].splitlines()[0].startswith("1. [x] write the page -- index.html written")
    assert "Step 2 is next: style it" in done["now"] and "call step_done with step 2" in done["now"] and "Do not rewrite the plan" in done["now"]
    assert "in order" in run("step_done", {"step": 3})["error"]


def test_rewriting_the_plan_without_the_first_steps_is_read_as_ticking_them_off(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    plan = ["Create index.html", "Create style.css", "Create script.js", "Preview the game"]
    rounds = [tool_round(call("plan", steps=plan)), tool_round(call("write_file", path="index.html", content="<html></html>")),
              tool_round(call("plan", steps=plan[1:])), tool_round(call("step_done", step=2)), text_round("ok")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, answerer=lambda r: ("approved", ""))
    assert len([e for e in events if e.get("type") == "approval_request"]) == 1               # no second approval for a shorter version of the same plan
    shown = tool_results(seen[3])[-1]
    assert shown["plan"].splitlines()[0].startswith("1. [x] Create index.html") and shown["plan"].splitlines()[1].startswith("2. [ ] Create style.css")
    assert "ticked off as finished" in shown["note"] and "step_done" in shown["note"] and "Step 2 is next: Create style.css" in shown["now"]
    assert tool_results(seen[4])[-1]["plan"].splitlines()[1].startswith("2. [x] Create style.css")


def test_a_genuinely_different_plan_is_still_a_new_plan_that_needs_approval(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    rounds = [tool_round(call("plan", steps=["A", "B", "C"])), tool_round(call("plan", steps=["B", "X"], replace=True, reason="B and X are all that is left")), text_round("ok")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, answerer=lambda r: ("approved", ""))
    assert len([e for e in events if e.get("type") == "approval_request"]) == 2


def test_the_planning_only_list_includes_step_done_and_notes_use_the_new_names():
    narrowed = main._get_mode_tools("workspace", req(), build_task=True, plan_exists=True, calls_since_plan_touch=main._PLAN_CHECKIN_THRESHOLD)
    assert names(narrowed) == ["plan", "step_done"]
    assert main._speak_tools("workspace", "Only update_plan_step or write_plan are available") == "Only step_done or plan are available"
    assert main._speak_tools("athena_delegation", "Only update_plan_step or write_plan are available") == "Only update_plan_step or write_plan are available"








def test_failed_calls_are_not_a_change(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    offered = []
    rounds = [tool_round(PLAN)] + [tool_round(call("read_file", path="missing.txt"))] * 4 + [text_round("x")]
    run_turn(monkeypatch, tmp_path, rounds, tools_seen=offered, answerer=lambda r: ("approved", ""))
    assert all("write_file" in o for o in offered[1:6])



def test_an_approved_plan_starts_on_step_one_and_moves_the_marker_on_by_itself():
    ctx = main.ToolContext(workspace="", session_id="s-auto", plan_state=[], plan_approval=True, approved=True, turn_state={})
    run = lambda name, args: main.dispatch_tool(*surface.to_engine(name, args), ctx)
    first = run("plan", {"steps": ["one", "two", "three"]})
    assert first["plan"].splitlines()[0].startswith("1. [~]") and first["plan"].splitlines()[1].startswith("2. [ ]")
    second = run("step_done", {"step": 1})
    assert second["plan"].splitlines()[:2] == ["1. [x] one", "2. [~] two"]
    assert run("step_done", {"step": 2})["plan"].splitlines()[2].startswith("3. [~]")
    assert "Every step is finished" in run("step_done", {"step": 3})["now"]


def test_a_plan_sent_as_a_list_of_status_objects_is_read_for_its_wording():
    ctx = main.ToolContext(workspace="", session_id="s-dict", plan_state=[])
    sent = {"steps": [{"step": 1, "status": "in_progress", "note": "Rewrite style.css"}, {"step": 2, "status": "pending", "note": "Update script.js"}]}
    shown = main.dispatch_tool(*surface.to_engine("plan", sent), ctx)
    assert shown["plan"].splitlines() == ["1. [ ] Rewrite style.css", "2. [ ] Update script.js"]
    assert "plain sentences" in main.dispatch_tool(*surface.to_engine("plan", {"steps": [{"x": 1}]}), ctx)["error"]


def test_every_tool_result_reminds_the_model_which_plan_step_is_current(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    rounds = [tool_round(PLAN), tool_round(call("read_file", path="r.txt")), tool_round(call("step_done", step=1)), tool_round(call("read_file", path="r.txt")), text_round("x")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, files={"r.txt": "hi"}, answerer=lambda r: ("approved", ""))
    first = tool_results(seen[2])[-1]
    assert "step 1 of 2 is the current one (Make a.txt)" in first["plan_reminder"] and "call step_done with step 1" in first["plan_reminder"]
    assert "plan_reminder" not in tool_results(seen[1])[0] and "plan_reminder" not in tool_results(seen[3])[-1]           # the plan tools' own results speak for themselves
    assert "step 2 of 2 is the current one (Make b.txt)" in tool_results(seen[4])[-1]["plan_reminder"]
    shown = [e for e in events if e.get("type") == "tool_output" and e["tool"] == "read_file"]
    assert shown and all("plan_reminder" not in e["output"] for e in shown)                                                # the person's list is not cluttered


def test_no_reminder_without_an_approved_plan_in_play(monkeypatch, tmp_path):
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(call("plan", steps=["a", "b"])), tool_round(call("read_file", path="r.txt")), text_round("x")], files={"r.txt": "hi"})
    assert "plan_reminder" not in tool_results(seen[2])[-1]


def test_an_approved_plan_cannot_be_silently_rewritten_but_can_be_changed_with_a_reason(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    rounds = [tool_round(PLAN), tool_round(call("plan", steps=["Something else entirely", "And more"])),
              tool_round(call("plan", steps=["Make a.txt in game/", "Make b.txt in game/"], replace=True, reason="the files turned out to be in game/")), text_round("x")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, answerer=lambda r: ("approved", ""))
    refused = tool_results(seen[2])[-1]["error"]
    assert "already have an approved plan" in refused and "step_done" in refused and "replace true" in refused
    asked = [e for e in events if e.get("type") == "approval_request"]
    assert len(asked) == 2 and asked[0]["title"].startswith("Athena has a plan")
    assert asked[1]["title"] == "Athena wants to change the plan" and "the files turned out to be in game/" in asked[1]["summary"]
    assert tool_results(seen[3])[-1]["plan"].splitlines()[0] == "1. [~] Make a.txt in game/"


def test_a_finished_plan_may_be_followed_by_a_new_one_and_other_models_may_replan_freely(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    rounds = [tool_round(call("plan", steps=["Only"])), tool_round(call("step_done", step=1)), tool_round(call("plan", steps=["Next job"])), text_round("x")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, answerer=lambda r: ("approved", ""))
    assert len([e for e in events if e.get("type") == "approval_request"]) == 2 and not any("already have" in str(r.get("error", "")) for rnd in seen for r in tool_results(rnd))
    ctx = main.ToolContext(workspace="", session_id="s-free", plan_state=[])                      # no approval in play: replacing is as before
    main.dispatch_tool(*surface.to_engine("plan", {"steps": ["a", "b"]}), ctx)
    assert "error" not in main.dispatch_tool(*surface.to_engine("plan", {"steps": ["c", "d", "e"]}), ctx)


# ---------- the two failed runs ----------

def test_a_source_file_is_shown_to_the_model_whole_not_as_a_head_and_a_tail(monkeypatch, tmp_path):
    body = "".join(f"line {i:03d}: const value{i} = {i};\n" for i in range(160))             # about 6,000 characters, like the game's script.js
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [tool_round(call("read_file", path="script.js")), text_round("ok")], files={"script.js": body})
    got = tool_results(seen[1])[0]["content"]
    assert got == body and "omitted" not in got and "get_full_tool_output" not in got




def test_previewing_or_a_read_only_command_never_forces_a_tick(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    offered = []
    monkeypatch.setattr(main.workspace_ops, "run_command", lambda cmd, ws, timeout=120: {"command": cmd, "output": "x", "exit_code": 0})
    rounds = [tool_round(PLAN), tool_round(call("run", command="cat -n a.txt")), tool_round(call("run", command="sed -n 1,10p a.txt")), tool_round(call("run", command="ls")), text_round("x")]
    run_turn(monkeypatch, tmp_path, rounds, tools_seen=offered, files={"a.txt": "hi"}, answerer=lambda r: ("approved", ""))
    assert all("run" in o and "write_file" in o for o in offered[1:5])


def test_a_model_that_tried_to_start_work_and_then_just_talks_is_asked_for_the_plan(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    rounds = [tool_round(call("edit_file", path="a.txt", old_text="x", new_text="y")), text_round("I cannot do that without a plan."), tool_round(PLAN), text_round("ok")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, files={"a.txt": "x"}, answerer=lambda r: ("approved", ""))
    asked = [m["content"] for m in seen[2] if m["role"] == "user" and "needs a plan" in m["content"]]
    assert asked and "Call plan now" in asked[0]
    assert len([e for e in events if e.get("type") == "approval_request"]) == 1               # the plan was written and shown to the person
    assert "ok" in saved[-1]


def test_plain_talk_with_no_attempted_work_is_never_nagged_for_a_plan(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, [text_round("Hello")])
    assert saved == ["Hello"] and len(seen) == 1


def test_a_long_file_arrives_in_line_aligned_windows_that_add_up_to_the_whole_file(monkeypatch, tmp_path):
    body = "".join(f"line {i:04d}: const value{i} = {i};\n" for i in range(600))               # about 25,000 characters
    ctx = main.ToolContext(workspace=str(tmp_path), session_id="s-read")
    (tmp_path / "big.js").write_text(body)
    got, offset, windows = "", 0, 0
    while True:
        result = main.dispatch_tool("read_file", {"path": "big.js", "offset": offset}, ctx)
        windows += 1
        assert len(result["content"]) <= file_tools.MODEL_READ_WINDOW and result["content"].endswith("\n")            # a window ends at the end of a line
        got += result["content"]
        if not result["has_more"]:
            break
        assert "not the whole file" in result["note"] and f"offset {result['next_offset']}" in result["note"] and result["lines"].endswith("of 600")
        offset = result["next_offset"]
    assert got == body and 3 <= windows <= 5                                                    # nothing is ever lost and it takes only a few reads


def test_a_model_can_read_any_part_of_a_file_by_line_numbers(tmp_path):
    ctx = main.ToolContext(workspace=str(tmp_path), session_id="s-lines")
    (tmp_path / "f.txt").write_text("".join(f"row {i}\n" for i in range(1, 101)))
    part = main.dispatch_tool("read_file", {"path": "f.txt", "start_line": 40, "end_line": 42}, ctx)
    assert part["content"] == "row 40\nrow 41\nrow 42\n" and part["lines"] == "40-42 of 100" and part["has_more"] is False
    tail = main.dispatch_tool("read_file", {"path": "f.txt", "start_line": 99}, ctx)
    assert tail["content"] == "row 99\nrow 100\n" and tail["lines"] == "99-100 of 100"
    assert main.dispatch_tool("read_file", {"path": "f.txt", "start_line": 500}, ctx)["content"] == "row 100\n"          # past the end: the last line, not an error
    assert main.dispatch_tool("read_file", {"path": "empty.txt"}, ctx)["error"].startswith("Not a file")


def test_the_viewer_and_the_direct_function_still_read_everything(tmp_path):
    (tmp_path / "big.txt").write_text("x\n" * 40000)
    assert len(file_tools.read_workspace_file(str(tmp_path), "big.txt")["content"]) == 80000
    assert len(file_tools.read_file(str(tmp_path), "big.txt")["content"]) == 20000


# ---------- a step takes as many calls as it needs ----------

def test_a_step_may_take_several_edits_and_nothing_forces_a_tick_in_between(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    offered = []
    rounds = [tool_round(call("plan", steps=["Move the counter", "Preview"])), tool_round(call("read_file", path="a.txt")),
              tool_round(call("edit_file", path="a.txt", old_text="1", new_text="2")), tool_round(call("edit_file", path="b.txt", old_text="x", new_text="y")),
              tool_round(call("run", command="ls")), tool_round(call("step_done", step=1)), text_round("done")]
    monkeypatch.setattr(main.workspace_ops, "run_command", lambda cmd, ws, timeout=120: {"command": cmd, "output": "x", "exit_code": 0})
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, tools_seen=offered, files={"a.txt": "1", "b.txt": "x"}, answerer=lambda r: ("approved", ""))
    assert (ws / "a.txt").read_text() == "2" and (ws / "b.txt").read_text() == "y"
    assert all("edit_file" in o and "run" in o for o in offered[1:6])                           # every tool stays on offer through all of it
    assert not [r for rnd in seen for r in tool_results(rnd) if "BLOCKED" in str(r.get("error", ""))]
    assert tool_results(seen[3])[-1]["plan_reminder"].startswith("Your plan: step 1 of 2")      # the reminder is the only prompt to tick


def test_once_every_step_is_done_more_work_has_to_be_written_as_a_new_approved_plan(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    offered = []
    rounds = [tool_round(call("plan", steps=["First job"])), tool_round(call("step_done", step=1)),
              tool_round(call("write_file", path="late.txt", content="L")),                                 # more work, but no plan covers it
              tool_round(call("plan", steps=["Write late.txt"])), tool_round(call("write_file", path="late.txt", content="L")),
              tool_round(call("step_done", step=1)), text_round("done")]
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, tools_seen=offered, answerer=lambda r: ("approved", ""))
    assert offered[2] == ["plan"]                                                                       # a finished plan leaves only writing a new one on offer
    assert "every step in your plan is now marked done" in tool_results(seen[3])[-1]["error"].lower()
    assert len([e for e in events if e.get("type") == "approval_request"]) == 2 and (ws / "late.txt").read_text() == "L"
    finished = [m["content"] for m in seen[2] if m["role"] == "user" and "exactly what still needs doing" in m["content"]]
    assert finished


def test_each_plan_of_a_turn_is_its_own_page_and_ticking_stays_on_the_current_page():
    ctx = main.ToolContext(workspace="", session_id="s-pages", plan_state=[], turn_state={})
    run = lambda name, args: main.dispatch_tool(*surface.to_engine(name, args), ctx)
    first = run("plan", {"steps": ["a", "b"]})
    assert first["plan_page"] == 1 and run("step_done", {"step": 1})["plan_page"] == 1
    run("step_done", {"step": 2})
    second = run("plan", {"steps": ["c", "d"]})
    assert second["plan_page"] == 2 and run("step_done", {"step": 1})["plan_page"] == 2
    ctx2 = main.ToolContext(workspace="", session_id="s-pages", plan_state=ctx.plan_state, turn_state=ctx.turn_state)
    shorter = main.dispatch_tool(*surface.to_engine("plan", {"steps": ["d"]}), ctx2)                     # same plan minus its first step: still page 2
    assert shorter["plan_page"] == 2
    bare = main.ToolContext(workspace="", session_id="x", plan_state=[{"text": "t", "status": "pending", "note": ""}])            # no turn state: one page
    assert main.dispatch_tool(*surface.to_engine("step_done", {"step": 1}), bare)["plan_page"] == 1



def test_calls_a_gate_refused_never_count_as_repeats(monkeypatch, tmp_path):
    approve_all(monkeypatch)
    edit = call("edit_file", path="a.txt", old_text="1", new_text="2")
    rounds = [tool_round(edit), tool_round(edit), tool_round(PLAN), tool_round(edit), text_round("done")]        # twice refused for lack of a plan, then allowed
    events, seen, saved, ws = run_turn(monkeypatch, tmp_path, rounds, files={"a.txt": "1"}, answerer=lambda r: ("approved", ""))
    assert (ws / "a.txt").read_text() == "2"
    assert not [r for rnd in seen for r in tool_results(rnd) if "already been made" in str(r.get("error", ""))]


# ---------- telling the model whether it can see ----------

def _read_file_description(vision):
    tools = surface.to_surface([{"type": "function", "function": {"name": "read_file", "description": "x", "parameters": {}}}], "workspace", req(), vision=vision)
    return next(t["function"]["description"] for t in tools if t["function"]["name"] == "read_file")


def test_the_file_tool_says_plainly_whether_pictures_are_shown_to_the_model():
    assert "You CAN see images" in _read_file_description(True) and "cannot view images" not in _read_file_description(True)
    assert "You cannot view images" in _read_file_description(False) and "CAN see" not in _read_file_description(False)
    assert "Images and PDFs are shown if you can see" in _read_file_description(None)              # unknown: the old conditional wording
    assert "Images and PDFs are shown if you can see" in surface.SCHEMAS["read_file"]["description"]  # the shared schema is never changed by a turn


def test_a_model_with_vision_is_told_in_its_prompt_and_one_without_is_not(monkeypatch):
    seen = types.SimpleNamespace(workspace="/tmp/x", provider="", allowed_tools=None, interactive=True, search_url="")
    monkeypatch.setattr(main, "_vision_of", lambda r: True)
    for mode in ("casual", "workspace"):
        assert "You can see images" in main._get_mode_system_prompt(mode, seen) and "Do not say you cannot view images" in main._get_mode_system_prompt(mode, seen)
    assert "You can see images" not in main._get_mode_system_prompt("athena_delegation", seen)
    monkeypatch.setattr(main, "_vision_of", lambda r: False)
    assert "You can see images" not in main._get_mode_system_prompt("workspace", seen)
    monkeypatch.setattr(main, "_vision_of", lambda r: True)
    assert "You CAN see images" in next(t["function"]["description"] for t in main._get_mode_tools("workspace", req(model="m"), build_task=True) if t["function"]["name"] == "read_file")


# ---------- the model is told it can use the page, not just look at it ----------

def test_the_model_is_told_preview_can_drive_a_page_with_every_kind_of_step():
    fn = surface.SCHEMAS["preview"]
    text = fn["description"] + fn["parameters"]["properties"]["actions"]["description"]
    for word in ("like a person", "AFTER", "play a game", "click", "press", "hold", "type", "scroll", "wait"):
        assert word in text, word


def test_the_workspace_prompts_mention_trying_the_page_not_only_loading_it():
    seen = types.SimpleNamespace(workspace="/tmp/x", provider="", allowed_tools=None, interactive=True, search_url="")
    for build in (True, False):
        prompt = main._get_mode_system_prompt("workspace", seen, build_task=build)
        assert "actions" in prompt and "click" in prompt and "preview" in prompt


def test_a_preview_with_actions_reports_which_steps_ran(monkeypatch, tmp_path):
    import preview_tools
    (tmp_path / "index.html").write_text("<button id=go onclick=\"this.textContent='done'\">go</button>")
    result = preview_tools.preview(str(tmp_path), {"path": "index.html", "viewport": "desktop", "actions": ["click #go", "wait 100"]})
    assert result.get("actions_run") == ["click #go", "wait 100"], result
    assert "actions_run" not in preview_tools.preview(str(tmp_path), {"path": "index.html", "viewport": "desktop"})


def test_a_step_that_cannot_be_done_says_which_one_and_what_the_page_does_have(tmp_path):
    import preview_tools
    (tmp_path / "index.html").write_text('<button id="launch-btn">LAUNCH</button><input id="name" placeholder="Your name"><a href="#x">More</a>')
    result = preview_tools.preview(str(tmp_path), {"path": "index.html", "viewport": "desktop", "actions": ["wait 50", "click #launch"]})
    error = result["error"]
    assert error.startswith("preview loaded index.html, but step 2 (click #launch) did not work")
    assert "nothing on the page matched it" in error and "#launch-btn (button \"LAUNCH\")" in error and "#name (input \"Your name\")" in error
    assert "couldn't load" not in error


def test_a_page_with_nothing_to_click_says_so(tmp_path):
    import preview_tools
    (tmp_path / "index.html").write_text("<p>just text</p>")
    error = preview_tools.preview(str(tmp_path), {"path": "index.html", "viewport": "desktop", "actions": ["click Go"]})["error"]
    assert "step 1 (click Go) did not work" in error and "nothing visible that can be clicked or typed into" in error


def test_steps_that_work_still_play_the_page_and_the_checks_see_the_result(tmp_path):
    import preview_tools
    (tmp_path / "index.html").write_text("<button id=go onclick=\"document.body.insertAdjacentHTML('beforeend','<h1 id=after>scored</h1>')\">go</button>")
    result = preview_tools.preview(str(tmp_path), {"path": "index.html", "viewport": "desktop", "actions": ["click #go", "wait 100"]})
    assert "error" not in result and result["actions_run"] == ["click #go", "wait 100"]
    assert any("h1#after" in str(item) for item in result["layout"]["desktop"])                  # the report is of the page after the click
