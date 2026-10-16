"""preview: let a model look at what it just built.

Renders a workspace HTML file in a real browser (desktop and mobile), runs automatic
checks, saves screenshots into the workspace, and returns the checks plus a compact
layout outline as text -- so a model that can't see images still gets real feedback.
Vision-capable models additionally get the screenshots (main.py pops `_images` from
the result and attaches them to the conversation; they never reach the UI or logs).

The checks find rendering problems (errors, overflow, unreadable text, broken images);
they cannot judge whether a design is good, and the result says so.
"""
import base64
import functools
import io
import json
import ipaddress
import os
import re
import shlex
import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import file_tools
import generated_files

VIEWPORTS = {
    "desktop": {"width": 1280, "height": 800},
    "mobile": {"width": 390, "height": 844},
}
_MAX_SHOT_HEIGHT = {"desktop": 3000, "mobile": 2400}
_MAX_ACTIONS = 20
_NAV_TIMEOUT_MS = 15000
_ACTION_TIMEOUT_MS = 5000

PREVIEW_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "preview",
            "description": (
                "Open an HTML page from the workspace in a real browser and look at it. Returns automatic checks "
                "(console errors, failed requests, horizontal overflow, low-contrast text, broken images, tiny tap "
                "targets, blank page), a layout outline with positions, and saves screenshots to .athena/preview/ "
                "(you also see them if your model supports images). Run it after building or changing anything "
                "visual, fix what it flags, then run it again. It finds rendering problems; it can't judge taste."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "HTML file relative to the workspace root. Default index.html."},
                    "viewport": {"type": "string", "enum": ["desktop", "mobile", "both"], "description": "Default both."},
                    "actions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional steps to run before the screenshot, e.g. [\"click #start\", \"press ArrowRight\", \"type #name Sam\", \"wait 500\"].",
                    },
                    "wait_ms": {"type": "integer", "description": "Settle time after load in ms (animations, canvas). Default 600, max 5000."},
                },
            },
        },
    },
]


# ---------- network confinement ----------

_host_cache = {}


def _is_public_host(host):
    """True only if every address the host resolves to is globally routable. A page the model
    wrote must not be able to reach loopback, the LAN, or link-local services from this machine."""
    if not host:
        return False
    if host in _host_cache:
        return _host_cache[host]
    try:
        try:
            addrs = [ipaddress.ip_address(host)]
        except ValueError:
            addrs = [ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(host, None)]
        ok = bool(addrs) and all(a.is_global for a in addrs)
    except (OSError, ValueError):
        ok = False
    _host_cache[host] = ok
    return ok


# ---------- static server (loopback, workspace-rooted) ----------

class _Handler(SimpleHTTPRequestHandler):
    """Serves the workspace; a root-absolute path that isn't there is retried relative to the
    page's own directory, so a built site (dist/index.html using /assets/x.js) works too."""

    def __init__(self, *args, fallback_dir=None, **kwargs):
        self._fallback_dir = fallback_dir
        super().__init__(*args, **kwargs)

    def translate_path(self, path):
        primary = super().translate_path(path)
        if os.path.exists(primary) or not self._fallback_dir:
            return primary
        rel = os.path.relpath(primary, self.directory)
        candidate = os.path.realpath(os.path.join(self._fallback_dir, rel))
        if candidate.startswith(os.path.realpath(self._fallback_dir) + os.sep) and os.path.exists(candidate):
            return candidate
        return primary

    def log_message(self, *args):
        pass


class _StaticServer:
    def __init__(self, root, fallback_dir):
        handler = functools.partial(_Handler, directory=root, fallback_dir=fallback_dir)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


# ---------- actions ----------

class ActionError(ValueError):
    pass


