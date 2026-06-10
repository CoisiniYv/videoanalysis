"""Repository for storage maintenance jobs and related business rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class MaintenanceRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def create_job(
        self,
        *,
        job_type: str,
        target_type: str,
        status: str,
        delete_mode: str | None,
        requested_by: str | None,
        reason: str | None,
        request_payload: dict[str, Any],
        preview_payload: dict[str, Any],
        result_payload: dict[str, Any] | None = None,
        candidate_hash: str | None = None,
        preview_expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO maintenance_jobs (
                    job_type, target_type, status, delete_mode, requested_by,
                    reason, request_payload, preview_payload, result_payload,
                    candidate_hash, preview_expires_at
                )
                VALUES (
                    %(job_type)s, %(target_type)s, %(status)s, %(delete_mode)s,
                    %(requested_by)s, %(reason)s, %(request_payload)s,
                    %(preview_payload)s, %(result_payload)s, %(candidate_hash)s,
                    %(preview_expires_at)s
                )
                RETURNING *
                """,
                {
                    "job_type": job_type,
                    "target_type": target_type,
                    "status": status,
                    "delete_mode": delete_mode,
                    "requested_by": requested_by,
                    "reason": reason,
                    "request_payload": Jsonb(request_payload),
                    "preview_payload": Jsonb(preview_payload),
                    "result_payload": Jsonb(result_payload or {}),
                    "candidate_hash": candidate_hash,
                    "preview_expires_at": preview_expires_at,
                },
            )
            return dict(cur.fetchone())

    def add_items(self, job_id: str, items: list[dict[str, Any]]) -> None:
        with self._conn.cursor() as cur:
            for item in items:
                cur.execute(
                    """
                    INSERT INTO maintenance_job_items (
                        job_id, target_type, target_id, status, absolute_path,
                        relative_path, resolved_path, size_bytes, mtime_ns,
                        content_fingerprint, db_event_id, db_task_id,
                        media_status_at_preview, eligibility_status, skip_reason,
                        item_payload
                    )
                    VALUES (
                        %(job_id)s, %(target_type)s, %(target_id)s, %(status)s,
                        %(absolute_path)s, %(relative_path)s, %(resolved_path)s,
                        %(size_bytes)s, %(mtime_ns)s, %(content_fingerprint)s,
                        %(db_event_id)s, %(db_task_id)s,
                        %(media_status_at_preview)s, %(eligibility_status)s,
                        %(skip_reason)s, %(item_payload)s
                    )
                    ON CONFLICT (job_id, target_type, target_id) DO NOTHING
                    """,
                    {
                        **item,
                        "job_id": job_id,
                        "item_payload": Jsonb(item.get("item_payload") or {}),
                    },
                )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM maintenance_jobs WHERE id = %(job_id)s", {"job_id": job_id})
            row = cur.fetchone()
            return dict(row) if row else None

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        result_payload: dict[str, Any] | None = None,
        error_message: str | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        assignments = ["updated_at = now()"]
        params: dict[str, Any] = {"job_id": job_id}
        if status is not None:
            assignments.append("status = %(status)s")
            params["status"] = status
        if result_payload is not None:
            assignments.append("result_payload = %(result_payload)s")
            params["result_payload"] = Jsonb(result_payload)
        if error_message is not None:
            assignments.append("error_message = %(error_message)s")
            params["error_message"] = error_message
        if started:
            assignments.append("started_at = COALESCE(started_at, now())")
        if finished:
            assignments.append("finished_at = now()")
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE maintenance_jobs SET {', '.join(assignments)} WHERE id = %(job_id)s",
                params,
            )

    def list_items(
        self,
        job_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        where = ["job_id = %(job_id)s"]
        params: dict[str, Any] = {"job_id": job_id, "offset": offset}
        if status:
            where.append("status = %(status)s")
            params["status"] = status
        sql = f"""
            SELECT *
            FROM maintenance_job_items
            WHERE {' AND '.join(where)}
            ORDER BY id
        """
        if limit is not None:
            sql += " LIMIT %(limit)s OFFSET %(offset)s"
            params["limit"] = limit
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def count_items(self, job_id: str, *, status: str | None = None) -> int:
        where = ["job_id = %(job_id)s"]
        params: dict[str, Any] = {"job_id": job_id}
        if status:
            where.append("status = %(status)s")
            params["status"] = status
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT count(*) AS total FROM maintenance_job_items WHERE {' AND '.join(where)}",
                params,
            )
            return int(cur.fetchone()["total"])

    def item_status_counts(self, job_id: str) -> dict[str, int]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT status, count(*) AS total
                FROM maintenance_job_items
                WHERE job_id = %(job_id)s
                GROUP BY status
                """,
                {"job_id": job_id},
            )
            return {str(row["status"]): int(row["total"]) for row in cur.fetchall()}

    def update_item(
        self,
        item_id: int,
        *,
        status: str,
        skip_reason: str | None = None,
        error_message: str | None = None,
        trash_path: str | None = None,
        item_payload: dict[str, Any] | None = None,
        completed: bool = False,
    ) -> None:
        assignments = ["status = %(status)s", "updated_at = now()"]
        params: dict[str, Any] = {"item_id": item_id, "status": status}
        if skip_reason is not None:
            assignments.append("skip_reason = %(skip_reason)s")
            params["skip_reason"] = skip_reason
        if error_message is not None:
            assignments.append("error_message = %(error_message)s")
            params["error_message"] = error_message
        if trash_path is not None:
            assignments.append("trash_path = %(trash_path)s")
            params["trash_path"] = trash_path
        if item_payload is not None:
            assignments.append("item_payload = %(item_payload)s")
            params["item_payload"] = Jsonb(item_payload)
        if completed:
            assignments.append("completed_at = COALESCE(completed_at, now())")
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE maintenance_job_items SET {', '.join(assignments)} WHERE id = %(item_id)s",
                params,
            )

    def get_evidence_record(self, target_id: str) -> dict[str, Any] | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    e.id AS event_id,
                    e.source_event_id,
                    e.event_type,
                    e.camera_id,
                    e.source_id,
                    e.start_ts,
                    e.created_at,
                    e.media_status,
                    e.payload,
                    et.task_id,
                    et.status AS evidence_task_status
                FROM events e
                LEFT JOIN LATERAL (
                    SELECT task_id, status
                    FROM evidence_tasks
                    WHERE event_id = e.id OR source_event_id = e.source_event_id
                    ORDER BY created_at DESC, task_id DESC
                    LIMIT 1
                ) et ON true
                WHERE e.source_event_id = %(target_id)s OR e.id::text = %(target_id)s
                ORDER BY e.created_at DESC
                LIMIT 1
                """,
                {"target_id": target_id},
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def update_event_media_deleted(
        self,
        *,
        event_id: str,
        job_id: str,
        operator: str,
        reason: str,
        media_status: str = "media_deleted",
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET media_status = %(media_status)s::text,
                    payload = jsonb_set(
                        COALESCE(payload, '{}'::jsonb),
                        '{maintenance}',
                        COALESCE(payload->'maintenance', '{}'::jsonb)
                        || jsonb_build_object(
                            'deleted_at', now(),
                            'deleted_by', %(operator)s::text,
                            'delete_reason', %(reason)s::text,
                            'delete_job_id', %(job_id)s::text,
                            'no_auto_regenerate', true
                        ),
                        true
                    ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "event_id": event_id,
                    "job_id": job_id,
                    "operator": operator,
                    "reason": reason,
                    "media_status": media_status,
                },
            )

    def mark_evidence_tasks_deleted_metadata(self, *, event_id: str, job_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evidence_tasks
                SET error_message = %(message)s::text,
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                """,
                {"event_id": event_id, "message": f"deleted_by_storage_maintenance:{job_id}"},
            )

    def find_people_for_delete(
        self,
        *,
        person_ids: list[int],
        external_person_ids: list[str],
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: dict[str, Any] = {}
        if person_ids:
            where.append("id = ANY(%(person_ids)s)")
            params["person_ids"] = person_ids
        if external_person_ids:
            where.append("external_person_id = ANY(%(external_person_ids)s)")
            params["external_person_ids"] = external_person_ids
        if created_from:
            where.append("created_at >= %(created_from)s")
            params["created_from"] = created_from
        if created_to:
            where.append("created_at <= %(created_to)s")
            params["created_to"] = created_to
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT id, name, external_person_id, is_active, created_at, updated_at
                FROM persons
                {where_sql}
                ORDER BY id
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]

    def list_gallery_for_people(self, person_ids: list[int]) -> list[dict[str, Any]]:
        if not person_ids:
            return []
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, person_id, source_image_path, is_active, is_primary,
                       payload, created_at, updated_at
                FROM person_gallery_embeddings
                WHERE person_id = ANY(%(person_ids)s)
                ORDER BY person_id, id
                """,
                {"person_ids": person_ids},
            )
            return [dict(row) for row in cur.fetchall()]

    def find_gallery_for_delete(
        self,
        *,
        gallery_embedding_ids: list[int],
        person_ids: list[int],
        created_from: datetime | None,
        created_to: datetime | None,
        only_inactive: bool,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: dict[str, Any] = {}
        if gallery_embedding_ids:
            where.append("id = ANY(%(gallery_embedding_ids)s)")
            params["gallery_embedding_ids"] = gallery_embedding_ids
        if person_ids:
            where.append("person_id = ANY(%(person_ids)s)")
            params["person_ids"] = person_ids
        if created_from:
            where.append("created_at >= %(created_from)s")
            params["created_from"] = created_from
        if created_to:
            where.append("created_at <= %(created_to)s")
            params["created_to"] = created_to
        if only_inactive:
            where.append("is_active = false")
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT id, person_id, source_image_path, is_active, is_primary,
                       payload, created_at, updated_at
                FROM person_gallery_embeddings
                {where_sql}
                ORDER BY id
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]

    def gallery_image_references(self) -> list[dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, person_id, source_image_path, is_active, payload, created_at, updated_at
                FROM person_gallery_embeddings
                WHERE source_image_path IS NOT NULL
                   OR payload ? 'registered_crop_path'
                ORDER BY id
                """
            )
            return [dict(row) for row in cur.fetchall()]

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, is_active FROM persons WHERE id = %(id)s", {"id": person_id})
            row = cur.fetchone()
            return dict(row) if row else None

    def get_gallery(self, gallery_id: int) -> dict[str, Any] | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, person_id, is_active, is_primary FROM person_gallery_embeddings WHERE id = %(id)s",
                {"id": gallery_id},
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def soft_delete_person(self, *, person_id: int, operator: str, reason: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE persons
                SET is_active = false,
                    updated_by = %(operator)s,
                    deleted_at = COALESCE(deleted_at, now()),
                    deleted_by = COALESCE(deleted_by, %(operator)s),
                    delete_reason = COALESCE(delete_reason, %(reason)s),
                    payload = jsonb_set(
                        COALESCE(payload, '{}'::jsonb),
                        '{maintenance}',
                        COALESCE(payload->'maintenance', '{}'::jsonb)
                        || jsonb_build_object('deleted_by_storage_maintenance', true),
                        true
                    ),
                    updated_at = now()
                WHERE id = %(person_id)s
                """,
                {"person_id": person_id, "operator": operator, "reason": reason},
            )

    def soft_delete_gallery(self, *, gallery_id: int, operator: str, reason: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE person_gallery_embeddings
                SET is_active = false,
                    is_primary = false,
                    deleted_at = COALESCE(deleted_at, now()),
                    deleted_by = COALESCE(deleted_by, %(operator)s),
                    delete_reason = COALESCE(delete_reason, %(reason)s),
                    payload = jsonb_set(
                        COALESCE(payload, '{}'::jsonb),
                        '{maintenance}',
                        COALESCE(payload->'maintenance', '{}'::jsonb)
                        || jsonb_build_object('deleted_by_storage_maintenance', true),
                        true
                    ),
                    updated_at = now()
                WHERE id = %(gallery_id)s
                """,
                {"gallery_id": gallery_id, "operator": operator, "reason": reason},
            )
