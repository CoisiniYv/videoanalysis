"""Gallery search backend selection for face-worker watchlist matching."""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import psycopg

from app.config import Config
from app.vector_store import FaceVectorStore

logger = logging.getLogger(__name__)


class GallerySearchBackend(Protocol):
    def search_gallery(
        self,
        embedding: list[float],
        *,
        top_k: int = 10,
        min_similarity: float | None = None,
        person_ids: list[int] | None = None,
        include_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        ...


class PgvectorGallerySearchBackend:
    """Rollback-safe wrapper around the existing exact pgvector search."""

    backend_name = "pgvector"

    def __init__(self, conn: psycopg.Connection) -> None:
        self._store = FaceVectorStore(conn)

    def search_gallery(
        self,
        embedding: list[float],
        *,
        top_k: int = 10,
        min_similarity: float | None = None,
        person_ids: list[int] | None = None,
        include_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        return self._store.search_gallery(
            embedding,
            top_k=top_k,
            min_similarity=min_similarity,
            person_ids=person_ids,
            include_embedding=include_embedding,
        )


class ShadowGallerySearchBackend:
    """Use pgvector for decisions and Qdrant only for parity telemetry."""

    backend_name = "shadow"

    def __init__(
        self,
        primary: GallerySearchBackend,
        shadow: GallerySearchBackend,
    ) -> None:
        self._primary = primary
        self._shadow = shadow

    def search_gallery(
        self,
        embedding: list[float],
        *,
        top_k: int = 10,
        min_similarity: float | None = None,
        person_ids: list[int] | None = None,
        include_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        primary_started = time.perf_counter()
        primary_rows = self._primary.search_gallery(
            embedding,
            top_k=top_k,
            min_similarity=min_similarity,
            person_ids=person_ids,
            include_embedding=include_embedding,
        )
        primary_ms = int(round((time.perf_counter() - primary_started) * 1000))

        shadow_started = time.perf_counter()
        try:
            shadow_rows = self._shadow.search_gallery(
                embedding,
                top_k=top_k,
                min_similarity=min_similarity,
                person_ids=person_ids,
                include_embedding=include_embedding,
            )
            shadow_ms = int(round((time.perf_counter() - shadow_started) * 1000))
            mismatch = _shadow_mismatch(primary_rows, shadow_rows)
            logger.info(
                "qdrant_shadow_query_completed target_count=%s top_k=%d "
                "threshold=%s pgvector_query_duration_ms=%d "
                "qdrant_query_duration_ms=%d shadow_result_count=%d "
                "primary_result_count=%d qdrant_shadow_mismatch=%d "
                "qdrant_shadow_top1_primary=%s qdrant_shadow_top1_shadow=%s",
                len(person_ids) if person_ids is not None else "all",
                top_k,
                f"{min_similarity:.4f}" if min_similarity is not None else "",
                primary_ms,
                shadow_ms,
                len(shadow_rows),
                len(primary_rows),
                1 if mismatch else 0,
                _top1_person_id(primary_rows),
                _top1_person_id(shadow_rows),
            )
        except Exception:
            shadow_ms = int(round((time.perf_counter() - shadow_started) * 1000))
            logger.exception(
                "qdrant_shadow_query_failed target_count=%s top_k=%d "
                "threshold=%s pgvector_query_duration_ms=%d "
                "qdrant_query_duration_ms=%d",
                len(person_ids) if person_ids is not None else "all",
                top_k,
                f"{min_similarity:.4f}" if min_similarity is not None else "",
                primary_ms,
                shadow_ms,
            )
        return primary_rows


class HybridGallerySearchBackend:
    """Route small target lists to pgvector and larger/broad searches to Qdrant."""

    backend_name = "hybrid"

    def __init__(
        self,
        *,
        pgvector: GallerySearchBackend,
        qdrant: GallerySearchBackend,
        small_target_threshold: int,
    ) -> None:
        self._pgvector = pgvector
        self._qdrant = qdrant
        self._small_target_threshold = max(0, int(small_target_threshold))

    def search_gallery(
        self,
        embedding: list[float],
        *,
        top_k: int = 10,
        min_similarity: float | None = None,
        person_ids: list[int] | None = None,
        include_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        target_count = None if person_ids is None else len(person_ids)
        if target_count == 0:
            logger.info("gallery_search_route selected=empty_target result_count=0")
            return []
        if target_count is not None and target_count <= self._small_target_threshold:
            route = "pgvector_exact"
            backend = self._pgvector
        else:
            route = "qdrant"
            backend = self._qdrant
        logger.info(
            "gallery_search_route selected=%s target_count=%s "
            "small_target_threshold=%d",
            route,
            target_count if target_count is not None else "all",
            self._small_target_threshold,
        )
        return backend.search_gallery(
            embedding,
            top_k=top_k,
            min_similarity=min_similarity,
            person_ids=person_ids,
            include_embedding=include_embedding,
        )


def build_gallery_search_backend(
    cfg: Config,
    conn: psycopg.Connection,
) -> GallerySearchBackend:
    backend = cfg.face_vector_backend
    pgvector = PgvectorGallerySearchBackend(conn)
    if backend == "pgvector":
        return pgvector
    if backend not in {"qdrant", "shadow", "hybrid"}:
        raise ValueError(
            "FACE_VECTOR_BACKEND must be one of pgvector, shadow, qdrant, hybrid; "
            f"got {backend!r}"
        )

    from app.qdrant_gallery_store import QdrantGallerySearchBackend

    qdrant = QdrantGallerySearchBackend.from_config(
        cfg,
        conn=conn,
        fallback_backend=pgvector,
    )
    if backend == "qdrant":
        return qdrant
    if backend == "shadow":
        return ShadowGallerySearchBackend(primary=pgvector, shadow=qdrant)
    return HybridGallerySearchBackend(
        pgvector=pgvector,
        qdrant=qdrant,
        small_target_threshold=cfg.face_vector_small_target_threshold,
    )


def _top1_person_id(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    try:
        return str(int(rows[0]["person_id"]))
    except Exception:
        return str(rows[0].get("person_id", ""))


def _shadow_mismatch(
    primary_rows: list[dict[str, Any]],
    shadow_rows: list[dict[str, Any]],
) -> bool:
    if bool(primary_rows) != bool(shadow_rows):
        return True
    if not primary_rows:
        return False
    if _top1_person_id(primary_rows) != _top1_person_id(shadow_rows):
        return True
    primary_ids = [str(row.get("person_id")) for row in primary_rows]
    shadow_ids = [str(row.get("person_id")) for row in shadow_rows]
    return primary_ids != shadow_ids
