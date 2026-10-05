from PIL import Image

from nextbox import jobs
from nextbox.render import WIDTH


def test_today_dry_run_renders_png_and_manifest(ctx):
    r = jobs.print_today(ctx)
    assert r["status"] == "dry_run"
    img = Image.open(r["png"])
    assert img.width == WIDTH and img.height > 200
    # urgent first (#1 p1), then the rest of today (#2, #4), then overdue (#3)
    assert [m["task_id"] for m in r["manifest"]] == ["1", "2", "4", "3"]
    assert [m["row"] for m in r["manifest"]] == [1, 2, 3, 4]
    assert ctx.store.get_printed(r["id"])["manifest"] == r["manifest"]


def test_auto_prints_once_per_day(ctx):
    first = jobs.print_today(ctx, source="auto")
    second = jobs.print_today(ctx, source="auto")
    assert first["status"] == "dry_run"
    assert second["status"] == "skipped"
    # manual prints are never blocked
    assert jobs.print_today(ctx)["status"] == "dry_run"


def test_dry_auto_does_not_use_up_real_auto(ctx):
    jobs.print_today(ctx, source="auto")
    assert ctx.store.claim_auto("2026-09-28", dry_run=False) is True


def test_auto_guard_released_if_todoist_fails(ctx, todo):
    def fail(q):
        raise RuntimeError("todoist down")
    todo.filter_tasks = fail
    try:
        jobs.print_today(ctx, source="auto")
    except RuntimeError:
        pass
    todo.filter_tasks = lambda q: []
    assert jobs.print_today(ctx, source="auto")["status"] == "dry_run"


def test_list_task_text_and_todo(ctx, todo):
    assert jobs.print_filter(ctx, "#Groceries", "GROCERIES")["kind"] == "list"
    assert jobs.print_task(ctx, "1")["manifest"][0]["task_id"] == "1"
    note = jobs.print_text(ctx, "Pick up keys\nfrom Sam")
    assert note["manifest"] == [] and note["text"].startswith("Pick up")
    added = jobs.add_and_print(ctx, "Buy tape\n62mm DK-22205")
    assert ("add", "Buy tape") in todo.calls
    assert added["kind"] == "task"


def test_long_words_wrap_without_overflow(ctx, todo):
    todo.tasks["1"]["content"] = "x" * 300
    r = jobs.print_today(ctx)
    assert Image.open(r["png"]).width == WIDTH


def test_timed_tasks_first_and_overflow_goes_to_waiting(ctx, todo):
    todo.tasks["t"] = {"id": "t", "content": "Standup", "priority": 1,
                       "due": {"date": "2026-09-28T09:00:00"}}
    for i in range(12):
        todo.tasks[f"x{i}"] = {"id": f"x{i}", "content": f"Chore {i}", "priority": 1,
                               "due": {"date": "2026-09-28"}}
    r = jobs.print_today(ctx)
    assert r["manifest"][0]["task_id"] == "t"
    assert len(r["manifest"]) == jobs.MAX_ROWS
    assert r["id"].startswith("260928-") and len(r["id"]) == 11


def test_time_and_tag_helpers():
    from datetime import date
    assert jobs._time({"due": {"date": "2026-09-30T19:00:00"}}) == "7:00p"
    assert jobs._time({"due": {"date": "2026-09-30T00:05:00"}}) == "12:05a"
    assert jobs._time({"due": {"date": "2026-09-30"}}) == ""
    assert jobs._tag({"due": {"date": "2026-09-28"}}, date(2026, 9, 30)) == "overdue 2d"
    assert jobs._tag({"due": {"date": "2026-09-30", "is_recurring": True}}, date(2026, 9, 30)) == "↻"
