"""AuditLogRepository — write audit trail entries for status mutations."""

from __future__ import annotations

import json
from typing import Any, Dict

import psycopg


_INSERT_SQL = """
INSERT INTO audit_logs (actor, action, entity_type, entity_id, payload)
VALUES (%(actor)s, %(action)s, %(entity_type)s, %(entity_id)s, %(payload)s::jsonb)
"""


class AuditLogRepository:
    """Append-only audit log store."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def write(
        self,
        *,
        actor: str,
        action: str,
        entity_type: str = "event",
        entity_id: str = "",
        previous_status: str = "",
        new_status: str = "",
        comment: str = "",
        source_event_id: str = "",
    ) -> None:
        payload = {
            "previous_status": previous_status,
            "new_status": new_status,
            "comment": comment,
            "source_event_id": source_event_id,
        }
        params = {
            "actor": actor,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": json.dumps(payload, ensure_ascii=False),
        }
        with self._conn.cursor() as cur:
            cur.execute(_INSERT_SQL, params)
