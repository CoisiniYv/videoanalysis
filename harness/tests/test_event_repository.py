"""Focused tests for API event repository database row handling."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.repositories.events import EventRepository


class _Cursor:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.query = ""
        self.params = {}

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: str, params: dict[str, Any]) -> None:
        self.query = query
        self.params = params

    def fetchone(self) -> Any:
        return self.result

    def fetchall(self) -> Any:
        return self.result


class _Conn:
    def __init__(self) -> None:
        self.cursors: list[_Cursor] = []

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        result: Any
        if not self.cursors:
            result = {"total": 2}
        else:
            result = [{"id": "event-1"}, {"id": "event-2"}]
        cursor = _Cursor(result)
        self.cursors.append(cursor)
        return cursor


def test_list_events_reads_count_from_dict_row_connection() -> None:
    conn = _Conn()
    rows, total = EventRepository(conn).list_events(limit=10)

    assert total == 2
    assert rows == [{"id": "event-1"}, {"id": "event-2"}]
    assert "COUNT(*) AS total" in conn.cursors[0].query
