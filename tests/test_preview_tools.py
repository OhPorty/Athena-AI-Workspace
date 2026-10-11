import base64
import http.server
import os
import threading

import pytest

import preview_tools as pt

GOOD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Good page</title><style>
body{margin:0;font:16px/1.5 sans-serif;color:#222;background:#fff}
header{background:#123;color:#fff;padding:24px}
main{padding:24px;max-width:700px}
button{padding:12px 20px;font-size:16px}
img{max-width:100%}
</style></head><body><header><h1>Studio Lumen</h1></header>
<main><h2>About</h2><p>A small, well-behaved page.</p><button id="go">Go</button></main></body></html>"""

BAD = """<!doctype html><html><head><meta charset="utf-8"><title>Bad page</title><style>
body{margin:0;font:16px sans-serif;background:#fff}
.wide{width:1100px;height:40px;background:#eee}
.faint{color:#cccccc}
.tiny{width:10px;height:10px;display:block}
</style></head><body>
<div class="wide">too wide</div>
<p class="faint">Low contrast text on white</p>
<img src="missing.png"><img src="also-missing.png" alt="x">
<a class="tiny" href="#">x</a>
<script>console.error("boom from page"); fetch("/nope.json");</script>
</body></html>"""

APP = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>App</title></head>
<body style="font:18px sans-serif"><h1 id="out">idle</h1><input id="name" style="font-size:18px">
<button id="start" style="padding:12px 20px;font-size:18px" onclick="document.getElementById('out').textContent='started ' + document.getElementById('name').value">Start</button>
<script>document.addEventListener('keydown',e=>{document.getElementById('out').textContent='key '+e.key})</script></body></html>"""


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "good.html").write_text(GOOD)
    (tmp_path / "bad.html").write_text(BAD)
    (tmp_path / "app.html").write_text(APP)
    return str(tmp_path)


def kinds(result, viewport=None):
    return {(i["type"], i["viewport"]) if viewport is None else i["type"] for i in result["issues"] if viewport in (None, i["viewport"])}


# ---------- pure helpers ----------

def test_actions_parse_and_reject_garbage():
    assert pt.parse_actions(["click #go", "press ArrowRight", "type #name Sam Rivera", "wait 99999"]) == [
        ("click", "#go"), ("press", "ArrowRight"), ("type", "#name", "Sam Rivera"), ("wait", 5000)]
    assert pt.parse_actions(None) == []
    for bad in (["jump"], ["wait soon"], ["click"], [3], "click #go", ["press"] * 21):
        with pytest.raises(pt.ActionError):
            pt.parse_actions(bad)


@pytest.mark.parametrize("host,expected", [("127.0.0.1", False), ("10.0.0.5", False), ("192.168.1.131", False),
                                           ("169.254.169.254", False), ("::1", False), ("8.8.8.8", True), ("", False)])
def test_only_globally_routable_hosts_are_reachable(host, expected):
    assert pt._is_public_host(host) is expected


# ---------- argument validation ----------

def test_path_must_stay_inside_workspace_and_be_html(ws):
    assert "outside the workspace" in pt.preview(ws, {"path": "../../etc/passwd"})["error"]
    assert "No such file" in pt.preview(ws, {"path": "nope.html"})["error"]
    open(os.path.join(ws, "notes.txt"), "w").write("x")
    assert "isn't one" in pt.preview(ws, {"path": "notes.txt"})["error"]
    assert "workspace" in pt.preview("", {})["error"]
    assert "viewport" in pt.preview(ws, {"path": "good.html", "viewport": "tablet"})["error"]
    assert "can't read action" in pt.preview(ws, {"path": "good.html", "actions": ["dance"]})["error"]


# ---------- real rendering ----------

def test_clean_page_passes_and_reports_layout_and_screenshots(ws):
    r = pt.preview(ws, {"path": "good.html"})
    assert "error" not in r, r
    assert r["issues"] == [] and "No problems found" in r["summary"]
    assert r["title"] == "Good page" and set(r["viewports"]) == {"desktop", "mobile"}
    outline = r["layout"]["desktop"]
    assert any(o["el"] == "h1" and o["text"] == "Studio Lumen" for o in outline)
    assert all(set(o) == {"el", "text", "x", "y", "w", "h"} for o in outline)
    for name in ("desktop", "mobile"):
        shot = os.path.join(ws, r["screenshots"][name])
        assert open(shot, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    assert len(r["_images"]) == 2 and base64.b64decode(r["_images"][0])[:2] == b"\xff\xd8"


def test_broken_page_is_caught_with_actionable_detail(ws):
    r = pt.preview(ws, {"path": "bad.html"})
    found = kinds(r)
    assert ("console-error", "desktop") in found and ("console-error", "mobile") in found
    assert ("broken-image", "desktop") in found
    assert ("low-contrast", "desktop") in found
    assert ("missing-alt", "desktop") in found
    assert ("small-tap-target", "mobile") in found
    assert ("no-viewport-meta", "mobile") in found
    assert ("request-failed", "desktop") in found
    assert not [i for i in r["issues"] if i["type"] == "horizontal-overflow" and i["viewport"] == "mobile"]  # no meta tag: zoomed out, not overflowing
    assert "boom from page" in " ".join(i["detail"] for i in r["issues"] if i["type"] == "console-error")
    contrast = next(i["detail"] for i in r["issues"] if i["type"] == "low-contrast")
    assert "#cccccc" in contrast and "needs 4.5:1" in contrast
    assert "error(s)" in r["summary"]
    errors = [i for i in r["issues"] if i["severity"] == "error"]
    assert errors and all(i["severity"] in ("error", "warn") for i in r["issues"])


def test_overflow_is_an_error_on_mobile_but_only_a_warning_on_desktop(tmp_path):
    (tmp_path / "w.html").write_text("<html><head><meta name=viewport content='width=device-width'></head><body style='margin:0'><div style='width:1500px'>x</div></body></html>")
    r = pt.preview(str(tmp_path), {"path": "w.html"})
    sev = {(i["viewport"], i["type"]): i["severity"] for i in r["issues"]}
    assert sev[("mobile", "horizontal-overflow")] == "error" and sev[("desktop", "horizontal-overflow")] == "warn"
    detail = next(i["detail"] for i in r["issues"] if i["viewport"] == "mobile" and i["type"] == "horizontal-overflow")
    assert "390px" in detail and "div" in detail


def test_blank_page_is_an_error(tmp_path):
    (tmp_path / "b.html").write_text("<html><body></body></html>")
    r = pt.preview(str(tmp_path), {"path": "b.html", "viewport": "desktop"})
    assert [i["type"] for i in r["issues"] if i["severity"] == "error"] == ["blank-page"]


def test_actions_drive_the_page_before_the_check(ws):
    r = pt.preview(ws, {"path": "app.html", "viewport": "desktop", "actions": ["type #name Sam", "click #start", "wait 100"]})
    assert "error" not in r, r
    assert any(o["el"] == "h1#out" and o["text"] == "started Sam" for o in r["layout"]["desktop"])
    r = pt.preview(ws, {"path": "app.html", "viewport": "desktop", "actions": ["press ArrowRight"]})
    assert any(o["text"] == "key ArrowRight" for o in r["layout"]["desktop"])


def test_failing_action_reports_instead_of_hanging(ws):
    r = pt.preview(ws, {"path": "app.html", "viewport": "desktop", "actions": ["click #does-not-exist"]})
    assert "error" in r and "preview couldn't load" in r["error"]


def test_long_pages_are_cropped_not_exploded(tmp_path):
    (tmp_path / "long.html").write_text("<html><head><meta name=viewport content='width=device-width'></head><body style='margin:0'><div style='height:9000px;background:#ddd'>tall</div></body></html>")
    r = pt.preview(str(tmp_path), {"path": "long.html", "viewport": "desktop"})
    assert "cropped" in r["viewports"]["desktop"]
    from PIL import Image
    assert Image.open(os.path.join(str(tmp_path), r["screenshots"]["desktop"])).height == 3000


def test_relative_assets_nested_pages_and_root_absolute_build_output_all_resolve(tmp_path):
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "s.css").write_text("h1{color:rgb(255,0,0)}")
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "a.html").write_text("<html><head><meta name=viewport content='width=device-width'><link rel=stylesheet href='/css/s.css'></head><body><h1>nested</h1></body></html>")
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app.js").write_text("document.title='built'")
    (dist / "index.html").write_text("<html><head><meta name=viewport content='width=device-width'><script src='/assets/app.js'></script></head><body><h1>built</h1></body></html>")
    nested = pt.preview(str(tmp_path), {"path": "pages/a.html", "viewport": "desktop"})
    built = pt.preview(str(tmp_path), {"path": "dist/index.html", "viewport": "desktop"})
    assert not [i for i in nested["issues"] if i["type"] == "request-failed"], nested["issues"]
    assert not [i for i in built["issues"] if i["type"] == "request-failed"], built["issues"]
    assert built["title"] == "built"  # /assets/app.js (root-absolute) found via the page's own directory


# ---------- the page can't reach other things on this machine ----------

def test_a_page_cannot_reach_other_local_servers(tmp_path):
    hits = []

    class Spy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"secret")

        def log_message(self, *a):
            pass

    spy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Spy)
    threading.Thread(target=spy.serve_forever, daemon=True).start()
    try:
        port = spy.server_address[1]
        (tmp_path / "ssrf.html").write_text(
            f"<html><head><meta name=viewport content='width=device-width'></head><body><h1>x</h1>"
            f"<script>fetch('http://127.0.0.1:{port}/internal');fetch('http://192.168.1.1/admin');</script></body></html>")
        r = pt.preview(str(tmp_path), {"path": "ssrf.html", "viewport": "desktop", "wait_ms": 500})
    finally:
        spy.shutdown()
        spy.server_close()
    assert hits == [], "the page reached a local server it should not be able to see"
    assert any("blocked" in i["detail"] for i in r["issues"] if i["type"] == "request-failed")


def test_dispatch_and_gate_integration(ws):
    import main
    ctx = main.ToolContext(session_id="s", workspace=ws, search_url="", bot=None, session_key=None,
                           req=None, plan_state=None, passthrough_session_id=None, require_athena_bots_session=False)
    r = main.dispatch_tool("preview", {"path": "good.html", "viewport": "mobile"}, ctx)
    assert "error" not in r and main._result_is_progress("preview", r)

    class Req:
        search_url = ""
        allowed_tools = None
    names = lambda t: {x["function"]["name"] for x in t}
    assert "preview" in names(main._get_mode_tools("workspace", Req(), build_task=True))
    assert "preview" in names(main._get_mode_tools("workspace", Req()))
    assert "preview" not in names(main._get_mode_tools("casual", Req()))


# ---------- contrast over gradients (found when a 4B model's edit made a button unreadable and the check missed it) ----------

def _page(body_css, el_css):
    return (f"<html><head><meta name=viewport content='width=device-width'><style>body{{margin:0;{body_css}}}p{{font-size:18px;{el_css}}}</style></head>"
            "<body><p>Readable words here</p></body></html>")


def test_dark_text_on_a_dark_gradient_is_flagged_and_light_text_is_not(tmp_path):
    grad = "background:radial-gradient(circle at 50% 0%, #232a5c, #0f1226 60%)"
    (tmp_path / "bad.html").write_text(_page(grad, "color:#1a1500"))
    (tmp_path / "ok.html").write_text(_page(grad, "color:#f2f4ff"))
    bad = pt.preview(str(tmp_path), {"path": "bad.html", "viewport": "desktop"})
    ok = pt.preview(str(tmp_path), {"path": "ok.html", "viewport": "desktop"})
    flagged = [i for i in bad["issues"] if i["type"] == "low-contrast"]
    assert flagged and "part of a gradient" in flagged[0]["detail"] and "#1a1500" in flagged[0]["detail"]
    assert not [i for i in ok["issues"] if i["type"] == "low-contrast"]


def test_gradient_check_uses_the_worst_stop_but_photos_are_not_guessed_at(tmp_path):
    (tmp_path / "half.html").write_text(_page("background:linear-gradient(90deg,#ffffff,#202020)", "color:#777777"))
    r = pt.preview(str(tmp_path), {"path": "half.html", "viewport": "desktop"})
    assert [i for i in r["issues"] if i["type"] == "low-contrast"]          # #777 fails against one end of the gradient
    (tmp_path / "photo.html").write_text(_page("background:url(data:image/gif;base64,R0lGODlhAQABAAAAACw=) #000", "color:#111111"))
    r = pt.preview(str(tmp_path), {"path": "photo.html", "viewport": "desktop"})
    assert not [i for i in r["issues"] if i["type"] == "low-contrast"]      # image background: can't know, so say nothing


# ---------- screenshots reach the chat UI ----------

def test_screenshots_are_served_inline_to_the_ui_and_the_model_never_sees_the_urls(ws):
    import main
    r = pt.preview(ws, {"path": "good.html"})
    assert set(r["screenshot_urls"]) == {"desktop", "mobile"}
    for name, url in r["screenshot_urls"].items():
        assert url.startswith("/api/generated-files/") and url.endswith(f"preview-good-{name}.png")
        response = main.get_generated_file(url.rsplit("/", 1)[-1])
        assert response.media_type == "image/png"
        assert response.headers["content-disposition"].startswith("inline")
        assert open(response.path, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    # PDFs still download rather than display
    import generated_files
    pdf = main.get_generated_file(generated_files.store(b"%PDF-1.4", "Resume.pdf"))
    assert pdf.media_type == "application/pdf" and pdf.headers["content-disposition"].startswith("attachment")


def test_old_preview_screenshots_are_pruned_but_pdfs_and_fresh_ones_stay(tmp_path):
    import generated_files, time
    old_shot = generated_files.store(b"x", "preview-a-desktop.png")
    fresh_shot = generated_files.store(b"x", "preview-b-desktop.png")
    old_pdf = generated_files.store(b"x", "Resume.pdf")
    ancient = time.time() - 40 * 86400
    for token in (old_shot, old_pdf):
        os.utime(generated_files.resolve(token), (ancient, ancient))
    generated_files.prune("preview-", 30)
    assert generated_files.resolve(old_shot) is None
    assert generated_files.resolve(fresh_shot) and generated_files.resolve(old_pdf)
