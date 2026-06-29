"""Qdrant-backed registered-person gallery search."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.config import Config
from app.gallery_search import GallerySearchBackend
from app.vector_store import _validate_query_embedding, register_vector_if_supported

logger = logging.getLogger(__name__)

_EMBEDDING_MODEL = "adaface"


@dataclass(frozen=True)
class QdrantGallerySettings:
    url: str
    api_key: str
    collection: str
    timeout_seconds: float
    search_ef: int
    candidate_multiplier: int
    min_candidates: int
    exact_rerank_enabled: bool
    fallback_to_pgvector: bool


class QdrantGallerySearchBackend:
    """Search Qdrant candidates and optionally exact-rerank from PostgreSQL."""

    backend_name = "qdrant"

    def __init__(
        self,
        *,
        settings: QdrantGallerySettings,
        conn: psycopg.Connection,
        fallback_backend: GallerySearchBackend | None = None,
        client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._conn = conn
        self._fallback_backend = fallback_backend
        self._client = client if client is not None else _make_qdrant_client(settings)
        self._models = getattr(self._client, "models", None)
        register_vector_if_supported(conn)

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        *,
        conn: psycopg.Connection,
        fallback_backend: GallerySearchBackend | None = None,
    ) -> "QdrantGallerySearchBackend":
        return cls(
            settings=QdrantGallerySettings(
                url=cfg.qdrant_url,
                api_key=cfg.qdrant_api_key,
                collection=cfg.qdrant_collection,
                timeout_seconds=cfg.qdrant_timeout_seconds,
                search_ef=cfg.qdrant_search_ef,
                candidate_multiplier=cfg.qdrant_candidate_multiplier,
                min_candidates=cfg.qdrant_min_candidates,
                exact_rerank_enabled=cfg.qdrant_exact_rerank_enabled,
                fallback_to_pgvector=cfg.qdrant_fallback_to_pgvector,
            ),
            conn=conn,
            fallback_backend=fallback_backend,
        )

    def search_gallery(
        self,
        embedding: list[float],
        *,
        top_k: int = 10,
        min_similarity: float | None = None,
        person_ids: list[int] | None = None,
        include_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        if person_ids is not None and len(person_ids) == 0:
            return []
        query_vector = _validate_query_embedding(embedding)
        top_k = max(1, min(int(top_k), 100))
        candidate_limit = max(
            top_k * max(1, self._settings.candidate_multiplier),
            max(top_k, self._settings.min_candidates),
        )

        qdrant_started = time.perf_counter()
        try:
            qdrant_rows = self._query_qdrant_candidates(
                query_vector,
                limit=candidate_limit,
                person_ids=person_ids,
            )
            qdrant_ms = int(round((time.perf_counter() - qdrant_started) * 1000))
            if self._settings.exact_rerank_enabled:
                rerank_started = time.perf_counter()
                rows = self._exact_rerank(
                    query_vector,
                    qdrant_rows,
                    top_k=top_k,
                    min_similarity=min_similarity,
                    person_ids=person_ids,
                    include_embedding=include_embedding,
                )
                rerank_ms = int(round((time.perf_counter() - rerank_started) * 1000))
                logger.info(
                    "qdrant_gallery_query_completed route=qdrant_exact_rerank "
                    "collection=%s target_count=%s top_k=%d threshold=%s "
                    "candidate_limit=%d qdrant_result_count=%d result_count=%d "
                    "qdrant_query_duration_ms=%d "
                    "qdrant_exact_rerank_duration_ms=%d qdrant_fallback_count=0",
                    self._settings.collection,
                    len(person_ids) if person_ids is not None else "all",
                    top_k,
                    f"{min_similarity:.4f}" if min_similarity is not None else "",
                    candidate_limit,
                    len(qdrant_rows),
                    len(rows),
                    qdrant_ms,
                    rerank_ms,
                )
                return rows
            rows = _qdrant_hits_to_gallery_rows(qdrant_rows)
            if min_similarity is not None:
                rows = [
                    row for row in rows
                    if float(row.get("similarity") or 0.0) >= min_similarity
                ]
            rows = rows[:top_k]
            if not include_embedding:
                for row in rows:
                    row.pop("embedding", None)
            logger.info(
                "qdrant_gallery_query_completed route=qdrant "
                "collection=%s target_count=%s top_k=%d threshold=%s "
                "candidate_limit=%d qdrant_result_count=%d result_count=%d "
                "qdrant_query_duration_ms=%d qdrant_exact_rerank_duration_ms=0 "
                "qdrant_fallback_count=0",
                self._settings.collection,
                len(person_ids) if person_ids is not None else "all",
                top_k,
                f"{min_similarity:.4f}" if min_similarity is not None else "",
                candidate_limit,
                len(qdrant_rows),
                len(rows),
                qdrant_ms,
            )
            return rows
        except Exception:
            qdrant_ms = int(round((time.perf_counter() - qdrant_started) * 1000))
            if self._settings.fallback_to_pgvector and self._fallback_backend is not None:
                logger.exception(
                    "qdrant_gallery_query_failed_fallback_to_pgvector "
                    "collection=%s target_count=%s top_k=%d threshold=%s "
                    "qdrant_query_duration_ms=%d qdrant_fallback_count=1",
                    self._settings.collection,
                    len(person_ids) if person_ids is not None else "all",
                    top_k,
                    f"{min_similarity:.4f}" if min_similarity is not None else "",
                    qdrant_ms,
                )
                return self._fallback_backend.search_gallery(
                    embedding,
                    top_k=top_k,
                    min_similarity=min_similarity,
                    person_ids=person_ids,
                    include_embedding=include_embedding,
                )
            logger.exception(
                "qdrant_gallery_query_failed_no_fallback collection=%s "
                "target_count=%s top_k=%d threshold=%s "
                "qdrant_query_duration_ms=%d qdrant_fallback_count=0",
                self._settings.collection,
                len(person_ids) if person_ids is not None else "all",
                top_k,
                f"{min_similarity:.4f}" if min_similarity is not None else "",
                qdrant_ms,
            )
            raise

    def _query_qdrant_candidates(
        self,
        query_vector: list[float],
        *,
        limit: int,
        person_ids: list[int] | None,
    ) -> list[Any]:
        models = self._models or _qdrant_models()
        query_filter = build_qdrant_gallery_filter(person_ids=person_ids, models=models)
        kwargs: dict[str, Any] = {
            "collection_name": self._settings.collection,
            "query": query_vector,
            "limit": limit,
            "query_filter": query_filter,
            "with_payload": True,
            "with_vectors": False,
        }
        if self._settings.search_ef > 0:
            kwargs["search_params"] = models.SearchParams(
                hnsw_ef=self._settings.search_ef
            )
        result = self._client.query_points(**kwargs)
        return list(getattr(result, "points", result))

    def _exact_rerank(
        self,
        query_vector: list[float],
        qdrant_hits: list[Any],
        *,
        top_k: int,
        min_similarity: float | None,
        person_ids: list[int] | None,
        include_embedding: bool,
    ) -> list[dict[str, Any]]:
        candidate_ids = [int(_hit_id(hit)) for hit in qdrant_hits]
        if not candidate_ids:
            return []
        rows = self._fetch_candidate_rows(candidate_ids, person_ids=person_ids)
        by_id = {int(row["id"]): row for row in rows}
        reranked: list[dict[str, Any]] = []
        for candidate_id in candidate_ids:
            row = by_id.get(candidate_id)
            if row is None:
                logger.warning(
                    "qdrant_candidate_missing_in_postgres gallery_embedding_id=%s",
                    candidate_id,
                )
                continue
            embedding = _coerce_embedding(row.pop("embedding"))
            similarity = _cosine_similarity(query_vector, embedding)
            if min_similarity is not None and similarity < min_similarity:
                continue
            row["similarity"] = similarity
            row["distance"] = 1.0 - similarity
            if include_embedding:
                row["embedding"] = embedding
            reranked.append(row)
        reranked.sort(key=lambda item: float(item["similarity"]), reverse=True)
        return reranked[:top_k]

    def _fetch_candidate_rows(
        self,
        candidate_ids: list[int],
        *,
        person_ids: list[int] | None,
    ) -> list[dict[str, Any]]:
        where = [
            "p.is_active = true",
            "pge.is_active = true",
            "pge.embedding IS NOT NULL",
            "pge.id = ANY(%(candidate_ids)s::bigint[])",
        ]
        params: dict[str, Any] = {"candidate_ids": candidate_ids}
        if person_ids is not None:
            where.append("pge.person_id = ANY(%(person_ids)s::bigint[])")
            params["person_ids"] = person_ids
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT
                    pge.id,
                    pge.person_id,
                    p.name AS person_name,
                    p.external_person_id,
                    pge.source_type,
                    pge.embedding_model,
                    pge.is_primary,
                    pge.quality,
                    pge.embedding,
                    pge.created_at
                FROM person_gallery_embeddings pge
                JOIN persons p ON p.id = pge.person_id
                WHERE {' AND '.join(where)}
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]


def build_qdrant_gallery_filter(*, person_ids: list[int] | None, models: Any = None) -> Any:
    """Build the Qdrant payload filter for active AdaFace gallery rows."""
    if person_ids is not None and len(person_ids) == 0:
        raise ValueError("empty person_ids must be handled before Qdrant search")
    models = models or _qdrant_models()
    must = [
        models.FieldCondition(
            key="is_active",
            match=models.MatchValue(value=True),
        ),
        models.FieldCondition(
            key="embedding_model",
            match=models.MatchValue(value=_EMBEDDING_MODEL),
        ),
    ]
    if person_ids is not None:
        must.append(
            models.FieldCondition(
                key="person_id",
                match=models.MatchAny(any=[int(value) for value in person_ids]),
            )
        )
    return models.Filter(must=must)


def _make_qdrant_client(settings: QdrantGallerySettings) -> Any:
    try:
        from qdrant_client import QdrantClient
    except ModuleNotFoundError as exc:
        raise RuntimeError("qdrant-client is required for FACE_VECTOR_BACKEND=qdrant") from exc
    api_key = settings.api_key or None
    return QdrantClient(
        url=settings.url,
        api_key=api_key,
        timeout=settings.timeout_seconds,
    )


def _qdrant_models() -> Any:
    try:
        from qdrant_client import models
    except ModuleNotFoundError as exc:
        raise RuntimeError("qdrant-client is required for Qdrant gallery search") from exc
    return models


def _qdrant_hits_to_gallery_rows(hits: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for hit in hits:
        payload = dict(getattr(hit, "payload", None) or {})
        score = float(getattr(hit, "score", payload.get("score", 0.0)) or 0.0)
        rows.append(
            {
                "id": int(payload.get("gallery_embedding_id") or _hit_id(hit)),
                "person_id": int(payload["person_id"]),
                "person_name": payload.get("person_name") or "",
                "external_person_id": payload.get("external_person_id"),
                "source_type": payload.get("source_type"),
                "embedding_model": payload.get("embedding_model") or _EMBEDDING_MODEL,
                "is_primary": bool(payload.get("is_primary", False)),
                "quality": payload.get("quality"),
                "similarity": score,
                "distance": 1.0 - score,
                "created_at": payload.get("created_at"),
            }
        )
    rows.sort(key=lambda item: float(item["similarity"]), reverse=True)
    return rows


def _hit_id(hit: Any) -> Any:
    if hasattr(hit, "id"):
        return getattr(hit, "id")
    return hit["id"]


def _coerce_embedding(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        return [float(item) for item in value.tolist()]
    return [float(item) for item in value]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = 0.0
    left_sq = 0.0
    right_sq = 0.0
    for lval, rval in zip(left, right):
        dot += lval * rval
        left_sq += lval * lval
        right_sq += rval * rval
    denom = math.sqrt(left_sq) * math.sqrt(right_sq)
    if denom <= 0:
        return 0.0
    return dot / denom
