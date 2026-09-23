import web_tools


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def json(self):
        import json
        return json.loads(self.text)


def test_chunk_content_no_pagination_needed():
    result = web_tools._chunk_content("http://example.com", "short content")
    assert result == {"url": "http://example.com", "content": "short content", "total_chars": 13, "has_more": False}
    assert "next_offset" not in result


def test_chunk_content_paginates_long_content():
    content = "x" * 25000
    first = web_tools._chunk_content("http://example.com", content)
    assert first["has_more"] is True
    assert first["next_offset"] == web_tools._WEB_FETCH_CHUNK_SIZE
    assert len(first["content"]) == web_tools._WEB_FETCH_CHUNK_SIZE

    second = web_tools._chunk_content("http://example.com", content, offset=first["next_offset"])
    assert second["has_more"] is False
    assert "next_offset" not in second
    assert len(second["content"]) == 25000 - web_tools._WEB_FETCH_CHUNK_SIZE
    assert first["content"] + second["content"] == content


def test_web_fetch_general_html_chunks_long_page(monkeypatch):
    long_text = "x" * 25000
    html = f"<html><head><title>Test Page</title></head><body><p>{long_text}</p></body></html>"
    monkeypatch.setattr(web_tools.httpx, "get", lambda *a, **k: FakeResponse(text=html))

    first = web_tools.web_fetch("http://example.com/page")
    assert first["has_more"] is True
    assert first["url"] == "http://example.com/page"
    assert "next_offset" in first

    second = web_tools.web_fetch("http://example.com/page", offset=first["next_offset"])
    assert second["has_more"] is False
    full = first["content"] + second["content"]
    assert long_text in full
    assert "Test Page" in first["content"]


def test_web_fetch_general_html_short_page_no_pagination(monkeypatch):
    # Short pages are exactly what triggers the browser-fallback check
    # (len(text) < _MIN_READABLE_TEXT_LENGTH) -- force the fallback itself
    # to report "unavailable" so this test doesn't try to launch a real
    # browser, and confirm the original short-but-real content still wins.
    html = "<html><head><title>Short</title></head><body><p>hello world</p></body></html>"
    monkeypatch.setattr(web_tools.httpx, "get", lambda *a, **k: FakeResponse(text=html))
    monkeypatch.setattr(web_tools, "_fetch_with_browser", lambda url: None)

    result = web_tools.web_fetch("http://example.com/short")
    assert result["has_more"] is False
    assert "next_offset" not in result
    assert "hello world" in result["content"]


def test_web_fetch_empty_page_returns_error(monkeypatch):
    monkeypatch.setattr(web_tools.httpx, "get", lambda *a, **k: FakeResponse(text="<html><body></body></html>"))
    monkeypatch.setattr(web_tools, "_fetch_with_browser", lambda url: None)
    result = web_tools.web_fetch("http://example.com/empty")
    assert "error" in result


def test_web_fetch_github_readme_chunks_long_content(monkeypatch):
    long_readme = "y" * 25000

    def fake_get(url, **kwargs):
        if "raw.githubusercontent.com" in url and url.endswith("main/README.md"):
            return FakeResponse(text=long_readme, status_code=200)
        return FakeResponse(text="", status_code=404)

    monkeypatch.setattr(web_tools.httpx, "get", fake_get)

    first = web_tools.web_fetch("https://github.com/someowner/somerepo")
    assert first["has_more"] is True
    assert "raw.githubusercontent.com" in first["url"]

    second = web_tools.web_fetch("https://github.com/someowner/somerepo", offset=first["next_offset"])
    assert second["has_more"] is False
    assert long_readme in (first["content"] + second["content"])


def test_web_fetch_github_blob_chunks_long_content(monkeypatch):
    long_file = "z" * 25000

    def fake_get(url, **kwargs):
        return FakeResponse(text=long_file, status_code=200)

    monkeypatch.setattr(web_tools.httpx, "get", fake_get)

    first = web_tools.web_fetch("https://github.com/someowner/somerepo/blob/main/src/thing.py")
    assert first["has_more"] is True
    assert first["url"] == "https://raw.githubusercontent.com/someowner/somerepo/main/src/thing.py"