def parse_actions(actions):
    """['click #go', 'click Sign in', 'press Space', 'type #name Sam', 'scroll 800', 'wait 300'] -> [(verb, args...)]"""
    if actions is None:
        return []
    if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
        raise ActionError("actions must be a list of strings like \"click #start\"")
    if len(actions) > _MAX_ACTIONS:
        raise ActionError(f"at most {_MAX_ACTIONS} actions per preview")
    steps = []
    for raw in actions:
        parts = raw.strip().split(None, 2)
        verb = parts[0].lower() if parts else ""
        if verb == "click" and len(parts) >= 2:
            steps.append(("click", raw.strip().split(None, 1)[1]))
        elif verb == "press" and len(parts) == 2:
            steps.append(("press", parts[1]))
        elif verb == "type" and len(parts) >= 3 and parts[1][:1] in ("'", '"'):
            try:
                tokens = shlex.split(raw.strip())
            except ValueError:
                raise ActionError(f"can't read action {raw!r}; close the quote around the field name")
            if len(tokens) < 3:
                raise ActionError(f"can't read action {raw!r}; use: type \"field name\" <text>")
            steps.append(("type", tokens[1], " ".join(tokens[2:])))
        elif verb == "type" and len(parts) == 3:
            steps.append(("type", parts[1], parts[2]))
        elif verb == "wait" and len(parts) == 2 and parts[1].isdigit():
            steps.append(("wait", min(int(parts[1]), 5000)))
        elif verb == "scroll" and len(parts) == 2 and (parts[1].lstrip("-").isdigit() or parts[1].lower() in ("top", "bottom")):
            steps.append(("scroll", parts[1].lower()))
        elif verb == "hold" and len(raw.split()) >= 3 and raw.split()[-1].isdigit():
            words = raw.split()
            ms = min(int(words[-1]), 5000)
            if words[1].lower() == "mouse" and len(words) >= 4:
                steps.append(("hold_mouse", " ".join(words[2:-1]), ms))
            elif len(words) == 3 and words[1].lower() != "mouse":
                steps.append(("hold_key", words[1], ms))
            else:
                raise ActionError(f"can't read action {raw!r}; use: hold <Key> <ms> | hold mouse <selector or visible text> <ms>")
        else:
            raise ActionError(f"can't read action {raw!r}; use: click <selector or visible text> | press <Key> | type <selector, label or placeholder, in quotes if it has spaces> <text> | hold <Key> <ms> | hold mouse <target> <ms> | scroll <pixels|top|bottom> | wait <ms>")
    return steps


# ---------- in-page checks ----------

# Wording the scaffold starters ship with; still on the page means the content was never written.
_STARTER_PHRASES = ["Studio Name", "First service", "Second service", "Project one", "Your city", "Untitled", "Replace with", "Replace the tiles", "A clear headline that says"]

