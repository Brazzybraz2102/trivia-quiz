"""The normal app pages: Privacy, Terms, About, a friendly 404, and agreeing to terms at sign-up."""
import dataclasses

from fastapi.testclient import TestClient

from nextbox.auth import Accounts
from nextbox.server import create_app


def test_pages_need_no_sign_in(ctx, monkeypatch):
    monkeypatch.setenv("NEXTBOX_OPERATOR", "Braznarkel")
    monkeypatch.setenv("NEXTBOX_CONTACT", "mike@braznarkel.com")
    c = TestClient(create_app(ctx))
    for page, words in (("privacy.html", "Privacy Policy"), ("terms.html", "Terms of Use"),
                        ("about.html", "About Next Box"), ("guide.html", "quick start")):
        r = c.get(f"/app/{page}")
        assert r.status_code == 200 and words in r.text
    a = c.get("/about").json()
    assert a["operator"] == "Braznarkel" and a["contact"] == "mike@braznarkel.com" and a["retention_days"] == 90


def test_friendly_404_for_people_json_for_apps(ctx):
    c = TestClient(create_app(ctx))
    page = c.get("/app/nope.html", headers={"accept": "text/html"})
    assert page.status_code == 404 and "couldn't find that page" in page.text
    assert c.get("/nope", headers={"accept": "text/html"}).text.count("couldn't find that page") == 1
    api = c.get("/nope", headers={"accept": "application/json"})
    assert api.status_code == 404 and api.json() == {"detail": "Not Found"}


def test_joining_needs_agreement(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    code = a.create_invite("home", "boss")
    c = TestClient(create_app(ctx), base_url="http://testserver")
    r = c.post("/auth/signup", json={"username": "sam", "password": "sam password", "invite": code})
    assert r.status_code == 400 and "Terms" in r.json()["detail"]
    assert c.post("/auth/signup", json={"username": "sam", "password": "sam password", "invite": code,
                                         "agree": True}).status_code == 200
