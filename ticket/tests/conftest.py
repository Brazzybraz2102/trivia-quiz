from __future__ import annotations

from datetime import date

import pytest

from ticket import printer
from ticket.config import Settings
from ticket.jobs import Context
from ticket.store import Store

TODAY = date(2026, 9, 28)


class FakeTodoist:
    def __init__(self, tasks: list[dict] | None = None):
        self.tasks = {t["id"]: t for t in (tasks or [])}
        self.calls: list[tuple] = []

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
    settings = Settings(todoist_token="x", printer_ip="", ticket_key="k", dry_run=True,
                        data_dir=tmp_path)
    return Context(settings=settings, store=Store(tmp_path), todoist_factory=lambda: todo,
                   today=lambda: TODAY)
