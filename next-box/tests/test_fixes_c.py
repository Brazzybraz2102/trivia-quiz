"""Batch C fixes: the admin and superadmin screens (printers, households, people, usage, invites, debug)."""
import csv
import dataclasses
import io
import threading
import time
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from nextbox import db, printer
from nextbox.auth import Accounts
from nextbox.events import Events
from nextbox.printers import Printers
from nextbox.server import create_app


def site(ctx):
    """boss (superadmin), anna (Home's household admin), mike and sam, with My list only."""
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("anna", "anna password", role="admin")
    a.create("mike", "mike password")
    a.create("sam", "sam password")
    app = create_app(ctx)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, login


def audits(ctx, action):
    return [e for e in Events(ctx.settings.data_dir).query(kind="audit", limit=1000) if e["action"] == action]


# --- C-02 Brother model and label size -----------------------------------------------------------
def test_brother_model_and_label_are_sent_and_checked(ctx):
    ctx, a, login = site(ctx)
    anna = login("anna", "anna password")
    bq = anna.get("/printing-options").json()["drivers"]["brother_ql"]
    assert "QL-820NWB" in bq["models"] and {"id": "62", "name": "62 mm continuous (DK-22205)", "width": 696} in bq["labels"]
    base = {"name": "Office QL", "driver": "brother_ql", "address": "192.0.2.50"}
    red = anna.post("/admin/printers", json={**base, "ink": "black_red", "model": "ql-820nwb", "label": "62"})
    assert red.status_code == 200 and red.json()["model"] == "QL-820NWB" and red.json()["ink"] == "black_red"
    assert anna.post("/admin/printers", json={**base, "ink": "black_red"}).status_code == 400   # no model: QL-1110NWB
    narrow = anna.post("/admin/printers", json={**base, "model": "QL-700", "label": "29", "width_px": 306}).json()
    assert (narrow["model"], narrow["label"], narrow["width_px"]) == ("QL-700", "29", 306)
    for bad, says in (({"model": "QL-9000"}, "model from the list"), ({"label": "63"}, "label size from the list"),
                      ({"model": "QL-820NWB", "label": "29", "ink": "black_red"}, "62 mm"),
                      ({"model": "QL-820NWB", "label": "pt12"}, "P-touch"), ({"model": "QL-820NWB", "label": "102"}, "QL-1000")):
        r = anna.post("/admin/printers", json={**base, **bad})
        assert r.status_code == 400 and says in r.json()["detail"], (bad, r.json())
    assert anna.post("/admin/printers", json={**base, "model": "QL-1110NWB", "label": "102"}).status_code == 200


# --- C-03 edit a printer in place; Remove says whose usual printer it is ---------------------------
def test_edit_keeps_the_printer_and_remove_counts_who_uses_it(ctx):
    ctx, a, login = site(ctx)
    anna, mike, sam = login("anna", "anna password"), login("mike", "mike password"), login("sam", "sam password")
    pid = anna.post("/admin/printers", json={"name": "Kitchen", "driver": "escpos", "address": "192.0.2.10"}).json()["id"]
    anna.post("/admin/printers", json={"name": "Office", "driver": "escpos", "address": "192.0.2.11"})
    assert mike.patch("/settings", json={"default_printer": pid}).status_code == 200
    assert sam.patch("/settings", json={"default_printer": pid}).status_code == 200
    r = anna.patch(f"/admin/printers/{pid}", json={"name": "Kitchen QL", "address": "192.0.2.20", "driver": "brother_ql",
                                                    "model": "QL-810W", "label": "62", "ink": "black_red"})
    assert r.status_code == 200 and r.json()["id"] == pid and r.json()["address"] == "192.0.2.20"
    assert mike.get("/auth/me").json()["prefs"]["default_printer"] == pid            # still their usual printer
    counts = {p["name"]: p["usual_for"] for p in anna.get("/printing-options").json()["printers"]}
    assert counts == {"Kitchen QL": 2, "Office": 0}
    assert "usual_for" not in mike.get("/printing-options").json()["printers"][0]     # admins only
    assert anna.patch(f"/admin/printers/{pid}", json={"label": "nope"}).status_code == 400
    assert audits(ctx, "edit_printer")[0]["detail"]["printer"] == pid


