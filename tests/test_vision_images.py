"""Screenshots for vision models: sized by a shared pixel budget (never a count limit), taken only of public pages."""
import base64
import http.server
import io
import threading

import pytest
from PIL import Image

import main
import model_profile
import preview_tools
import vision_images as vi


def png(width, height, color="white"):
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode()


def size_of(b64):
    return Image.open(io.BytesIO(base64.b64decode(b64))).size


def pixels(b64):
    w, h = size_of(b64)
    return w * h


# ---------- sizing ----------

def test_a_large_screenshot_is_shrunk_to_the_tier_budget_and_comes_back_as_jpeg():
    out = vi.fit_images([png(1280, 3000)], "standard")
    assert base64.b64decode(out[0])[:2] == b"\xff\xd8"
    assert pixels(out[0]) <= vi.PIXEL_BUDGET["standard"] * 1.02
    assert size_of(out[0])[0] / size_of(out[0])[1] == pytest.approx(1280 / 3000, rel=0.02)           # same shape


def test_a_bigger_model_gets_a_bigger_picture_and_a_smaller_one_a_smaller_picture():
    shot = [png(1280, 2400)]
    small, standard, large = (pixels(vi.fit_images(shot, t)[0]) for t in ("small", "standard", "large"))
    assert small < standard < large


def test_more_images_share_the_budget_so_each_is_smaller_but_none_is_dropped():
    one = vi.fit_images([png(1280, 1600)], "standard")
    four = vi.fit_images([png(1280, 1600)] * 4, "standard")
    assert len(four) == 4 and pixels(four[0]) < pixels(one[0]) / 2
    twenty = vi.fit_images([png(1280, 1600)] * 20, "standard")
    assert len(twenty) == 20                                                                     # no count cap at all


def test_images_never_get_narrower_than_readable_or_larger_than_they_were():
    assert size_of(vi.fit_images([png(1280, 6000)] * 3, "small")[0])[0] >= vi.MIN_WIDTH          # a legibility floor, not a limit
    assert size_of(vi.fit_images([png(390, 700)], "standard")[0]) == (390, 700)                   # a phone screenshot stays as it is
    assert size_of(vi.fit_images([png(300, 200)], "large")[0]) == (300, 200)                      # never scaled up
    assert size_of(vi.fit_images([png(3000, 1000)], "large")[0])[0] <= vi.MAX_WIDTH


def test_a_short_context_model_gets_a_smaller_budget_than_its_tier_alone_would_give():
    assert vi.batch_budget("large", ctx_size=65536) == vi.PIXEL_BUDGET["large"]
    assert vi.batch_budget("large", ctx_size=8192) < vi.PIXEL_BUDGET["large"] / 3
    assert vi.batch_budget("standard", ctx_size=100) == vi.MIN_IMAGE_PIXELS                      # but never absurdly small
    assert vi.fit_images([]) == []


def test_jpeg_input_is_accepted_too():
    jpeg = io.BytesIO()
    Image.new("RGB", (1500, 1500), "red").save(jpeg, format="JPEG")
    assert pixels(vi.fit_images([base64.b64encode(jpeg.getvalue()).decode()], "small")[0]) <= vi.PIXEL_BUDGET["small"] * 1.02


# ---------- only public pages ----------

@pytest.mark.parametrize("url,fragment", [
    ("file:///etc/passwd", "http(s)"), ("ftp://example.com/x", "http(s)"), ("", "http(s)"), ("not a url", "http(s)"),
    ("http://127.0.0.1:11434/", "public web"), ("http://localhost:4444/", "public web"),
    ("http://192.168.0.10:4444/", "public web"), ("http://10.0.0.5/", "public web"), ("http://169.254.169.254/latest/meta-data", "public web"),
])
def test_only_public_web_pages_are_ever_opened(url, fragment):
    assert fragment in preview_tools.capture_public_page(url)["error"]


