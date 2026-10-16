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


# ---------- download the whole workspace as a zip ----------

def test_zip_has_every_project_file_with_its_folders_and_leaves_out_rebuildable_and_private_things(workspace):
    import os, zipfile
    for rel, text in {"index.html": "<html>", "src/app.js": "x", "src/deep/more.css": "y", "README.md": "hi",
                      "node_modules/pkg/index.js": "big", "__pycache__/a.pyc": "b", ".athena/preview/shot.png": "p", ".git/config": "[core]"}.items():
        path = os.path.join(workspace, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write(text)
    zip_path, count, left_out = file_tools.zip_workspace(workspace)
    try:
        names = sorted(zipfile.ZipFile(zip_path).namelist())
    finally:
        os.remove(zip_path)
    assert names == [".git/config", "README.md", "index.html", "src/app.js", "src/deep/more.css"] and count == 5
    assert {"node_modules", "__pycache__", ".athena"} <= set(left_out)


def test_zip_never_follows_a_link_out_of_the_workspace_and_an_empty_workspace_still_zips(workspace, tmp_path):
    import os, zipfile
    outside = tmp_path / "secret.txt"
    outside.write_text("do not include")
    os.symlink(str(outside), os.path.join(workspace, "link.txt"))
    os.symlink(str(tmp_path), os.path.join(workspace, "linkdir"))
    zip_path, count, left_out = file_tools.zip_workspace(workspace)
    try:
        names = zipfile.ZipFile(zip_path).namelist()
    finally:
        os.remove(zip_path)
    assert names == [".empty"] and count == 0 and {"link.txt", "linkdir"} <= set(left_out)


def test_zip_leaves_out_athenas_private_files_when_the_workspace_is_her_own_folder(tmp_path, monkeypatch):
    import os, zipfile, safety
    home = tmp_path / "athena"
    (home / "logs").mkdir(parents=True)
    for name in ("athena_auth.json", "athena_settings.json", "athena.db", "main.py"):
        (home / name).write_text("x")
    (home / "logs" / "agent_trace.jsonl").write_text("x")
    monkeypatch.setattr(safety, "ATHENA_ROOT", str(home))
    zip_path, count, left_out = file_tools.zip_workspace(str(home))
    try:
        names = sorted(zipfile.ZipFile(zip_path).namelist())
    finally:
        os.remove(zip_path)
    assert names == ["main.py"] and {"athena_auth.json", "athena_settings.json", "athena.db", "logs"} <= set(left_out)



def test_zip_too_big_is_refused_not_built(workspace, monkeypatch):
    import os
    open(os.path.join(workspace, "a.bin"), "wb").write(b"x" * 100)
    monkeypatch.setattr(file_tools, "_ZIP_MAX_BYTES", 50)
    with pytest.raises(ValueError, match="over 1 GB"):
        file_tools.zip_workspace(workspace)
    import glob, tempfile
    assert not glob.glob(os.path.join(tempfile.gettempdir(), "athena_workspace_*.zip"))        # no half-built zip left behind
