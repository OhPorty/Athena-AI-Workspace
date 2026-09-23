import re
import concurrent.futures

import httpx

from logging_setup import logger

WEB_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web and automatically fetch a readable preview of the top results, so you usually don't need a separate web_fetch call just to see what's out there. Each result's preview is capped for skimming multiple pages at once -- call web_fetch on a specific promising URL for its genuinely full, paginated content. Use for anything current/external not already in context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and extract readable text from a specific URL. Use after web_search to read a promising result in full, or when given a URL directly. Large pages are capped at 20000 characters per call -- if the response has has_more: true, call again with offset set to the returned next_offset to continue reading from where you left off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch."},
                    "offset": {"type": "integer", "description": "Character offset to start reading from. Omit or use 0 to start from the beginning."},
                },
                "required": ["url"],
            },
        },
    },
]

# Matches file_tools._READ_FILE_CHUNK_SIZE -- one consistent "how much
# text am I looking at per call" size across every tool that reads a
# large text source, rather than each picking its own arbitrary number.
_WEB_FETCH_CHUNK_SIZE = 20000

# web_search deliberately caps each result's preview well below a full
# web_fetch chunk -- up to 5 results come back in one call, and the
# point of search is skimming to find the right page, not deep-reading
# all 5 at once. Call web_fetch(url) directly (now paginated) for a
# specific result's genuinely full content.
_WEB_SEARCH_RESULT_PREVIEW_SIZE = 5000


_MIN_READABLE_TEXT_LENGTH = 250

# A JS-rendered SPA shell can still clear _MIN_READABLE_TEXT_LENGTH on
# nav labels/ad placeholders/column headers alone (confirmed directly:
# weather.com's static shell is 405 characters of exactly that, with
# zero real forecast data) -- length alone isn't a reliable signal.
# What is: the ratio of raw HTML size to extracted text. A real content
# page's markup-to-text ratio is modest (NWS ~15:1, Python's own docs
# ~4:1) because most of the HTML wraps actual text; an SPA shell's ratio
# is extreme (weather.com ~880:1: 356KB of HTML, mount points and JSON
# blobs, for 405 characters of real text) because the actual content
# only exists after client-side JS runs. 50:1 sits with wide margin
# above every real page measured and far below every SPA shell measured.
_SPA_HTML_TO_TEXT_RATIO_THRESHOLD = 50


def _extract_readable_text(html):
    """Strips script/style/nav/etc, then extracts clean text with
    structural separators -- shared by both the direct httpx fetch and
    the browser-rendered fallback (see _fetch_with_browser) so their
    output is identical in shape regardless of which path produced it.
    Uses BeautifulSoup for real HTML parsing (same technique Odysseus
    uses), not a regex tag-strip -- a regex-only approach leaves nav/ad/
    script text mixed into the output, which was confusing local models
    into treating real content as unreliable."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    text = soup.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    text = "\n".join(lines)
    return title, text


_PLAYWRIGHT_UNAVAILABLE = False  # cached once Playwright itself is confirmed not installed, so we don't re-import it on every single fetch -- NOT set on a single page's navigation failure, since that's per-URL, not a global unavailability signal


def _fetch_with_browser(url):
    """Local, fully on-machine JS-rendering fallback for pages the fast
    httpx+BeautifulSoup path can't get real content from -- React/Vue/etc
    single-page apps whose real content only exists after client-side JS
    runs (confirmed directly: weather.com's forecast page returns an
    empty shell via plain HTTP fetch, but renders real forecast data
    here). A fresh sync_playwright() context is launched per call rather
    than sharing one browser instance across calls -- Athena's tool
    calls can run on any request thread, and a shared Playwright driver
    isn't safe to use across threads without extra lifecycle machinery
    this doesn't need; a fresh per-call context is the supported pattern
    for multi-threaded use.

    Returns the fully-rendered HTML, or None if Playwright/Chromium
    isn't available (the browser binary is a separate one-time
    `playwright install chromium` step -- pip alone can't fetch it) or
    the render itself failed -- callers fall back to whatever the direct
    fetch already produced rather than raising."""
    global _PLAYWRIGHT_UNAVAILABLE
    if _PLAYWRIGHT_UNAVAILABLE:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _PLAYWRIGHT_UNAVAILABLE = True
        logger.warning("web_fetch: playwright is not installed -- JS-rendered pages will stay unreadable until `pip install playwright && playwright install chromium` is run")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, timeout=20000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass  # some pages never go fully idle (ads/analytics polling) -- use whatever rendered so far
                return page.content()
            finally:
                browser.close()
    except Exception as e:
        # Covers a missing Chromium binary (playwright install chromium
        # never run) as well as genuine navigation failures -- either
        # way, the caller just falls back cleanly to the direct fetch.
        logger.debug(f"browser fallback fetch failed for {url}: {e!r}")
        return None


def _chunk_content(url, content, offset=0):
    """Same shape as file_tools.read_file's own pagination -- offset in,
    a bounded chunk out, with has_more/next_offset telling the caller
    whether there's more to read. Replaces a flat slice that discarded
    the rest of the page with no way back."""
    offset = max(0, offset or 0)
    total_chars = len(content)
    chunk = content[offset:offset + _WEB_FETCH_CHUNK_SIZE]
    next_offset = offset + len(chunk)
    has_more = next_offset < total_chars
    result = {"url": url, "content": chunk, "total_chars": total_chars, "has_more": has_more}
    if has_more:
        result["next_offset"] = next_offset
    return result


def web_search(search_url: str, query: str):
    """Search then auto-fetch the top results concurrently, keeping only
    the ones that actually parsed to real content -- ports Odysseus's
    proven approach (services/search/core.py comprehensive_web_search):
    the model never has to decide "try another URL", since failed/blocked
    fetches are silently filtered out before it ever sees the response.
    This is what fixed Odysseus's own indecisive-looping problem."""
    try:
        resp = httpx.get(f"{search_url.rstrip('/')}/search", params={"q": query, "format": "json"}, timeout=15)
        if resp.status_code != 200:
            return {"error": f"SearXNG responded with HTTP {resp.status_code}"}
        results = resp.json().get("results", [])[:5]
        if not results:
            return {"error": "No search results found."}

        fetched = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_result = {
                executor.submit(web_fetch, r.get("url", "")): r
                for r in results if r.get("url")
            }
            for future in concurrent.futures.as_completed(future_to_result):
                r = future_to_result[future]
                try:
                    fetch_result = future.result()
                    if "content" in fetch_result and fetch_result["content"]:
                        fetched.append({
                            "title": r.get("title"),
                            "url": r.get("url"),
                            "content": fetch_result["content"][:_WEB_SEARCH_RESULT_PREVIEW_SIZE],
                        })
                except Exception:
                    pass  # a single failed fetch shouldn't fail the whole search

        if not fetched:
            # Every candidate failed to fetch -- fall back to snippets alone
            # rather than returning nothing at all.
            return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")} for r in results]

        return fetched
    except Exception as e:
        return {"error": f"Search failed: {e}"}


