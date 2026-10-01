import bots
import main


# --- main.py's four structural gates (_check_tool_gate) ---


def test_search_gate_blocks_bash_read_only_before_search_codebase():
    result = main._check_tool_gate("workspace", "bash_read_only", set(), 0, 0)
    assert result is not None
    assert "search_codebase" in result["error"]


def test_search_gate_allows_bash_read_only_after_search_codebase():
    result = main._check_tool_gate("workspace", "bash_read_only", {"search_codebase"}, 0, 0)
    assert result is None


def test_search_gate_does_not_apply_in_casual_mode():
    result = main._check_tool_gate("casual", "bash_read_only", set(), 0, 0)
    assert result is None


def test_search_gate_does_not_apply_to_ungated_tools():
    result = main._check_tool_gate("workspace", "list_files", set(), 0, 0)
    assert result is None


def test_exploration_cutoff_blocks_after_threshold():
    result = main._check_tool_gate(
        "athena_delegation", "read_file", {"search_codebase"}, main._SELF_INVESTIGATION_TOOL_THRESHOLD, 0
    )
    assert result is not None
    assert "self-investigation" in result["error"]


def test_exploration_cutoff_allows_below_threshold():
    result = main._check_tool_gate(
        "athena_delegation", "read_file", {"search_codebase"}, main._SELF_INVESTIGATION_TOOL_THRESHOLD - 1, 0
    )
    assert result is None


def test_exploration_cutoff_lifts_once_delegated():
    result = main._check_tool_gate(
        "athena_delegation", "read_file", {"search_codebase", "run_delegation_step"},
        main._SELF_INVESTIGATION_TOOL_THRESHOLD, 0,
    )
    assert result is None


def test_exploration_cutoff_only_applies_in_delegation_mode():
    result = main._check_tool_gate(
        "workspace", "read_file", {"search_codebase"}, main._SELF_INVESTIGATION_TOOL_THRESHOLD, 0
    )
    assert result is None


class _FakeReqForModeTools:
    def __init__(self, search_url="", allowed_tools=None):
        self.search_url = search_url
        self.allowed_tools = allowed_tools


def test_plan_required_threshold_narrows_schema_to_write_plan_only():
    # The actual doom-loop fix: once the threshold trips, write_plan isn't
    # just recommended via an error message -- every other tool is genuinely
    # absent from the schema, so a model can't keep re-selecting one.
    req = _FakeReqForModeTools()
    tools = main._get_mode_tools("casual", req, total_tool_call_count=main._PLAN_REQUIRED_THRESHOLD)
    names = {t["function"]["name"] for t in tools}
    assert names == {"write_plan"}


def test_plan_required_threshold_full_list_below_threshold():
    req = _FakeReqForModeTools()
    tools = main._get_mode_tools("casual", req, total_tool_call_count=main._PLAN_REQUIRED_THRESHOLD - 1)
    names = {t["function"]["name"] for t in tools}
    assert "write_plan" in names
    assert len(names) > 1


def test_plan_required_threshold_schema_restored_once_written():
    req = _FakeReqForModeTools()
    tools = main._get_mode_tools(
        "casual", req, called_tool_names={"write_plan"}, total_tool_call_count=main._PLAN_REQUIRED_THRESHOLD + 5
    )
    names = {t["function"]["name"] for t in tools}
    assert len(names) > 1


def test_plan_required_threshold_survives_allowed_tools_restriction():
    # Regression: task_scheduler.py (and any other caller restricted via
    # allowed_tools) never lists "write_plan" as a selectable capability --
    # it's mode-independent governance infrastructure, not something a
    # restricted caller opts into. Before this fix, once a task crossed
    # the plan-required threshold, the gate above narrowed the schema to
    # write_plan-only and then the allowed_tools filter stripped it right
    # back out, leaving an empty tools list while the system prompt still
    # demanded a write_plan call -- the model had nothing structured to
    # call and fell back to emitting a fake <tool_call> text block instead.
    req = _FakeReqForModeTools(allowed_tools=["read_file", "web_fetch", "web_search"])
    tools = main._get_mode_tools("casual", req, total_tool_call_count=main._PLAN_REQUIRED_THRESHOLD)
    names = {t["function"]["name"] for t in tools}
    assert names == {"write_plan"}


def test_plan_checkin_threshold_survives_allowed_tools_restriction():
    req = _FakeReqForModeTools(allowed_tools=["read_file", "web_fetch", "web_search"])
    tools = main._get_mode_tools(
        "casual", req, plan_exists=True, calls_since_plan_touch=main._PLAN_CHECKIN_THRESHOLD,
    )
    names = {t["function"]["name"] for t in tools}
    assert names == {"write_plan", "update_plan_step"}


def test_workspace_search_codebase_cutoff_removes_tool_from_schema():
    req = _FakeReqForModeTools()
    tools = main._get_mode_tools(
        "workspace", req, called_tool_names={"search_codebase"},
        search_codebase_call_count=main._WORKSPACE_SEARCH_CODEBASE_THRESHOLD,
    )
    names = {t["function"]["name"] for t in tools}
    assert "search_codebase" not in names
    assert "read_file" in names
    assert "bash_read_only" in names