# --- C-10 printer checks run side by side --------------------------------------------------------
def test_printer_checks_run_in_parallel_with_a_short_timeout(ctx, monkeypatch):
    ctx, a, login = site(ctx)
    boss = login("boss", "boss password")
    pids = [boss.post("/admin/printers", json={"name": f"P{i}", "driver": "escpos", "address": f"192.0.2.{10 + i}"}).json()["id"]
            for i in range(4)]
    knocks, lock = [], threading.Lock()

    def slow(p, timeout=2.0):
        with lock:
            knocks.append((p.id, timeout))
        time.sleep(0.5)
        return p.id != pids[0]
    monkeypatch.setattr(printer, "is_reachable", slow)
    start = time.monotonic()
    status = boss.get("/admin/printers/status").json()
    assert time.monotonic() - start < 1.5          # one after another would take 2 s
    assert status == {pids[0]: False, pids[1]: True, pids[2]: True, pids[3]: True}
    assert sorted(knocks) == sorted((p, 1.0) for p in pids)
    boss.get("/admin/printers/status")
    d = boss.get("/super/diagnostics").json()      # remembered, not knocked on again
    assert len(knocks) == 4 and [p["reachable"] for p in d["printer"]["printers"]] == [False, True, True, True]
    boss.patch(f"/admin/printers/{pids[0]}", json={"address": "192.0.2.99"})   # a new address is checked again
    boss.get("/admin/printers/status")
    assert len(knocks) == 5 and len(audits(ctx, "printer_check")) == 2


# --- C-04 households: unique names, rename, delete when empty -------------------------------------
def test_households_rename_and_delete_only_when_empty(ctx):
    ctx, a, login = site(ctx)
    boss, anna = login("boss", "boss password"), login("anna", "anna password")
    chen = boss.post("/super/households", json={"name": "Chen family"}).json()
    assert boss.post("/super/households", json={"name": " chen  FAMILY "}).status_code == 400
    r = boss.post("/super/households", json={"name": "x" * 41})
    assert r.status_code == 400 and "40" in r.json()["detail"]
    park = boss.post("/super/households", json={"name": "Park"}).json()
    assert boss.patch(f"/super/households/{park['id']}", json={"name": "Chen Family"}).status_code == 400
    assert boss.patch(f"/super/households/{park['id']}", json={"name": "Park house"}).json()["name"] == "Park house"
    assert boss.patch(f"/super/households/{chen['id']}", json={"name": "Chen family"}).status_code == 200  # its own name
    assert boss.patch("/super/households/nowhere", json={"name": "Z"}).status_code == 404
    assert anna.patch(f"/super/households/{park['id']}", json={"name": "Mine"}).status_code == 403
    assert anna.delete(f"/super/households/{park['id']}").status_code == 403

    a.create("lin", "lin password", household_id=chen["id"])
    r = boss.delete(f"/super/households/{chen['id']}")
    assert r.status_code == 409 and "first" in r.json()["detail"]
    assert boss.delete("/super/households/home").status_code == 409
    code = boss.post("/admin/invites", json={"household_id": park["id"]}).json()["code"]
    Printers(ctx.settings.data_dir, ctx.settings).add({"name": "Park printer", "driver": "escpos", "address": "192.0.2.30"},
                                                      park["id"])
    assert boss.delete(f"/super/households/{park['id']}").json() == {"ok": True}
    assert park["id"] not in {h["id"] for h in boss.get("/super/households").json()}
    with a.db.connect() as conn:
        assert not conn.execute(select(db.invites).where(db.invites.c.code == code)).first()
        assert not conn.execute(select(db.printers).where(db.printers.c.household_id == park["id"])).first()
    assert boss.delete(f"/super/households/{park['id']}").status_code == 404
    assert [e["detail"]["household_id"] for e in audits(ctx, "delete_household")] == [park["id"]]
    assert audits(ctx, "rename_household")


# --- C-07 a superadmin can't force their own password change -------------------------------------
def test_superadmin_cant_sign_themselves_out_with_must_change(ctx):
    ctx, a, login = site(ctx)
    boss = login("boss", "boss password")
    r = boss.patch("/super/users/boss", json={"must_change": True})
    assert r.status_code == 400 and "Profile" in r.json()["detail"]
    assert boss.get("/auth/me").status_code == 200 and not a.get("boss")["must_change"]
    assert boss.patch("/super/users/sam", json={"must_change": True}).status_code == 200   # others: as before


# --- C-09 usage covers the whole period, without a 5000-event cut-off ------------------------------
def test_usage_series_covers_the_period_and_counts_every_event(ctx):
    ctx, a, login = site(ctx)
    boss = login("boss", "boss password")
    ev = Events(ctx.settings.data_dir)
    ev.log("sam", "brain_dump")
    now = time.time()
    with a.db.begin() as conn:  # a busy server: lots of newer events of other kinds
        conn.execute(insert(db.events), [{"ts": now, "user": "mike", "kind": "error", "action": "x", "ok": False}
                                         for _ in range(6000)])
    u = boss.get("/super/usage", params={"days": 30}).json()
    assert u["bucket"] == "day" and len(u["series"]) == 30 and u["series"][-1]["date"] == date.today().isoformat()
    sam = {p["username"]: p for p in u["people"]}["sam"]
    assert dict(sam["top_actions"]).get("brain_dump") == 1 and sam["consented"] is False
    assert len(boss.get("/super/usage", params={"days": 7}).json()["series"]) == 7
    u = boss.get("/super/usage", params={"days": 90}).json()
    assert u["bucket"] == "week" and 13 <= len(u["series"]) <= 14
    assert all(date.fromisoformat(d["date"]).weekday() == 0 for d in u["series"])
    first = date.fromisoformat(u["series"][0]["date"])
    assert date.today() - timedelta(days=95) <= first <= date.today() - timedelta(days=89)  # the Monday of day one


