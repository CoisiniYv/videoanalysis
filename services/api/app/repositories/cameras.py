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
                      gpu_id: int, enabled: bool, input_type: str = "rtsp",
                      rtsp_transport: str = "tcp",
                      fps_policy: Optional[Dict[str, Any]] = None,
                      alert_policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        query = """
            INSERT INTO cameras (id, source_id, name, rtsp_url, site_id, location,
                                 gpu_id, enabled, input_type, rtsp_transport,
                                 fps_policy, alert_policy)
            VALUES (%(id)s, %(source_id)s, %(name)s, %(rtsp_url)s, %(site_id)s,
                    %(location)s, %(gpu_id)s, %(enabled)s, %(input_type)s,
                    %(rtsp_transport)s, %(fps_policy)s::jsonb,
                    %(alert_policy)s::jsonb)
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
                "input_type": input_type,
                "rtsp_transport": rtsp_transport,
                "fps_policy": json.dumps(fps_policy or {}),
                "alert_policy": json.dumps(alert_policy or {}),
            })
            return cur.fetchone()

    def update_camera(
        self,
        camera_id: str,
        *,
        source_id: Optional[str] = None,
        name: Optional[str] = None,
        rtsp_url: Optional[str] = None,
        site_id: Optional[str] = None,
        location: Optional[str] = None,
        gpu_id: Optional[int] = None,
        enabled: Optional[bool] = None,
        input_type: Optional[str] = None,
        rtsp_transport: Optional[str] = None,
        fps_policy: Optional[Dict[str, Any]] = None,
        alert_policy: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        current = self.get_camera(camera_id)
        if current is None:
            return None

        next_row = {
            "source_id": current.get("source_id") if source_id is None else source_id,
            "name": current.get("name") if name is None else name,
            "rtsp_url": current.get("rtsp_url") if rtsp_url is None else rtsp_url,
            "site_id": current.get("site_id") if site_id is None else site_id,
            "location": current.get("location") if location is None else location,
            "gpu_id": current.get("gpu_id", 0) if gpu_id is None else gpu_id,
            "enabled": current.get("enabled", True) if enabled is None else enabled,
            "input_type": current.get("input_type", "rtsp") if input_type is None else input_type,
            "rtsp_transport": current.get("rtsp_transport", "tcp") if rtsp_transport is None else rtsp_transport,
            "fps_policy": current.get("fps_policy") if fps_policy is None else fps_policy,
            "alert_policy": current.get("alert_policy") if alert_policy is None else alert_policy,
        }

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE cameras
                SET source_id = %(source_id)s,
                    name = %(name)s,
                    rtsp_url = %(rtsp_url)s,
                    site_id = %(site_id)s,
                    location = %(location)s,
                    gpu_id = %(gpu_id)s,
                    enabled = %(enabled)s,
                    input_type = %(input_type)s,
                    rtsp_transport = %(rtsp_transport)s,
                    fps_policy = %(fps_policy)s::jsonb,
                    alert_policy = %(alert_policy)s::jsonb,
                    updated_at = now()
                WHERE id = %(id)s
                RETURNING *
                """,
                {
                    "id": camera_id,
                    **next_row,
                    "fps_policy": json.dumps(next_row["fps_policy"] or {}),
                    "alert_policy": json.dumps(next_row["alert_policy"] or {}),
                },
            )
            return cur.fetchone()

    def set_camera_enabled(self, camera_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE cameras
                SET enabled = %(enabled)s, updated_at = now()
                WHERE id = %(id)s
                RETURNING *
                """,
                {"id": camera_id, "enabled": enabled},
            )
            return cur.fetchone()

    def set_alert_policy(
        self, camera_id: str, alert_policy: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE cameras
                SET alert_policy = %(alert_policy)s::jsonb,
                    updated_at = now()
                WHERE id = %(id)s
                RETURNING *
                """,
                {"id": camera_id, "alert_policy": json.dumps(alert_policy or {})},
            )
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
                    points: List[List[float]], payload: Dict[str, Any],
                    zone_id: Optional[str] = None,
                    coordinate_space: str = "pixel",
                    enabled: bool = True) -> Dict[str, Any]:
        query = """
            INSERT INTO camera_zones (
                camera_id, zone_id, zone_name, zone_type, coordinate_space,
                points, enabled, payload
            )
            VALUES (
                %(camera_id)s, %(zone_id)s, %(zone_name)s, %(zone_type)s,
                %(coordinate_space)s, %(points)s::jsonb, %(enabled)s,
                %(payload)s::jsonb
            )
            RETURNING *
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {
                "camera_id": camera_id,
                "zone_id": zone_id or zone_name,
                "zone_name": zone_name,
                "zone_type": zone_type,
                "coordinate_space": coordinate_space,
                "points": json.dumps(points),
                "enabled": enabled,
                "payload": json.dumps(payload or {}),
            })
            return cur.fetchone()

    def get_zone(self, camera_id: str, zone_id: str) -> Optional[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT * FROM camera_zones
                WHERE camera_id = %(camera_id)s
                  AND (zone_id = %(zone_id)s OR zone_name = %(zone_id)s)
                """,
                {"camera_id": camera_id, "zone_id": zone_id},
            )
            return cur.fetchone()

    def update_zone(
        self,
        *,
        camera_id: str,
        zone_id: str,
        new_zone_id: Optional[str] = None,
        zone_name: Optional[str] = None,
        zone_type: Optional[str] = None,
        coordinate_space: Optional[str] = None,
        points: Optional[List[List[float]]] = None,
        enabled: Optional[bool] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        current = self.get_zone(camera_id, zone_id)
        if current is None:
            return None
        next_zone_id = new_zone_id or current.get("zone_id") or current["zone_name"]
        next_zone_name = zone_name or current["zone_name"]
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE camera_zones
                SET zone_id = %(new_zone_id)s,
                    zone_name = %(zone_name)s,
                    zone_type = %(zone_type)s,
                    coordinate_space = %(coordinate_space)s,
                    points = %(points)s::jsonb,
                    enabled = %(enabled)s,
                    payload = %(payload)s::jsonb,
                    updated_at = now()
                WHERE camera_id = %(camera_id)s
                  AND (zone_id = %(zone_id)s OR zone_name = %(zone_id)s)
                RETURNING *
                """,
                {
                    "camera_id": camera_id,
                    "zone_id": zone_id,
                    "new_zone_id": next_zone_id,
                    "zone_name": next_zone_name,
                    "zone_type": zone_type or current["zone_type"],
                    "coordinate_space": coordinate_space or current.get("coordinate_space", "pixel"),
                    "points": json.dumps(points if points is not None else current.get("points", [])),
                    "enabled": current.get("enabled", True) if enabled is None else enabled,
                    "payload": json.dumps(payload if payload is not None else current.get("payload", {})),
                },
            )
            return cur.fetchone()

    def delete_zone(self, camera_id: str, zone_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM camera_zones
                WHERE camera_id = %(camera_id)s
                  AND (zone_id = %(zone_id)s OR zone_name = %(zone_id)s)
                """,
                {"camera_id": camera_id, "zone_id": zone_id},
            )
            return cur.rowcount is not None and cur.rowcount > 0

    def list_zones(self, camera_id: str) -> List[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM camera_zones WHERE camera_id = %(id)s "
                "ORDER BY zone_name",
                {"id": camera_id},
            )
            return cur.fetchall()

    def get_zone_names(self, camera_id: str) -> List[str]:
        # The connection is opened with row_factory=dict_row in app.db, so
        # cursors inherit that factory unless overridden. We explicitly
        # request dict_row here to keep the dependency obvious, and read
        # the column by name rather than by index.
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT zone_name FROM camera_zones WHERE camera_id = %(id)s",
                {"id": camera_id},
            )
            return [row["zone_name"] for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Rule CRUD
    # ------------------------------------------------------------------

    def create_rule(self, *, camera_id: str, rule_type: str, enabled: bool,
                    config: Dict[str, Any], rule_id: Optional[str] = None,
                    algorithm_id: Optional[str] = None) -> Dict[str, Any]:
        query = """
            INSERT INTO camera_rules (
                camera_id, rule_id, algorithm_id, rule_type, enabled, config
            )
            VALUES (
                %(camera_id)s, %(rule_id)s, %(algorithm_id)s, %(rule_type)s,
                %(enabled)s, %(config)s::jsonb
            )
            RETURNING *
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {
                "camera_id": camera_id,
                "rule_id": rule_id or rule_type,
                "algorithm_id": algorithm_id or rule_type,
                "rule_type": rule_type,
                "enabled": enabled,
                "config": json.dumps(config or {}),
            })
            return cur.fetchone()

    def list_rules(self, camera_id: str) -> List[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM camera_rules WHERE camera_id = %(id)s "
                "ORDER BY rule_id, rule_type",
                {"id": camera_id},
            )
            return cur.fetchall()

    def get_rule(self, camera_id: str, rule_id: str) -> Optional[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT * FROM camera_rules
                WHERE camera_id = %(camera_id)s
                  AND (rule_id = %(rule_id)s OR id::text = %(rule_id)s)
                """,
                {"camera_id": camera_id, "rule_id": rule_id},
            )
            return cur.fetchone()

    def update_rule(
        self,
        *,
        camera_id: str,
        rule_id: str,
        new_rule_id: Optional[str] = None,
        algorithm_id: Optional[str] = None,
        rule_type: Optional[str] = None,
        enabled: Optional[bool] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        current = self.get_rule(camera_id, rule_id)
        if current is None:
            return None
        next_algorithm_id = algorithm_id or current.get("algorithm_id") or current["rule_type"]
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE camera_rules
                SET rule_id = %(new_rule_id)s,
                    algorithm_id = %(algorithm_id)s,
                    rule_type = %(rule_type)s,
                    enabled = %(enabled)s,
                    config = %(config)s::jsonb,
                    updated_at = now()
                WHERE camera_id = %(camera_id)s
                  AND (rule_id = %(rule_id)s OR id::text = %(rule_id)s)
                RETURNING *
                """,
                {
                    "camera_id": camera_id,
                    "rule_id": rule_id,
                    "new_rule_id": new_rule_id or current.get("rule_id") or f"rule_{current['id']}",
                    "algorithm_id": next_algorithm_id,
                    "rule_type": rule_type or next_algorithm_id,
                    "enabled": current.get("enabled", True) if enabled is None else enabled,
                    "config": json.dumps(config if config is not None else current.get("config", {})),
                },
            )
            return cur.fetchone()

    def delete_rule(self, camera_id: str, rule_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM camera_rules
                WHERE camera_id = %(camera_id)s
                  AND (rule_id = %(rule_id)s OR id::text = %(rule_id)s)
                """,
                {"camera_id": camera_id, "rule_id": rule_id},
            )
            return cur.rowcount is not None and cur.rowcount > 0

    def set_rule_enabled(
        self, camera_id: str, rule_id: str, enabled: bool
    ) -> Optional[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE camera_rules
                SET enabled = %(enabled)s, updated_at = now()
                WHERE camera_id = %(camera_id)s
                  AND (rule_id = %(rule_id)s OR id::text = %(rule_id)s)
                RETURNING *
                """,
                {"camera_id": camera_id, "rule_id": rule_id, "enabled": enabled},
            )
            return cur.fetchone()

    # ------------------------------------------------------------------
    # R3 algorithm rule CRUD (reuses camera_rules)
    # ------------------------------------------------------------------

    def create_algorithm_rule(
        self,
        *,
        camera_id: str,
        rule_id: Optional[str],
        algorithm_id: str,
        rule_type: str,
        enabled: bool,
        zone_id: Optional[str],
        line_id: Optional[str],
        config: Dict[str, Any],
        evidence_policy: Dict[str, Any],
    ) -> Dict[str, Any]:
        query = """
            INSERT INTO camera_rules (
                camera_id, rule_id, algorithm_id, rule_type, enabled, zone_id, line_id,
                config, evidence_policy
            )
            VALUES (
                %(camera_id)s, %(rule_id)s, %(algorithm_id)s,
                %(rule_type)s, %(enabled)s,
                %(zone_id)s, %(line_id)s,
                %(config)s::jsonb, %(evidence_policy)s::jsonb
            )
            RETURNING *, algorithm_id AS algorithm_type
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                query,
                {
                    "camera_id": camera_id,
                    "rule_id": rule_id or f"rule_{algorithm_id.replace('.', '_')}",
                    "algorithm_id": algorithm_id,
                    "rule_type": rule_type,
                    "enabled": enabled,
                    "zone_id": zone_id,
                    "line_id": line_id,
                    "config": json.dumps(config or {}),
                    "evidence_policy": json.dumps(evidence_policy or {}),
                },
            )
            return cur.fetchone()

    def list_algorithm_rules(self, camera_id: str) -> List[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT *, algorithm_id AS algorithm_type
                FROM camera_rules
                WHERE camera_id = %(id)s
                ORDER BY id
                """,
                {"id": camera_id},
            )
            return cur.fetchall()

    def get_algorithm_rule(
        self, camera_id: str, rule_id: int | str
    ) -> Optional[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT *, algorithm_id AS algorithm_type
                FROM camera_rules
                WHERE camera_id = %(camera_id)s
                  AND (id::text = %(rule_id)s OR rule_id = %(rule_id)s)
                """,
                {"camera_id": camera_id, "rule_id": str(rule_id)},
            )
            return cur.fetchone()

    def update_algorithm_rule(
        self,
        *,
        camera_id: str,
        rule_id: int | str,
        enabled: Optional[bool],
        zone_id: Optional[str],
        line_id: Optional[str],
        config: Optional[Dict[str, Any]],
        evidence_policy: Optional[Dict[str, Any]],
        algorithm_id: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_algorithm_rule(camera_id, rule_id)
        if existing is None:
            return None

        next_enabled = existing["enabled"] if enabled is None else enabled
        next_zone_id = existing.get("zone_id") if zone_id is None else zone_id
        next_line_id = existing.get("line_id") if line_id is None else line_id
        next_config = existing.get("config") if config is None else config
        next_algorithm_id = existing.get("algorithm_id") if algorithm_id is None else algorithm_id
        next_rule_type = existing.get("rule_type") if rule_type is None else rule_type
        next_evidence_policy = (
            existing.get("evidence_policy")
            if evidence_policy is None
            else evidence_policy
        )

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE camera_rules
                SET enabled = %(enabled)s,
                    algorithm_id = %(algorithm_id)s,
                    rule_type = %(rule_type)s,
                    zone_id = %(zone_id)s,
                    line_id = %(line_id)s,
                    config = %(config)s::jsonb,
                    evidence_policy = %(evidence_policy)s::jsonb,
                    updated_at = now()
                WHERE camera_id = %(camera_id)s
                  AND (id::text = %(rule_id)s OR rule_id = %(rule_id)s)
                RETURNING *, algorithm_id AS algorithm_type
                """,
                {
                    "camera_id": camera_id,
                    "rule_id": str(rule_id),
                    "enabled": next_enabled,
                    "algorithm_id": next_algorithm_id,
                    "rule_type": next_rule_type,
                    "zone_id": next_zone_id,
                    "line_id": next_line_id,
                    "config": json.dumps(next_config or {}),
                    "evidence_policy": json.dumps(next_evidence_policy or {}),
                },
            )
            return cur.fetchone()

    def set_algorithm_rule_enabled(
        self, camera_id: str, rule_id: int | str, enabled: bool
    ) -> Optional[Dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE camera_rules
                SET enabled = %(enabled)s, updated_at = now()
                WHERE camera_id = %(camera_id)s
                  AND (id::text = %(rule_id)s OR rule_id = %(rule_id)s)
                RETURNING *, algorithm_id AS algorithm_type
                """,
                {"camera_id": camera_id, "rule_id": str(rule_id), "enabled": enabled},
            )
            return cur.fetchone()

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
