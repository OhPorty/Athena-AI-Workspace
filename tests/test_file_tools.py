import os

import pytest

import file_tools


def test_resolve_normal_relative_path(workspace):
    resolved = file_tools.resolve_workspace_path(workspace, "sub/file.txt")
    assert resolved == os.path.realpath(os.path.join(workspace, "sub/file.txt"))


def test_resolve_workspace_root_itself(workspace):
    resolved = file_tools.resolve_workspace_path(workspace, ".")
    assert resolved == os.path.realpath(workspace)


def test_resolve_rejects_traversal_outside_workspace(workspace):
    with pytest.raises(ValueError):
        file_tools.resolve_workspace_path(workspace, "../../etc/passwd")


def test_resolve_rejects_absolute_path_escape(workspace):
    with pytest.raises(ValueError):
        file_tools.resolve_workspace_path(workspace, "/etc/passwd")


def test_write_then_read_file(workspace):
    write_result = file_tools.write_file(workspace, "hello.txt", "hello world")
    assert write_result["written"] is True

    read_result = file_tools.read_file(workspace, "hello.txt")
    assert read_result["content"] == "hello world"
    assert read_result["has_more"] is False


def test_read_file_pagination(workspace):
    content = "x" * 25000
    file_tools.write_file(workspace, "big.txt", content)

    first = file_tools.read_file(workspace, "big.txt")
    assert first["has_more"] is True
    assert first["next_offset"] == 20000
    assert len(first["content"]) == 20000

    second = file_tools.read_file(workspace, "big.txt", offset=first["next_offset"])
    assert second["has_more"] is False
    assert len(second["content"]) == 5000


def test_read_file_outside_workspace_rejected(workspace):
    result = file_tools.read_file(workspace, "../outside.txt")
    assert "error" in result


def test_edit_file_replaces_unique_text(workspace):
    file_tools.write_file(workspace, "code.py", "print('old')")
    result = file_tools.edit_file(workspace, "code.py", "old", "new")
    assert result["edited"] is True
    assert file_tools.read_file(workspace, "code.py")["content"] == "print('new')"


def test_edit_file_rejects_non_unique_text(workspace):
    file_tools.write_file(workspace, "code.py", "a = 1\na = 1\n")
    result = file_tools.edit_file(workspace, "code.py", "a = 1", "a = 2")
    assert "error" in result
    assert "unique" in result["error"]


def test_edit_file_rejects_missing_text(workspace):
    file_tools.write_file(workspace, "code.py", "a = 1\n")
    result = file_tools.edit_file(workspace, "code.py", "not present", "a = 2")
    assert "error" in result


def test_list_files(workspace):
    file_tools.write_file(workspace, "a.txt", "1")
    os.mkdir(os.path.join(workspace, "subdir"))
    result = file_tools.list_files(workspace, ".")
    names = {e["name"]: e["type"] for e in result["entries"]}
    assert names == {"a.txt": "file", "subdir": "dir"}


def test_replace_lines_exact_match(workspace):
    file_tools.write_file(workspace, "code.py", "line1\nline2\nline3\n")
    result = file_tools.replace_lines(workspace, "code.py", 2, 2, "replaced", "line2")
    assert result["edited"] is True
    assert file_tools.read_file(workspace, "code.py")["content"] == "line1\nreplaced\nline3\n"


def test_replace_lines_finds_shifted_content_nearby(workspace):
    file_tools.write_file(workspace, "code.py", "a\nb\nc\nd\ne\n")
    # Stale line numbers (claims line 1 is "c") but "c" is really at line 3,
    # within the nearby-search window -- should self-correct rather than
    # corrupt the file.
    result = file_tools.replace_lines(workspace, "code.py", 1, 1, "C", "c")
    assert result["edited"] is True
    assert "stale" in result["note"]
    assert file_tools.read_file(workspace, "code.py")["content"] == "a\nb\nC\nd\ne\n"


def test_replace_lines_fails_safely_when_ambiguous(workspace):
    # Line 1 is "a", not "dup" -- expected_content doesn't match at the
    # given range, forcing the nearby-search path, where "dup" appears
    # three times, which is ambiguous.
    file_tools.write_file(workspace, "code.py", "a\ndup\ndup\ndup\nb\n")
    result = file_tools.replace_lines(workspace, "code.py", 1, 1, "new", "dup")
    assert "error" in result
