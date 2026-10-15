"""Print on paper: any printer, via a PDF page."""
import dataclasses

from fastapi.testclient import TestClient
from PIL import Image

from nextbox import paper
from nextbox.auth import Accounts
from nextbox.server import create_app


def test_sticker_sheet_keeps_real_size_and_flows_onto_more_pages():
    stickers = [Image.new("L", (696, 300), 0) for _ in range(30)]
    pages = paper.sticker_sheets(stickers, "letter")
    assert len(pages) >= 2 and pages[0].size == paper.PAGES["letter"]
    assert pages[0].getpixel((paper.MARGIN + 10, paper.MARGIN + 10)) == 0  # placed at real size, not shrunk
    tall = paper.single_pages(Image.new("L", (696, 8000), 0), "a4")
    assert len(tall) > 1 and tall[0].size == paper.PAGES["a4"]
    assert paper.to_pdf(pages).startswith(b"%PDF")


def test_print_on_paper_endpoint(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("sam", "sam password", create=True)
    a.create("kim", "kim password")
    app = create_app(ctx)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        c.post("/auth/login", json={"username": name, "password": pw})
        return c
    sam, kim = login("sam", "sam password"), login("kim", "kim password")
    sam.post("/list/dump", json={"text": "Pay bill today\nCall dentist today\nWater plants today"})
    stickers = sam.post("/print/stickers", json={}).json()
    today = sam.post("/print/today", json={}).json()
    r = sam.get(f"/printed/{stickers['id']}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert ctx.store.get_parts(stickers["id"]) and len(ctx.store.get_parts(stickers["id"])) == 3
    assert sam.get(f"/printed/{today['id']}/pdf?paper=a4").status_code == 200
    assert sam.get(f"/printed/{today['id']}/pdf?paper=tabloid").status_code == 400
    assert kim.get(f"/printed/{today['id']}/pdf").status_code == 404  # someone else's ticket
    assert sam.patch("/settings", json={"paper_size": "a4"}).json()["paper_size"] == "a4"
