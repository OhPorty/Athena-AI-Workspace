import os
import stat

import pytest

import backup_tools
import file_tools


@pytest.fixture(autouse=True)
def isolated_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_tools, "_BACKUP_ROOT", str(tmp_path / "backups"))


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return str(d)


def put(ws, name, data):
    path = os.path.join(ws, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    return path


# ---------- reading for edit ----------

def test_the_whole_file_is_returned_not_the_20k_preview_chunk(ws):
    big = "line of code\n" * 5000          # 65,000 chars: read_file's preview would cut this at 20,000
    put(ws, "big.js", big)
    r = file_tools.read_for_edit(ws, "big.js")
    assert r["content"] == big and file_tools.read_file(ws, "big.js")["has_more"]
    assert r["bytes"] == len(big) and len(r["version"]) == 64


def test_crlf_files_are_edited_as_lf_and_remembered(ws):
    put(ws, "win.txt", "a\r\nb\r\n")
    r = file_tools.read_for_edit(ws, "win.txt")
    assert (r["content"], r["eol"]) == ("a\nb\n", "crlf")
    assert file_tools.read_for_edit(ws, put(ws, "unix.txt", "a\nb\n") and "unix.txt")["eol"] == "lf"


@pytest.mark.parametrize("name,data", [("pic.png", b"\x89PNG\r\n\x1a\n\x00\x00"), ("latin.txt", "caf\xe9".encode("latin-1"))])
def test_binary_and_non_utf8_files_are_refused_not_mangled(ws, name, data):
    put(ws, name, data)
    r = file_tools.read_for_edit(ws, name)
    assert r["uneditable"] and "content" not in r


def test_oversized_files_are_refused(ws, monkeypatch):
    monkeypatch.setattr(file_tools, "_EDIT_MAX_BYTES", 100)
    put(ws, "huge.txt", "x" * 101)
    assert file_tools.read_for_edit(ws, "huge.txt")["uneditable"]


def test_paths_outside_the_workspace_and_missing_files_are_refused(ws, tmp_path):
    (tmp_path / "secret.txt").write_text("nope")
    assert "outside the workspace" in file_tools.read_for_edit(ws, "../secret.txt")["error"]
    assert "outside the workspace" in file_tools.save_edit(ws, "../secret.txt", "x")["error"]
    assert "Not a file" in file_tools.read_for_edit(ws, "missing.py")["error"]
    assert "Not a file" in file_tools.save_edit(ws, "missing.py", "x")["error"]


# ---------- saving ----------

def test_save_round_trips_exactly_including_crlf_and_trailing_newline(ws):
    put(ws, "win.txt", "a\r\nb\r\n")
    opened = file_tools.read_for_edit(ws, "win.txt")
    saved = file_tools.save_edit(ws, "win.txt", opened["content"] + "c\n", opened["eol"], opened["version"])
    assert saved["saved"] and open(os.path.join(ws, "win.txt"), "rb").read() == b"a\r\nb\r\nc\r\n"
    assert file_tools.read_for_edit(ws, "win.txt")["version"] == saved["version"]


def test_saving_snapshots_the_version_being_replaced_and_it_can_really_be_restored(ws):
    path = put(ws, "app.js", "ORIGINAL\n")
    v = file_tools.read_for_edit(ws, "app.js")["version"]
    file_tools.save_edit(ws, "app.js", "MY EDIT\n", "lf", v)
    assert open(path).read() == "MY EDIT\n"
    entries = backup_tools._read_backup_manifest()
    assert len(entries) == 1 and entries[0]["path"] == os.path.realpath(path)
    restored = backup_tools.restore_file(path=os.path.realpath(path))
    assert "error" not in restored, restored
    assert open(path).read() == "ORIGINAL\n"           # the manual edit is undoable


def test_an_unchanged_save_writes_and_backs_up_nothing(ws):
    put(ws, "a.py", "x = 1\n")
    r = file_tools.read_for_edit(ws, "a.py")
    out = file_tools.save_edit(ws, "a.py", "x = 1\n", "lf", r["version"])
    assert out["saved"] and out["unchanged"] and backup_tools._read_backup_manifest() == []


def test_a_file_changed_on_disk_since_opening_is_a_conflict_not_a_silent_overwrite(ws):
    path = put(ws, "a.py", "v1\n")
    opened = file_tools.read_for_edit(ws, "a.py")
    open(path, "w").write("changed by the model\n")                   # someone else edits meanwhile
    out = file_tools.save_edit(ws, "a.py", "my version\n", "lf", opened["version"])
    assert out["conflict"] and "changed on disk" in out["error"]
    assert open(path).read() == "changed by the model\n"             # untouched
    forced = file_tools.save_edit(ws, "a.py", "my version\n", "lf", None)   # the user chose to overwrite
    assert forced["saved"] and open(path).read() == "my version\n"
    assert len(backup_tools._read_backup_manifest()) == 1            # the model's version was kept


def test_save_is_atomic_leaves_no_temp_files_and_keeps_permissions(ws):
    path = put(ws, "run.sh", "echo 1\n")
    os.chmod(path, 0o755)
    v = file_tools.read_for_edit(ws, "run.sh")["version"]
    file_tools.save_edit(ws, "run.sh", "echo 2\n", "lf", v)
    assert os.listdir(ws) == ["run.sh"] and stat.S_IMODE(os.stat(path).st_mode) == 0o755


def test_unicode_survives_the_round_trip(ws):
    put(ws, "t.txt", "café → \U0001f36a\n")
    r = file_tools.read_for_edit(ws, "t.txt")
    file_tools.save_edit(ws, "t.txt", r["content"] + "ok\n", "lf", r["version"])
    assert open(os.path.join(ws, "t.txt"), encoding="utf-8").read() == "café → \U0001f36a\nok\n"


def test_http_routes_wrap_the_same_logic(ws):
    put(ws, "a.py", "v1\n")
    got = file_tools.get_workspace_file(ws, "a.py")
    out = file_tools.put_workspace_file(file_tools.WorkspaceFileIn(workspace=ws, path="a.py", content="v2\n", eol=got["eol"], base_version=got["version"]))
    assert out["saved"] and open(os.path.join(ws, "a.py")).read() == "v2\n"
    assert file_tools.get_workspace_file("", "a.py")["error"] == "No workspace set."
    assert file_tools.put_workspace_file(file_tools.WorkspaceFileIn(workspace="", path="a.py", content="x"))["error"] == "No workspace set."
