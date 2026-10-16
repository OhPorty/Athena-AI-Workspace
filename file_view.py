"""Letting a vision model LOOK at a file it made or was given.

A picture is rendered to a PNG for the model: a raster image as it is (tiny pixel art scaled up so it can
actually be seen, transparency shown over a plain light background), an SVG as a browser would draw it, and a
PDF page as a page. The chat loop then sizes the picture to the model's budget (vision_images) and shows it
only to models that can see. Nothing here decides who sees what.
"""
import base64
import io
import os

import file_tools

RASTER = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
VIEWABLE = RASTER | {".svg", ".pdf"}
_MAX_BYTES = 40 * 1024 * 1024
_MIN_LONG_SIDE = 256            # pixel art is shown at least this large, in whole-number steps, with hard edges
_MAX_UPSCALE = 24
_BACKDROP = (232, 232, 232)


def is_viewable(path):
    return os.path.splitext(str(path))[1].lower() in VIEWABLE


def _png_b64(image):
    out = io.BytesIO()
    image.save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode()


def _over_backdrop(image):
    from PIL import Image
    rgba = image.convert("RGBA")
    backdrop = Image.new("RGBA", rgba.size, _BACKDROP + (255,))
    return Image.alpha_composite(backdrop, rgba).convert("RGB")


def _scale_up_small(image):
    from PIL import Image
    long_side = max(image.size)
    if long_side >= _MIN_LONG_SIDE:
        return image, 1
    factor = min(_MAX_UPSCALE, -(-_MIN_LONG_SIDE // long_side))
    return image.resize((image.width * factor, image.height * factor), Image.NEAREST), factor


def _raster(path):
    from PIL import Image
    with Image.open(path) as im:
        frames = getattr(im, "n_frames", 1)
        im.seek(0)
        original = im.size
        shown, factor = _scale_up_small(_over_backdrop(im))
    note = f"{original[0]}x{original[1]} pixels"
    if factor > 1:
        note += f", scaled up {factor}x so it can be seen"
    if frames > 1:
        note += f"; showing the first of {frames} frames"
    return shown, note


def _svg(path):
    from PIL import Image
    from playwright.sync_api import sync_playwright
    with open(path, encoding="utf-8", errors="replace") as f:
        svg = f.read()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 800, "height": 800})
            page.route("**/*", lambda route: route.abort() if route.request.url.startswith(("http", "file")) else route.continue_())
            page.set_content(f"<body style='margin:0;background:#e8e8e8;display:inline-block'>{svg}</body>")
            page.wait_for_timeout(150)
            png = page.locator("svg").first.screenshot()
        finally:
            browser.close()
    image = Image.open(io.BytesIO(png)).convert("RGB")
    shown, factor = _scale_up_small(image)
    return shown, f"SVG drawn at {image.width}x{image.height} pixels" + (f", scaled up {factor}x" if factor > 1 else "")


def pdf_page_count(path):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    try:
        return len(doc)
    finally:
        doc.close()


def _pdf(path, page):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    try:
        total = len(doc)
        number = int(page or 1)
        if not 1 <= number <= total:
            raise ValueError(f"This PDF has {total} page(s); there is no page {number}.")
        rendered = doc[number - 1].render(scale=1.6).to_pil().convert("RGB")
    finally:
        doc.close()
    return rendered, f"page {number} of {total}" + ("" if total == 1 else "; pass page to see another")


def pdf_pages_b64(path):
    """Every page of a PDF as PNGs (for checking a document that was just made)."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    try:
        return [_png_b64(doc[i].render(scale=1.6).to_pil().convert("RGB")) for i in range(len(doc))]
    finally:
        doc.close()


def view(workspace, rel_path, page=None):
    """-> {"png": base64, "note": str} or {"error": str}"""
    try:
        path = file_tools.resolve_workspace_path(workspace, rel_path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isfile(path):
        return {"error": f"Not a file: {rel_path}"}
    ext = os.path.splitext(path)[1].lower()
    if ext not in VIEWABLE:
        return {"error": f"{rel_path} is not an image, SVG or PDF, so there is nothing to look at."}
    if os.path.getsize(path) > _MAX_BYTES:
        return {"error": f"{rel_path} is too large to render ({os.path.getsize(path) // (1024 * 1024)} MB)."}
    try:
        if ext in RASTER:
            image, note = _raster(path)
        elif ext == ".svg":
            image, note = _svg(path)
        else:
            image, note = _pdf(path, page)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Could not draw {rel_path}: {str(e)[:160]}"}
    return {"png": _png_b64(image), "note": note}