def test_workspace_search_codebase_present_below_threshold_in_schema():
    req = _FakeReqForModeTools()
    tools = main._get_mode_tools(
        "workspace", req, called_tool_names={"search_codebase"},
        search_codebase_call_count=main._WORKSPACE_SEARCH_CODEBASE_THRESHOLD - 1,
    )
    names = {t["function"]["name"] for t in tools}
    assert "search_codebase" in names


def test_generate_closure_binds_tools_as_nonlocal_not_local_shadow():
    # Regression guard: chat_stream's generate() reassigns `tools` each
    # round from live gate state. Without `nonlocal tools`, that assignment
    # would make Python treat `tools` as local to generate() for its ENTIRE
    # body, breaking the very first read (round 0) with an UnboundLocalError
    # -- this confirms the closure is wired correctly instead.
    code = main.chat_stream.__code__
    generate_code = next(c for c in code.co_consts if hasattr(c, "co_name") and c.co_name == "generate")
    assert "tools" in generate_code.co_freevars


def test_search_codebase_cutoff_blocks_in_workspace_mode():
    result = main._check_tool_gate(
        "workspace", "search_codebase", set(), 0, 0,
        pre_round_search_codebase_count=main._WORKSPACE_SEARCH_CODEBASE_THRESHOLD,
    )
    assert result is not None
    assert "search_codebase" in result["error"]


def test_search_codebase_cutoff_allows_below_threshold_in_workspace_mode():
    result = main._check_tool_gate(
        "workspace", "search_codebase", set(), 0, 0,
        pre_round_search_codebase_count=main._WORKSPACE_SEARCH_CODEBASE_THRESHOLD - 1,
    )
    assert result is None


def test_search_codebase_cutoff_does_not_block_other_exploration_tools():
    # Proves the surgical scope: read_file/bash_read_only stay available even once
    # search_codebase itself is cut off, unlike delegation mode's blanket lockout.
    result = main._check_tool_gate(
        "workspace", "read_file", {"search_codebase"}, 0, 0,
        pre_round_search_codebase_count=main._WORKSPACE_SEARCH_CODEBASE_THRESHOLD,
    )
    assert result is None


def test_search_codebase_cutoff_does_not_apply_outside_workspace_mode():
    for mode in ("casual", "athena_delegation"):
        result = main._check_tool_gate(
            mode, "search_codebase", {"search_codebase"}, 0, 0,
            pre_round_search_codebase_count=main._WORKSPACE_SEARCH_CODEBASE_THRESHOLD,
        )
        assert result is None


def test_delegation_commitment_blocks_run_delegation_step_after_plan():
    result = main._check_tool_gate("athena_delegation", "run_delegation_step", {"plan_delegation"}, 0, 0)
    assert result is not None
    assert "plan_delegation" in result["error"]


def test_delegation_commitment_allows_run_delegation_step_before_plan():
    result = main._check_tool_gate("athena_delegation", "run_delegation_step", set(), 0, 0)
    assert result is None


def test_plan_required_threshold_blocks_everything_except_write_plan():
    result = main._check_tool_gate("casual", "web_search", set(), 0, main._PLAN_REQUIRED_THRESHOLD)
    assert result is not None
    assert "write_plan" in result["error"]


def test_plan_required_threshold_allows_write_plan_itself():
    result = main._check_tool_gate("casual", "write_plan", set(), 0, main._PLAN_REQUIRED_THRESHOLD)
    assert result is None


def test_plan_required_threshold_lifts_once_written():
    result = main._check_tool_gate("casual", "web_search", {"write_plan"}, 0, main._PLAN_REQUIRED_THRESHOLD)
    assert result is None


def test_plan_required_threshold_applies_below_the_call_count_too():
    # total_tool_call_count only reaches the threshold via the round
    # loop's own counting -- below it, nothing is blocked by this gate.
    result = main._check_tool_gate("casual", "web_search", set(), 0, main._PLAN_REQUIRED_THRESHOLD - 1)
    assert result is None


# --- Plan check-in gate: a plan written once isn't enough, it has to be revisited ---


def test_plan_is_complete_true_when_every_step_done_or_failed():
    assert main._plan_is_complete([{"status": "done"}, {"status": "failed"}])


def test_plan_is_complete_false_when_any_step_pending_or_in_progress():
    assert not main._plan_is_complete([{"status": "done"}, {"status": "in_progress"}])
    assert not main._plan_is_complete([{"status": "pending"}])


def test_plan_is_complete_false_for_empty_plan():
    assert not main._plan_is_complete([])


def test_checkin_gate_blocks_other_tools_after_threshold_since_last_touch():
    result = main._check_tool_gate(
        "casual", "web_fetch", {"write_plan"}, 0, 10,
        pre_round_plan_exists=True, pre_round_calls_since_plan_touch=main._PLAN_CHECKIN_THRESHOLD,
    )
    assert result is not None
    assert "update_plan_step" in result["error"]