# --- C-11 the users CSV reads well in a spreadsheet ------------------------------------------------
def test_users_csv_has_dates_yes_no_and_household_ids(ctx):
    ctx, a, login = site(ctx)
    boss = login("boss", "boss password")
    a.update("sam", email="=cmd@example.com")
    r = boss.get("/super/users.csv")
    assert r.content.startswith(b"\xef\xbb\xbf")
    assert f"nextbox-users-{date.today().isoformat()}.csv" in r.headers["content-disposition"]
    rows = {row["username"]: row for row in csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")))}
    assert list(rows["boss"])[:6] == ["username", "email", "role", "household", "household_id", "protected"]
    boss_row, sam_row = rows["boss"], rows["sam"]
    assert boss_row["household_id"] == "home" and boss_row["disabled"] == "no"
    assert sam_row["email"] == "'=cmd@example.com" and sam_row["last_login"] == "" and sam_row["consented"] == "no"
    assert len(boss_row["created"]) == 16 and boss_row["created"][4] == "-" and boss_row["last_active"]
    assert "hash" not in r.text and "salt" not in r.text


# --- C-13 invites: cancelling a used one says so; old ones can be cleared ---------------------------
def test_invites_cancel_used_or_expired_is_refused_and_old_ones_clear(ctx):
    ctx, a, login = site(ctx)
    boss, anna = login("boss", "boss password"), login("anna", "anna password")
    used = anna.post("/admin/invites", json={"note": "Kim"}).json()["code"]
    gone = anna.post("/admin/invites", json={"note": "Lee"}).json()["code"]
    still = anna.post("/admin/invites", json={"note": "Max"}).json()["code"]
    other = a.create_household("Elsewhere")
    theirs = boss.post("/admin/invites", json={"household_id": other}).json()["code"]
    TestClient(boss.app, base_url="http://testserver").post(
        "/auth/signup", json={"username": "kim", "password": "kim password", "invite": used, "agree": True})
    with a.db.begin() as conn:
        conn.execute(update(db.invites).where(db.invites.c.code.in_([gone, theirs])).values(expires=int(time.time()) - 60))
    r = anna.delete(f"/admin/invites/{used}")
    assert r.status_code == 409 and "already used" in r.json()["detail"]
    r = anna.delete(f"/admin/invites/{gone}")
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    assert anna.post("/admin/invites/clear").json() == {"removed": 2}
    assert [i["code"] for i in anna.get("/admin/invites").json()] == [still]       # open codes stay
    assert theirs in {i["code"] for i in boss.get("/admin/invites").json()}       # another household's: not hers
    assert boss.post("/admin/invites/clear").json() == {"removed": 1}
    assert login("mike", "mike password").post("/admin/invites/clear").status_code == 403
    assert anna.delete(f"/admin/invites/{still}").status_code == 200
    assert audits(ctx, "clear_invites")


# --- C-16 diagnostics say what's really going on --------------------------------------------------
def test_diagnostics_name_the_linked_app_printers_and_auto_print(ctx, monkeypatch):
    ctx, a, login = site(ctx)
    boss = login("boss", "boss password")
    monkeypatch.setattr(printer, "is_reachable", lambda p, timeout=2.0: p.driver == "brother_ql")
    boss.post("/admin/printers", json={"name": "Office QL", "driver": "brother_ql", "address": "192.0.2.50", "label": "29"})
    boss.post("/admin/printers", json={"name": "Receipt", "driver": "escpos", "address": "192.0.2.60"})
    d = boss.get("/super/diagnostics").json()
    assert d["todoist"]["ok"] and d["todoist"]["linked"] is False and d["todoist"]["app"] is None
    assert d["printer"]["printers"] == [
        {"name": "Office QL", "driver": "brother_ql", "stock_color": "white", "label": "29", "reachable": True},
        {"name": "Receipt", "driver": "escpos", "stock_color": "white", "label": None, "reachable": False}]
    assert d["auto_print_last"] == {"enabled": True, "real": None, "dry": None}
    ctx.store.claim_auto("2026-10-01", dry_run=True, scope="boss")
    ctx.store.claim_auto("2026-10-02", dry_run=True, scope="mike")
    assert boss.get("/super/diagnostics").json()["auto_print_last"] == {"enabled": True, "real": None, "dry": "2026-10-02"}
