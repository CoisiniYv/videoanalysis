"""PeopleRepository - read-side API queries for persons and gallery metadata."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row


class PeopleRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def list_people(
        self,
        *,
        include_inactive: bool = False,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if not include_inactive:
            where.append("p.is_active = true")
        if q:
            params["q"] = f"%{q}%"
            where.append("(p.name ILIKE %(q)s OR p.external_person_id ILIKE %(q)s)")
        where_sql = "WHERE " + " AND ".join(where) if where else ""

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT count(*) AS total
                FROM persons p
                {where_sql}
                """,
                params,
            )
            total = int(cur.fetchone()["total"])
            cur.execute(
                f"""
                SELECT
                    p.id AS person_id,
                    p.name,
                    p.external_person_id,
                    p.description,
                    p.is_active,
                    p.created_at,
                    p.updated_at,
                    count(pge.id) FILTER (WHERE pge.is_active = true) AS active_gallery_count,
                    max(pge.id) FILTER (
                        WHERE pge.is_active = true AND pge.is_primary = true
                    ) AS primary_gallery_embedding_id,
                    (
                        array_remove(
                            array_agg(
                                pge.source_image_path
                                ORDER BY pge.is_primary DESC, pge.id DESC
                            ) FILTER (
                                WHERE pge.is_active = true
                                  AND pge.source_image_path IS NOT NULL
                            ),
                            NULL
                        )
                    )[1] AS primary_source_image_path,
                    (
                        array_remove(
                            array_agg(
                                pge.payload ->> 'registered_crop_path'
                                ORDER BY pge.is_primary DESC, pge.id DESC
                            ) FILTER (
                                WHERE pge.is_active = true
                                  AND pge.payload ->> 'registered_crop_path' IS NOT NULL
                            ),
                            NULL
                        )
                    )[1] AS primary_registered_crop_path
                FROM persons p
                LEFT JOIN person_gallery_embeddings pge ON pge.person_id = p.id
                {where_sql}
                GROUP BY p.id
                ORDER BY p.id
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                params,
            )
            return list(cur.fetchall()), total

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, name, external_person_id, description, is_active,
                       created_by, updated_by, payload, created_at, updated_at
                FROM persons
                WHERE id = %(person_id)s
                """,
                {"person_id": person_id},
            )
            return cur.fetchone()

    def list_gallery(self, person_id: int) -> list[dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, person_id, source_type, source_image_path,
                       source_observation_id, embedding_model, model_version,
                       embedding_dim, embedding_norm, quality, face_bbox, landmarks,
                       is_primary, is_active, payload, created_at, updated_at
                FROM person_gallery_embeddings
                WHERE person_id = %(person_id)s
                ORDER BY is_active DESC, is_primary DESC, id DESC
                """,
                {"person_id": person_id},
            )
            return list(cur.fetchall())
