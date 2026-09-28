from ticket import jobs, scan
from ticket.scan import ReadBack, RowMark


def fake_vision(rows, code=None):
    def run(image, media_type, manifest):
        return ReadBack(label_code=code, rows=[RowMark(**r) for r in rows])
    return run


def test_scan_applies_confident_and_holds_drops(ctx, todo):
    label = jobs.print_today(ctx)  # rows: 1=#3, 2=#1, 3=#2 (recurring), 4=#4
    vision = fake_vision([
        {"row": 1, "mark": "done", "confidence": 0.95},
        {"row": 2, "mark": "tomorrow", "confidence": 0.9},
        {"row": 3, "mark": "tomorrow", "confidence": 0.9},
        {"row": 4, "mark": "drop", "confidence": 0.99},
        {"row": 9, "mark": "done", "confidence": 0.99},
    ], code=label["id"])
    rec = scan.scan_photo(ctx, vision, b"img", "image/jpeg")

    assert ("close", "3") in todo.calls
    assert ("due_date", "1", "2026-09-29") in todo.calls
    # recurring task moved with due_date, never due_string
    assert ("due_date", "2", "2026-09-29") in todo.calls
    # drop is never applied without confirmation
    assert not any(c[0] == "delete" for c in todo.calls)
    assert [p["task_id"] for p in rec["needs_confirmation"]] == ["4"]
    assert rec["ignored"][0]["row"] == 9


def test_low_confidence_needs_confirmation(ctx, todo):
    label = jobs.print_today(ctx)
    vision = fake_vision([{"row": 1, "mark": "done", "confidence": 0.5}])
    rec = scan.scan_photo(ctx, vision, b"img", "image/jpeg", label_id=label["id"])
    assert rec["applied"] == []
    assert rec["needs_confirmation"][0]["mark"] == "done"
    assert not any(c[0] == "close" for c in todo.calls)


def test_confirm_and_skip_are_one_shot(ctx, todo):
    label = jobs.print_today(ctx)
    vision = fake_vision([
        {"row": 4, "mark": "drop", "confidence": 0.99},
        {"row": 1, "mark": "done", "confidence": 0.4},
    ], code=label["id"])
    rec = scan.scan_photo(ctx, vision, b"img", "image/jpeg")
    rec = scan.confirm(ctx, rec["id"], {4: "confirm", 1: "skip"})
    assert todo.calls.count(("delete", "4")) == 1
    assert not any(c[0] == "close" for c in todo.calls)
    # second tap does nothing
    scan.confirm(ctx, rec["id"], {4: "confirm", 1: "confirm"})
    assert todo.calls.count(("delete", "4")) == 1
    assert not any(c[0] == "close" for c in todo.calls)


def test_unmatched_photo_changes_nothing(ctx, todo):
    rec = scan.scan_photo(ctx, fake_vision([{"row": 1, "mark": "drop", "confidence": 1}], "zzzzzz"),
                          b"img", "image/jpeg")
    assert rec["errors"] and rec["label_id"] is None
    assert [c for c in todo.calls if c[0] != "filter"] == []
