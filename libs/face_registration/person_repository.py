"""PersonRepository — PostgreSQL CRUD for the ``persons`` table.

Follows the FaceObservationRepository pattern (psycopg.Connection, dict_row).
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

import psycopg
from psycopg.rows import dict_row


_INSERT_SQL = """
INSERT INTO persons (
    name, external_person_id, description, is_active,
    created_by, updated_by, payload
) VALUES (
    %(name)s, %(external_person_id)s, %(description)s, %(is_active)s,
    %(created_by)s, %(updated_by)s, %(payload)s::jsonb
)
RETURNING id
"""

_GET_BY_ID_SQL = """
SELECT id, name, external_person_id, description, is_active,
       created_by, updated_by, payload, created_at, updated_at
FROM persons
WHERE id = %(id)s
"""

_GET_BY_EXTERNAL_SQL = """
SELECT id, name, external_person_id, description, is_active,
       created_by, updated_by, payload, created_at, updated_at
FROM persons
WHERE external_person_id = %(external_person_id)s
"""

_LIST_ACTIVE_SQL = """
SELECT id, name, external_person_id, description, is_active,
       created_by, updated_by, payload, created_at, updated_at
FROM persons
WHERE is_active = true
ORDER BY id
"""

_DEACTIVATE_SQL = """
UPDATE persons SET
    is_active = false,
    updated_at = now()
WHERE id = %(id)s
RETURNING id
"""


def _to_jsonb(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False)


class PersonRepository:
    """CRUD operations for the ``persons`` table."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def create_person(
        self,
        name: str,
        *,
        external_person_id: str | None = None,
        description: str | None = None,
        is_active: bool = True,
        created_by: str | None = None,
        updated_by: str | None = None,
        payload: dict | None = None,
    ) -> int:
        """Create a new person. Returns the new person's BIGINT id."""
        params = {
            "name": name,
            "external_person_id": external_person_id,
            "description": description,
            "is_active": is_active,
            "created_by": created_by,
            "updated_by": updated_by,
            "payload": _to_jsonb(payload) if payload else "{}",
        }
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_INSERT_SQL, params)
            row = cur.fetchone()
            return int(row["id"])

    def get_by_id(self, person_id: int) -> dict | None:
        """Return person dict by id, or None if not found."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_GET_BY_ID_SQL, {"id": person_id})
            return cur.fetchone()

    def get_by_external_person_id(self, external_person_id: str) -> dict | None:
        """Return person dict by external_person_id, or None if not found."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_GET_BY_EXTERNAL_SQL, {"external_person_id": external_person_id})
            return cur.fetchone()

    def list_active(self) -> List[dict[str, Any]]:
        """Return all active persons ordered by id."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_LIST_ACTIVE_SQL)
            return list(cur.fetchall())

    def deactivate(self, person_id: int) -> bool:
        """Soft-deactivate a person. Returns True if a row was updated."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_DEACTIVATE_SQL, {"id": person_id})
            return cur.fetchone() is not None