def test_checkin_gate_allows_update_plan_step_and_write_plan():
    for name in ("update_plan_step", "write_plan"):
        result = main._check_tool_gate(
            "casual", name, {"write_plan"}, 0, 10,
            pre_round_plan_exists=True, pre_round_calls_since_plan_touch=main._PLAN_CHECKIN_THRESHOLD,
        )
        assert result is None


def test_checkin_gate_allows_everything_below_threshold():
    result = main._check_tool_gate(
        "casual", "web_fetch", {"write_plan"}, 0, 10,
        pre_round_plan_exists=True, pre_round_calls_since_plan_touch=main._PLAN_CHECKIN_THRESHOLD - 1,
    )
    assert result is None


def test_checkin_gate_does_not_apply_without_a_plan():
    # write_plan already in pre_round_called so the unrelated
    # plan-required gate doesn't also fire and confound this check.
    result = main._check_tool_gate(
        "casual", "web_fetch", {"write_plan"}, 0, 10,
        pre_round_plan_exists=False, pre_round_calls_since_plan_touch=main._PLAN_CHECKIN_THRESHOLD + 5,
    )
    assert result is None


def test_plan_complete_gate_blocks_everything_except_write_plan():
    result = main._check_tool_gate("casual", "web_fetch", {"write_plan"}, 0, 10, pre_round_plan_complete=True)
    assert result is not None
    assert "write_plan" in result["error"]

    allowed = main._check_tool_gate("casual", "write_plan", {"write_plan"}, 0, 10, pre_round_plan_complete=True)
    assert allowed is None


def test_plan_complete_gate_blocks_update_plan_step_too():
    # Nothing left to update once every step is already done/failed --
    # write_plan (a fresh plan) or a final answer are the only ways forward.
    result = main._check_tool_gate("casual", "update_plan_step", {"write_plan"}, 0, 10, pre_round_plan_complete=True)
    assert result is not None


def test_get_mode_tools_narrows_to_checkin_tools_only():
    req = _FakeReqForModeTools()
    tools = main._get_mode_tools(
        "casual", req, called_tool_names={"write_plan"}, total_tool_call_count=10,
        plan_exists=True, calls_since_plan_touch=main._PLAN_CHECKIN_THRESHOLD,
    )
    names = {t["function"]["name"] for t in tools}
    assert names == {"write_plan", "update_plan_step"}


def test_get_mode_tools_full_list_below_checkin_threshold():
    req = _FakeReqForModeTools()
    tools = main._get_mode_tools(
        "casual", req, called_tool_names={"write_plan"}, total_tool_call_count=10,
        plan_exists=True, calls_since_plan_touch=main._PLAN_CHECKIN_THRESHOLD - 1,
    )
    names = {t["function"]["name"] for t in tools}
    assert len(names) > 2


def test_get_mode_tools_narrows_to_write_plan_only_when_plan_complete():
    req = _FakeReqForModeTools()
    tools = main._get_mode_tools(
        "casual", req, called_tool_names={"write_plan"}, total_tool_call_count=10, plan_complete=True,
    )
    names = {t["function"]["name"] for t in tools}
    assert names == {"write_plan"}


# --- bots.py's tool-type-group hard-stop (_check_bot_tool_gate) ---


def test_bot_gate_blocks_hard_stopped_group():
    result = bots._check_bot_tool_gate("explore", {"explore"})
    assert result is not None
    assert "explore" in result["error"]


def test_bot_gate_allows_non_hard_stopped_group():
    result = bots._check_bot_tool_gate("web", {"explore"})
    assert result is None


def test_bot_gate_allows_ungrouped_tools():
    # append_scratch_note, run_tool_program: never in a group, always survive.
    result = bots._check_bot_tool_gate(None, {"explore", "web", "social", "recall"})
    assert result is None


def test_bot_gate_allows_everything_when_nothing_hard_stopped():
    result = bots._check_bot_tool_gate("explore", set())
    assert result is None


# --- Stable per-turn tool schema (the actual cache-discipline fix) ---


def test_bot_tool_schemas_maximal_set_ignores_hard_stopped_groups():
    """_call_bot_endpoint now computes tool_schemas once with
    hard_stopped_groups=None -- confirm that really does produce the
    full set (i.e. passing an empty/None hard-stop set is equivalent to
    passing the real one before anything's been hard-stopped)."""
    allowed = ["bash_read_only", "web_search", "list_bots"]
    maximal = bots._bot_tool_schemas(allowed, None, ptc_enabled=False)
    also_maximal = bots._bot_tool_schemas(allowed, set(), ptc_enabled=False)
    narrowed = bots._bot_tool_schemas(allowed, {"explore"}, ptc_enabled=False)

    maximal_names = {s["function"]["name"] for s in maximal}
    narrowed_names = {s["function"]["name"] for s in narrowed}
    assert maximal_names == {s["function"]["name"] for s in also_maximal}
    assert "bash_read_only" in maximal_names  # explore-group tool present when nothing's hard-stopped
    assert "bash_read_only" not in narrowed_names  # confirms the underlying narrowing still works when asked for
