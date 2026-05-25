"""CameraRepository — CRUD for cameras / camera_zones / camera_rules (Phase C1).

Mirrors the EventRepository style: synchronous psycopg connection injected at
construction time. JSON columns are inserted with explicit ``::jsonb`` casts so
psycopg passes dict payloads as JSON without server-side type inference quirks.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row


class CameraRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Camera CRUD
    # ------------------------------------------------------------------

    def create_camera(self, *, camera_id: str, source_id: str, name: str,
                      rtsp_url: str, site_id: Optional[str], location: Optional[str],
                      gpu_id: int, enabled: bool) -> Dict[str, Any]:
        query = """
            INSERT INTO cameras (id, source_id, name, rtsp_url, site_id, location,
                                 gpu_id, enabled)
            VALUES (%(id)s, %(source_id)s, %(name)s, %(rtsp_url)s, %(site_id)s,
                    %(location)s, %(gpu_id)s, %(enabled)s)
            RETURNING *
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {
                "id": camera_id,
                "source_id": source_id,
                "name": name,
                "rtsp_url": rtsp_url,
                "site_id": site_id,
                "location": location,
                "gpu_id": gpu_id,
                "enabled": enabled,
            })
            return cur.fetchone()

    def get_camera(self, camera_id: str) -> Optional[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM cameras WHERE id = %(id)s", {"id": camera_id})
            return cur.fetchone()

    def list_cameras(self, *, enabled: Optional[bool] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM cameras"
        params: Dict[str, Any] = {}
        if enabled is not None:
            query += " WHERE enabled = %(enabled)s"
            params["enabled"] = enabled
        query += " ORDER BY id"
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchall()

    # ------------------------------------------------------------------
    # Zone CRUD
    # ------------------------------------------------------------------

    def create_zone(self, *, camera_id: str, zone_name: str, zone_type: str,
                    points: List[List[float]], payload: Dict[str, Any]) -> Dict[str, Any]:
        query = """
            INSERT INTO camera_zones (camera_id, zone_name, zone_type, points, payload)
            VALUES (%(camera_id)s, %(zone_name)s, %(zone_type)s,
                    %(points)s::jsonb, %(payload)s::jsonb)
            RETURNING *
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {
                "camera_id": camera_id,
                "zone_name": zone_name,
                "zone_type": zone_type,
                "points": json.dumps(points),
                "payload": json.dumps(payload or {}),
            })
            return cur.fetchone()

    def list_zones(self, camera_id: str) -> List[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM camera_zones WHERE camera_id = %(id)s "
                "ORDER BY zone_name",
                {"id": camera_id},
            )
            return cur.fetchall()

    def get_zone_names(self, camera_id: str) -> List[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT zone_name FROM camera_zones WHERE camera_id = %(id)s",
                {"id": camera_id},
            )
            return [row[0] for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Rule CRUD
    # ------------------------------------------------------------------

    def create_rule(self, *, camera_id: str, rule_type: str, enabled: bool,
                    config: Dict[str, Any]) -> Dict[str, Any]:
        query = """
            INSERT INTO camera_rules (camera_id, rule_type, enabled, config)
            VALUES (%(camera_id)s, %(rule_type)s, %(enabled)s, %(config)s::jsonb)
            RETURNING *
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {
                "camera_id": camera_id,
                "rule_type": rule_type,
                "enabled": enabled,
                "config": json.dumps(config or {}),
            })
            return cur.fetchone()

    def list_rules(self, camera_id: str) -> List[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM camera_rules WHERE camera_id = %(id)s "
                "ORDER BY rule_type",
                {"id": camera_id},
            )
            return cur.fetchall()

    # ------------------------------------------------------------------
    # Bulk for export
    # ------------------------------------------------------------------

    def list_zones_for_cameras(self, camera_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        if not camera_ids:
            return {}
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM camera_zones WHERE camera_id = ANY(%(ids)s) "
                "ORDER BY camera_id, zone_name",
                {"ids": camera_ids},
            )
            rows = cur.fetchall()
        out: Dict[str, List[Dict[str, Any]]] = {cid: [] for cid in camera_ids}
        for r in rows:
            out.setdefault(r["camera_id"], []).append(r)
        return out

    def list_rules_for_cameras(self, camera_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        if not camera_ids:
            return {}
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM camera_rules WHERE camera_id = ANY(%(ids)s) "
                "ORDER BY camera_id, rule_type",
                {"ids": camera_ids},
            )
            rows = cur.fetchall()
        out: Dict[str, List[Dict[str, Any]]] = {cid: [] for cid in camera_ids}
        for r in rows:
            out.setdefault(r["camera_id"], []).append(r)
        return out