_CHECK_JS = r"""
(vw) => {
  const out = {title: document.title, overflow: null, contrast: [], broken_images: [], missing_alt: 0,
               tap_targets: [], small_text: [], starter_text: [], blank: false, viewport_meta: !!document.querySelector('meta[name=viewport]'), outline: []};
  // vw is the device width being emulated. window.innerWidth can't be used: a phone shrinks a wide
  // page to fit, so innerWidth grows to match the content and nothing ever looks too wide.
  const de = document.documentElement;
  const cssEsc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  const sel = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) return s + '#' + el.id;
    const cls = [...el.classList].slice(0, 2);
    return cls.length ? s + '.' + cls.join('.') : s;
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0.05;
  };
  const clipped = (el) => {
    let p = el.parentElement;
    while (p && p !== document.body) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'hidden' || o === 'auto' || o === 'scroll' || o === 'clip') return true;
      p = p.parentElement;
    }
    return false;
  };
  const all = [...document.body.querySelectorAll('*')].filter(e => !['SCRIPT','STYLE','NOSCRIPT','LINK','META','TEMPLATE'].includes(e.tagName));

  if (de.scrollWidth > vw + 1 && (out.viewport_meta || vw >= 700)) {
    const off = [];
    for (const el of all) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > vw + 1 && visible(el) && !clipped(el)) off.push([Math.round(r.right), sel(el)]);
    }
    off.sort((a, b) => b[0] - a[0]);
    out.overflow = {page_width: de.scrollWidth, viewport_width: vw, widest: off.slice(0, 3).map(o => o[1] + ' (right edge ' + o[0] + 'px)')};
  }

  const parse = (c) => {
    const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
    return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1};
  };
  const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const lum = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const over = (fg, bg) => ({r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1});
  const hex = (c) => '#' + [c.r, c.g, c.b].map(v => Math.round(v).toString(16).padStart(2, '0')).join('');
  // Background colors the text could sit on: one for a solid background, every color stop for a
  // gradient (worst case is what matters for legibility), null when a photo/image is involved.
  const stackOver = (layers, base) => { let c = base; for (let i = layers.length - 1; i >= 0; i--) c = over(layers[i], c); return c; };
  const bgOf = (el) => {
    const layers = []; let p = el;
    while (p) {
      const cs = getComputedStyle(p), img = cs.backgroundImage;
      if (img && img !== 'none') {
        if (!img.includes('gradient') || img.includes('url(')) return null;
        const stops = (img.match(/rgba?\([^)]+\)/g) || []).map(parse).filter(Boolean);
        if (!stops.length) return null;
        return stops.map(st => stackOver(layers, over(st, {r: 255, g: 255, b: 255, a: 1})));
      }
      const c = parse(cs.backgroundColor);
      if (c && c.a > 0) { layers.push(c); if (c.a >= 1) break; }
      p = p.parentElement;
    }
    return [stackOver(layers, {r: 255, g: 255, b: 255, a: 1})];
  };

  const seen = new Set();
  for (const el of all) {
    const direct = [...el.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim());
    if (!direct.length || !visible(el)) continue;
    const cs = getComputedStyle(el), size = parseFloat(cs.fontSize);
    if (size < 12 && out.small_text.length < 3) out.small_text.push(sel(el) + ' (' + Math.round(size * 10) / 10 + 'px)');
    const fg0 = parse(cs.color), bgs = bgOf(el);
    if (!fg0 || !bgs) continue;
    let ratio = Infinity, worst = bgs[0], worstFg = fg0;
    for (const bg of bgs) {
      const fg = over(fg0, bg), l1 = lum(fg), l2 = lum(bg), r = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      if (r < ratio) { ratio = r; worst = bg; worstFg = fg; }
    }
    const large = size >= 24 || (size >= 18.66 && parseInt(cs.fontWeight) >= 700);
    if (ratio < (large ? 3 : 4.5)) {
      const key = sel(el) + hex(worstFg) + hex(worst);
      if (!seen.has(key) && out.contrast.length < 5) {
        seen.add(key);
        out.contrast.push({element: sel(el), text: direct[0].textContent.trim().slice(0, 28), ratio: Math.round(ratio * 10) / 10, text_color: hex(worstFg), background: hex(worst) + (bgs.length > 1 ? ' (part of a gradient)' : '')});
      }
    }
  }

  for (const img of document.images) {
    if (!img.getAttribute('src')) continue;
    if (!img.complete || img.naturalWidth === 0) { if (out.broken_images.length < 5) out.broken_images.push(img.getAttribute('src').slice(0, 80)); }
    if (!img.hasAttribute('alt')) out.missing_alt++;
  }

  for (const el of document.querySelectorAll('button, input:not([type=hidden]), select, textarea, [role=button], a[href]')) {
    if (!visible(el)) continue;
    if (el.tagName === 'A' && getComputedStyle(el).display === 'inline') continue;
    const r = el.getBoundingClientRect();
    if ((r.width < 24 || r.height < 24) && out.tap_targets.length < 5) out.tap_targets.push(sel(el) + ' (' + Math.round(r.width) + 'x' + Math.round(r.height) + 'px)');
  }

  const pageText = document.title + '\n' + document.body.innerText;
  out.starter_text = __STARTER_PHRASES__.filter(w => pageText.includes(w));

  out.blank = document.body.innerText.trim().length === 0 && !document.querySelector('canvas, img, svg, video, iframe');

  const landmarks = [...document.querySelectorAll('header, nav, main, section, article, aside, footer, h1, h2, h3, button, form, canvas, img, [role=button], [role=status], [aria-live]')].filter(visible);
  out.outline = landmarks.slice(0, 40).map(el => {
    const r = el.getBoundingClientRect();
    return {el: sel(el), text: (el.innerText || el.getAttribute('alt') || '').trim().replace(/\s+/g, ' ').slice(0, 32),
            x: Math.round(r.left + scrollX), y: Math.round(r.top + scrollY), w: Math.round(r.width), h: Math.round(r.height)};
  }).sort((a, b) => a.y - b.y || a.x - b.x).slice(0, 16);
  return out;
}
"""
_CHECK_JS = _CHECK_JS.replace("__STARTER_PHRASES__", json.dumps(_STARTER_PHRASES))


# ---------- rendering ----------

def _model_image(png_bytes):
    """The screenshot as the model will receive it, before sizing: vision_images.fit_images shrinks the batch to the
    model's budget, so nothing here decides how big it ends up."""
    return base64.b64encode(png_bytes).decode()


_TAG_NAMES = {"a", "button", "input", "select", "textarea", "canvas", "form", "label", "div", "span", "p", "img", "video", "audio", "svg",
              "ul", "ol", "li", "table", "tr", "td", "th", "nav", "main", "header", "footer", "section", "article", "aside", "iframe", "summary",
              "h1", "h2", "h3", "h4", "h5", "h6", "body", "html"}


