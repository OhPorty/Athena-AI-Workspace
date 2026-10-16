"""Using a page like a person: click, type, hold, scroll, wait."""
import base64
import http.server
import io
import re
import threading

import pytest
from PIL import Image

import main
import model_profile
import preview_tools as pt

PLAYGROUND = b"""<!doctype html><title>Playground</title><h1>Playground</h1>
<a href="/docs">Docs</a>
<button id="reveal" onclick="document.getElementById('out').textContent='Revealed!'">Reveal</button>
<label for="q">Search terms</label><input id="q" placeholder="Type here">
<button onclick="document.getElementById('out').textContent='You searched: '+document.getElementById('q').value">Search</button>
<div id="out"></div><h2 id="held"></h2><div id="charge" style="width:90px;height:40px;background:#ddd">charge</div><div id="scroll"></div>
<div style="height:3000px">tall page</div>
<script>
let down=null;
addEventListener('keydown',e=>{if(e.key==='ArrowRight'&&down===null)down=performance.now()});
addEventListener('keyup',e=>{if(e.key==='ArrowRight'&&down!==null){document.getElementById('held').textContent='held '+Math.round(performance.now()-down)+' ms';down=null}});
let md=null;document.getElementById('charge').addEventListener('mousedown',()=>md=performance.now());
addEventListener('mouseup',()=>{if(md!==null){document.getElementById('charge').textContent='charged '+Math.round(performance.now()-md)+' ms';md=null}});
addEventListener('scroll',()=>document.getElementById('scroll').textContent='scrolled '+Math.round(scrollY));
</script>"""


STORE = b"""<!doctype html><title>Shop</title>
<style>:root{--brand:#9147ff;--gap:12px} .product-card{color:rgb(20,30,40);padding:8px;display:grid;gap:var(--gap)} .hidden-promo{display:none} #menu{display:none} #menu.open{display:block}</style>
<link rel="stylesheet" href="/site.css">
<header class="site-header main"><nav><a href="/shop" data-nav="shop">Shop</a><a href="/about">About</a></nav><button id="open" onclick="document.getElementById('menu').classList.add('open')">Open menu</button></header>
<div id="menu"><a class="menu-link" href="/sale">Sale</a></div>
<main><article class="product-card" data-sku="A1" style="margin:4px"><h2>Red hat</h2><p>Warm</p></article>
<article class="product-card" data-sku="B2"><h2>Blue hat</h2><p>Cool</p></article>
<article class="product-card" data-sku="C3"><h2>Green hat</h2><p>Fresh</p></article>
<div class="hidden-promo">Secret</div><div id="host"></div></main>
<script>const r=document.getElementById('host').attachShadow({mode:'open'});r.innerHTML='<button class="shadow-btn">In shadow</button>';</script>"""


class Handler(http.server.BaseHTTPRequestHandler):
    hits = []
    agents = []

    def do_GET(self):
        type(self).hits.append(self.path)
        type(self).agents.append(self.headers.get("User-Agent"))
        body = {"/": PLAYGROUND, "/store": STORE, "/forbidden": b"<title>403 Forbidden</title><h1>403 Forbidden</h1>",
                "/verify": b"<title>Human Verification</title><p>Please verify you are human to continue.</p>"}.get(self.path, b"ok")
        self.send_response(403 if self.path == "/forbidden" else 202 if self.path == "/verify" else 200)
        self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def site(monkeypatch):
    Handler.hits = []
    Handler.agents = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(pt, "_is_public_host", lambda host: host == "127.0.0.1")        # stand in for "a public site"
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# ---------- the actions ----------

def test_the_new_actions_parse_and_bad_ones_are_refused():
    steps = pt.parse_actions(["click Sign in", "type Search hello world", "type \"Search terms\" two words", "hold ArrowRight 600", "hold mouse #go 1500",
                              "hold mouse Big red button 800", "scroll bottom", "scroll -200", "wait 99999"])
    assert steps == [("click", "Sign in"), ("type", "Search", "hello world"), ("type", "Search terms", "two words"), ("hold_key", "ArrowRight", 600), ("hold_mouse", "#go", 1500),
                     ("hold_mouse", "Big red button", 800), ("scroll", "bottom"), ("scroll", "-200"), ("wait", 5000)]
    assert pt.parse_actions(["hold ArrowRight 99999"])[0][2] == 5000
    for bad in ("hold", "hold ArrowRight", "hold mouse 500", "hold a b 5", "scroll sideways", "drag a b"):
        with pytest.raises(pt.ActionError):
            pt.parse_actions([bad])


