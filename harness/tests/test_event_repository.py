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

    def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        self.query = query
        self.params = params or {}

    def fetchone(self) -> Any:
        return self.result

    def fetchall(self) -> Any:
        return self.result


class _Conn:
    def __init__(self, results: list[Any] | None = None) -> None:
        self._results = results
        self.cursors: list[_Cursor] = []

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        result: Any
        if self._results is not None:
            result = self._results[len(self.cursors)]
        elif not self.cursors:
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


def test_list_events_joins_camera_name_and_qualifies_filters() -> None:
    conn = _Conn()
    EventRepository(conn).list_events(
        event_type="intrusion",
        camera_id="camera-1",
        track_id="track-1",
        status="new",
        start="2026-06-24T00:00:00Z",
        end="2026-06-24T01:00:00Z",
        limit=10,
    )

    count_query = conn.cursors[0].query
    data_query = conn.cursors[1].query

    assert "FROM events e" in count_query
    assert "LEFT JOIN cameras c" in data_query
    assert "c.name AS camera_name" in data_query
    assert "e.event_type = %(event_type)s" in data_query
    assert "e.camera_id = %(camera_id)s" in data_query
    assert "e.track_id = %(track_id)s" in data_query
    assert "e.status = %(status)s" in data_query
    assert "e.start_ts >= %(start)s::timestamptz" in data_query
    assert "e.start_ts <= %(end)s::timestamptz" in data_query
    assert "ORDER BY e.created_at DESC" in data_query


def test_recent_and_lookup_queries_join_camera_name() -> None:
    event_id = "11111111-1111-4111-8111-111111111111"
    row = {"id": event_id, "source_event_id": "source-event-1", "camera_name": "Lobby"}
    conn = _Conn(results=[[row], row, row])
    repo = EventRepository(conn)

    recent = repo.list_recent(limit=1)
    by_id = repo.get_by_id(event_id)
    by_source_event_id = repo.get_by_source_event_id("source-event-1")

    assert recent == [row]
    assert by_id == row
    assert by_source_event_id == row
    assert all("LEFT JOIN cameras c" in cursor.query for cursor in conn.cursors)
    assert all("c.name AS camera_name" in cursor.query for cursor in conn.cursors)
    assert "WHERE e.id = %(id)s" in conn.cursors[1].query
    assert "WHERE e.source_event_id = %(sid)s" in conn.cursors[2].query


def test_update_status_returns_joined_camera_name_row() -> None:
    event_id = "11111111-1111-4111-8111-111111111111"
    initial_row = {
        "id": event_id,
        "source_event_id": "source-event-1",
        "status": "new",
    }
    updated_row = {
        "id": event_id,
        "source_event_id": "source-event-1",
        "status": "acknowledged",
    }
    joined_row = {**updated_row, "camera_name": "Lobby"}
    conn = _Conn(results=[initial_row, updated_row, joined_row])

    row = EventRepository(conn).update_status(event_id, "acknowledged")

    assert row == joined_row
    assert "UPDATE events" in conn.cursors[1].query
    assert "LEFT JOIN cameras c" in conn.cursors[2].query
