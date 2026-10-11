import os
import random
import re
import types

import pytest

import main
import preview_tools
import scaffold_tools as st

KINDS = ["audio", "canvas", "game", "page", "site"]
ALL_THEMES = [(kind, theme) for kind, meta in sorted(st._kinds().items()) for theme in sorted(meta.get("themes", {}))]
SITE_THEMES = [t for k, t in ALL_THEMES if k == "site"]
BARE_KINDS = ["page", "canvas"]


def test_every_scaffold_dir_is_registered_with_metadata():
    assert sorted(st._kinds()) == KINDS
    for kind, meta in st._kinds().items():
        assert os.path.isfile(os.path.join(st._ROOT, kind, meta["entry"])), kind
        assert meta["description"] and meta["notes"]


def test_schema_lists_the_starters_and_stays_small():
    schema = st.SCAFFOLD_TOOL_SCHEMAS[0]["function"]
    assert schema["parameters"]["properties"]["kind"]["enum"] == KINDS
    assert schema["parameters"]["required"] == ["kind"]
    import json
    assert len(json.dumps(st.SCAFFOLD_TOOL_SCHEMAS)) // 4 < 450


def test_scaffold_copies_files_and_reports_where_to_start(tmp_path):
    r = st.scaffold(str(tmp_path), {"kind": "site"})
    assert r["created"] == ["index.html", "script.js", "styles.css"] and r["open"] == "index.html"
    assert "scaffold.json" not in os.listdir(tmp_path) and "token" in r["notes"].lower()
    assert r["theme"] in st._kinds()["site"]["themes"] and r["theme_description"]
    assert (tmp_path / "styles.css").read_text().startswith("/* THEME:START */")


def test_scaffold_into_a_subfolder(tmp_path):
    r = st.scaffold(str(tmp_path), {"kind": "game", "path": "games/star"})
    assert r["open"] == "games/star/index.html" and "games/star/game.js" in r["created"]
    assert (tmp_path / "games" / "star" / "game.js").is_file()


def test_scaffold_refuses_to_overwrite_and_changes_nothing(tmp_path):
    (tmp_path / "index.html").write_text("mine")
    r = st.scaffold(str(tmp_path), {"kind": "site"})
    assert "Refusing to overwrite" in r["error"] and "index.html" in r["error"]
    assert (tmp_path / "index.html").read_text() == "mine" and not (tmp_path / "styles.css").exists()


def test_scaffold_rejects_bad_input(tmp_path):
    assert "kind must be one of" in st.scaffold(str(tmp_path), {"kind": "spaceship"})["error"]
    assert "outside the workspace" in st.scaffold(str(tmp_path), {"kind": "site", "path": "../../etc"})["error"]
    assert "workspace" in st.scaffold("", {"kind": "site"})["error"]


@pytest.mark.parametrize("kind,theme", ALL_THEMES)
def test_every_theme_of_every_starter_passes_the_preview_checks_on_desktop_and_mobile(tmp_path, kind, theme):
    r = st.scaffold(str(tmp_path), {"kind": kind, "theme": theme})
    assert r["theme"] == theme
    for f in r["created"]:
        assert "{{" not in (tmp_path / f).read_text(), f"unfilled placeholder in {f}"
    p = preview_tools.preview(str(tmp_path), {"path": r["open"], "wait_ms": 300})
    assert "error" not in p, p
    assert [i for i in p["issues"] if i["type"] != "starter-text"] == [], p["issues"]


def test_themes_produce_genuinely_different_sites(tmp_path):
    shots, accents = set(), set()
    for i, theme in enumerate(SITE_THEMES):
        ws = tmp_path / theme
        ws.mkdir()
        st.scaffold(str(ws), {"kind": "site", "theme": theme})
        accents.add(re.search(r"--accent: ([^;]+);", (ws / "styles.css").read_text()).group(1))
        p = preview_tools.preview(str(ws), {"path": "index.html", "viewport": "desktop", "wait_ms": 200})
        shots.add(open(ws / p["screenshots"]["desktop"], "rb").read())
    assert len(accents) == len(SITE_THEMES) and len(shots) == len(SITE_THEMES)


def test_omitting_the_theme_gives_the_neutral_look_and_unknown_themes_are_refused(tmp_path):
    for kind in ("site", "game"):
        ws = tmp_path / kind
        ws.mkdir()
        assert st.scaffold(str(ws), {"kind": kind})["theme"] == "plain"   # never a preset the model didn't ask for
    err = st.scaffold(str(tmp_path / "x"), {"kind": "site", "theme": "neon-dinosaur"})["error"]
    assert "theme must be one of" in err and "editorial" in err
    assert "theme must be one of" in st.scaffold(str(tmp_path / "y"), {"kind": "game", "theme": "ember"})["error"]  # a site theme, wrong kind


@pytest.mark.parametrize("kind", BARE_KINDS)
def test_bare_shells_carry_no_design_and_pass_the_preview_checks(tmp_path, kind):
    r = st.scaffold(str(tmp_path), {"kind": kind})
    assert "theme" not in r and r["notes"]
    p = preview_tools.preview(str(tmp_path), {"path": r["open"], "wait_ms": 300})
    assert "error" not in p, p
    assert [i for i in p["issues"] if i["type"] != "starter-text"] == [], p["issues"]
    assert any(i["type"] == "starter-text" for i in p["issues"])   # still says "Untitled" until the model names it