def test_clicking_typing_holding_and_scrolling_work_on_a_real_page(site):
    shot = pt.capture_public_page(site + "/", actions=["click Reveal", "type 'Type here' hello", "click Search"], wait_ms=100)
    assert "error" not in shot and "You searched: hello" in shot["text"] and shot["final_url"].startswith(site)

    assert "Revealed!" in pt.capture_public_page(site + "/", actions=["click Reveal"], wait_ms=100)["text"]
    assert "Revealed!" in pt.capture_public_page(site + "/", actions=["click #reveal"], wait_ms=100)["text"]                # a selector still works

    held = int(re.search(r"held (\d+) ms", pt.capture_public_page(site + "/", actions=["hold ArrowRight 400"], wait_ms=100)["text"]).group(1))
    assert 350 <= held <= 1200                                                                                              # it really was held down

    charged = int(re.search(r"charged (\d+) ms", pt.capture_public_page(site + "/", actions=["hold mouse #charge 600"], wait_ms=100)["text"]).group(1))
    assert 550 <= charged <= 1400

    scrolled = int(re.search(r"scrolled (\d+)", pt.capture_public_page(site + "/", actions=["scroll 600", "wait 100"], wait_ms=100)["text"]).group(1))
    assert 590 <= scrolled <= 700


def test_the_page_report_lists_what_can_be_clicked_and_includes_a_picture(site):
    shot = pt.capture_public_page(site + "/", wait_ms=100)
    kinds = {(i["kind"], i.get("text") or i.get("label")) for i in shot["interactive"]}
    assert ("link", "Docs") in kinds and ("button", "Reveal") in kinds and ("button", "Search") in kinds and ("input:text", "Search terms") in kinds
    assert next(i for i in shot["interactive"] if i.get("text") == "Docs")["href"].endswith("/docs")
    assert Image.open(io.BytesIO(shot["png"])).width == 1280 and shot["title"] == "Playground"


def test_a_missing_target_is_a_clear_error_not_a_hang(site):
    out = pt.capture_public_page(site + "/", actions=["click Nonexistent Button"], wait_ms=100)
    assert "error" in out and "Step 1 (click Nonexistent Button) did not work" in out["error"] and '#reveal (button "Reveal")' in out["error"]
    assert "error" in pt.capture_public_page(site + "/", actions=["hold mouse Nothing Here 300"], wait_ms=100)


def test_the_workspace_preview_tool_can_hold_keys_for_testing_a_game(tmp_path):
    (tmp_path / "index.html").write_bytes(PLAYGROUND)
    out = pt.preview(str(tmp_path), {"path": "index.html", "viewport": "desktop", "actions": ["hold ArrowRight 400"], "wait_ms": 100})
    assert "error" not in out
    text = " ".join(o["text"] for o in out["layout"]["desktop"])
    assert re.search(r"held (3[5-9]\d|[4-9]\d\d|1[01]\d\d) ms", text) or "held" in text


# ---------- through the web_fetch tool ----------

def _ctx(req):
    return main.ToolContext(session_id="s", workspace="", search_url="http://x", bot=None, session_key=None, req=req,
                            plan_state=None, passthrough_session_id=None, require_athena_bots_session=False)


def _req():
    return type("R", (), {"provider": "", "endpoint_url": "http://127.0.0.1:1", "api_key": "", "model": "m"})()


def _png():
    out = io.BytesIO(); Image.new("RGB", (1280, 800), "white").save(out, format="PNG"); return out.getvalue()


def _vision(monkeypatch, value):
    real = model_profile.resolve
    monkeypatch.setattr(main.model_profile, "resolve", lambda *a, **k: {**real(*a, **k), "vision": value})


