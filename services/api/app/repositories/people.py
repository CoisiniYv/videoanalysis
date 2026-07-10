"""PeopleRepository - read-side API queries for persons and gallery metadata."""

from __future__ import annotations

from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row


class PeopleRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        if isinstance(conn, psycopg.Connection):
            register_vector(conn)

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

    def latest_location(
        self,
        person_id: int,
        *,
        min_similarity: float = 0.6,
        camera_id: str | None = None,
        source_id: str | None = None,
        start_ts_ms: int | None = None,
        end_ts_ms: int | None = None,
        include_unregistered_sources: bool = False,
    ) -> dict[str, Any] | None:
        rows = self.trajectory(
            person_id,
            min_similarity=min_similarity,
            camera_id=camera_id,
            source_id=source_id,
            start_ts_ms=start_ts_ms,
            end_ts_ms=end_ts_ms,
            include_unregistered_sources=include_unregistered_sources,
            limit=1,
            offset=0,
        )
        return rows[0] if rows else None

    def trajectory(
        self,
        person_id: int,
        *,
        min_similarity: float = 0.6,
        camera_id: str | None = None,
        source_id: str | None = None,
        start_ts_ms: int | None = None,
        end_ts_ms: int | None = None,
        include_unregistered_sources: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                WITH person_embeddings AS (
                    SELECT id AS gallery_embedding_id, person_id, embedding
                    FROM person_gallery_embeddings
                    WHERE person_id = %(person_id)s
                      AND is_active = true
                ),
                observation_matches AS (
                    SELECT DISTINCT ON (fo.source_observation_id)
                        NULL::uuid AS event_id,
                        NULL::text AS source_event_id,
                        'person_search_observation'::text AS event_type,
                        pe.person_id,
                        fo.camera_id,
                        fo.source_id,
                        fo.timestamp_ms AS event_ts_ms,
                        fo.created_at AS event_created_at,
                        1 - (fo.embedding <=> pe.embedding) AS similarity,
                        fo.source_observation_id,
                        fo.timestamp_ms AS observation_timestamp_ms,
                        fo.face_bbox,
                        fo.person_bbox,
                        fo.crop_path AS face_crop_uri,
                        fo.snapshot_path AS full_frame_uri,
                        NULL::text AS annotated_frame_uri,
                        NULL::text AS evidence_media_status,
                        '{}'::jsonb AS evidence_summary,
                        2 AS source_rank
                    FROM face_observations fo
                    JOIN person_embeddings pe ON true
                    WHERE 1 - (fo.embedding <=> pe.embedding) >= %(min_similarity)s
                    ORDER BY fo.source_observation_id,
                             1 - (fo.embedding <=> pe.embedding) DESC,
                             fo.timestamp_ms DESC
                ),
                face_events AS (
                    SELECT
                        e.id AS event_id,
                        e.source_event_id,
                        e.event_type,
                        e.person_id,
                        e.camera_id,
                        e.source_id,
                        e.event_ts_ms,
                        e.created_at AS event_created_at,
                        e.confidence,
                        e.payload,
                        e.payload->'match'->>'source_observation_id'
                            AS source_observation_id,
                        COALESCE(
                            NULLIF(e.payload->'match'->>'similarity', '')::double precision,
                            e.confidence
                        ) AS similarity,
                        1 AS source_rank
                    FROM events e
                    WHERE e.person_id = %(person_id)s
                      AND e.event_type IN ('watchlist_hit', 'live_search_hit')
                ),
                event_matches AS (
                    SELECT
                        fe.event_id,
                        fe.source_event_id,
                        fe.event_type,
                        fe.person_id,
                        COALESCE(fe.camera_id, fo.camera_id) AS camera_id,
                        COALESCE(fe.source_id, fo.source_id) AS source_id,
                        fe.event_ts_ms,
                        fe.event_created_at,
                        fe.similarity,
                        fe.source_observation_id,
                        fo.timestamp_ms AS observation_timestamp_ms,
                        fo.face_bbox,
                        fo.person_bbox,
                        COALESCE(face_crop.uri, fo.crop_path) AS face_crop_uri,
                        COALESCE(full_frame.uri, fo.snapshot_path) AS full_frame_uri,
                        annotated_frame.uri AS annotated_frame_uri,
                        eb.media_status AS evidence_media_status,
                        jsonb_strip_nulls(
                            jsonb_build_object(
                                'playback_kind', eb.summary->>'playback_kind',
                                'evidence_mode', eb.summary->>'evidence_mode',
                                'image_status', eb.summary->>'image_status'
                            )
                        ) AS evidence_summary,
                        fe.source_rank
                    FROM face_events fe
                    LEFT JOIN face_observations fo
                      ON fo.source_observation_id = fe.source_observation_id
                    LEFT JOIN evidence_bundles eb ON eb.event_id = fe.event_id
                    LEFT JOIN evidence_artifacts face_crop
                      ON face_crop.event_id = fe.event_id
                     AND face_crop.artifact_type = 'face_crop'
                    LEFT JOIN evidence_artifacts full_frame
                      ON full_frame.event_id = fe.event_id
                     AND full_frame.artifact_type = 'full_frame'
                    LEFT JOIN evidence_artifacts annotated_frame
                      ON annotated_frame.event_id = fe.event_id
                     AND annotated_frame.artifact_type = 'annotated_frame'
                    WHERE COALESCE(fe.similarity, 0.0) >= %(min_similarity)s
                ),
                ranked_matches AS (
                    SELECT DISTINCT ON (source_observation_id) *
                    FROM (
                        SELECT * FROM event_matches
                        UNION ALL
                        SELECT * FROM observation_matches
                    ) all_matches
                    WHERE COALESCE(source_observation_id, '') <> ''
                    ORDER BY source_observation_id, source_rank ASC, event_ts_ms DESC
                )
                SELECT
                    rm.event_id::text,
                    rm.source_event_id,
                    rm.event_type,
                    rm.person_id,
                    p.name AS person_name,
                    p.external_person_id,
                    rm.camera_id,
                    rm.source_id,
                    c.name AS camera_name,
                    rm.event_ts_ms,
                    rm.event_created_at,
                    rm.similarity,
                    rm.source_observation_id,
                    rm.observation_timestamp_ms,
                    rm.face_bbox,
                    rm.person_bbox,
                    rm.face_crop_uri,
                    rm.full_frame_uri,
                    rm.annotated_frame_uri,
                    rm.evidence_media_status,
                    rm.evidence_summary,
                    CASE
                        WHEN rm.source_rank = 1 THEN 'watchlist_event'
                        ELSE 'gallery_observation'
                    END AS trajectory_source
                FROM ranked_matches rm
                JOIN persons p ON p.id = rm.person_id
                LEFT JOIN cameras c
                  ON c.id::text = rm.camera_id
                  OR c.source_id = rm.source_id
                WHERE (%(include_unregistered_sources)s::boolean OR c.id IS NOT NULL)
                  AND (%(camera_id)s::text IS NULL
                       OR rm.camera_id = %(camera_id)s::text
                       OR c.id::text = %(camera_id)s::text)
                  AND (%(source_id)s::text IS NULL
                       OR rm.source_id = %(source_id)s::text
                       OR c.source_id = %(source_id)s::text)
                  AND (%(start_ts_ms)s::bigint IS NULL
                       OR rm.event_ts_ms >= %(start_ts_ms)s::bigint)
                  AND (%(end_ts_ms)s::bigint IS NULL
                       OR rm.event_ts_ms <= %(end_ts_ms)s::bigint)
                ORDER BY rm.event_ts_ms DESC NULLS LAST,
                         rm.event_created_at DESC,
                         rm.source_observation_id DESC
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                {
                    "person_id": person_id,
                    "min_similarity": min_similarity,
                    "camera_id": camera_id,
                    "source_id": source_id,
                    "start_ts_ms": start_ts_ms,
                    "end_ts_ms": end_ts_ms,
                    "include_unregistered_sources": include_unregistered_sources,
                    "limit": limit,
                    "offset": offset,
                },
            )
            return list(cur.fetchall())
