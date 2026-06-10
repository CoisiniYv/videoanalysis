"""Repository-level row-shape regression tests for CameraRepository.

The camera API tests use a FakeCameraRepository — they never
exercise the real psycopg cursor → dict_row → fetch shape. That gap let
``cameras.py:get_zone_names`` ship with positional ``row[0]`` access
against dict rows; the real DB then raised ``KeyError: 0`` and turned
``POST /api/v1/cameras/{id}/rules`` into a 500.

These tests drive the **real** ``CameraRepository`` against a tiny fake
``psycopg.Connection`` whose cursor returns dict-shaped rows — exactly
what ``row_factory=dict_row`` (set at the connection level in
``services/api/app/db.py``) produces in production. Any repository
method that mishandles dict rows will fail here at unit level.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

# Sibling runtime tests may have cached app.* — re-import a fresh tree.
for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.repositories.cameras import CameraRepository


# ---------------------------------------------------------------------------
# Minimal psycopg-shaped fake (matches the row_factory=dict_row contract)
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Mimics psycopg's cursor when ``row_factory=dict_row`` is active.

    Calls to ``execute(query, params)`` are recorded for assertions.
    ``fetchall`` / ``fetchone`` return the dict rows queued by the test.
    """

    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = list(rows)
        self.executed: List[Tuple[str, Optional[Dict[str, Any]]]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query: str, params: Optional[Dict[str, Any]] = None) -> None:
        self.executed.append((query, params))

    def fetchall(self) -> List[Dict[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> Optional[Dict[str, Any]]:
        return self._rows[0] if self._rows else None


class _FakeConnection:
    """Mimics ``psycopg.Connection`` with a sticky ``row_factory=dict_row``.

    In production ``app.db.get_conn`` opens the real connection with
    ``row_factory=dict_row``. Cursors created without an explicit factory
    therefore inherit dict rows — which is what burned ``get_zone_names``.
    This fake reproduces that exact behavior: every ``cursor(...)``
    call returns dict rows regardless of ``row_factory`` argument.
    """

    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows
        self.last_cursor: Optional[_FakeCursor] = None

    def cursor(self, row_factory: Any = None) -> _FakeCursor:
        # Argument intentionally ignored — connection's dict_row dominates.
        self.last_cursor = _FakeCursor(self._rows)
        return self.last_cursor


# ===========================================================================
# 1. get_zone_names returns the column by name, not by index
# ===========================================================================


def test_get_zone_names_returns_zone_name_column():
    conn = _FakeConnection(rows=[{"zone_name": "perimeter"}])
    repo = CameraRepository(conn)
    assert repo.get_zone_names("cam_midterm") == ["perimeter"]


def test_get_zone_names_handles_multiple_rows():
    conn = _FakeConnection(rows=[
        {"zone_name": "perimeter"},
        {"zone_name": "loading_dock"},
        {"zone_name": "parking"},
    ])
    repo = CameraRepository(conn)
    assert repo.get_zone_names("cam_midterm") == [
        "perimeter", "loading_dock", "parking",
    ]


def test_get_zone_names_empty_returns_empty_list():
    conn = _FakeConnection(rows=[])
    repo = CameraRepository(conn)
    assert repo.get_zone_names("cam_does_not_exist") == []


# ===========================================================================
# 2. get_zone_names does NOT do positional access on dict rows
# ===========================================================================


def test_get_zone_names_does_not_use_positional_access():
    """If the repository tried row[0] against a dict row it would raise
    KeyError: 0 — exactly the production traceback. Asserting the rows
    have no integer key 0 and the call still succeeds locks down the
    fix permanently.
    """
    rows = [{"zone_name": "perimeter"}]
    assert 0 not in rows[0], "fixture must mirror dict_row semantics"
    conn = _FakeConnection(rows=rows)
    repo = CameraRepository(conn)
    # Must not raise.
    assert repo.get_zone_names("cam_midterm") == ["perimeter"]


def test_get_zone_names_raises_keyerror_only_when_column_missing():
    """A row that genuinely lacks 'zone_name' is a schema bug, not a
    repository bug — assert we still surface it as a KeyError so callers
    don't silently swallow it.
    """
    conn = _FakeConnection(rows=[{"wrong_column": "perimeter"}])
    repo = CameraRepository(conn)
    with pytest.raises(KeyError):
        repo.get_zone_names("cam_midterm")


# ===========================================================================
# 3. SQL stays parameterised by camera_id
# ===========================================================================


def test_get_zone_names_uses_parameter_binding():
    conn = _FakeConnection(rows=[{"zone_name": "perimeter"}])
    repo = CameraRepository(conn)
    repo.get_zone_names("cam_midterm")
    assert conn.last_cursor is not None
    query, params = conn.last_cursor.executed[0]
    assert "camera_id = %(id)s" in query
    assert params == {"id": "cam_midterm"}
