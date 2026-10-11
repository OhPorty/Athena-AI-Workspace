import json
import os

import bash_tools
import main
import model_profile
import settings
import workspace_kind


class _Req:
    search_url = ""
    allowed_tools = None


def tool_names(tools):
    return {t["function"]["name"] for t in tools}


# ---------- progress-aware gate counters ----------

def test_successful_write_run_and_render_count_as_progress():
    for tool in ("write_file", "edit_file", "replace_lines", "bash_exec_start", "generate_pdf", "preview"):
        assert main._result_is_progress(tool, {"path": "x"}), tool
    assert main._result_is_progress("bash_exec", {"exit_code": 0, "output": ""})


def test_failures_and_exploration_earn_no_credit():
    assert not main._result_is_progress("bash_exec", {"exit_code": 1, "output": "boom"})
    assert not main._result_is_progress("write_file", {"error": "outside workspace"})
    assert not main._result_is_progress("write_file", {"error": "BLOCKED: no"})
    for tool in ("read_file", "list_files", "web_search", "web_fetch", "search_codebase", "bash_read_only", "write_plan"):
        assert not main._result_is_progress(tool, {"ok": True}), tool


def test_progress_resets_calls_without_plan_and_is_neutral_for_checkin_cadence():
    # The loop counts a call as +1 on both before it runs; crediting undoes that for progress.
    assert main._credit_progress("write_file", {"path": "a"}, 5, 4) == (0, 3)
    assert main._credit_progress("write_file", {"path": "a"}, 5, 0) == (0, 0)
    assert main._credit_progress("web_fetch", {"ok": 1}, 5, 4) == (5, 4)
    assert main._credit_progress("bash_exec", {"exit_code": 2}, 5, 4) == (5, 4)


def test_a_building_turn_never_trips_the_plan_gate_but_pure_exploration_still_does():
    # Simulate the loop's own bookkeeping: +1 per call, then credit on the result.
    def run(calls):
        without_progress, since_touch = 0, 0
        for tool, result in calls:
            without_progress += 1
            since_touch += 1
            without_progress, since_touch = main._credit_progress(tool, result, without_progress, since_touch)
        return without_progress, since_touch

    build = [("write_file", {"path": f"f{i}"}) for i in range(10)] + [("bash_exec", {"exit_code": 0})] * 5
    assert run(build) == (0, 0)
    explore = [("web_fetch", {"ok": 1})] * main._PLAN_REQUIRED_THRESHOLD
    assert run(explore)[0] >= main._PLAN_REQUIRED_THRESHOLD
    mixed = [("read_file", {"ok": 1}), ("read_file", {"ok": 1}), ("write_file", {"path": "a"}), ("read_file", {"ok": 1})]
    assert run(mixed)[0] == 1


# ---------- build vs maintain ----------

def test_search_first_gate_is_skipped_for_build_tasks_only():
    blocked = main._check_tool_gate("workspace", "read_file", set(), 0, 0)
    assert blocked and "search_codebase first" in blocked["error"]
    assert main._check_tool_gate("workspace", "read_file", set(), 0, 0, build_task=True) is None
    assert main._check_tool_gate("workspace", "bash_read_only", set(), 0, 0, build_task=True) is None


def test_build_tool_set_is_lean_and_ungated():
    maintain = tool_names(main._get_mode_tools("workspace", _Req()))
    build = tool_names(main._get_mode_tools("workspace", _Req(), build_task=True))
    for name in ("search_codebase", "find_definition", "find_references", "type_info", "backup_file"):
        assert name in maintain and name not in build, name
    for name in ("write_file", "edit_file", "read_file", "list_files", "bash_exec", "bash_read_only", "write_plan"):
        assert name in build, name
    assert "read_file" not in maintain  # still hidden behind search_codebase in maintain mode


def test_build_task_has_no_effect_outside_workspace_mode():
    assert tool_names(main._get_mode_tools("casual", _Req(), build_task=True)) == tool_names(main._get_mode_tools("casual", _Req()))


def test_build_tool_set_is_materially_smaller():
    size = lambda tools: len(json.dumps(tools))
    assert size(main._get_mode_tools("workspace", _Req(), build_task=True)) < 0.85 * size(main._get_mode_tools("workspace", _Req(), called_tool_names={"search_codebase"}))


# ---------- profile thresholds ----------

def test_thresholds_default_to_the_module_constants():
    t = main._default_gate_thresholds()
    assert (t["plan_required"], t["plan_checkin"]) == (main._PLAN_REQUIRED_THRESHOLD, main._PLAN_CHECKIN_THRESHOLD)


