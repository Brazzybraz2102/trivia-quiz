from fastapi.testclient import TestClient

from ticket.scan import ReadBack, RowMark
from ticket.server import create_app

H = {"X-Ticket-Key": "k"}


def client(ctx, rows=None):
    def vision(image, media_type, manifest):
        return ReadBack(label_code=None, rows=[RowMark(**r) for r in rows or []])
    return TestClient(create_app(ctx, vision=vision))


def test_auth_required(ctx):
    c = client(ctx)
    assert c.get("/health").status_code == 200
    assert c.post("/print/today", json={}).status_code == 401
    assert c.post("/print/today", json={}, headers={"X-Ticket-Key": "nope"}).status_code == 401


def test_status_never_leaks_secrets(ctx):
    body = client(ctx).get("/status", headers=H).json()
    assert "printer_reachable" in body
    assert body["config"]["ticket_key"] == "set"
    assert "k" not in body["config"].values()


def test_auto_twice_second_skipped(ctx):
    c = client(ctx)
    assert c.post("/print/today", json={"source": "auto"}, headers=H).json()["status"] == "dry_run"
    assert c.post("/print/today", json={"source": "auto"}, headers=H).json()["status"] == "skipped"


def test_print_types_and_history(ctx):
    c = client(ctx)
    c.post("/print/list", json={"query": "p1"}, headers=H)
    c.post("/print/task", json={"task_id": "1"}, headers=H)
    r = c.post("/print/text", json={"text": "hello"}, headers=H).json()
    assert c.get(f"/printed/{r['id']}/png", params={"k": "k"}).headers["content-type"] == "image/png"
    assert len(c.get("/printed", headers=H).json()) == 3


def test_scan_multipart_then_confirm(ctx, todo):
    c = client(ctx, rows=[{"row": 4, "mark": "drop", "confidence": 0.99}])
    label = c.post("/print/today", json={}, headers=H).json()
    rec = c.post("/scan", headers=H, data={"label_id": label["id"]},
                 files={"photo": ("p.jpg", b"fake", "image/jpeg")}).json()
    assert rec["needs_confirmation"][0]["task_id"] == "4"
    assert not any(call[0] == "delete" for call in todo.calls)
    out = c.post(f"/scan/{rec['id']}/confirm", json={"decisions": {"4": "confirm"}}, headers=H).json()
    assert out["needs_confirmation"][0]["status"] == "applied"
    assert ("delete", "4") in todo.calls


def test_scan_rejects_non_images(ctx):
    r = client(ctx).post("/scan", headers=H, files={"photo": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 415
