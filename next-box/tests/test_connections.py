"""Each person uses their own to-do app, sees only their own tickets, and the audit fixes hold."""
import dataclasses
import os
import stat
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import SAMPLE, FakeTodoist, tiny_jpeg
from nextbox import jobs, printer, scan
from nextbox.auth import Accounts
from nextbox.providers import ProviderError
from nextbox.scan import ReadBack, RowMark
from nextbox.server import create_app
from nextbox.store import Store

H = {"X-NextBox-Key": "k"}


class Apps:
    """A fake builder: one FakeTodoist per token, so each person's tasks are distinct."""
    def __init__(self):
        self.by_token: dict[str, FakeTodoist] = {}
        self.fail = False

    def __call__(self, provider, config, secrets):
        token = secrets.get("token") or secrets.get("password")
        if self.fail or token == "bad":
            class Bad(FakeTodoist):
                def check(self):
                    raise ProviderError("Todoist rejected the token. Reconnect Todoist in Settings.")
            return Bad()
        if token not in self.by_token:
            self.by_token[token] = FakeTodoist([{**t, "content": f"{token}: {t['content']}"} for t in SAMPLE])
        return self.by_token[token]


@pytest.fixture
def world(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None)  # no shared app: per-person connections
    a = Accounts(ctx.settings.data_dir)
    a.set_password("mike", "mike password", create=True)  # owner
    a.create("sam", "sam password")
    apps = Apps()

    def vision(image, media_type, manifest):
        return ReadBack(label_code=None, rows=[RowMark(row=1, mark="drop", confidence=0.99)])
    app = create_app(ctx, vision=vision, builder=apps)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, apps, app, login


def test_not_connected_is_a_clear_409(world):
    ctx, a, apps, app, login = world
    sam = login("sam", "sam password")
    r = sam.post("/print/today", json={})
    assert r.status_code == 409 and "Connect your to-do app" in r.json()["detail"]
    assert sam.get("/connection").json() == {"connected": False}


def test_connect_checks_first_and_never_stores_the_token_in_clear(world):
    ctx, a, apps, app, login = world
    sam = login("sam", "sam password")
    r = sam.put("/connection", json={"provider": "todoist", "fields": {"token": "bad"}})
    assert r.status_code == 400 and "rejected" in r.json()["detail"]
    assert sam.get("/connection").json()["connected"] is False
    assert sam.put("/connection", json={"provider": "todoist", "fields": {}}).status_code == 400
    assert sam.put("/connection", json={"provider": "things", "fields": {}}).status_code == 400

    st = sam.put("/connection", json={"provider": "todoist", "fields": {"token": "sam-secret-token"}}).json()
    assert st["connected"] and st["name"] == "Todoist" and st["account"] == "fake@example.com"
    assert "secret" not in st and "sam-secret-token" not in str(st)
    from conftest import dump
    assert "sam-secret-token" not in dump(ctx.settings.data_dir)
    assert oct(stat.S_IMODE((ctx.settings.data_dir / "secret.key").stat().st_mode)) == "0o600"
    # Changing the server key (NEXTBOX_KEY) doesn't break saved connections.
    ctx.settings.server_key = "rotated"
    assert sam.post("/print/today", json={}).json()["manifest"][0]["content"].startswith("sam-secret-token")
    assert sam.delete("/connection").json() == {"connected": False}


def test_each_person_prints_their_own_tasks_and_sees_only_their_tickets(world):
    ctx, a, apps, app, login = world
    mike, sam = login("mike", "mike password"), login("sam", "sam password")
    mike.put("/connection", json={"provider": "todoist", "fields": {"token": "mike"}})
    sam.put("/connection", json={"provider": "caldav", "fields": {
        "url": "https://dav.example.com", "username": "sam", "password": "samdav"}})
    m = mike.post("/print/today", json={}).json()
    s = sam.post("/print/today", json={}).json()
    assert all(r["content"].startswith("mike:") for r in m["manifest"])
    assert all(r["content"].startswith("samdav:") for r in s["manifest"])
    assert [r["id"] for r in sam.get("/printed").json()] == [s["id"]]
    assert sam.get(f"/printed/{m['id']}/png").status_code == 404
    assert mike.get(f"/printed/{m['id']}/png").status_code == 200

    # Sam photographs Mike's ticket: it doesn't match, and nothing in anyone's app changes.
    rec = sam.post("/scan", data={"label_id": m["id"]},
                   files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).json()
    assert rec["label_id"] is None and rec["errors"]
    # Mike's own scan can't be read or confirmed by Sam.
    mrec = mike.post("/scan", data={"label_id": m["id"]},
                     files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).json()
    assert sam.get(f"/scan/{mrec['id']}").status_code == 404
    assert sam.post(f"/scan/{mrec['id']}/confirm", json={"decisions": {"1": "confirm"}}).status_code == 404
    assert not any(c[0] == "delete" for f in apps.by_token.values() for c in f.calls)
    users = {u["username"]: u for u in mike.get("/admin/users").json()}
    assert users["sam"]["app"] == "CalDAV task list"


def test_home_assistant_and_legacy_token_act_as_the_owner(world):
    ctx, a, apps, app, login = world
    ctx.settings.todoist_token = "legacy-env"
    client = TestClient(app, base_url="http://testserver")
    r = client.post("/print/today", json={}, headers=H).json()
    assert r["by"] == "mike" and r["manifest"][0]["content"].startswith("legacy-env")
    # ...but the .env token never serves anyone else.
    assert login("sam", "sam password").post("/print/today", json={}).status_code == 409
    assert login("mike", "mike password").get("/connection").json()["legacy"] is True


