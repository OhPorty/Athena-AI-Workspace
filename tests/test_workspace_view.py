"""Viewing a workspace page in its own tab: served from a secret link, always inside a browser sandbox."""
import os
import time

import pytest
from fastapi.testclient import TestClient

import auth_routes
import workspace_view
import ownership

MAIN_PW = "Entrance#Lemon-4821"


def _client():
    import main
    return TestClient(main.app, base_url="https://testserver", raise_server_exceptions=False)


@pytest.fixture
def signed_in():
    c = _client()
    assert c.post("/api/auth/setup", json={"username": "boss", "password": MAIN_PW}).status_code == 200
    return c


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "game"
    (root / "css").mkdir(parents=True)
    (root / "index.html").write_text('<link rel="stylesheet" href="css/style.css"><script src="script.js"></script><p>hi</p>')
    (root / "css" / "style.css").write_text("p { color: red; }")
    (root / "script.js").write_text("console.log('x')")
    (root / "about.htm").write_text("<p>about</p>")
    (root / ".env").write_text("SECRET=1")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]")
    (tmp_path / "outside.txt").write_text("not in the workspace")
    os.symlink(str(tmp_path / "outside.txt"), root / "link.txt")
    return str(root)


def link_for(client, root, path="index.html"):
    got = client.post("/api/workspace/view-link", json={"workspace": root, "path": path})
    assert got.status_code == 200, got.text
    return got.json()["url"]


def test_a_link_serves_the_page_and_the_files_it_refers_to_with_the_sandbox_on_every_response(signed_in, site):
    url = link_for(signed_in, site)
    assert url.startswith("/workspace-view/") and url.endswith("/index.html")
    anonymous = _client()                                                                        # the link is the key: no login needed to follow it
    page = anonymous.get(url)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html") and "<p>hi</p>" in page.text
    base = url.rsplit("/", 1)[0]
    for rel, kind in (("css/style.css", "text/css"), ("script.js", "javascript")):
        got = anonymous.get(f"{base}/{rel}")
        assert got.status_code == 200 and kind in got.headers["content-type"]
    for response in (page, anonymous.get(f"{base}/css/style.css"), anonymous.get(f"{base}/missing.html")):
        csp = response.headers["content-security-policy"]
        assert csp.startswith("sandbox allow-scripts") and "allow-same-origin" not in csp and "frame-ancestors 'none'" in csp
        assert response.headers["x-content-type-options"] == "nosniff" and response.headers["cross-origin-opener-policy"] == "same-origin"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_the_folder_link_opens_index_html_and_other_html_extensions_work(signed_in, site):
    base = link_for(signed_in, site).rsplit("/", 1)[0]
    assert "<p>hi</p>" in _client().get(base + "/").text
    assert "<p>about</p>" in _client().get(link_for(signed_in, site, "about.htm")).text


def test_hidden_files_links_out_of_the_folder_and_traversal_are_not_served(signed_in, site):
    base = link_for(signed_in, site).rsplit("/", 1)[0]
    anon = _client()
    for rel in (".env", ".git/config", "link.txt", "../outside.txt", "%2e%2e/outside.txt", "css/../../outside.txt"):
        assert anon.get(f"{base}/{rel}").status_code == 404, rel
    assert "SECRET" not in anon.get(f"{base}/.env").text


def test_only_html_files_get_a_link_and_a_login_is_needed_to_make_one(signed_in, site):
    assert signed_in.post("/api/workspace/view-link", json={"workspace": site, "path": "script.js"}).status_code == 400
    assert signed_in.post("/api/workspace/view-link", json={"workspace": site, "path": "nope.html"}).status_code == 400
    assert signed_in.post("/api/workspace/view-link", json={"workspace": site, "path": "../outside.txt"}).status_code == 400
    assert _client().post("/api/workspace/view-link", json={"workspace": site, "path": "index.html"}).status_code == 401


def test_an_unknown_or_expired_link_says_so_and_stays_sandboxed(signed_in, site, monkeypatch):
    anon = _client()
    gone = anon.get("/workspace-view/not-a-real-token/index.html")
    assert gone.status_code == 404 and "expired" in gone.text and "sandbox" in gone.headers["content-security-policy"]
    url = link_for(signed_in, site)
    assert anon.get(url).status_code == 200
    real_time = time.time
    monkeypatch.setattr(workspace_view.time, "time", lambda: real_time() + workspace_view.LINK_SECONDS + 5)
    assert anon.get(url).status_code == 404


def test_a_limited_account_can_only_link_pages_inside_its_own_folder(signed_in, site):
    import auth_routes as routes
    made = routes.auth_manager.create_user("bob", "temp password 1")
    routes.auth_manager.change_password("bob", made["temporary_password"], "Own#Phrase-Zebra-22")
    routes.auth_manager._users()["bob"]["must_setup_totp"] = False
    bob = _client()
    assert bob.post("/api/auth/login", json={"username": "bob", "password": "Own#Phrase-Zebra-22"}).status_code == 200
    assert bob.post("/api/workspace/view-link", json={"workspace": site, "path": "index.html"}).status_code == 403
    mine = ownership.workspace_root("bob") + "/page"
    os.makedirs(mine)
    open(mine + "/index.html", "w").write("<p>mine</p>")
    assert "<p>mine</p>" in _client().get(link_for(bob, mine)).text


def test_the_page_viewer_is_one_of_the_files_athenas_own_tools_cannot_edit():
    import safety
    assert safety.classify(os.path.join(safety.ATHENA_ROOT, "workspace_view.py")) == "readonly"
    assert "/workspace-view" in auth_routes.AUTH_EXEMPT_PREFIXES
