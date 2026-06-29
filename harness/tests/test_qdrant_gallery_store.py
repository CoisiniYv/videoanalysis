"""Qdrant gallery-search adapter contract tests."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FACE_WORKER_ROOT = REPO_ROOT / "services" / "face-worker"
FACE_WORKER_ROOT_STR = str(FACE_WORKER_ROOT)
if FACE_WORKER_ROOT_STR in sys.path:
    sys.path.remove(FACE_WORKER_ROOT_STR)
sys.path.insert(0, FACE_WORKER_ROOT_STR)
for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.gallery_search import HybridGallerySearchBackend, ShadowGallerySearchBackend
from app.qdrant_gallery_store import (
    QdrantGallerySearchBackend,
    QdrantGallerySettings,
    _cosine_similarity,
    _qdrant_hits_to_gallery_rows,
    build_qdrant_gallery_filter,
)


def _unit_embedding() -> list[float]:
    value = 1.0 / math.sqrt(512)
    return [value] * 512


class _FakeModels:
    class SearchParams:
        def __init__(self, hnsw_ef):
            self.hnsw_ef = hnsw_ef

    class MatchValue:
        def __init__(self, value):
            self.value = value

    class MatchAny:
        def __init__(self, any):
            self.any = any

    class FieldCondition:
        def __init__(self, key, match):
            self.key = key
            self.match = match

    class Filter:
        def __init__(self, must):
            self.must = must


class _FakeClient:
    def __init__(self, hits=None, error: Exception | None = None):
        self.hits = hits or []
        self.error = error
        self.calls = []
        self.models = _FakeModels

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(points=self.hits)


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self.rows)


class _FakeConn:
    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)

    def cursor(self, *_, **__):
        return self.cursor_obj


def _settings(**overrides):
    values = {
        "url": "http://qdrant:6333",
        "api_key": "",
        "collection": "face_gallery_current",
        "prefer_grpc": True,
        "timeout_seconds": 2.0,
        "search_ef": 0,
        "candidate_multiplier": 3,
        "min_candidates": 20,
        "exact_rerank_enabled": True,
        "fallback_to_pgvector": True,
    }
    values.update(overrides)
    return QdrantGallerySettings(**values)


def test_filter_targets_person_ids_without_broadening_empty_list():
    flt = build_qdrant_gallery_filter(person_ids=[7, 9], models=_FakeModels)
    by_key = {condition.key: condition.match for condition in flt.must}

    assert by_key["is_active"].value is True
    assert by_key["embedding_model"].value == "adaface"
    assert by_key["person_id"].any == [7, 9]

    with pytest.raises(ValueError, match="empty person_ids"):
        build_qdrant_gallery_filter(person_ids=[], models=_FakeModels)


def test_qdrant_score_maps_to_similarity_and_distance():
    hit = SimpleNamespace(
        id=123,
        score=0.8125,
        payload={
            "gallery_embedding_id": 123,
            "person_id": 44,
            "person_name": "Reese",
            "external_person_id": "demo:midterm:reese",
            "source_type": "manual_upload",
            "embedding_model": "adaface",
            "is_primary": True,
            "quality": 0.9,
            "created_at": "2026-06-29T00:00:00Z",
        },
    )
    row = _qdrant_hits_to_gallery_rows([hit])[0]
    assert row["id"] == 123
    assert row["person_id"] == 44
    assert row["similarity"] == pytest.approx(0.8125)
    assert row["distance"] == pytest.approx(0.1875)


def test_exact_rerank_uses_postgres_candidate_vectors_and_threshold():
    query = _unit_embedding()
    good = query[:]
    bad = [-value for value in query]
    rows = [
        {
            "id": 1,
            "person_id": 10,
            "person_name": "Good",
            "external_person_id": "good",
            "source_type": "manual_upload",
            "embedding_model": "adaface",
            "is_primary": True,
            "quality": 0.9,
            "embedding": good,
            "created_at": None,
        },
        {
            "id": 2,
            "person_id": 11,
            "person_name": "Bad",
            "external_person_id": "bad",
            "source_type": "manual_upload",
            "embedding_model": "adaface",
            "is_primary": True,
            "quality": 0.9,
            "embedding": bad,
            "created_at": None,
        },
    ]
    client = _FakeClient(
        hits=[
            SimpleNamespace(id=2, score=0.99, payload={"gallery_embedding_id": 2, "person_id": 11}),
            SimpleNamespace(id=1, score=0.60, payload={"gallery_embedding_id": 1, "person_id": 10}),
        ]
    )
    backend = QdrantGallerySearchBackend(
        settings=_settings(),
        conn=_FakeConn(rows),
        client=client,
    )

    result = backend.search_gallery(
        query,
        top_k=5,
        min_similarity=0.60,
        person_ids=[10, 11],
    )

    assert [row["person_id"] for row in result] == [10]
    assert result[0]["similarity"] == pytest.approx(1.0)


def test_qdrant_error_falls_back_to_pgvector_when_enabled():
    class _Fallback:
        def __init__(self):
            self.calls = 0

        def search_gallery(self, *args, **kwargs):
            self.calls += 1
            return [{"person_id": 1, "id": 1, "similarity": 0.9}]

    fallback = _Fallback()
    backend = QdrantGallerySearchBackend(
        settings=_settings(fallback_to_pgvector=True),
        conn=_FakeConn([]),
        client=_FakeClient(error=TimeoutError("boom")),
        fallback_backend=fallback,
    )
    rows = backend.search_gallery(_unit_embedding(), person_ids=[1])
    assert rows[0]["person_id"] == 1
    assert fallback.calls == 1


def test_hybrid_routes_small_target_to_pgvector_and_large_to_qdrant():
    pg = MagicMock()
    qd = MagicMock()
    pg.search_gallery.return_value = ["pg"]
    qd.search_gallery.return_value = ["qd"]
    backend = HybridGallerySearchBackend(
        pgvector=pg,
        qdrant=qd,
        small_target_threshold=2,
    )

    assert backend.search_gallery(_unit_embedding(), person_ids=[1, 2]) == ["pg"]
    assert backend.search_gallery(_unit_embedding(), person_ids=[1, 2, 3]) == ["qd"]
    assert backend.search_gallery(_unit_embedding(), person_ids=[]) == []


def test_shadow_backend_returns_pgvector_results():
    pg = MagicMock()
    qd = MagicMock()
    pg.search_gallery.return_value = [{"person_id": 1}]
    qd.search_gallery.return_value = [{"person_id": 1}]
    backend = ShadowGallerySearchBackend(primary=pg, shadow=qd)

    assert backend.search_gallery(_unit_embedding(), person_ids=[1]) == [{"person_id": 1}]
    assert pg.search_gallery.called
    assert qd.search_gallery.called


def test_cosine_similarity_boundary():
    query = _unit_embedding()
    assert _cosine_similarity(query, query) == pytest.approx(1.0)
    opposite = [-value for value in query]
    assert _cosine_similarity(query, opposite) == pytest.approx(-1.0)
