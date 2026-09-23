import pytest

import bash_tools


def test_allowed_command_runs(workspace):
    result = bash_tools.execute_readonly_bash("pwd", [], workspace)
    assert "error" not in result
    assert result["exit_code"] == 0


def test_disallowed_command_rejected(workspace):
    result = bash_tools.execute_readonly_bash("rm", ["-rf", "/"], workspace)
    assert "error" in result
    assert "not allowed" in result["error"]


def test_malformed_command_full_line_rejected(workspace):
    result = bash_tools.execute_readonly_bash("ls -la", [], workspace)
    assert "error" in result


def test_malformed_command_json_array_rejected(workspace):
    result = bash_tools.execute_readonly_bash('["ls"]', [], workspace)
    assert "error" in result


def test_non_string_arg_rejected(workspace):
    result = bash_tools.execute_readonly_bash("ls", [123], workspace)
    assert "error" in result
    assert "strings" in result["error"]


def test_sed_in_place_flag_rejected(workspace):
    result = bash_tools.execute_readonly_bash("sed", ["-i", "s/a/b/", "file.txt"], workspace)
    assert "error" in result
    assert "not allowed" in result["error"]


def test_find_delete_flag_rejected(workspace):
    result = bash_tools.execute_readonly_bash("find", [".", "-delete"], workspace)
    assert "error" in result


def test_sed_without_dangerous_flag_allowed(workspace):
    result = bash_tools.execute_readonly_bash("sed", ["-n", "1p", "/dev/null"], workspace)
    assert "error" not in result


@pytest.mark.parametrize("operator_arg", [
    "foo | bar",
    "foo > out.txt",
    "foo < in.txt",
    "foo && bar",
    "foo || bar",
    "foo; bar",
    "$(whoami)",
    "`whoami`",
])
def test_shell_operators_rejected_as_literal_text(workspace, operator_arg):
    result = bash_tools.execute_readonly_bash("grep", [operator_arg], workspace)
    assert "error" in result
    assert "shell operator" in result["error"]


def test_missing_command_rejected(workspace):
    result = bash_tools.execute_readonly_bash("", [], workspace)
    assert "error" in result


def test_command_not_found(workspace):
    result = bash_tools.execute_readonly_bash("date", ["--this-flag-does-not-exist-xyz"], workspace)
    # date exists and is allowed; a bad flag just produces a nonzero exit
    # code with stderr captured, not a Python-level error -- confirms
    # output capture and exit_code plumbing work end to end.
    assert "error" not in result
    assert result["exit_code"] != 0
