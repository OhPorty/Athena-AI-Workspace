"""Deterministic HTML/Chromium PDF rendering for Athena's document tools.

The model never writes HTML or CSS. It supplies structured content plus a
template and palette name; this module owns everything that used to go wrong
when a model hand-wrote print CSS: page size, bleed, margins, page breaks,
fonts, and fitting a resume onto one page.

Page-geometry rules the templates rely on (all verified against the bundled
Chromium, not assumed):
  * Chromium does not paint the root background into @page margins, so any
    template with a full-bleed band uses `@page { margin: 0 }` and a root
    background gradient, which repeats on every page.
  * Per-page top/bottom spacing for those templates comes from
    `box-decoration-break: clone` padding, since @page margins are unavailable.
  * Templates with no full-bleed elements use real @page margins and margin
    boxes (page numbers).
"""
import colorsys
import io
import re
import tempfile
from pathlib import Path

import jinja2
import pypdf

_ASSETS = Path(__file__).resolve().parent / "pdf_assets"
_FONT_DIR = _ASSETS / "fonts"

PALETTES = {
    "navy": "#2f6bd0",
    "forest": "#2f8f5b",
    "teal": "#16899a",
    "burgundy": "#a3243f",
    "plum": "#7a3fa0",
    "charcoal": "#4a5568",
    "copper": "#b5651d",
}
DEFAULT_PALETTE = "navy"

# (family, weight, style, file)
_FACES = [
    ("Inter", 400, "normal", "inter-latin-400-normal.woff2"),
    ("Inter", 500, "normal", "inter-latin-500-normal.woff2"),
    ("Inter", 600, "normal", "inter-latin-600-normal.woff2"),
    ("Inter", 700, "normal", "inter-latin-700-normal.woff2"),
    ("Inter", 400, "italic", "inter-latin-400-italic.woff2"),
    ("Source Serif 4", 400, "normal", "source-serif-4-latin-400-normal.woff2"),
    ("Source Serif 4", 600, "normal", "source-serif-4-latin-600-normal.woff2"),
    ("Source Serif 4", 700, "normal", "source-serif-4-latin-700-normal.woff2"),
    ("Source Serif 4", 400, "italic", "source-serif-4-latin-400-italic.woff2"),
    ("Playfair Display", 600, "normal", "playfair-display-latin-600-normal.woff2"),
    ("Playfair Display", 700, "normal", "playfair-display-latin-700-normal.woff2"),
]
FONT_SETS = {
    "modern": {"body": "Inter", "display": "Inter", "label": "Inter"},
    "classic": {"body": "Source Serif 4", "display": "Source Serif 4", "label": "Inter"},
    "editorial": {"body": "Inter", "display": "Playfair Display", "label": "Inter"},
}
DEFAULT_FONT = "modern"

PAGE_SIZES = {"letter": "Letter", "a4": "A4"}

RESUME_TEMPLATES = {
    "sidebar": "resume_sidebar.html.j2",
    "banner": "resume_banner.html.j2",
    "classic": "resume_classic.html.j2",
}
DEFAULT_RESUME_TEMPLATE = "sidebar"

# One-page fit ladder of (type scale, spacing scale). Whitespace tightens first
# and type shrinks last, the way a person fits a resume by hand. The type floor
# keeps body text near 8.5pt; below that a resume stops being readable and is
# better left as two good pages than one cramped one.
FIT_STEPS = [(1.0, 1.0), (1.0, 0.8), (1.0, 0.62), (0.97, 0.5), (0.94, 0.42), (0.9, 0.35), (0.87, 0.3)]
NATURAL = [(1.0, 1.0)]


def _hls_hex(h, l, s):
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _luminance(hex_color):
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(int(hex_color[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a_hex, b_hex):
    hi, lo = sorted((_luminance(a_hex), _luminance(b_hex)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _with_contrast(h, l, s, against_hex, minimum, step):
    """Move lightness in `step` direction until the color reaches `minimum` contrast.

    HSL lightness is not perceived brightness (a saturated yellow at L=0.4 is
    still pale), so legibility is enforced on the WCAG contrast ratio instead.
    """
    color = _hls_hex(h, l, s)
    for _ in range(60):
        if contrast_ratio(color, against_hex) >= minimum or not 0.04 < l < 0.96:
            break
        l += step
        color = _hls_hex(h, l, s)
    return color


def derive_palette(accent_hex):
    """One accent color -> the whole coordinated set a template needs.

    `text` is the accent adjusted to read as small type on white, `light` the
    accent adjusted to read on the dark band; both reach 4.5:1 for any input.
    """
    r, g, b = (int(accent_hex[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    dark = _hls_hex(h, 0.17, min(s, 0.42))
    return {
        "accent": accent_hex,
        "text": _with_contrast(h, min(l, 0.45), min(s, 0.85), "#ffffff", 4.5, -0.015),
        "light": _with_contrast(h, 0.72, min(s, 0.80), dark, 4.5, 0.015),
        "dark": dark,
        "tint": _hls_hex(h, 0.965, min(s, 0.45)),
        "rule": _hls_hex(h, 0.86, min(s, 0.30)),
    }


def _font_faces(font_set):
    families = set(FONT_SETS[font_set].values())
    return [
        {"family": fam, "weight": weight, "style": style, "url": (_FONT_DIR / fname).as_uri()}
        for fam, weight, style, fname in _FACES
        if fam in families
    ]


def _css_str(value):
    """Escape arbitrary text for use inside a CSS string literal in a <style> block."""
    out = []
    for ch in str(value):
        if ch in '\\"':
            out.append("\\" + ch)
        elif ch in "<>&\n\r":
            out.append("\\%x " % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


_NUMERIC = re.compile(r"^[\s$€£¥+\-–−(]*\d[\d.,\s]*[%)]?\s*$")


def _is_numeric(value):
    return bool(_NUMERIC.match(str(value or "")))


_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_ASSETS / "templates")),
    autoescape=True,
    undefined=jinja2.StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["css_str"] = _css_str
_env.filters["is_numeric"] = _is_numeric


def render_html(template_file, data, palette_hex, font_set, page_size):
    template = _env.get_template(template_file)
    return template.render(
        d=data,
        p=derive_palette(palette_hex),
        f=FONT_SETS[font_set],
        font_faces=_font_faces(font_set),
        page_css=PAGE_SIZES[page_size],
    )


def render_pdf(html, steps):
    """Render once, try each (type, spacing) step in order, return (pdf_bytes, pages, step).

    The first step that yields a single page wins. If none do, the first
    (natural-size) render is kept -- shrinking past readability to chase one
    page is worse than a clean second page.
    """
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "index.html"
        html_path.write_text(html, encoding="utf-8")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                context = browser.new_context()
                # Content is autoescaped and fonts are local; nothing here should
                # ever touch the network, so refuse it outright.
                context.route(re.compile(r"^https?://"), lambda route: route.abort())
                page = context.new_page()
                page.goto(html_path.as_uri(), wait_until="load")
                page.evaluate("document.fonts.ready.then(() => true)")

                first = None
                for step in steps:
                    page.evaluate(
                        "([s, sp]) => { const r = document.documentElement.style; r.setProperty('--s', s); r.setProperty('--sp', sp); }",
                        list(step),
                    )
                    pdf = page.pdf(print_background=True, prefer_css_page_size=True, tagged=True)
                    pages = len(pypdf.PdfReader(io.BytesIO(pdf)).pages)
                    if first is None:
                        first = (pdf, pages, step)
                    if pages == 1:
                        return pdf, pages, step
                return first
            finally:
                browser.close()