def _looks_like_selector(target):
    target = target.strip()
    if target.startswith(("#", ".", "[", "//", "text=", "role=", "xpath=", "css=")):
        return True
    if " " not in target:
        return target.lower() in _TAG_NAMES or any(c in target for c in "#.[>=:")
    return any(c in target for c in "#[>=")                      # "div.card > a" is a selector; "Sign in now" is words


def _locate(page, target, kinds):
    """A selector when the target looks like one, otherwise the visible words of what a person would point at."""
    if _looks_like_selector(target):
        return page.locator(target).first
    for kind in kinds:
        if kind in ("button", "link"):
            found = page.get_by_role(kind, name=target)
        elif kind == "label":
            found = page.get_by_label(target)
        elif kind == "placeholder":
            found = page.get_by_placeholder(target)
        else:
            found = page.get_by_text(target)
        if found.count():
            return found.first
    return page.get_by_text(target).first


_WHAT_IS_THERE_JS = r"""
() => {
  const visible = (el) => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const out = [];
  for (const el of document.querySelectorAll('button, a[href], input:not([type=hidden]), select, textarea, [role=button], [onclick], canvas')) {
    if (!visible(el) || out.length >= 14) continue;
    const tag = el.tagName.toLowerCase();
    const sel = el.id ? '#' + el.id : (el.classList.length ? tag + '.' + el.classList[0] : tag);
    const words = (el.innerText || el.value || el.getAttribute('aria-label') || el.placeholder || el.name || '').trim().replace(/\s+/g, ' ').slice(0, 30);
    out.push(sel + (words ? ' (' + tag + ' "' + words + '")' : ' (' + tag + ')'));
  }
  return out;
}
"""


def _describe_step(step):
    return " ".join(str(part) for part in step)


def _what_is_there(page):
    try:
        found = page.evaluate(_WHAT_IS_THERE_JS)
    except Exception:
        return ""
    return (" What the page has that can be clicked or typed into: " + "; ".join(found) + ".") if found else " The page has nothing visible that can be clicked or typed into."


def run_steps(page, steps):
    """Run the steps in order. A step that cannot be done stops the run with a message that says which step it was, why, and what the
    page does have, so a model can correct a wrong selector instead of deciding the page itself is broken."""
    for number, step in enumerate(steps, 1):
        try:
            _run_step(page, step)
        except ActionError as e:
            raise ActionError(f"Step {number} ({_describe_step(step)}) did not work: {e}.{_what_is_there(page)}") from None
        except Exception as e:
            reason = "nothing on the page matched it, or it was hidden or covered" if "Timeout" in type(e).__name__ or "Timeout" in str(e) else str(e).splitlines()[0][:160]
            raise ActionError(f"Step {number} ({_describe_step(step)}) did not work: {reason}.{_what_is_there(page)}") from None
        page.wait_for_timeout(120)


def _run_step(page, step):
    if step[0] == "click":
        _locate(page, step[1], ("button", "link", "text")).click()
    elif step[0] == "press":
        page.keyboard.press(step[1])
    elif step[0] == "type":
        _locate(page, step[1], ("label", "placeholder")).fill(step[2])
    elif step[0] == "hold_key":
        page.keyboard.down(step[1])
        page.wait_for_timeout(step[2])
        page.keyboard.up(step[1])
    elif step[0] == "hold_mouse":
        box = _locate(page, step[1], ("button", "link", "text")).bounding_box()
        if box is None:
            raise ActionError(f"nothing visible to hold the mouse on for {step[1]!r}")
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.wait_for_timeout(step[2])
        page.mouse.up()
    elif step[0] == "scroll":
        if step[1] == "top":
            page.evaluate("window.scrollTo(0, 0)")
        elif step[1] == "bottom":
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        else:
            page.evaluate("(dy) => window.scrollBy(0, dy)", int(step[1]))
    elif step[0] == "wait":
        page.wait_for_timeout(step[1])


