from __future__ import annotations

from datetime import date

import pytest

from nextbox import printer
from nextbox.config import Settings
from nextbox.jobs import Context
from nextbox.store import Store

TODAY = date(2026, 9, 28)


def tiny_jpeg(size=(40, 30)) -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "JPEG")
    return buf.getvalue()


class FakeTodoist:
    def __init__(self, tasks: list[dict] | None = None):
        self.tasks = {t["id"]: t for t in (tasks or [])}
        self.calls: list[tuple] = []

    def check(self):
        return {"account": "fake@example.com", "lists": ["Inbox"]}

    def today(self):
        return self.filter_tasks("today | overdue")

    def filter_tasks(self, query):
        self.calls.append(("filter", query))
        return list(self.tasks.values())

    def projects(self):
        return [{"id": "p1", "name": "Inbox"}]

    def get_task(self, task_id):
        return self.tasks[task_id]

    def add_task(self, content, due_string=None, description=""):
        tid = f"new{len(self.tasks)}"
        self.tasks[tid] = {"id": tid, "content": content, "description": description, "priority": 1}
        self.calls.append(("add", content))
        return self.tasks[tid]

    def close_task(self, task_id):
        self.calls.append(("close", task_id))

    def set_due_date(self, task_id, date_iso):
        self.calls.append(("due_date", task_id, date_iso))

    def delete_task(self, task_id):
        self.calls.append(("delete", task_id))


SAMPLE = [
    {"id": "1", "content": "Call the dentist", "priority": 4, "due": {"date": "2026-09-28"}},
    {"id": "2", "content": "Water plants", "priority": 1,
     "due": {"date": "2026-09-28", "is_recurring": True, "string": "every day"}},
    {"id": "3", "content": "Renew passport", "priority": 2, "due": {"date": "2026-09-25"}},
    {"id": "4", "content": "Old idea", "priority": 1, "due": {"date": "2026-09-28"}},
]


def rows(data_dir, table: str) -> list[dict]:
    """Every row of a database table, for tests that check what's stored."""
    from sqlalchemy import select

    from nextbox import db
    with db.database(data_dir).connect() as conn:
        return [dict(r) for r in conn.execute(select(db.metadata.tables[table])).mappings()]


def dump(data_dir, *, skip: tuple = ()) -> str:
    """All stored data as text (every table except `skip`), for "is this anywhere?" checks."""
    import json

    from nextbox import db
    return "\n".join(f"{t}: {json.dumps(rows(data_dir, t), default=str)}"
                     for t in db.metadata.tables if t not in skip)


@pytest.fixture(autouse=True)
def fresh_database():
    """SQLite: every test has its own temp data dir. PostgreSQL (DATABASE_URL set): one shared
    server, so wipe the tables before each test."""
    import os

    from nextbox import db
    if os.environ.get("DATABASE_URL"):
        engine = db.database(os.environ["DATABASE_URL"])
        db.metadata.drop_all(engine)
        db.metadata.create_all(engine)
    yield
    db.reset_engines()


@pytest.fixture(autouse=True)
def no_real_printer(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tests must never talk to the printer")
    monkeypatch.setattr(printer, "_send_raster", boom)


@pytest.fixture
def todo():
    return FakeTodoist([dict(t) for t in SAMPLE])


@pytest.fixture
def ctx(tmp_path, todo):
    settings = Settings(todoist_token="x", printer_ip="", server_key="k", dry_run=True,
                        data_dir=tmp_path)
    return Context(settings=settings, store=Store(tmp_path), tasks_factory=lambda: todo,
                   today=lambda: TODAY)