class _Counting(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        type(self).hits.append(self.path)
        body = (b"<title>Hello page</title><body style='margin:0'><h1>Hello</h1>"
                b"<img src='http://localhost:%d/secret.png' width=10 height=10></body>" % self.server.other_port) if self.path == "/" else b"secret"
        self.send_response(200); self.send_header("Content-Type", "text/html" if self.path == "/" else "image/png"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_public_page_is_captured_and_a_request_it_makes_to_a_private_address_is_blocked(monkeypatch):
    class Page(_Counting):
        hits = []

    class Secret(_Counting):
        hits = []

    secret, page = _serve(Secret), _serve(Page)
    page.other_port = secret.server_address[1]
    # Pretend only the page's own host is public: "127.0.0.1" passes, "localhost" (the page's embedded request) does not.
    monkeypatch.setattr(preview_tools, "_is_public_host", lambda host: host == "127.0.0.1")
    try:
        shot = preview_tools.capture_public_page(f"http://127.0.0.1:{page.server_address[1]}/", wait_ms=200)
    finally:
        page.shutdown(); secret.shutdown()
    assert "error" not in shot and shot["title"] == "Hello page" and shot["png"][:4] == b"\x89PNG"
    assert shot["width"] == 1280 and shot["height"] > 0 and shot["truncated"] is False
    assert Secret.hits == []                                                                      # the "LAN" server was never contacted


# ---------- the web_fetch tool and the chat loop ----------

def _ctx(req):
    return main.ToolContext(session_id="s", workspace="", search_url="http://x", bot=None, session_key=None, req=req,
                            plan_state=None, passthrough_session_id=None, require_athena_bots_session=False)


def _req():
    return type("R", (), {"provider": "", "endpoint_url": "http://127.0.0.1:1", "api_key": "", "model": "m"})()


def _vision(monkeypatch, value):
    real = model_profile.resolve
    monkeypatch.setattr(main.model_profile, "resolve", lambda *a, **k: {**real(*a, **k), "vision": value})


def test_web_fetch_with_screenshot_attaches_the_picture_only_for_a_model_that_can_see(monkeypatch):
    monkeypatch.setattr(main.web_tools, "web_fetch", lambda url, offset=0: {"url": url, "content": "page text"})
    monkeypatch.setattr(preview_tools, "capture_public_page", lambda url: {"title": "T", "png": base64.b64decode(png(1280, 800)), "width": 1280, "height": 800, "truncated": False})
    _vision(monkeypatch, True)
    seen = main.dispatch_tool("web_fetch", {"url": "https://example.com", "screenshot": True}, _ctx(_req()))
    assert seen["content"] == "page text" and len(seen["_images"]) == 1 and seen["screenshot_urls"]["page"].startswith("/api/generated-files/")
    assert seen["screenshot"].startswith("attached (1280x800px")
    _vision(monkeypatch, False)
    blind = main.dispatch_tool("web_fetch", {"url": "https://example.com", "screenshot": True}, _ctx(_req()))
    assert "_images" not in blind and "cannot view images" in blind["screenshot"] and blind["content"] == "page text"
    plain = main.dispatch_tool("web_fetch", {"url": "https://example.com"}, _ctx(_req()))
    assert "screenshot" not in plain and "_images" not in plain                                    # asked for nothing, got nothing extra


def test_a_failed_screenshot_leaves_the_text_result_intact(monkeypatch):
    monkeypatch.setattr(main.web_tools, "web_fetch", lambda url, offset=0: {"url": url, "content": "page text"})
    monkeypatch.setattr(preview_tools, "capture_public_page", lambda url: {"error": "Screenshots are only taken of pages on the public web"})
    _vision(monkeypatch, True)
    out = main.dispatch_tool("web_fetch", {"url": "http://192.168.0.10:4444", "screenshot": True}, _ctx(_req()))
    assert out["content"] == "page text" and "public web" in out["screenshot"] and "_images" not in out


def test_the_schema_offers_screenshot_to_the_model():
    props = next(t for t in main.web_tools.WEB_TOOL_SCHEMAS if t["function"]["name"] == "web_fetch")["function"]["parameters"]["properties"]
    assert props["screenshot"]["type"] == "boolean" and "public web pages" in props["screenshot"]["description"].lower()


def test_the_chat_loop_shows_the_model_a_picture_sized_to_its_budget(monkeypatch, tmp_path):
    import test_chat_loop_recovery as loop
    _vision(monkeypatch, True)
    monkeypatch.setattr(main.web_tools, "web_fetch", lambda url, offset=0: {"url": url, "content": "text"})
    monkeypatch.setattr(preview_tools, "capture_public_page", lambda url: {"title": "T", "png": base64.b64decode(png(1280, 3000)), "width": 1280, "height": 3000, "truncated": False})
    rounds = [loop.tool_round(loop.call("web_fetch", url="https://example.com", screenshot=True)), loop.text_round("I can see it.")]
    _, seen, _, _ = loop.run_turn(monkeypatch, tmp_path, rounds, search_url="http://search")
    attached = [m for m in seen[1] if m.get("images")]
    assert len(attached) == 1 and len(attached[0]["images"]) == 1
    shown = attached[0]["images"][0]
    assert base64.b64decode(shown)[:2] == b"\xff\xd8" and pixels(shown) <= vi.PIXEL_BUDGET["standard"] * 1.02
    assert not any("_images" in str(m.get("content")) for m in seen[1])                           # the raw picture never enters the text context