def _check_viewport(browser, name, url, port, steps, wait_ms):
    """-> (report dict, png bytes, truncated flag)"""
    size = VIEWPORTS[name]
    context = browser.new_context(viewport=size, is_mobile=(name == "mobile"), has_touch=(name == "mobile"), device_scale_factor=1)
    console_errors, failed = [], []
    try:
        def guard(route):
            target = urlparse(route.request.url)
            if target.scheme in ("data", "blob", "about"):
                return route.continue_()
            if target.hostname == "127.0.0.1" and target.port == port:
                return route.continue_()
            if target.scheme in ("http", "https") and _is_public_host(target.hostname):
                return route.continue_()
            failed.append(f"blocked: {route.request.url[:100]} (pages may only reach the page's own server and public hosts)")
            route.abort()

        context.route("**/*", guard)
        page = context.new_page()
        page.set_default_timeout(_ACTION_TIMEOUT_MS)
        page.on("console", lambda m: console_errors.append(m.text[:200]) if m.type == "error" and "favicon" not in m.text else None)
        page.on("pageerror", lambda e: console_errors.append(("Uncaught " + str(e))[:240]))
        page.on("dialog", lambda d: d.dismiss())
        page.on("response", lambda r: failed.append(f"HTTP {r.status}: {r.url[:100]}") if r.status >= 400 and not r.url.endswith("favicon.ico") else None)
        page.on("requestfailed", lambda r: failed.append(f"failed: {r.url[:100]}") if not r.url.endswith("favicon.ico") and not any(r.url[:100] in f for f in failed) else None)
        page.goto(url, wait_until="load", timeout=_NAV_TIMEOUT_MS)
        page.wait_for_timeout(wait_ms)
        run_steps(page, steps)
        report = page.evaluate(_CHECK_JS, size["width"])
        full_height = page.evaluate("document.documentElement.scrollHeight")
        cap = _MAX_SHOT_HEIGHT[name]
        truncated = full_height > cap
        png = page.screenshot(full_page=True, clip=None) if not truncated else page.screenshot(
            full_page=True, clip={"x": 0, "y": 0, "width": size["width"], "height": cap})
        report["console_errors"] = list(dict.fromkeys(console_errors))[:8]
        report["failed_requests"] = list(dict.fromkeys(failed))[:8]
        report["page_height"] = full_height
        return report, png, truncated
    finally:
        context.close()


_INTERACTIVE_JS = r"""
() => {
  const visible = (el) => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const out = [];
  for (const el of document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link]')) {
    if (!visible(el) || out.length >= 40) continue;
    const tag = el.tagName.toLowerCase();
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    if (tag === 'a' || el.getAttribute('role') === 'link') { if (text) out.push({kind: 'link', text, href: (el.href || '').slice(0, 100)}); }
    else if (tag === 'button' || el.getAttribute('role') === 'button' || ['submit', 'button'].includes(el.type)) { if (text) out.push({kind: 'button', text}); }
    else { const label = (el.labels && el.labels[0] && el.labels[0].innerText.trim()) || el.getAttribute('aria-label') || el.placeholder || el.name || '';
           out.push({kind: tag === 'input' ? 'input:' + (el.type || 'text') : tag, label: label.slice(0, 60)}); }
  }
  return out;
}
"""


def _guarded_page(browser, size):
    """A page in a fresh browser context that may only talk to public addresses (never loopback or the LAN), that says
    what Athena is, and that dismisses dialogs."""
    import web_tools
    context = browser.new_context(viewport=size, device_scale_factor=1, user_agent=web_tools.USER_AGENT)

    def guard(route):
        request_url = urlparse(route.request.url)
        if request_url.scheme in ("data", "blob", "about") or (request_url.scheme in ("http", "https") and _is_public_host(request_url.hostname)):
            return route.continue_()
        route.abort()

    context.route("**/*", guard)
    page = context.new_page()
    page.set_default_timeout(_ACTION_TIMEOUT_MS)
    page.on("dialog", lambda d: d.dismiss())
    return page


