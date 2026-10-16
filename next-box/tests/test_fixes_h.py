"""Global UI, accessibility and the static pages: fixes from the H batch of the audit."""
import dataclasses
import io
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nextbox import server
from nextbox.auth import Accounts
from nextbox.server import create_app

WEB = Path(server.WEB_DIR)


@pytest.fixture
def site(ctx):
    """boss (superadmin) and sam (user), My list only."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    app = create_app(ctx)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return a, TestClient(app, base_url="http://testserver"), login


# ------------------------------------------------------------------ H-01: the Privacy Policy tells the truth
def test_privacy_policy_covers_the_journal_and_what_claude_reads(site):
    a, anon, login = site
    page = anon.get("/app/privacy.html").text
    assert "The photo itself is not kept" not in page
    assert "Your journal" in page and "Only you can see them" in page
    assert "transcribes your handwritten notes" in page
    assert "Delete my account" in page and "journal pages and photos" in page
    # No service named that this copy may not use: hosting comes from the operator's .env.
    assert "Cloudflare" not in page and 'data-needs="hosting"' in page
    about = anon.get("/app/about.html").text
    assert "Cloudflare" not in about and "PostgreSQL" not in about and "data-db" in about
    snap = anon.get("/app/").text
    assert "Your photo is read by Claude (Anthropic)." in snap


def test_about_says_which_database_and_host_this_copy_really_uses(site, monkeypatch):
    a, anon, login = site
    facts = anon.get("/about").json()
    assert facts["database"] in ("SQLite", "PostgreSQL") and facts["hosting"] == ""
    monkeypatch.setenv("NEXTBOX_HOSTING", "  Cloudflare  ")
    assert anon.get("/about").json()["hosting"] == "Cloudflare"
    monkeypatch.setenv("NEXTBOX_HOSTING", "x" * 500)
    assert len(anon.get("/about").json()["hosting"]) == 80  # a short name, never a wall of text


def test_new_privacy_policy_is_agreed_to_again(site):
    """The Terms version went up with the new policy: everyone who agreed to the old one is asked again."""
    a, anon, login = site
    assert server.TERMS_VERSION >= 2
    a.record_consent("sam", server.DATA_NOTICE_VERSION, terms=1)  # agreed before the Journal was covered
    sam = login("sam", "sam password")
    me = sam.get("/auth/me").json()
    assert me["consented"] is True and me["terms_ok"] is False and me["notice_changed"] is True
    assert sam.post("/auth/consent").status_code == 200
    me = sam.get("/auth/me").json()
    assert me["terms_ok"] is True and me["notice_changed"] is False


# ------------------------------------------------------------------ H-29: open the app at Snap
def test_open_to_can_be_snap(site):
    a, anon, login = site
    sam = login("sam", "sam password")
    assert sam.patch("/settings", json={"start_tab": "scan"}).json()["start_tab"] == "scan"
    assert sam.get("/auth/me").json()["prefs"]["start_tab"] == "scan"
    assert sam.patch("/settings", json={"start_tab": "admin"}).status_code == 400
    assert '<option value="scan">Snap</option>' in anon.get("/app/").text


# ------------------------------------------------------------------ H-23: the 404 page works anywhere
def test_404_page_links_and_styles_work_from_any_address(site):
    a, anon, login = site
    for path in ("/nothing-here", "/app/a/b/c", "/deep/down/page.html"):
        page = anon.get(path, headers={"accept": "text/html"})
        assert page.status_code == 404 and "couldn't find that page" in page.text
        refs = re.findall(r'(?:href|src)="([^"]+)"', page.text)
        assert refs and all(r.startswith("/app/") for r in refs), refs  # never relative to the missing address
        for r in {r for r in refs if not r.endswith("/")}:
            assert anon.get(r).status_code == 200, r


# ------------------------------------------------------------------ H-27: help and legal pages follow the app's theme
def test_help_and_legal_pages_set_the_theme_before_they_show(site):
    a, anon, login = site
    for name in ("privacy.html", "terms.html", "about.html", "guide.html", "404.html"):
        page = (WEB / name).read_text()
        head = page.split("</head>")[0]
        assert re.search(r'<script src="(/app/)?pages\.js"></script>', head), name  # in <head>: no flash
        assert "pages.js" not in page.split("</head>")[1], name
    js = anon.get("/app/pages.js").text
    assert 'localStorage.getItem("theme")' in js and "dataset.theme" in js
    css = anon.get("/app/pages.css").text
    assert ':root[data-theme="dark"]' in css and ':not([data-theme="light"])' in css and "--onAccent" in css
    guide = anon.get("/app/guide.html").text
    assert '<header>' in guide and 'href="privacy.html"' in guide and "Print just this" in guide
    assert "Print it big" not in guide


# ------------------------------------------------------------------ H-28: home-screen icons
def test_home_screen_icons_and_manifest(site):
    from PIL import Image
    a, anon, login = site
    m = anon.get("/app/manifest.json").json()
    assert m["id"] == "/app/" and m["description"]
    purposes = {i["src"]: i.get("purpose", "any") for i in m["icons"]}
    assert purposes["icons/icon-512-maskable.png"] == "maskable"
    assert "maskable" not in purposes["icons/icon-512.png"]  # one image can't be both
    for src in purposes:
        assert anon.get(f"/app/{src}").status_code == 200
    # iOS fills transparent corners with black: the apple-touch icon is opaque to the edges.
    for name in ("icon-180.png", "icon-512-maskable.png"):
        img = Image.open(io.BytesIO(anon.get(f"/app/icons/{name}").content))
        assert img.mode == "RGB" or img.convert("RGBA").getextrema()[3][0] == 255, name
    html = anon.get("/app/").text
    assert 'name="mobile-web-app-capable"' in html and 'name="color-scheme"' in html
    assert html.count('name="theme-color"') == 2 and 'media="(prefers-color-scheme: dark)"' in html


# ------------------------------------------------------------------ H-25, H-26, H-16, H-07: words and names
def test_one_name_per_action_and_named_controls(site):
    a, anon, login = site
    html = anon.get("/app/").text
    assert "Print it big" not in html and "Print as one thing" not in html
    assert html.count(">Print just this<") == 2  # Just one thing and the Edit task dialog
    assert '<button data-tab="board">Feedback board</button>' in html and ">Send feedback</button>" in html
    for dialog in ("meDialog", "userDialog", "taskDialog"):
        assert re.search(rf'<dialog id="{dialog}" aria-labelledby="\w+"', html), dialog
    assert 'aria-describedby="noticeBody"' in html
    assert not re.search(r'<button data-me="\w+"[^>]*>[^<]', html)  # menu emoji are hidden from screen readers
    for field in ("pwCur", "pwNew", "newTag", "nuName", "jText"):
        assert re.search(rf'<label[^>]* for="{field}"', html), field  # a visible label, not just a placeholder
    assert 'role="tablist"' not in html and 'aria-pressed="true"' in html