def _capture(monkeypatch, **overrides):
    seen = {}
    base = {"title": "T", "final_url": "https://example.com/after", "text": "page after the steps", "interactive": [{"kind": "button", "text": "Next"}],
            "png": _png(), "width": 1280, "height": 800, "truncated": False}

    def fake(url, actions=None, wait_ms=900):
        seen["call"] = (url, actions)
        return {**base, **overrides}

    monkeypatch.setattr(pt, "capture_public_page", fake)
    return seen


def test_web_fetch_with_actions_returns_the_page_after_the_steps_with_what_is_clickable(monkeypatch):
    seen = _capture(monkeypatch)
    _vision(monkeypatch, True)
    out = main.dispatch_tool("web_fetch", {"url": "https://example.com", "actions": ["click Next", "scroll 500"]}, _ctx(_req()))
    assert seen["call"] == ("https://example.com", ["click Next", "scroll 500"])
    assert out["content"] == "page after the steps" and out["url"].endswith("/after") and out["interactive"] == [{"kind": "button", "text": "Next"}]
    assert len(out["_images"]) == 1 and out["screenshot"].startswith("attached")                  # a picture by default when it can see


def test_a_model_that_cannot_see_gets_text_and_the_clickable_list_but_no_picture(monkeypatch):
    _capture(monkeypatch)
    _vision(monkeypatch, False)
    out = main.dispatch_tool("web_fetch", {"url": "https://example.com", "actions": ["click Next"]}, _ctx(_req()))
    assert out["content"] and out["interactive"] and "_images" not in out and "cannot view images" in out["screenshot"]
    _vision(monkeypatch, True)
    quiet = main.dispatch_tool("web_fetch", {"url": "https://example.com", "actions": ["click Next"], "screenshot": False}, _ctx(_req()))
    assert "_images" not in quiet and "screenshot" not in quiet


def test_long_page_text_is_cut_with_a_flag(monkeypatch):
    _capture(monkeypatch, text="word " * 5000)
    _vision(monkeypatch, False)
    out = main.dispatch_tool("web_fetch", {"url": "https://example.com", "actions": ["wait 100"]}, _ctx(_req()))
    assert len(out["content"]) == main._PAGE_TEXT_LIMIT and out["content_truncated"] is True


def test_capture_errors_pass_through_and_the_schema_documents_the_actions(monkeypatch):
    _capture(monkeypatch, error="Screenshots are only taken of pages on the public web")
    out = main.dispatch_tool("web_fetch", {"url": "http://192.168.0.10:4444", "actions": ["click X"]}, _ctx(_req()))
    assert "public web" in out["error"]
    props = next(t for t in main.web_tools.WEB_TOOL_SCHEMAS if t["function"]["name"] == "web_fetch")["function"]["parameters"]["properties"]
    assert props["actions"]["type"] == "array" and "hold ArrowRight" in props["actions"]["description"]


@pytest.mark.parametrize("target,is_selector", [
    ("#start", True), (".card", True), ("[data-x=1]", True), ("button", True), ("canvas", True), ("div.card > a", True), ("a:hover", True), ("text=Sign in", True),
    ("Reveal", False), ("Sign in", False), ("Add to cart", False), ("Accept all cookies", False), ("Search terms", False),
])
def test_selectors_and_visible_words_are_told_apart(target, is_selector):
    assert pt._looks_like_selector(target) is is_selector


def test_a_page_that_refuses_the_reader_is_reported_not_screenshotted(site):
    for path in ("/forbidden", "/verify"):
        out = pt.capture_public_page(site + path, wait_ms=100)
        assert "error" in out and "refused to serve" in out["error"] and "png" not in out, (path, out)
    assert pt.capture_public_page(site + "/", wait_ms=100)["title"] == "Playground"           # a normal page still works


def test_the_browser_says_what_it_is(site):
    pt.capture_public_page(site + "/", wait_ms=100)
    assert Handler.agents and all(a.startswith("AthenaBot/") and "Headless" not in a for a in Handler.agents)


# ---------- looking inside a page: inspect_public_page ----------