def capture_public_page(url, actions=None, wait_ms=900):
    """Open a page on the public web, optionally act on it (click, type, hold, scroll, wait), and report what is there:
    title, address, readable text, what can be clicked, and a screenshot. Every request the page makes must go to a public
    address (like preview), so a page cannot be used to reach loopback or LAN services such as Ollama or the search engine.
    -> {"title", "final_url", "text", "interactive", "png", "width", "height", "truncated"} | {"error"}"""
    target = urlparse(str(url or "").strip())
    if target.scheme not in ("http", "https") or not target.hostname:
        return {"error": "A screenshot needs a full http(s) web address."}
    if not _is_public_host(target.hostname):
        return {"error": "Screenshots are only taken of pages on the public web, not of this machine or the local network."}
    try:
        steps = parse_actions(actions)
    except ActionError as e:
        return {"error": str(e)}
    size = VIEWPORTS["desktop"]
    try:
        from playwright.sync_api import sync_playwright
        import web_tools
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = _guarded_page(browser, size)
                response = page.goto(target.geturl(), wait_until="load", timeout=_NAV_TIMEOUT_MS)
                page.wait_for_timeout(wait_ms)
                first_title, first_text = web_tools._extract_readable_text(page.content())
                if web_tools.looks_blocked(response.status if response is not None else None, first_text):
                    return {"error": web_tools.blocked_message(target.geturl(), response.status if response is not None else None).replace("web_fetch: ", "")}

                def read():
                    title, text = web_tools._extract_readable_text(page.content())
                    return title or page.title(), text

                if steps:
                    run_steps(page, steps)
                title, text = read()
                full_height = page.evaluate("document.documentElement.scrollHeight")
                cap = _MAX_SHOT_HEIGHT["desktop"]
                truncated = full_height > cap
                clip = {"x": 0, "y": 0, "width": size["width"], "height": cap} if truncated else None
                png = page.screenshot(full_page=True, clip=clip)
                return {"title": title, "final_url": page.url, "text": text, "interactive": page.evaluate(_INTERACTIVE_JS), "png": png,
                        "width": size["width"], "height": min(full_height, cap), "truncated": truncated}
            finally:
                browser.close()
    except ActionError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Could not take a screenshot of that page: {str(e)[:200]}"}


_INSPECT_STYLES = ["display", "position", "z-index", "width", "height", "margin", "padding", "color", "background-color", "background-image",
                   "font-family", "font-size", "font-weight", "line-height", "text-align", "border", "border-radius", "opacity", "overflow", "box-shadow"]
_INSPECT_JS = r"""
({selector, depth, limit, html, styles}) => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'LINK', 'META', 'HEAD']);
  const clip = (t, n) => (t || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const label = el => (el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + [...el.classList].slice(0, 6).map(c => '.' + c).join('')).slice(0, 90);
  const path = el => { const parts = []; let e = el;
    while (e && e.nodeType === 1 && parts.length < 6) { parts.unshift(label(e)); if (e.id) break; e = e.parentElement || (e.getRootNode && e.getRootNode().host) || null; }
    return parts.join(' > '); };
  const deepAll = (root, sel, out) => { root.querySelectorAll(sel).forEach(e => out.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) deepAll(e.shadowRoot, sel, out); }); return out; };
  if (selector) {
    let found;
    try { found = deepAll(document, selector, []); } catch (e) { return {error: 'That is not a valid CSS selector: ' + clip(String(e.message), 120)}; }
    const elements = found.slice(0, limit).map(el => {
      const cs = getComputedStyle(el), r = el.getBoundingClientRect();
      const out = {path: path(el), tag: el.tagName.toLowerCase(), id: el.id || undefined, classes: [...el.classList], text: clip(el.innerText || el.textContent, 160),
        box: {x: Math.round(r.x + scrollX), y: Math.round(r.y + scrollY), width: Math.round(r.width), height: Math.round(r.height)},
        visible: !!(r.width && r.height) && cs.visibility !== 'hidden' && cs.display !== 'none'};
      const attrs = {}; for (const a of el.attributes) { if (!['class', 'id', 'style'].includes(a.name) && Object.keys(attrs).length < 12) attrs[a.name] = clip(a.value, 120); }
      if (Object.keys(attrs).length) out.attributes = attrs;
      if (el.getAttribute('style')) out.inline_style = clip(el.getAttribute('style'), 200);
      const st = {}; for (const p of styles) st[p] = clip(cs.getPropertyValue(p), 100); out.styles = st;
      const kids = [...el.children].filter(k => !SKIP.has(k.tagName));
      if (kids.length) out.children = kids.slice(0, 10).map(label).concat(kids.length > 10 ? ['... ' + (kids.length - 10) + ' more'] : []);
      if (el.getRootNode() !== document) out.in_shadow_root = true;
      if (html) out.html = el.outerHTML.slice(0, 1200);
      return out; });
    return {matches: found.length, shown: elements.length, elements};
  }
  const lines = []; let count = 0;
  const visit = (el, d) => { if (count >= 120) return; count++;
    const kids = [...el.children].filter(k => !SKIP.has(k.tagName));
    const own = clip([...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' '), 50);
    lines.push('  '.repeat(d) + label(el) + (el.shadowRoot ? ' {shadow root}' : '') + (kids.length ? ' [' + kids.length + ']' : '') + (own ? ' "' + own + '"' : ''));
    if (d >= depth) return;
    kids.slice(0, 12).forEach(k => visit(k, d + 1));
    if (kids.length > 12) lines.push('  '.repeat(d + 1) + '... ' + (kids.length - 12) + ' more'); };
  visit(document.body, 0);
  const vars = {}; let unreadable = 0;
  for (const sheet of document.styleSheets) { let rules; try { rules = sheet.cssRules; } catch (e) { unreadable++; continue; }
    for (const r of rules) { if (r.style && /^(:root|html|body)$/.test((r.selectorText || '').trim())) for (const p of r.style) { if (p.startsWith('--') && Object.keys(vars).length < 60) vars[p] = clip(r.style.getPropertyValue(p), 80); } } }
  const linked = [...document.querySelectorAll('link[rel~=stylesheet]')].map(l => l.href).slice(0, 10);
  return {outline: lines.join('\n'), css_variables: vars, stylesheets: {linked, inline: document.querySelectorAll('style').length, unreadable_cross_origin: unreadable}};
}
"""
_INSPECT_LIMITS = {"depth": (1, 8, 4), "limit": (1, 25, 8)}