def test_web_search_applies_per_result_preview_cap(monkeypatch):
    search_response = FakeResponse(text='{"results": [{"title": "A", "url": "http://a.example", "content": "snippet a"}]}')
    monkeypatch.setattr(web_tools.httpx, "get", lambda *a, **k: search_response)
    monkeypatch.setattr(web_tools, "web_fetch", lambda url, offset=0: {"url": url, "content": "z" * 10000, "total_chars": 10000, "has_more": False})

    results = web_tools.web_search("http://searx.example", "some query")
    assert len(results) == 1
    assert len(results[0]["content"]) == web_tools._WEB_SEARCH_RESULT_PREVIEW_SIZE


def test_web_search_falls_back_to_snippets_when_all_fetches_fail(monkeypatch):
    search_response = FakeResponse(text='{"results": [{"title": "A", "url": "http://a.example", "content": "snippet a"}]}')
    monkeypatch.setattr(web_tools.httpx, "get", lambda *a, **k: search_response)

    def failing_fetch(url, offset=0):
        raise RuntimeError("network down")

    monkeypatch.setattr(web_tools, "web_fetch", failing_fetch)

    results = web_tools.web_search("http://searx.example", "some query")
    assert results == [{"title": "A", "url": "http://a.example", "snippet": "snippet a"}]


def test_web_search_no_results(monkeypatch):
    monkeypatch.setattr(web_tools.httpx, "get", lambda *a, **k: FakeResponse(text='{"results": []}'))
    result = web_tools.web_search("http://searx.example", "nothing found query")
    assert "error" in result


# --- Local browser fallback for JS-rendered SPA pages ---


def test_extract_readable_text_shape_matches_regardless_of_source():
    # Same helper, same output shape whether called on a directly-fetched
    # page or on browser-rendered HTML -- confirms no formatting drift
    # between the two fetch paths.
    html = "<html><head><title>A Page</title></head><body><nav>skip</nav><p>real content here</p></body></html>"
    title, text = web_tools._extract_readable_text(html)
    assert title == "A Page"
    assert "real content here" in text
    assert "skip" not in text  # nav gets stripped


def test_web_fetch_falls_back_to_browser_when_html_to_text_ratio_is_extreme(monkeypatch):
    # Simulates weather.com's real failure mode: plenty of raw HTML, but
    # only a thin shell of nav/ad text once extracted (well over
    # _MIN_READABLE_TEXT_LENGTH on its own, so length alone wouldn't
    # trigger the fallback -- the ratio check is what catches this).
    shell_text = "Chevron Left\nToday\nHourly\n10 Day\nAdvertisement\n" * 8  # > 250 chars, still just chrome
    huge_html = "<html><head><title>Shell</title></head><body>" + ("<div data-x='y'></div>" * 5000) + f"<p>{shell_text}</p></body></html>"
    real_forecast_text = "Today High 76 Low 56 Precip 10% " * 20  # longer than the shell text -- realistic (real content usually dwarfs a thin nav shell)
    real_html = f"<html><head><title>Real Forecast</title></head><body><p>{real_forecast_text}</p></body></html>"

    monkeypatch.setattr(web_tools.httpx, "get", lambda *a, **k: FakeResponse(text=huge_html))
    monkeypatch.setattr(web_tools, "_fetch_with_browser", lambda url: real_html)

    result = web_tools.web_fetch("http://example.com/weather")
    assert "High 76" in result["content"]
    assert "Chevron Left" not in result["content"]


def test_web_fetch_keeps_direct_result_when_browser_fallback_is_not_better(monkeypatch):
    html = "<html><head><title>Short</title></head><body><p>tiny</p></body></html>"
    monkeypatch.setattr(web_tools.httpx, "get", lambda *a, **k: FakeResponse(text=html))
    monkeypatch.setattr(web_tools, "_fetch_with_browser", lambda url: "<html><body></body></html>")  # renders to nothing

    result = web_tools.web_fetch("http://example.com/short")
    assert "tiny" in result["content"]


def test_fetch_with_browser_returns_none_when_playwright_not_installed(monkeypatch):
    monkeypatch.setattr(web_tools, "_PLAYWRIGHT_UNAVAILABLE", False)

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright.sync_api":
            raise ImportError("no module named playwright")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    result = web_tools._fetch_with_browser("http://example.com")
    assert result is None
    assert web_tools._PLAYWRIGHT_UNAVAILABLE is True


def test_fetch_with_browser_short_circuits_once_marked_unavailable(monkeypatch):
    monkeypatch.setattr(web_tools, "_PLAYWRIGHT_UNAVAILABLE", True)
    # No httpx/playwright mocking needed -- should return immediately
    # without attempting any import or network activity at all.
    assert web_tools._fetch_with_browser("http://example.com") is None
