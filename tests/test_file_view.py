"""A vision model can look at what it made: images, SVGs and PDF pages."""
import base64
import io
import os

import pytest
from PIL import Image

import file_view
import main
import pdf_tools
import test_chat_loop_recovery as loop
import test_vision_images as vi_tests
from pdf_fixtures import DOCUMENT, RESUME

_ctx, _req, _vision = vi_tests._ctx, vi_tests._req, vi_tests._vision


def decode(b64):
    return Image.open(io.BytesIO(base64.b64decode(b64)))


@pytest.fixture
def ws(tmp_path):
    return tmp_path


def sprite(path, w=16, h=23):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for x in range(4, 12):
        for y in range(2, 10):
            img.putpixel((x, y), (200, 40, 40, 255))
    img.save(path)


# ---------- rendering ----------

def test_a_tiny_sprite_is_scaled_up_in_whole_steps_with_hard_edges_over_a_plain_backdrop(ws):
    sprite(ws / "hero.png")
    out = file_view.view(str(ws), "hero.png")
    img = decode(out["png"])
    assert "16x23 pixels, scaled up 12x" in out["note"] and img.size == (16 * 12, 23 * 12)
    assert img.getpixel((6 * 12 + 5, 5 * 12 + 5)) == (200, 40, 40)                       # a sprite pixel became a crisp 12x12 block
    assert img.getpixel((5, 5)) == (232, 232, 232)                                          # transparent areas are a plain light backdrop


def test_a_normal_sized_image_is_left_alone_and_a_gif_shows_its_first_frame(ws):
    Image.new("RGB", (640, 400), "teal").save(ws / "photo.jpg")
    out = file_view.view(str(ws), "photo.jpg")
    assert decode(out["png"]).size == (640, 400) and "scaled" not in out["note"]
    frames = [Image.new("RGB", (300, 300), c) for c in ("red", "blue", "green")]
    frames[0].save(ws / "anim.gif", save_all=True, append_images=frames[1:], duration=100, loop=0)
    gif = file_view.view(str(ws), "anim.gif")
    assert "first of 3 frames" in gif["note"] and decode(gif["png"]).getpixel((10, 10)) == (255, 0, 0)