def test_cli_connect_and_print_as_a_user(world, monkeypatch, capsys):
    ctx, a, apps, app, login = world
    from nextbox import cli, connections
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(ctx.settings.data_dir))
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(ctx.settings.data_dir / "none.env"))
    monkeypatch.setenv("NEXTBOX_DRY_RUN", "1")
    monkeypatch.setattr(connections, "build_provider", apps)
    monkeypatch.setattr(connections.Connections.__init__, "__defaults__", (apps,))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "cli-token")
    assert cli.main(["connect", "sam", "todoist"]) == 0
    assert cli.main(["--user", "sam", "--json", "today"]) == 0
    assert '"by": "sam"' in capsys.readouterr().out


# --- audit fixes --------------------------------------------------------------
def test_temp_password_must_be_changed_before_anything_else(world):
    ctx, a, apps, app, login = world
    a.set_password("sam", "temp-pass-1", must_change=True)
    sam = login("sam", "temp-pass-1")
    r = sam.post("/print/text", json={"text": "x"})
    assert r.status_code == 403 and "password" in r.json()["detail"]
    assert sam.get("/auth/me").status_code == 200
    assert sam.post("/auth/password", json={"current": "temp-pass-1", "new": "sams own pass"}).status_code == 200
    assert sam.get("/settings").status_code == 200


def test_turned_off_message_only_with_the_right_password(world):
    ctx, a, apps, app, login = world
    a.update("sam", disabled=True)
    c = TestClient(app, base_url="http://testserver")
    wrong = [c.post("/auth/login", json={"username": "sam", "password": "nope"}).status_code for _ in range(6)]
    assert wrong[:5] == [401] * 5 and wrong[5] == 429


def test_admins_cant_read_a_superadmins_activity(world):
    ctx, a, apps, app, login = world
    a.create("helper", "helper pass", role="admin")
    helper = login("helper", "helper pass")
    assert helper.get("/admin/users/mike/activity").status_code == 403
    assert helper.get("/admin/users/sam/activity").status_code == 200
    assert helper.get("/admin/users/helper/activity").status_code == 200


def test_security_headers(world):
    r = TestClient(world[3]).get("/app/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_one_print_job_at_a_time(tmp_path, monkeypatch):
    from nextbox.config import Settings
    active, peak = [0], [0]

    def slow(img, settings):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.1)
        active[0] -= 1
    monkeypatch.setattr(printer, "_send_raster", slow)
    s = Settings(printer_ip="10.0.0.9", data_dir=tmp_path)
    ts = [threading.Thread(target=printer.print_image, args=(Image.new("1", (696, 20), 1), s, False))
          for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1


def test_auto_print_waits_for_the_printer(ctx, monkeypatch):
    ctx.settings.dry_run = False
    ctx.settings.printer_ip = "10.0.0.9"
    up = {"v": False}
    monkeypatch.setattr(printer, "is_reachable", lambda ip, timeout=2.0: up["v"])
    monkeypatch.setattr(printer, "_send_raster", lambda img, s: None)
    first = jobs.print_today(ctx, source="auto")
    assert first["status"] == "skipped" and "offline" in first["reason"]
    up["v"] = True
    assert jobs.print_today(ctx, source="auto")["status"] == "printed"
    assert jobs.print_today(ctx, source="auto")["status"] == "skipped"


def test_data_dir_is_private_and_ids_never_collide(tmp_path, monkeypatch):
    store = Store(tmp_path / "d")
    assert oct(stat.S_IMODE((tmp_path / "d").stat().st_mode)) == "0o700"
    picks = iter("AAAA" + "AAAA" + "BBBB")
    monkeypatch.setattr("nextbox.store.secrets.choice", lambda alphabet: next(picks))
    first = store.new_id(date(2026, 10, 5))
    store.save_printed({"id": first, "created_at": "2026-10-05T09:00:00", "manifest": []})
    assert store.new_id(date(2026, 10, 5)) == "261005-BBBB"


def test_prune_removes_old_tickets_only(tmp_path):
    store = Store(tmp_path)
    for label_id, when in (("OLD-1", "2020-01-01T00:00:00"), ("NEW-1", "2099-01-01T00:00:00")):
        store.save_printed({"id": label_id, "created_at": when, "manifest": []})
        store.png_path(label_id).write_bytes(b"png")
    assert store.prune(90) == {"printed": 1, "scans": 0}
    assert store.get_printed("NEW-1") and not store.png_path("OLD-1").exists()


def test_photos_are_rotated_resized_and_bombs_refused():
    import io
    big = io.BytesIO()
    Image.new("RGB", (4000, 3000), "white").save(big, "JPEG")
    data, media = scan.prepare_photo(big.getvalue())
    assert media == "image/jpeg" and max(Image.open(io.BytesIO(data)).size) == scan.MAX_EDGE
    bomb = io.BytesIO()
    Image.new("1", (8000, 8000)).save(bomb, "PNG")
    with pytest.raises(scan.PhotoError):
        scan.prepare_photo(bomb.getvalue())


def test_sessions_slide_while_in_use(world):
    ctx, a, apps, app, login = world
    from sqlalchemy import update

    from conftest import rows
    from nextbox import db
    sam = login("sam", "sam password")
    with db.database(ctx.settings.data_dir).begin() as conn:
        conn.execute(update(db.sessions).values(expires=int(time.time()) + 3600))  # about to expire
    assert sam.get("/auth/me").status_code == 200
    assert min(r["expires"] for r in rows(ctx.settings.data_dir, "sessions")) > time.time() + 20 * 86400
