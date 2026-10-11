"""When code the model wrote fails, the result should carry what is needed to fix it in place
(the failing code, the nearest text for a missed edit), so the model patches a line instead of
re-reading or rewriting a whole file."""
import bash_tools
import file_tools

PY_TRACE = '''Traceback (most recent call last):
  File "{ws}/gen.py", line 12, in <module>
    main()
  File "{ws}/gen.py", line 8, in make_kick
    return kick * env
           ~~~~~^~~~~
ValueError: operands could not be broadcast together with shapes (15434,) (9041,)
'''
SCRIPT = "\n".join(f"line {n}" for n in range(1, 40))


def test_a_python_crash_shows_the_code_around_the_failing_line(tmp_path):
    (tmp_path / "gen.py").write_text(SCRIPT)
    ctx = bash_tools.failing_code_context(PY_TRACE.format(ws=tmp_path), str(tmp_path))
    assert ctx["file"] == "gen.py" and ctx["line"] == 8               # the innermost frame, not main()
    assert "    8>| line 8" in ctx["excerpt"] and "    3 | line 3" in ctx["excerpt"] and "   13 | line 13" in ctx["excerpt"]
    assert "line 14" not in ctx["excerpt"] and "line 2\n" not in ctx["excerpt"]


def test_a_crash_inside_a_library_points_at_the_models_own_last_frame(tmp_path):
    (tmp_path / "gen.py").write_text(SCRIPT)
    trace = (f'Traceback (most recent call last):\n  File "{tmp_path}/gen.py", line 20, in <module>\n    x()\n'
             '  File "/usr/lib/python3/dist-packages/numpy/core/fromnumeric.py", line 3000, in sum\n    raise\nValueError\n')
    assert bash_tools.failing_code_context(trace, str(tmp_path))["line"] == 20


def test_node_stack_frames_work_and_library_or_outside_frames_are_ignored(tmp_path):
    (tmp_path / "app.js").write_text(SCRIPT)
    node = f"TypeError: x is not a function\n    at run ({tmp_path}/app.js:15:3)\n    at Module._compile (node:internal/modules/cjs/loader:1:1)\n"
    assert bash_tools.failing_code_context(node, str(tmp_path))["line"] == 15
    assert bash_tools.failing_code_context(f'File "{tmp_path}/node_modules/x/i.py", line 3', str(tmp_path)) is None
    assert bash_tools.failing_code_context('File "/etc/passwd", line 1', str(tmp_path)) is None
    assert bash_tools.failing_code_context("no frames here", str(tmp_path)) is None


def test_the_excerpt_stays_small_even_for_minified_code(tmp_path):
    (tmp_path / "m.js").write_text("x" * 50_000 + "\nsecond")
    ctx = bash_tools.failing_code_context(f"at f ({tmp_path}/m.js:1:9)", str(tmp_path))
    assert len(ctx["excerpt"]) < 400


def test_a_missed_edit_names_the_nearest_real_text(tmp_path):
    (tmp_path / "gen.py").write_text("def kick():\n    t = 1\n    env = np.exp(-t * 9)[:9041]\n    return kick * env\n\nprint(kick())\n")
    out = file_tools.edit_file(str(tmp_path), "gen.py", "    env = np.exp(-t*9)[:9041]\n    return kick*env", "    return kick")
    assert "closest text is at lines 3-4" in out["error"] and "np.exp(-t * 9)" in out["closest_match"]["text"]
    assert (tmp_path / "gen.py").read_text().count("env") == 2          # nothing was changed


def test_a_missed_edit_with_nothing_similar_stays_a_plain_error(tmp_path):
    (tmp_path / "a.py").write_text("alpha\nbeta\n")
    assert file_tools.edit_file(str(tmp_path), "a.py", "zzzzzzzzzzzz qqqqqqqqqq", "x") == {"error": "old_text not found in file"}


def test_flags_stuck_to_the_program_name_are_moved_into_args():
    assert bash_tools.normalize_command_line("cat -n", ["gen.py"]) == ("cat", ["-n", "gen.py"])
    assert bash_tools.normalize_command_line("python3 -u", ["gen.py"]) == ("python3", ["-u", "gen.py"])
    assert bash_tools.normalize_read_only_call("cat -n", ["gen.py"], None) == ("cat", ["-n", "gen.py"], None)
    assert bash_tools.normalize_command_line("npm", ["install"]) == ("npm", ["install"])          # already fine
    assert bash_tools.normalize_command_line("ls -la", []) == ("ls", ["-la"])                      # the existing case still works