def test_an_svg_is_drawn_the_way_a_browser_draws_it(ws):
    (ws / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"><rect width="120" height="80" fill="#2a9d8f"/><circle cx="60" cy="40" r="25" fill="#e63946"/></svg>')
    out = file_view.view(str(ws), "logo.svg")
    img = decode(out["png"])
    assert img.getpixel((img.width // 2, img.height // 2)) == (230, 57, 70) and "SVG drawn at 120x80" in out["note"]


def test_a_script_inside_an_svg_cannot_reach_the_network(ws):
    (ws / "evil.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="red"/>'
                                 '<script>fetch("http://127.0.0.1:9/leak")</script></svg>')
    assert "png" in file_view.view(str(ws), "evil.svg")                                     # drawn fine, nothing escaped (routes abort http/file)


def test_pdf_pages_render_with_a_page_count_and_a_bad_page_is_a_plain_error(ws):
    out = pdf_tools.generate_pdf({"template": "document", "filename": "doc", "content": {**DOCUMENT, "blocks": DOCUMENT["blocks"] + [{"type": "page_break"}, {"type": "paragraph", "text": "Second page text."}]}}, str(ws))
    first, second = file_view.view(str(ws), "doc.pdf"), file_view.view(str(ws), "doc.pdf", page=2)
    assert "page 1 of 2" in first["note"] and "page 2 of 2" in second["note"] and first["png"] != second["png"]
    assert decode(first["png"]).width > 600
    assert "has 2 page(s); there is no page 5" in file_view.view(str(ws), "doc.pdf", page=5)["error"]
    assert file_view.pdf_page_count(str(ws / "doc.pdf")) == 2 == len(file_view.pdf_pages_b64(str(ws / "doc.pdf")))


def test_things_that_cannot_be_looked_at_say_so_plainly(ws):
    (ws / "notes.txt").write_text("hi")
    (ws / "bad.png").write_bytes(b"not an image")
    assert "nothing to look at" in file_view.view(str(ws), "notes.txt")["error"]
    assert "Could not draw bad.png" in file_view.view(str(ws), "bad.png")["error"]
    assert "Not a file" in file_view.view(str(ws), "missing.png")["error"]
    assert "outside the workspace" in file_view.view(str(ws), "../../etc/passwd.png")["error"]
    assert file_view.is_viewable("a/b.PNG") and file_view.is_viewable("x.pdf") and not file_view.is_viewable("x.py")


# ---------- read_file ----------

def test_read_file_shows_an_image_to_a_model_that_can_see_it_and_keeps_the_text_result(ws, monkeypatch):
    sprite(ws / "hero.png")
    _vision(monkeypatch, True)
    ctx = _ctx(_req()); ctx.workspace = str(ws)
    out = main.dispatch_tool("read_file", {"path": "hero.png"}, ctx)
    assert out["kind"] == "png" and "16x23 pixels" in out["content"]                         # the measured text is still there
    assert len(out["_images"]) == 1 and "scaled up 12x" in out["viewed"] and "shown to you" in out["viewed"]
    assert out["screenshot_urls"]["image"].startswith("/api/generated-files/")                 # and the person sees it in the chat too


def test_a_model_that_cannot_see_gets_the_measurements_and_a_plain_note(ws, monkeypatch):
    sprite(ws / "hero.png")
    _vision(monkeypatch, False)
    ctx = _ctx(_req()); ctx.workspace = str(ws)
    out = main.dispatch_tool("read_file", {"path": "hero.png"}, ctx)
    assert "16x23 pixels" in out["content"] and "_images" not in out and "cannot view images" in out["viewed"]


def test_ordinary_files_and_failed_reads_are_untouched(ws, monkeypatch):
    (ws / "a.py").write_text("print(1)\n")
    _vision(monkeypatch, True)
    ctx = _ctx(_req()); ctx.workspace = str(ws)
    plain = main.dispatch_tool("read_file", {"path": "a.py"}, ctx)
    assert plain["content"] == "print(1)\n" and "_images" not in plain and "viewed" not in plain
    missing = main.dispatch_tool("read_file", {"path": "gone.png"}, ctx)
    assert "error" in missing and "_images" not in missing


def test_a_pdf_shows_page_one_by_default_and_any_page_on_request(ws, monkeypatch):
    pdf_tools.generate_pdf({"template": "document", "filename": "doc", "content": {**DOCUMENT, "blocks": DOCUMENT["blocks"] + [{"type": "page_break"}, {"type": "paragraph", "text": "Second page text."}]}}, str(ws))
    _vision(monkeypatch, True)
    ctx = _ctx(_req()); ctx.workspace = str(ws)
    one = main.dispatch_tool("read_file", {"path": "doc.pdf"}, ctx)
    two = main.dispatch_tool("read_file", {"path": "doc.pdf", "page": 2}, ctx)
    assert one["kind"] == "pdf" and "[page 1]" in one["content"] and "page 1 of 2" in one["viewed"] and "page 2 of 2" in two["viewed"]
    assert one["_images"] != two["_images"]
    assert "no page 9" in main.dispatch_tool("read_file", {"path": "doc.pdf", "page": 9}, ctx)["viewed"]


def test_an_svgs_source_is_still_returned_with_its_drawing(ws, monkeypatch):
    (ws / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50"><circle cx="25" cy="25" r="20" fill="blue"/></svg>')
    _vision(monkeypatch, True)
    ctx = _ctx(_req()); ctx.workspace = str(ws)
    out = main.dispatch_tool("read_file", {"path": "logo.svg"}, ctx)
    assert "<circle" in out["content"] and len(out["_images"]) == 1


# ---------- preview and generate_pdf ----------

def test_preview_pointed_at_an_image_just_shows_it_instead_of_refusing(ws, monkeypatch):
    sprite(ws / "hero.png")
    _vision(monkeypatch, True)
    ctx = _ctx(_req()); ctx.workspace = str(ws)
    out = main.dispatch_tool("preview", {"path": "hero.png"}, ctx)
    assert "error" not in out and len(out["_images"]) == 1 and out["path"] == "hero.png"


def test_a_model_that_can_see_is_shown_every_page_of_the_pdf_it_just_made(ws, monkeypatch):
    _vision(monkeypatch, True)
    ctx = _ctx(_req()); ctx.workspace = str(ws)
    out = main.dispatch_tool("generate_pdf", {"template": "classic", "content": RESUME, "filename": "cv"}, ctx)
    assert out["saved_to"] == "cv.pdf" and len(out["_images"]) == out["pages"] and f"{out['pages']} page" in out["pages_shown"]
    assert list(out["screenshot_urls"]) == [f"page {i + 1}" for i in range(out["pages"])]
    _vision(monkeypatch, False)
    blind = main.dispatch_tool("generate_pdf", {"template": "classic", "content": RESUME, "filename": "cv2"}, ctx)
    assert "_images" not in blind and "pages_shown" not in blind and blind["pages"] >= 1
    assert "error" in main.dispatch_tool("generate_pdf", {"template": "fancy", "content": RESUME}, ctx)


def test_the_schema_tells_the_model_it_can_look_and_how_to_pick_a_pdf_page():
    schema = next(t for t in main.file_tools.FILE_TOOL_SCHEMAS if t["function"]["name"] == "read_file")["function"]
    assert "shown to you as a picture" in schema["description"] and schema["parameters"]["properties"]["page"]["type"] == "integer"


# ---------- the whole loop ----------

def test_in_a_real_turn_a_vision_model_is_shown_its_sprite_large_enough_to_see(monkeypatch, tmp_path):
    _vision(monkeypatch, True)
    buf = io.BytesIO()
    Image.new("RGBA", (16, 23), (200, 40, 40, 255)).save(buf, format="PNG")
    rounds = [loop.tool_round(loop.call("read_file", path="hero.png")), loop.text_round("The sprite is a red block.")]
    _, seen, _, _ = loop.run_turn(monkeypatch, tmp_path, rounds, files={"hero.png": buf.getvalue()})
    shown = [m for m in seen[1] if m.get("images")]
    assert len(shown) == 1 and decode(shown[0]["images"][0]).width >= 192                       # a 16-pixel sprite arrives big enough to see
    assert loop.outcome() == "completed"