def test_larger_tier_allows_more_calls_before_each_checkpoint():
    small, large = model_profile.GATE_THRESHOLDS["small"], model_profile.GATE_THRESHOLDS["large"]
    n = small["plan_required"]
    assert main._check_tool_gate("casual", "web_fetch", set(), 0, n, thresholds=small)
    assert main._check_tool_gate("casual", "web_fetch", set(), 0, n, thresholds=large) is None
    k = small["plan_checkin"]
    args = dict(pre_round_called={"write_plan"}, pre_round_exploration_count=0, pre_round_total_count=0,
                pre_round_plan_exists=True, pre_round_calls_since_plan_touch=k)
    assert main._check_tool_gate("casual", "web_fetch", **args, thresholds=small)
    assert main._check_tool_gate("casual", "web_fetch", **args, thresholds=large) is None


def test_tools_stay_available_longer_for_a_larger_tier():
    n = model_profile.GATE_THRESHOLDS["small"]["plan_required"]
    small = tool_names(main._get_mode_tools("casual", _Req(), total_tool_call_count=n, thresholds=model_profile.GATE_THRESHOLDS["small"]))
    large = tool_names(main._get_mode_tools("casual", _Req(), total_tool_call_count=n, thresholds=model_profile.GATE_THRESHOLDS["large"]))
    assert small == {"write_plan"} and len(large) > 1


# ---------- workspace kind ----------

def make_files(root, n, subdir=""):
    d = os.path.join(root, subdir)
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        open(os.path.join(d, f"f{i}.txt"), "w").write("x")


def test_empty_and_small_workspaces_are_build_tasks(tmp_path):
    assert workspace_kind.is_build_task(str(tmp_path))
    make_files(str(tmp_path), 12)
    assert workspace_kind.is_build_task(str(tmp_path))


def test_large_codebase_is_maintain_and_dependency_dirs_dont_count(tmp_path):
    make_files(str(tmp_path), workspace_kind.BUILD_FILE_LIMIT + 5)
    assert not workspace_kind.is_build_task(str(tmp_path))
    small = tmp_path / "proj"
    make_files(str(small), 10)
    make_files(str(small), 5000, "node_modules/pkg")
    make_files(str(small), 500, ".git/objects")
    assert workspace_kind.is_build_task(str(small))


def test_workspace_mode_setting_forces_either_way_and_missing_workspace_is_maintain(tmp_path):
    make_files(str(tmp_path), workspace_kind.BUILD_FILE_LIMIT + 5)
    settings.save_settings({"workspace_mode": "build"})
    assert workspace_kind.is_build_task(str(tmp_path))
    settings.save_settings({"workspace_mode": "maintain"})
    assert not workspace_kind.is_build_task(str(tmp_path / "x"))
    small = tmp_path / "small"
    small.mkdir()
    assert not workspace_kind.is_build_task(str(small))
    assert not workspace_kind.is_build_task("")


# ---------- bash_exec fixes ----------

def test_inline_code_for_python_and_node_may_contain_operator_characters():
    assert bash_tools._validate_bash_exec_call("python3", ["-c", "x = 1; print(x > 0 and x < 2)"]) is None
    assert bash_tools._validate_bash_exec_call("node", ["-e", "const f = x => x * 2; console.log(f(2) && 1)"]) is None
    assert bash_tools._validate_bash_exec_call("node", ["-p", "[1,2].map(x => x | 1)"]) is None


def test_real_shell_operators_are_still_rejected():
    for command, args in [("python3", ["script.py", ">", "out.txt"]), ("npm", ["run", "a", "&&", "b"]),
                          ("node", ["a.js", "|", "wc"]), ("python3", ["-m", "pip", ";", "ls"]),
                          ("python3", ["-c", "print(1)", "; rm -rf x"])]:
        assert "shell operator" in bash_tools._validate_bash_exec_call(command, args)["error"], (command, args)


def make_ctx(workspace):
    return main.ToolContext(session_id="s", workspace=workspace, search_url="", bot=None, session_key=None,
                            req=None, plan_state=None, passthrough_session_id=None, require_athena_bots_session=False)


def test_read_only_commands_sent_to_bash_exec_run_via_the_read_only_tool(tmp_path):
    (tmp_path / "a.txt").write_text("needle\n")
    result = main.dispatch_tool("bash_exec", {"command": "grep", "args": ["needle", "a.txt"]}, make_ctx(str(tmp_path)))
    assert "needle" in result["output"] and "bash_read_only" in result["note"]
    assert "error" not in result


