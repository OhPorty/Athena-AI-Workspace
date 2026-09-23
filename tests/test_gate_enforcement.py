import bots
import main


# --- main.py's four structural gates (_check_tool_gate) ---


def test_search_gate_blocks_bash_before_search_codebase():
    result = main._check_tool_gate("workspace", "bash", set(), 0, 0)
    assert result is not None
    assert "search_codebase" in result["error"]


def test_search_gate_allows_bash_after_search_codebase():
    result = main._check_tool_gate("workspace", "bash", {"search_codebase"}, 0, 0)
    assert result is None


def test_search_gate_does_not_apply_in_casual_mode():
    result = main._check_tool_gate("casual", "bash", set(), 0, 0)
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
    allowed = ["bash", "web_search", "list_bots"]
    maximal = bots._bot_tool_schemas(allowed, None, ptc_enabled=False)
    also_maximal = bots._bot_tool_schemas(allowed, set(), ptc_enabled=False)
    narrowed = bots._bot_tool_schemas(allowed, {"explore"}, ptc_enabled=False)

    maximal_names = {s["function"]["name"] for s in maximal}
    narrowed_names = {s["function"]["name"] for s in narrowed}
    assert maximal_names == {s["function"]["name"] for s in also_maximal}
    assert "bash" in maximal_names  # explore-group tool present when nothing's hard-stopped
    assert "bash" not in narrowed_names  # confirms the underlying narrowing still works when asked for
