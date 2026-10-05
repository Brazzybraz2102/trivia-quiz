"""The contract every to-do app connection implements.

Providers return tasks in one shape (Todoist's, since it came first) so printing and read-back
don't care which app a person uses:

    {"id": str, "content": str, "description": str,
     "priority": 1..4 (4 = most urgent),
     "due": {"date": "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SS" (local time), "is_recurring": bool} | None}

The person's to-do app is the only source of truth: providers never cache tasks.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ProviderError(RuntimeError):
    """A problem worth showing to the person as-is (bad token, server down, unsupported action)."""


class TaskProvider(ABC):
    key: str = ""

    @abstractmethod
    def check(self) -> dict:
        """Prove the connection works. Returns {"account": label, "lists": [names]}."""

    @abstractmethod
    def today(self) -> list[dict]:
        """Everything due today or overdue."""

    @abstractmethod
    def filter_tasks(self, query: str) -> list[dict]:
        """Provider-specific selection: a Todoist filter, or a list name for CalDAV."""

    @abstractmethod
    def get_task(self, task_id: str) -> dict: ...

    @abstractmethod
    def add_task(self, content: str, description: str = "") -> dict: ...

    @abstractmethod
    def close_task(self, task_id: str) -> None:
        """Complete it. A repeating task advances to its next occurrence."""

    @abstractmethod
    def set_due_date(self, task_id: str, date_iso: str) -> None:
        """Move to a date without rewriting a repeating rule."""

    @abstractmethod
    def delete_task(self, task_id: str) -> None: ...