def inspect_public_page(url, selector=None, actions=None, depth=None, limit=None, html=False, styles=None, wait_ms=900):
    """The DOM of a public web page, the way browser dev tools show it. Without a selector: the structure (tags, ids, classes),
    stylesheet links and CSS variables. With one: each match's classes, attributes, text, position and computed styles (and its
    HTML). Steps may be run first to put the page in a state (open a menu, scroll). Public pages only, like web_fetch.
    -> {"url", "title", ...} | {"error"}"""
    target = urlparse(str(url or "").strip())
    if target.scheme not in ("http", "https") or not target.hostname:
        return {"error": "Inspecting needs a full http(s) web address."}
    if not _is_public_host(target.hostname):
        return {"error": "Only pages on the public web can be inspected, not this machine or the local network. Read local files with read_file."}
    try:
        steps = parse_actions(actions)
    except ActionError as e:
        return {"error": str(e)}

    def pick(name, value):
        low, high, default = _INSPECT_LIMITS[name]
        try:
            return min(max(int(value), low), high)
        except (TypeError, ValueError):
            return default

    wanted = [str(p).strip().lower() for p in styles if str(p).strip()][:24] if isinstance(styles, (list, tuple)) and styles else _INSPECT_STYLES
    try:
        from playwright.sync_api import sync_playwright
        import web_tools
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = _guarded_page(browser, VIEWPORTS["desktop"])
                response = page.goto(target.geturl(), wait_until="load", timeout=_NAV_TIMEOUT_MS)
                page.wait_for_timeout(wait_ms)
                status = response.status if response is not None else None
                if web_tools.looks_blocked(status, web_tools._extract_readable_text(page.content())[1]):
                    return {"error": web_tools.blocked_message(target.geturl(), status).replace("web_fetch: ", "")}
                if steps:
                    run_steps(page, steps)
                found = page.evaluate(_INSPECT_JS, {"selector": (selector or "").strip(), "depth": pick("depth", depth), "limit": pick("limit", limit),
                                                    "html": bool(html), "styles": wanted})
                if "error" in found:
                    return found
                return {"url": page.url, "title": page.title(), **found}
            finally:
                browser.close()
    except ActionError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Could not inspect that page: {str(e)[:200]}"}


def _issues(name, report, same_origin_port):
    out = []

    def add(severity, kind, detail):
        out.append({"severity": severity, "viewport": name, "type": kind, "detail": detail})

    if report.get("blank"):
        add("error", "blank-page", "the page rendered with no visible content")
    for e in report.get("console_errors", []):
        add("error", "console-error", e)
    for f in report.get("failed_requests", []):
        add("error" if f"127.0.0.1:{same_origin_port}" in f else "warn", "request-failed", f)
    ov = report.get("overflow")
    if ov:
        add("error" if name == "mobile" else "warn", "horizontal-overflow",
            f"page is {ov['page_width']}px wide in a {ov['viewport_width']}px viewport; widest: {', '.join(ov['widest']) or 'unknown'}")
    for src in report.get("broken_images", []):
        add("error", "broken-image", src)
    for c in report.get("contrast", []):
        add("warn", "low-contrast", f"{c['element']} \"{c['text']}\" is {c['ratio']}:1 ({c['text_color']} on {c['background']}); needs 4.5:1 (3:1 for large text)")
    if report.get("starter_text") and name == "desktop":
        add("warn", "starter-text", "still has the starter's placeholder wording (" + ", ".join(f'"{w}"' for w in report["starter_text"][:4]) + "); write real content for this page")
    if report.get("missing_alt"):
        add("warn", "missing-alt", f"{report['missing_alt']} image(s) have no alt attribute")
    if name == "mobile":
        if not report.get("viewport_meta"):
            add("warn", "no-viewport-meta", "add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> or phones render it zoomed out")
        for t in report.get("tap_targets", []):
            add("warn", "small-tap-target", t + " is under 24px; make touch targets at least 24px (44px is comfortable)")
    for t in report.get("small_text", []):
        add("warn", "small-text", t + " is under 12px")
    return out