def test_write_commands_are_not_rerouted(tmp_path, monkeypatch):
    called = {}
    monkeypatch.setattr(bash_tools, "execute_write_bash", lambda c, a, w: called.setdefault("args", (c, a, w)) and {"exit_code": 0})
    main.dispatch_tool("bash_exec", {"command": "npm", "args": ["install"]}, make_ctx(str(tmp_path)))
    assert called["args"] == ("npm", ["install"], str(tmp_path))


# ---------- whole command line passed as `command` ----------

def test_full_command_lines_and_json_arrays_are_split_when_args_is_empty():
    n = bash_tools.normalize_command_line
    assert n("ls -la", []) == ("ls", ["-la"])
    assert n('find . -name "*.html" -o -name "*.css"', []) == ("find", [".", "-name", "*.html", "-o", "-name", "*.css"])
    assert n('["npm", "install", "--save-dev", "foo"]', []) == ("npm", ["install", "--save-dev", "foo"])
    assert n("grep 'two words' file.txt", []) == ("grep", ["two words", "file.txt"])


def test_normalization_leaves_correct_or_unparseable_calls_alone():
    n = bash_tools.normalize_command_line
    assert n("npm", ["install"]) == ("npm", ["install"])
    assert n("ls -la", ["src"]) == ("ls", ["-la", "src"])       # a program name with a space can never run as written, so its flags join the args
    assert n("ls", []) == ("ls", [])
    assert n("echo 'unclosed", []) == ("echo 'unclosed", [])     # shlex can't parse it: unchanged
    assert n("[not json", []) == ("[not json", [])
    assert n(None, []) == (None, [])


def test_split_command_lines_still_go_through_the_normal_safety_checks(tmp_path):
    ctx = make_ctx(str(tmp_path))
    (tmp_path / "a.txt").write_text("hello\n")
    ok = main.dispatch_tool("bash_read_only", {"command": "grep -n hello a.txt"}, ctx)
    assert "hello" in ok["output"]
    piped = main.dispatch_tool("bash_exec", {"command": "python3 a.py | wc"}, ctx)
    assert "shell operator" in piped["error"]
    blocked = main.dispatch_tool("bash_exec", {"command": "curl http://example.com"}, ctx)
    assert "not allowed" in blocked["error"]


def test_profile_tier_never_loosens_the_delegation_sessions_gates():
    large = {"thresholds": model_profile.GATE_THRESHOLDS["large"]}
    assert main._gate_thresholds_for("athena_delegation", large) == main._default_gate_thresholds()
    assert main._gate_thresholds_for("workspace", large) == model_profile.GATE_THRESHOLDS["large"]
    assert main._gate_thresholds_for("casual", large) == model_profile.GATE_THRESHOLDS["large"]


# ---------- read-only pipelines in natural forms ----------

def test_read_only_pipelines_are_accepted_in_every_natural_form():
    n = bash_tools.normalize_read_only_call
    stage = lambda c, *a: {"command": c, "args": list(a)}
    assert n("grep -n x f.txt | head -20", [], None) == ("grep", ["-n", "x", "f.txt"], [stage("head", "-20")])
    assert n("grep", ["-n", "x", "f.txt"], "head -20") == ("grep", ["-n", "x", "f.txt"], [stage("head", "-20")])
    assert n("cat", ["f.txt"], ["sort", "uniq -c"]) == ("cat", ["f.txt"], [stage("sort"), stage("uniq", "-c")])
    assert n("ls -la", [], None) == ("ls", ["-la"], None)
    assert n("grep -E 'a|b' f.txt", [], None) == ("grep", ["-E", "a|b", "f.txt"], None)   # quoted | is not a pipe
    proper = [stage("wc", "-l")]
    assert n("ls", [], proper) == ("ls", [], proper)


def test_pipelines_run_for_real_and_unsafe_ones_are_still_refused(tmp_path):
    (tmp_path / "f.txt").write_text("b\na\nb\n")
    ctx = make_ctx(str(tmp_path))
    ok = main.dispatch_tool("bash_read_only", {"command": "sort f.txt | uniq -c"}, ctx)
    assert "2 b" in ok["output"] and "1 a" in ok["output"]
    bad = main.dispatch_tool("bash_read_only", {"command": "cat f.txt | rm -rf x"}, ctx)
    assert "error" in bad
    assert n_unparseable("echo 'unclosed | x")


def n_unparseable(text):
    return bash_tools.normalize_read_only_call(text, [], None) == (text, [], None)