def test_leftover_starter_wording_is_flagged_and_real_content_is_not(tmp_path):
    st.scaffold(str(tmp_path), {"kind": "site"})
    first = preview_tools.preview(str(tmp_path), {"path": "index.html", "viewport": "desktop", "wait_ms": 200})
    assert any(i["type"] == "starter-text" and "Studio Name" in i["detail"] for i in first["issues"])
    (tmp_path / "index.html").write_text('<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><title>Fern &amp; Flint</title><h1>Fern &amp; Flint</h1><p>Hand-thrown pots.</p>')
    second = preview_tools.preview(str(tmp_path), {"path": "index.html", "viewport": "desktop", "wait_ms": 200})
    assert not any(i["type"] == "starter-text" for i in second["issues"])


def test_canvas_shell_runs_and_reports_state(tmp_path):
    st.scaffold(str(tmp_path), {"kind": "canvas"})
    run = lambda actions: preview_tools.preview(str(tmp_path), {"path": "index.html", "viewport": "desktop", "actions": actions, "wait_ms": 200})
    status = lambda p: " ".join(o["text"] for o in p["layout"]["desktop"] if o["el"].startswith("p#status"))
    assert "Press Start to play" in status(run([]))
    playing = run(["click #start", "wait 400"])
    assert "Score 0" in status(playing) and not [i for i in playing["issues"] if i["type"] != "starter-text"]
    assert "Paused" in status(run(["click #start", "wait 200", "press p"]))


def test_game_theme_sets_the_title_wording_and_shapes(tmp_path):
    st.scaffold(str(tmp_path), {"kind": "game", "theme": "arcade"})
    html, js = (tmp_path / "index.html").read_text(), (tmp_path / "game.js").read_text()
    assert "<title>Coin Rush</title>" in html and "Catch coins, dodge spikes" in html
    assert '"collectible": "coin"' in js and '"hazard": "spike"' in js and "// THEME:START" in js and "// THEME:END" in js


def test_every_shape_the_themes_can_use_draws_without_errors(tmp_path):
    from playwright.sync_api import sync_playwright
    st.scaffold(str(tmp_path), {"kind": "game", "theme": "night-sky"})
    shapes = sorted({spec["js"][k] for meta in [st._kinds()["game"]] for spec in meta["themes"].values() for k in ("collectible", "hazard")})
    assert {"star", "coin", "gem", "drop", "spike", "rock"} >= set(shapes) and len(shapes) >= 5
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto((tmp_path / "index.html").as_uri())
        page.evaluate("""() => { for (const s of ['star','coin','gem','drop','spike','rock']) drawShape(s, 100, 100, 12, 0.5, '#fff', '#000'); }""")
        browser.close()
    assert errors == []


def test_game_starter_actually_plays(tmp_path):
    st.scaffold(str(tmp_path), {"kind": "game"})
    text = lambda p: " ".join(o["text"] for o in p["layout"]["desktop"] if o["el"].startswith("p#status"))  # the live status line
    run = lambda actions: preview_tools.preview(str(tmp_path), {"path": "index.html", "viewport": "desktop", "actions": actions, "wait_ms": 200})
    assert "Press Start to play" in text(run([]))
    playing = run(["click #start", "wait 600"])
    assert "lives 3" in text(playing) and not [i for i in playing["issues"] if i["type"] != "starter-text"]
    assert "Paused" in text(run(["click #start", "wait 300", "press p"]))
    assert "Score" in text(run(["press Space", "wait 400"]))  # keyboard start works too


# ---------- build-mode prompt and tool set ----------

def test_build_prompt_swaps_the_search_first_rule_for_build_guidance_and_default_is_untouched():
    default, build = main.CODING_HARNESS_SYSTEM_PROMPT, main.CODING_HARNESS_BUILD_PROMPT
    assert "search_codebase" in default and "scaffold" not in default and "preview" not in default
    assert "search_codebase" not in build and "scaffold" in build and "preview" in build
    assert build.count("##") == default.count("##") + 1  # same sections, plus build_loop


def test_system_prompt_selection_by_mode_and_build_flag():
    req = types.SimpleNamespace(workspace="")
    assert main._get_mode_system_prompt("workspace", req, build_task=True) == main.CODING_HARNESS_BUILD_PROMPT
    assert main._get_mode_system_prompt("workspace", req) == main.CODING_HARNESS_SYSTEM_PROMPT
    assert main._get_mode_system_prompt("athena_delegation", req, build_task=True).startswith(main.CODING_HARNESS_SYSTEM_PROMPT)
    assert main.CODING_HARNESS_BUILD_PROMPT not in main._get_mode_system_prompt("casual", req, build_task=True)


def test_scaffold_tool_is_offered_only_for_build_tasks_and_counts_as_progress():
    class Req:
        search_url = ""
        allowed_tools = None
    names = lambda t: {x["function"]["name"] for x in t}
    assert "scaffold" in names(main._get_mode_tools("workspace", Req(), build_task=True))
    assert "scaffold" not in names(main._get_mode_tools("workspace", Req()))
    assert "scaffold" not in names(main._get_mode_tools("casual", Req(), build_task=True))
    assert main._result_is_progress("scaffold", {"created": ["index.html"]})
    ctx = main.ToolContext(session_id="s", workspace="", search_url="", bot=None, session_key=None, req=None,
                           plan_state=None, passthrough_session_id=None, require_athena_bots_session=False)
    assert "needs an active workspace" in main.dispatch_tool("scaffold", {"kind": "site"}, ctx)["error"]