def preview(workspace, args):
    if not workspace:
        return {"error": "preview needs an active workspace."}
    args = args if isinstance(args, dict) else {}
    rel = str(args.get("path") or "index.html").strip()
    try:
        target = file_tools.resolve_workspace_path(workspace, rel)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isfile(target):
        return {"error": f"No such file: {rel}. Create it first (write_file), or pass the path of the HTML file to preview."}
    if not target.lower().endswith((".html", ".htm")):
        return {"error": f"preview opens HTML pages; {rel} isn't one."}
    which = str(args.get("viewport") or "both").lower()
    if which not in ("desktop", "mobile", "both"):
        return {"error": "viewport must be desktop, mobile or both"}
    try:
        steps = parse_actions(args.get("actions"))
        wait_ms = max(0, min(int(args.get("wait_ms", 600)), 5000))
    except (ActionError, TypeError, ValueError) as e:
        return {"error": str(e)}
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
    except ImportError:
        return {"error": "preview isn't available: the playwright package isn't installed."}

    root = os.path.realpath(workspace)
    page_dir = os.path.dirname(target)
    url_path = "/" + os.path.relpath(target, root).replace(os.sep, "/")
    names = ["desktop", "mobile"] if which == "both" else [which]
    shots_dir = os.path.join(root, ".athena", "preview")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(os.path.relpath(target, root))[0]).strip("_") or "page"
    generated_files.prune("preview-", max_age_days=30)
    result = {"page": os.path.relpath(target, root), "viewports": {}, "issues": [], "screenshots": {}, "screenshot_urls": {}}
    images, outlines = [], {}
    try:
        with _StaticServer(root, page_dir) as server, sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for name in names:
                    report, png, truncated = _check_viewport(browser, name, f"http://127.0.0.1:{server.port}{url_path}", server.port, steps, wait_ms)
                    result["title"] = report.get("title") or result.get("title", "")
                    result["viewports"][name] = f"{VIEWPORTS[name]['width']}x{VIEWPORTS[name]['height']}, page height {report['page_height']}px" + (" (screenshot cropped)" if truncated else "")
                    result["issues"] += _issues(name, report, server.port)
                    outlines[name] = report["outline"]
                    os.makedirs(shots_dir, exist_ok=True)
                    shot = os.path.join(shots_dir, f"{stem}-{name}.png")
                    with open(shot, "wb") as f:
                        f.write(png)
                    result["screenshots"][name] = os.path.relpath(shot, root)
                    # A served copy for the chat UI (the workspace file is for the model and the user's own files).
                    result["screenshot_urls"][name] = generated_files.url_for(generated_files.store(png, f"preview-{stem}-{name}.png"))
                    images.append(_model_image(png))
            finally:
                browser.close()
    except ActionError as e:
        return {"error": f"preview loaded {rel}, but {str(e)[0].lower() + str(e)[1:]}"}
    except PlaywrightError as e:
        return {"error": f"preview couldn't load {rel}: {str(e).splitlines()[0][:200]}"}

    result["layout"] = outlines
    if steps:
        result["actions_run"] = [str(a) for a in args.get("actions")]      # so the model knows the checks and screenshots show the page after these
    errors = sum(1 for i in result["issues"] if i["severity"] == "error")
    warns = len(result["issues"]) - errors
    if result["issues"]:
        result["summary"] = f"{errors} error(s), {warns} warning(s). Fix the errors first, then run preview again."
    else:
        result["summary"] = ("No problems found by the automatic checks. These cover rendering errors, not taste -- "
                             "check the layout outline or screenshots for spacing, hierarchy and visual balance.")
    result["_images"] = images
    return result