def test_the_structure_of_a_page_shows_tags_ids_classes_and_css_variables(site):
    out = pt.inspect_public_page(site + "/store", wait_ms=100)
    assert out["title"] == "Shop" and "error" not in out
    lines = out["outline"].splitlines()
    assert any(l.strip().startswith("header.site-header.main") for l in lines) and any("article.product-card [2]" in l for l in lines)
    assert any("a" == l.strip().split(" ")[0].split(".")[0] and '"Shop"' in l for l in lines)
    assert out["css_variables"] == {"--brand": "#9147ff", "--gap": "12px"}
    assert out["stylesheets"]["linked"] == [site + "/site.css"] and out["stylesheets"]["inline"] == 1
    assert len(out["outline"]) < 9000


def test_a_selector_describes_each_match_with_its_real_styles(site):
    out = pt.inspect_public_page(site + "/store", selector=".product-card", wait_ms=100, html=True)
    assert out["matches"] == 3 and out["shown"] == 3
    first = out["elements"][0]
    assert first["classes"] == ["product-card"] and first["tag"] == "article" and first["attributes"] == {"data-sku": "A1"}
    assert first["inline_style"] == "margin:4px" and first["styles"]["color"] == "rgb(20, 30, 40)" and first["styles"]["display"] == "grid"
    assert first["styles"]["padding"] == "8px" and first["box"]["width"] > 0 and first["visible"] is True
    assert first["children"] == ["h2", "p"] and first["html"].startswith('<article class="product-card"')
    assert "html" not in pt.inspect_public_page(site + "/store", selector=".product-card", wait_ms=100)["elements"][0]


def test_hidden_elements_are_reported_as_hidden_and_the_limit_and_styles_are_honoured(site):
    hidden = pt.inspect_public_page(site + "/store", selector=".hidden-promo", wait_ms=100)["elements"][0]
    assert hidden["visible"] is False and hidden["styles"]["display"] == "none"
    out = pt.inspect_public_page(site + "/store", selector=".product-card", limit=2, styles=["gap", "color"], wait_ms=100)
    assert out["matches"] == 3 and out["shown"] == 2 and set(out["elements"][0]["styles"]) == {"gap", "color"}
    assert out["elements"][0]["styles"]["gap"] == "12px"


def test_elements_inside_a_shadow_root_are_found(site):
    out = pt.inspect_public_page(site + "/store", selector=".shadow-btn", wait_ms=100)
    assert out["matches"] == 1 and out["elements"][0]["in_shadow_root"] is True and out["elements"][0]["text"] == "In shadow"


def test_steps_can_put_the_page_in_a_state_before_inspecting(site):
    closed = pt.inspect_public_page(site + "/store", selector="#menu", wait_ms=100)["elements"][0]
    opened = pt.inspect_public_page(site + "/store", selector="#menu", actions=["click Open menu"], wait_ms=100)["elements"][0]
    assert closed["visible"] is False and opened["visible"] is True and "open" in opened["classes"]


def test_bad_selectors_and_non_public_pages_are_plain_errors(site, monkeypatch):
    assert "not a valid CSS selector" in pt.inspect_public_page(site + "/store", selector="div[", wait_ms=100)["error"]
    assert pt.inspect_public_page(site + "/store", selector=".nothing-here", wait_ms=100)["matches"] == 0
    monkeypatch.undo()
    assert "public web" in pt.inspect_public_page("http://127.0.0.1:1/x")["error"] and "http" in pt.inspect_public_page("ftp://x")["error"]


def test_a_page_that_refuses_the_reader_is_reported_when_inspecting_too(site):
    assert "refused to serve" in pt.inspect_public_page(site + "/forbidden", wait_ms=100)["error"]


def test_models_are_offered_the_tool_and_it_dispatches(site):
    import web_tools
    assert "inspect_page" in [t["function"]["name"] for t in web_tools.WEB_TOOL_SCHEMAS]
    out = main.dispatch_tool("inspect_page", {"url": site + "/store", "selector": "h2", "limit": 1}, _ctx(_req()))
    assert out["matches"] == 3 and out["elements"][0]["text"] == "Red hat"