def web_fetch(url: str, offset: int = 0):
    """Fetch and extract clean readable text -- uses BeautifulSoup for
    real HTML parsing (same technique Odysseus uses), not a regex
    tag-strip. A regex-only approach leaves nav/ad/script text mixed
    into the output, which was confusing local models into treating
    real content as unreliable. Structural elements (lists, headings)
    get separators so the model can still parse a readable shape.

    Large pages are chunked (see _chunk_content), not flatly truncated --
    a page longer than one chunk is still fully reachable via repeated
    calls with offset, rather than silently losing everything past a
    fixed cutoff with no way back.

    A bare GitHub repo root URL (github.com/owner/repo, no further
    path) gets rewritten to fetch that repo's raw README.md directly
    from raw.githubusercontent.com instead. Confirmed directly by
    testing: modern GitHub repo pages are React-rendered SPAs whose
    real content (README, file tree) only loads via JavaScript --
    fetching the raw HTML here just returns UI chrome ('You signed in
    with another tab...', 'Uh oh! There was an error while loading')
    which is non-empty so it survives this function's own empty-text
    check, but is useless to the model. The raw README route has none
    of that problem since it's a plain text file, no rendering at all."""
    github_repo_match = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/?$", url)
    if github_repo_match:
        owner, repo = github_repo_match.group(1), github_repo_match.group(2)
        for branch in ("main", "master"):
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
            try:
                raw_resp = httpx.get(raw_url, timeout=15, follow_redirects=True)
                if raw_resp.status_code == 200 and raw_resp.text.strip():
                    header = f"# {owner}/{repo} README\nSource: {raw_url}\n\n"
                    return _chunk_content(raw_url, header + raw_resp.text, offset)
            except Exception:
                pass
        # Both branches failed -- fall through to the normal HTML fetch
        # below rather than giving up, since some repos use a different
        # default branch name entirely.

    # A /blob/branch/path URL is GitHub's own viewer for one specific
    # file -- same React-SPA rendering problem as the repo root case
    # above, just for a single file instead of the README. The branch
    # is already given in the URL itself here, so no guessing between
    # main/master is needed the way it is for the repo-root case.
    blob_match = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", url)
    if blob_match:
        owner, repo, branch, file_path = blob_match.groups()
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
        try:
            raw_resp = httpx.get(raw_url, timeout=15, follow_redirects=True)
            if raw_resp.status_code == 200 and raw_resp.text.strip():
                header = f"# {owner}/{repo} -- {file_path}\nSource: {raw_url}\n\n"
                return _chunk_content(raw_url, header + raw_resp.text, offset)
        except Exception:
            pass
        # Raw fetch failed -- fall through to the normal HTML fetch below.

    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; AthenaBot/1.0)"
        })
        title, text = _extract_readable_text(resp.text)

        # A page that comes back empty, with only a thin shell of static
        # chrome text, OR with a huge HTML payload relative to how little
        # of it was actual text (nav labels/ad placeholders/column
        # headers can clear the length floor on their own -- confirmed
        # directly against weather.com, see _SPA_HTML_TO_TEXT_RATIO_THRESHOLD's
        # own comment) is almost always a JS-rendered SPA -- try
        # rendering it locally before giving up. Only swap in the
        # browser-rendered version if it's actually better, so a
        # transient render hiccup can't make a working page worse.
        html_to_text_ratio = len(resp.text) / max(1, len(text))
        if len(text) < _MIN_READABLE_TEXT_LENGTH or html_to_text_ratio > _SPA_HTML_TO_TEXT_RATIO_THRESHOLD:
            browser_html = _fetch_with_browser(url)
            if browser_html is not None:
                browser_title, browser_text = _extract_readable_text(browser_html)
                if len(browser_text) > len(text):
                    title, text = browser_title, browser_text

        if not text:
            return {"error": f"web_fetch: {url}: no readable text content (page may need JS, and the local browser fallback wasn't available or also found nothing)"}

        header = f"# {title}\nSource: {url}\n\n" if title else f"Source: {url}\n\n"
        output = header + text
        return _chunk_content(url, output, offset)
    except Exception as e:
        return {"error": f"Fetch failed: {e}"}
