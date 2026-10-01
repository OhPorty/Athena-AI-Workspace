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


def test_grep_expands_glob_against_matching_files(workspace, tmp_path):
    import pathlib
    (pathlib.Path(workspace) / "a.py").write_text("needle\n")
    (pathlib.Path(workspace) / "b.py").write_text("nothing here\n")
    (pathlib.Path(workspace) / "c.txt").write_text("needle\n")
    result = bash_tools.execute_readonly_bash("grep", ["-l", "needle", "*.py"], workspace)
    assert "error" not in result
    assert "a.py" in result["output"]
    assert "c.txt" not in result["output"]


def test_sed_expands_glob_against_matching_files(workspace):
    import pathlib
    (pathlib.Path(workspace) / "x.txt").write_text("one\ntwo\nthree\n")
    result = bash_tools.execute_readonly_bash("sed", ["-n", "2p", "*.txt"], workspace)
    assert "error" not in result
    assert result["output"].strip() == "two"


def test_unmatched_glob_left_literal_as_regex_pattern(workspace):
    # [Tt]est looks like a glob character class but matches no real file in
    # the workspace, so it must be passed through untouched as grep's search
    # pattern rather than silently vanishing or erroring.
    result = bash_tools.execute_readonly_bash("grep", ["-r", "[Tt]est"], workspace)
    assert "error" not in result
    assert result["exit_code"] == 1  # no matches, but ran fine


def test_oversized_output_truncated_with_explicit_notice(workspace):
    import pathlib
    big = pathlib.Path(workspace) / "big.txt"
    big.write_text("needle line\n" * 10000)
    result = bash_tools.execute_readonly_bash("grep", ["needle", "big.txt"], workspace)
    assert "error" not in result
    assert len(result["output"]) <= bash_tools._RAW_OUTPUT_CAP + 500
    assert "TRUNCATED" in result["output"]


def test_recursive_grep_excludes_git_and_binaries_by_default(workspace):
    import pathlib
    git_dir = pathlib.Path(workspace) / ".git"
    git_dir.mkdir()
    (git_dir / "hidden.txt").write_text("needle\n")
    (pathlib.Path(workspace) / "real.py").write_text("needle\n")
    (pathlib.Path(workspace) / "blob.bin").write_bytes(b"needle\x00\x01\x02")
    result = bash_tools.execute_readonly_bash("grep", ["-rn", "needle", "."], workspace)
    assert "error" not in result
    assert "real.py" in result["output"]
    assert ".git" not in result["output"]
    assert "blob.bin" not in result["output"]


def test_recursive_grep_respects_explicit_exclude_dir(workspace):
    import pathlib
    (pathlib.Path(workspace) / ".git").mkdir()
    (pathlib.Path(workspace) / ".git" / "hidden.txt").write_text("needle\n")
    result = bash_tools.execute_readonly_bash(
        "grep", ["-rn", "--exclude-dir=nothing", "needle", "."], workspace
    )
    assert "error" not in result
    # explicit filter given -> defaults are not layered on top, so .git IS searched
    assert ".git" in result["output"]


def test_find_name_pattern_not_glob_expanded(workspace):
    import pathlib
    (pathlib.Path(workspace) / "a.py").write_text("x")
    (pathlib.Path(workspace) / "b.py").write_text("x")
    result = bash_tools.execute_readonly_bash("find", [".", "-name", "*.py"], workspace)
    assert "error" not in result
    assert "a.py" in result["output"] and "b.py" in result["output"]


def test_pipe_to_grep_into_wc_counts_matches(workspace):
    import pathlib
    (pathlib.Path(workspace) / "a.txt").write_text("needle\nnothing\nneedle\n")
    result = bash_tools.execute_readonly_bash(
        "grep", ["needle", "a.txt"], workspace, [{"command": "wc", "args": ["-l"]}]
    )
    assert "error" not in result
    assert result["output"].strip() == "2"
    assert result["exit_code"] == 0
    assert [s["command"] for s in result["pipeline"]] == ["grep", "wc"]
    assert all(s["exit_code"] == 0 for s in result["pipeline"])


def test_pipe_to_multiple_stages_sort_and_uniq(workspace):
    import pathlib
    (pathlib.Path(workspace) / "a.txt").write_text("b\na\nb\na\nc\n")
    result = bash_tools.execute_readonly_bash(
        "cat", ["a.txt"], workspace,
        [{"command": "sort", "args": []}, {"command": "uniq", "args": ["-c"]}],
    )
    assert "error" not in result
    lines = [l.strip() for l in result["output"].strip().splitlines()]
    assert lines == ["2 a", "2 b", "1 c"]


def test_pipe_to_early_exit_downstream_does_not_hang(workspace):
    import pathlib
    (pathlib.Path(workspace) / "a.txt").write_text("line\n" * 5000)
    result = bash_tools.execute_readonly_bash(
        "cat", ["a.txt"], workspace, [{"command": "head", "args": ["-3"]}]
    )
    assert "error" not in result
    assert result["output"].strip().splitlines() == ["line", "line", "line"]


def test_pipe_to_disallowed_command_rejected(workspace):
    result = bash_tools.execute_readonly_bash(
        "grep", ["foo"], workspace, [{"command": "rm", "args": ["-rf", "/"]}]
    )
    assert "error" in result
    assert "pipe_to[0]" in result["error"]
    assert "not allowed" in result["error"]


def test_pipe_to_dangerous_flag_rejected(workspace):
    result = bash_tools.execute_readonly_bash(
        "cat", ["a.txt"], workspace, [{"command": "sed", "args": ["-i", "s/a/b/"]}]
    )
    assert "error" in result
    assert "pipe_to[0]" in result["error"]


def test_pipe_to_too_many_stages_rejected(workspace):
    result = bash_tools.execute_readonly_bash(
        "grep", ["foo"], workspace, [{"command": "sort", "args": []}] * 5
    )
    assert "error" in result
    assert "Too many piped stages" in result["error"]


def test_pipe_to_glob_and_default_excludes_still_apply_per_stage(workspace):
    import pathlib
    (pathlib.Path(workspace) / "a.py").write_text("needle\n")
    (pathlib.Path(workspace) / ".git").mkdir()
    (pathlib.Path(workspace) / ".git" / "hidden.txt").write_text("needle\n")
    result = bash_tools.execute_readonly_bash(
        "grep", ["-rl", "needle", "."], workspace, [{"command": "sort", "args": []}]
    )
    assert "error" not in result
    assert ".git" not in result["output"]
    assert "a.py" in result["output"]
