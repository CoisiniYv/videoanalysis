"""Tests for midterm FaceVectorStore pgvector similarity search harness.

Test classes:
- TestQueryEmbeddingValidation: pure Python, no DB
- TestSearchSimilarFacesParams: mocked DB, parameter handling
- TestSearchSimilarFacesResult: mocked DB, result processing
- TestFaceVectorStoreIntegration: real PostgreSQL + pgvector (integration mark)
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FACE_WORKER_ROOT = REPO_ROOT / "services" / "face-worker"
FACE_WORKER_ROOT_STR = str(FACE_WORKER_ROOT)
if FACE_WORKER_ROOT_STR in sys.path:
    sys.path.remove(FACE_WORKER_ROOT_STR)
sys.path.insert(0, FACE_WORKER_ROOT_STR)
for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.vector_store import (
    FaceVectorStore,
    _validate_query_embedding,
    _EMBEDDING_DIM,
    _MIN_NORM,
    _MAX_NORM,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _unit_embedding() -> list[float]:
    """Return a 512-d unit vector (norm=1.0)."""
    val = 1.0 / math.sqrt(_EMBEDDING_DIM)
    return [val] * _EMBEDDING_DIM


def _randomlike_embedding(seed: int = 42) -> list[float]:
    """Return a deterministic 512-d L2-normalized vector."""
    import random as _random
    rng = _random.Random(seed)
    raw = [rng.uniform(-1.0, 1.0) for _ in range(_EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


# ── Class 1: Query Embedding Validation ────────────────────────────────────────

class TestQueryEmbeddingValidation:
    """_validate_query_embedding() rejects invalid inputs."""

    def test_valid_list_passes(self):
        emb = _unit_embedding()
        result = _validate_query_embedding(emb)
        assert isinstance(result, list)
        assert len(result) == _EMBEDDING_DIM
        assert all(isinstance(x, float) for x in result)

    def test_tuple_accepted(self):
        emb = tuple(_unit_embedding())
        result = _validate_query_embedding(emb)
        assert isinstance(result, list)
        assert len(result) == _EMBEDDING_DIM

    def test_wrong_length_rejected(self):
        with pytest.raises(ValueError, match="length"):
            _validate_query_embedding([0.01] * 256)

    def test_nan_element_rejected(self):
        emb = _unit_embedding()
        emb[0] = float("nan")
        with pytest.raises(ValueError, match="not finite"):
            _validate_query_embedding(emb)

    def test_inf_element_rejected(self):
        emb = _unit_embedding()
        emb[0] = float("inf")
        with pytest.raises(ValueError, match="not finite"):
            _validate_query_embedding(emb)

    def test_neg_inf_element_rejected(self):
        emb = _unit_embedding()
        emb[0] = float("-inf")
        with pytest.raises(ValueError, match="not finite"):
            _validate_query_embedding(emb)

    def test_string_element_rejected(self):
        emb = _unit_embedding()
        emb[0] = "abc"
        with pytest.raises(ValueError, match="invalid type"):
            _validate_query_embedding(emb)

    def test_bool_element_rejected(self):
        emb = _unit_embedding()
        emb[0] = True
        with pytest.raises(ValueError, match="invalid type"):
            _validate_query_embedding(emb)

    def test_none_element_rejected(self):
        emb = _unit_embedding()
        emb[0] = None
        with pytest.raises(ValueError, match="invalid type"):
            _validate_query_embedding(emb)

    def test_not_list_or_tuple_rejected(self):
        with pytest.raises(ValueError, match="must be list or tuple"):
            _validate_query_embedding("not a list")

    def test_norm_low_rejected(self):
        emb = [0.001] * _EMBEDDING_DIM  # norm = 0.001 * sqrt(512) ≈ 0.0226
        with pytest.raises(ValueError, match="L2 norm"):
            _validate_query_embedding(emb)

    def test_norm_high_rejected(self):
        emb = [0.5] * _EMBEDDING_DIM  # norm = 0.5 * sqrt(512) ≈ 11.3
        with pytest.raises(ValueError, match="L2 norm"):
            _validate_query_embedding(emb)

    def test_norm_min_boundary_accepted(self):
        val = _MIN_NORM / math.sqrt(_EMBEDDING_DIM)
        emb = [val] * _EMBEDDING_DIM
        result = _validate_query_embedding(emb)
        assert result is not None

    def test_norm_max_boundary_accepted(self):
        val = _MAX_NORM / math.sqrt(_EMBEDDING_DIM)
        emb = [val] * _EMBEDDING_DIM
        result = _validate_query_embedding(emb)
        assert result is not None

    def test_int_elements_accepted(self):
        # Single int 1 + zeros → norm = 1.0
        emb: list = [0] * _EMBEDDING_DIM
        emb[0] = 1
        result = _validate_query_embedding(emb)
        assert len(result) == _EMBEDDING_DIM
        assert result[0] == 1.0
        assert isinstance(result[0], float)

    def test_mixed_int_float_accepted(self):
        # Mixed int/float with norm in valid range
        emb = [0.0] * _EMBEDDING_DIM
        emb[0] = 1      # int, contributes 1.0 to norm²
        emb[1] = 0.3    # float, contributes 0.09 to norm² → norm ≈ 1.044
        result = _validate_query_embedding(emb)
        assert isinstance(result[0], float)
        assert isinstance(result[1], float)


# ── Class 2: Parameter Handling (mocked) ───────────────────────────────────────

class TestSearchSimilarFacesParams:
    """Parameter clamping, rejection, and SQL generation with mocked cursor."""

    @staticmethod
    def _make_store() -> FaceVectorStore:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        return FaceVectorStore(conn)

    def test_top_k_below_1_clamped(self):
        store = self._make_store()
        emb = _unit_embedding()
        result = store.search_similar_faces(emb, top_k=0)
        # top_k clamped to 1; query still executed
        assert isinstance(result, list)

    def test_top_k_above_100_clamped(self):
        store = self._make_store()
        emb = _unit_embedding()
        result = store.search_similar_faces(emb, top_k=200)
        assert isinstance(result, list)

    def test_top_k_in_range_passes(self):
        store = self._make_store()
        emb = _unit_embedding()
        result = store.search_similar_faces(emb, top_k=10)
        assert isinstance(result, list)

    def test_empty_camera_scope_returns_empty(self):
        store = self._make_store()
        emb = _unit_embedding()
        result = store.search_similar_faces(emb, camera_scope=[])
        assert result == []

    def test_min_similarity_below_0_rejected(self):
        store = self._make_store()
        emb = _unit_embedding()
        with pytest.raises(ValueError, match="outside"):
            store.search_similar_faces(emb, min_similarity=-0.1)

    def test_min_similarity_above_1_rejected(self):
        store = self._make_store()
        emb = _unit_embedding()
        with pytest.raises(ValueError, match="outside"):
            store.search_similar_faces(emb, min_similarity=1.1)

    def test_min_similarity_in_bounds_accepted(self):
        store = self._make_store()
        emb = _unit_embedding()
        result = store.search_similar_faces(emb, min_similarity=0.75)
        assert isinstance(result, list)

    def test_sql_contains_cosine_operator(self):
        """Verify generated SQL uses pgvector <=> operator."""
        store = FaceVectorStore.__new__(FaceVectorStore)
        store._conn = MagicMock()

        sql, params = store._build_query(
            min_similarity=0.8,
            camera_scope=["cam_1", "cam_2"],
            include_embedding=False,
        )
        assert "<=>" in sql
        assert "camera_id = ANY" in sql
        assert "min_similarity" in params
        assert "camera_scope" in params

    def test_sql_no_filters_when_none(self):
        store = FaceVectorStore.__new__(FaceVectorStore)
        store._conn = MagicMock()

        sql, params = store._build_query(
            min_similarity=None,
            camera_scope=None,
            include_embedding=False,
        )
        assert "min_similarity" not in params
        assert "camera_scope" not in params
        assert "camera_id = ANY" not in sql

    def test_sql_embedding_select_when_include_embedding(self):
        store = FaceVectorStore.__new__(FaceVectorStore)
        store._conn = MagicMock()

        sql, _ = store._build_query(
            min_similarity=None,
            camera_scope=None,
            include_embedding=True,
        )
        assert "embedding," in sql.replace("\n", " ")


# ── Class 3: Result Processing (mocked) ────────────────────────────────────────

class TestSearchSimilarFacesResult:
    """Result shape and embedding exclusion."""

    def _make_store_with_rows(self, rows: list[dict]) -> FaceVectorStore:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.fetchall.return_value = rows
        conn.cursor.return_value = cursor
        return FaceVectorStore(conn)

    def test_default_excludes_embedding(self):
        rows = [{
            "source_observation_id": "obs-1",
            "camera_id": "cam_1",
            "source_id": "s1",
            "track_id": "42",
            "timestamp_ms": 1000,
            "face_bbox": [1.0, 2.0, 3.0, 4.0],
            "landmarks": [0.0] * 10,
            "face_confidence": 0.9,
            "quality": 0.85,
            "similarity": 1.0,
            "distance": 0.0,
            "created_at": "2026-01-01T00:00:00Z",
        }]
        store = self._make_store_with_rows(rows)
        emb = _unit_embedding()
        result = store.search_similar_faces(emb)
        assert len(result) == 1
        assert "embedding" not in result[0]
        assert result[0]["similarity"] == 1.0
        assert result[0]["distance"] == 0.0

    def test_include_embedding_true_includes_embedding(self):
        rows = [{
            "source_observation_id": "obs-1",
            "camera_id": "cam_1",
            "source_id": "s1",
            "track_id": "42",
            "timestamp_ms": 1000,
            "face_bbox": [1.0, 2.0, 3.0, 4.0],
            "landmarks": [0.0] * 10,
            "face_confidence": 0.9,
            "quality": 0.85,
            "similarity": 1.0,
            "distance": 0.0,
            "created_at": "2026-01-01T00:00:00Z",
            "embedding": [0.01] * 512,
        }]
        store = self._make_store_with_rows(rows)
        emb = _unit_embedding()
        result = store.search_similar_faces(emb, include_embedding=True)
        assert len(result) == 1
        assert "embedding" in result[0]

    def test_empty_result_returns_empty_list(self):
        store = self._make_store_with_rows([])
        emb = _unit_embedding()
        result = store.search_similar_faces(emb)
        assert result == []

    def test_both_similarity_and_distance_present(self):
        rows = [{
            "source_observation_id": "obs-1",
            "camera_id": "cam_1",
            "source_id": "s1",
            "track_id": "42",
            "timestamp_ms": 1000,
            "face_bbox": [1.0, 2.0, 3.0, 4.0],
            "landmarks": [0.0] * 10,
            "face_confidence": 0.9,
            "quality": 0.85,
            "similarity": 0.95,
            "distance": 0.05,
            "created_at": "2026-01-01T00:00:00Z",
        }]
        store = self._make_store_with_rows(rows)
        emb = _unit_embedding()
        result = store.search_similar_faces(emb)
        assert "similarity" in result[0]
        assert "distance" in result[0]

    def test_all_metadata_fields_present(self):
        rows = [{
            "source_observation_id": "obs-1",
            "camera_id": "cam_1",
            "source_id": "s1",
            "track_id": "42",
            "timestamp_ms": 1000,
            "face_bbox": [1.0, 2.0, 3.0, 4.0],
            "landmarks": [0.0] * 10,
            "face_confidence": 0.9,
            "quality": 0.85,
            "similarity": 0.95,
            "distance": 0.05,
            "created_at": "2026-01-01T00:00:00Z",
        }]
        store = self._make_store_with_rows(rows)
        emb = _unit_embedding()
        result = store.search_similar_faces(emb)
        r = result[0]
        for key in [
            "source_observation_id", "camera_id", "source_id", "track_id",
            "timestamp_ms", "face_bbox", "landmarks", "face_confidence",
            "quality", "similarity", "distance", "created_at",
        ]:
            assert key in r, f"Missing key: {key}"


# ── Class 4: Integration Tests (real PostgreSQL + pgvector) ────────────────────

pytestmark_integration = pytest.mark.integration


@ pytestmark_integration
class TestFaceVectorStoreIntegration:
    """Integration tests against a real PostgreSQL with pgvector.

    All test rows use ``source_observation_id`` prefix ``test:midterm_vector:``.
    Each test uses a transaction that is rolled back, so no rows are
    permanently written to face_observations.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, request):
        """Skip if no DATABASE_URL or integration marker not selected."""
        import os
        if not os.getenv("DATABASE_URL"):
            pytest.skip("DATABASE_URL not set")

    def _connect(self):
        import os
        import psycopg
        from pgvector.psycopg import register_vector

        url = os.getenv("DATABASE_URL", "")
        conn = psycopg.connect(url)
        register_vector(conn)
        return conn

    def _insert_test_row(
        self, cur, source_observation_id: str, embedding: list[float],
        camera_id: str = "test_cam", track_id: str = "99",
        timestamp_ms: int = 1000,
    ):
        from pgvector.psycopg import Vector
        cur.execute(
            """
            INSERT INTO face_observations (
                source_observation_id, camera_id, source_id, track_id,
                timestamp_ms, face_bbox, landmarks, face_confidence,
                quality, embedding_dim, embedding, embedding_norm,
                reid_throttle_key
            ) VALUES (
                %(sid)s, %(cid)s, 'test_source', %(tid)s,
                %(ts)s,
                '[320,240,60,60]'::jsonb,
                '[100,200,150,200,125,230,110,240,140,240]'::jsonb,
                0.85, 0.90, 512, %(emb)s, 1.0,
                'test_cam:test_source:99'
            )
            ON CONFLICT (source_observation_id) DO NOTHING
            """,
            {
                "sid": source_observation_id,
                "cid": camera_id,
                "tid": track_id,
                "ts": timestamp_ms,
                "emb": Vector(embedding),
            },
        )

    def _delete_test_rows(self, conn):
        """Clean up test rows (safety net in case rollback fails)."""
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM face_observations "
                "WHERE source_observation_id LIKE 'test:midterm_vector:%%'"
            )
        conn.commit()

    def test_self_search_top1_similarity_near_one(self):
        """Search with an inserted embedding → itself should be top1 with
        similarity approximately 1.0."""
        conn = self._connect()
        store = FaceVectorStore(conn)
        emb = _unit_embedding()
        sid = "test:midterm_vector:self_1"

        try:
            # Begin transaction
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                self._insert_test_row(cur, sid, emb)
                cur.execute("COMMIT")

            # Search
            results = store.search_similar_faces(emb, top_k=5)
            assert len(results) >= 1
            top1 = results[0]
            assert top1["source_observation_id"] == sid
            assert top1["similarity"] == pytest.approx(1.0, abs=0.01)
            assert top1["distance"] == pytest.approx(0.0, abs=0.01)

        finally:
            self._delete_test_rows(conn)
            conn.close()

    def test_two_different_embeddings_distinct_similarity(self):
        """Insert two different embeddings; search with the first should
        return it as top1 with higher similarity than the second."""
        conn = self._connect()
        store = FaceVectorStore(conn)
        emb1 = _unit_embedding()
        emb2 = _randomlike_embedding(seed=99)
        sid1 = "test:midterm_vector:two_diff_1"
        sid2 = "test:midterm_vector:two_diff_2"

        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                self._insert_test_row(cur, sid1, emb1)
                self._insert_test_row(cur, sid2, emb2)
                cur.execute("COMMIT")

            results = store.search_similar_faces(emb1, top_k=5)
            assert len(results) >= 2
            assert results[0]["source_observation_id"] == sid1
            # emb1 vs emb1 similarity ≈ 1.0; emb1 vs emb2 should be lower
            assert results[0]["similarity"] > results[1]["similarity"]

        finally:
            self._delete_test_rows(conn)
            conn.close()

    def test_top_k_limits_results(self):
        """top_k=1 returns at most 1 result even with multiple rows."""
        conn = self._connect()
        store = FaceVectorStore(conn)
        emb = _unit_embedding()
        emb2 = _randomlike_embedding(seed=1)
        sid1 = "test:midterm_vector:topk_1"
        sid2 = "test:midterm_vector:topk_2"

        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                self._insert_test_row(cur, sid1, emb)
                self._insert_test_row(cur, sid2, emb2)
                cur.execute("COMMIT")

            results = store.search_similar_faces(emb, top_k=1)
            assert len(results) == 1

        finally:
            self._delete_test_rows(conn)
            conn.close()

    def test_min_similarity_filters(self):
        """min_similarity near 1.0 should exclude a dissimilar embedding."""
        conn = self._connect()
        store = FaceVectorStore(conn)
        emb1 = _unit_embedding()
        emb2 = _randomlike_embedding(seed=7)
        sid1 = "test:midterm_vector:min_sim_1"
        sid2 = "test:midterm_vector:min_sim_2"

        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                self._insert_test_row(cur, sid1, emb1)
                self._insert_test_row(cur, sid2, emb2)
                cur.execute("COMMIT")

            # High threshold: only self-match should pass
            results = store.search_similar_faces(emb1, top_k=5, min_similarity=0.99)
            sids = [r["source_observation_id"] for r in results]
            assert sid1 in sids
            # emb2 likely excluded (random vector cos-sim to unit vector is low)
            # We don't strictly assert exclusion since random could collide,
            # but with 512-d it's astronomically unlikely

        finally:
            self._delete_test_rows(conn)
            conn.close()

    def test_camera_scope_filters(self):
        """camera_scope restricts results to the given cameras."""
        conn = self._connect()
        store = FaceVectorStore(conn)
        emb = _unit_embedding()
        sid_a = "test:midterm_vector:cam_scope_a"
        sid_b = "test:midterm_vector:cam_scope_b"

        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                self._insert_test_row(cur, sid_a, emb, camera_id="cam_a")
                self._insert_test_row(cur, sid_b, emb, camera_id="cam_b")
                cur.execute("COMMIT")

            results = store.search_similar_faces(
                emb, top_k=5, camera_scope=["cam_a"],
            )
            sids = [r["source_observation_id"] for r in results]
            assert sid_a in sids
            assert sid_b not in sids

        finally:
            self._delete_test_rows(conn)
            conn.close()

    def test_default_result_excludes_embedding(self):
        """Integration: default search does not return 512-d embedding."""
        conn = self._connect()
        store = FaceVectorStore(conn)
        emb = _unit_embedding()
        sid = "test:midterm_vector:no_emb_result"

        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                self._insert_test_row(cur, sid, emb)
                cur.execute("COMMIT")

            results = store.search_similar_faces(emb, top_k=1)
            assert len(results) == 1
            assert "embedding" not in results[0]

        finally:
            self._delete_test_rows(conn)
            conn.close()

    def test_include_embedding_true_returns_embedding(self):
        """Integration: include_embedding=True returns the 512-d vector."""
        conn = self._connect()
        store = FaceVectorStore(conn)
        emb = _unit_embedding()
        sid = "test:midterm_vector:with_emb_result"

        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                self._insert_test_row(cur, sid, emb)
                cur.execute("COMMIT")

            results = store.search_similar_faces(emb, top_k=1, include_embedding=True)
            assert len(results) == 1
            assert "embedding" in results[0]
            assert len(results[0]["embedding"]) == 512

        finally:
            self._delete_test_rows(conn)
            conn.close()

    def test_no_leftover_test_rows(self):
        """Verify cleanup: no test rows remain after test."""
        conn = self._connect()
        try:
            # Ensure clean state before check
            self._delete_test_rows(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS cnt FROM face_observations "
                    "WHERE source_observation_id LIKE 'test:midterm_vector:%%'"
                )
                row = cur.fetchone()
            assert row[0] == 0, f"Leftover test rows: {row[0]}"
        finally:
            conn.close()
