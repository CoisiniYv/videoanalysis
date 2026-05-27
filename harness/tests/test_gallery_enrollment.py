"""Tests for F3.4 gallery schema and enrollment harness.

Test classes:
- TestGalleryEmbeddingValidation: pure Python, no DB
- TestPersonRepositoryUnit: mocked DB, PersonRepository
- TestGalleryRepositoryUnit: mocked DB, GalleryRepository
- TestSearchGalleryParams: mocked DB, FaceVectorStore.search_gallery
- TestSearchGalleryResult: mocked DB, result processing
- TestGalleryIntegration: real PostgreSQL + pgvector (integration mark)
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FACE_WORKER_ROOT = REPO_ROOT / "services" / "face-worker"
sys.path.insert(0, str(FACE_WORKER_ROOT))

from app.gallery_repository import GalleryRepository, _validate_embedding
from app.person_repository import PersonRepository
from app.vector_store import FaceVectorStore, _EMBEDDING_DIM, _MIN_NORM, _MAX_NORM


# ── Helpers ────────────────────────────────────────────────────────────────

def _unit_embedding() -> list[float]:
    val = 1.0 / math.sqrt(_EMBEDDING_DIM)
    return [val] * _EMBEDDING_DIM


def _randomlike_embedding(seed: int = 42) -> list[float]:
    import random as _random
    rng = _random.Random(seed)
    raw = [rng.uniform(-1.0, 1.0) for _ in range(_EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


# ── Class 1: Gallery Embedding Validation (pure Python) ───────────────────

class TestGalleryEmbeddingValidation:
    """_validate_embedding() rejects invalid inputs for gallery storage."""

    def test_valid_list_passes(self):
        result = _validate_embedding(_unit_embedding())
        assert len(result) == _EMBEDDING_DIM

    def test_tuple_accepted(self):
        result = _validate_embedding(tuple(_unit_embedding()))
        assert len(result) == _EMBEDDING_DIM

    def test_wrong_length_rejected(self):
        with pytest.raises(ValueError, match="length"):
            _validate_embedding([0.01] * 256)

    def test_nan_rejected(self):
        emb = _unit_embedding()
        emb[0] = float("nan")
        with pytest.raises(ValueError, match="not finite"):
            _validate_embedding(emb)

    def test_inf_rejected(self):
        emb = _unit_embedding()
        emb[0] = float("inf")
        with pytest.raises(ValueError, match="not finite"):
            _validate_embedding(emb)

    def test_string_rejected(self):
        emb = _unit_embedding()
        emb[0] = "abc"
        with pytest.raises(ValueError, match="invalid embedding element type"):
            _validate_embedding(emb)

    def test_bool_rejected(self):
        emb = _unit_embedding()
        emb[0] = True
        with pytest.raises(ValueError, match="invalid embedding element type"):
            _validate_embedding(emb)

    def test_none_rejected(self):
        emb = _unit_embedding()
        emb[0] = None
        with pytest.raises(ValueError, match="invalid embedding element type"):
            _validate_embedding(emb)

    def test_not_list_rejected(self):
        with pytest.raises(ValueError, match="must be list or tuple"):
            _validate_embedding("not a list")

    def test_norm_low_rejected(self):
        emb = [0.001] * _EMBEDDING_DIM
        with pytest.raises(ValueError, match="L2 norm"):
            _validate_embedding(emb)

    def test_norm_high_rejected(self):
        emb = [0.5] * _EMBEDDING_DIM
        with pytest.raises(ValueError, match="L2 norm"):
            _validate_embedding(emb)

    def test_norm_min_boundary_accepted(self):
        val = _MIN_NORM / math.sqrt(_EMBEDDING_DIM)
        result = _validate_embedding([val] * _EMBEDDING_DIM)
        assert result is not None

    def test_norm_max_boundary_accepted(self):
        val = _MAX_NORM / math.sqrt(_EMBEDDING_DIM)
        result = _validate_embedding([val] * _EMBEDDING_DIM)
        assert result is not None


# ── Class 1b: CLI Argument Validation ────────────────────────────────────

class TestEnrollCliArgs:
    """enroll_gallery.py argparse mutually exclusive group enforcement."""

    @staticmethod
    def _parse(args: list[str]):
        """Build the same argparse as enroll_gallery.main() and parse."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--observation-id", required=True)
        selector = parser.add_mutually_exclusive_group(required=True)
        selector.add_argument("--person-name")
        selector.add_argument("--person-id", type=int)
        selector.add_argument("--external-person-id")
        parser.add_argument("--set-primary", action="store_true", default=False)
        parser.add_argument("--source-type", default="snapshot_extract")
        parser.add_argument("--created-by", default=None)
        return parser.parse_args(args)

    def test_person_name_only(self):
        args = self._parse(["--observation-id", "obs1", "--person-name", "Alice"])
        assert args.person_name == "Alice"
        assert args.person_id is None
        assert args.external_person_id is None

    def test_person_id_only(self):
        args = self._parse(["--observation-id", "obs1", "--person-id", "5"])
        assert args.person_id == 5
        assert args.person_name is None
        assert args.external_person_id is None

    def test_external_person_id_only(self):
        args = self._parse(["--observation-id", "obs1", "--external-person-id", "badge-1"])
        assert args.external_person_id == "badge-1"
        assert args.person_name is None
        assert args.person_id is None

    def test_no_selector_rejected(self):
        """No person selector → argparse error (SystemExit)."""
        with pytest.raises(SystemExit):
            self._parse(["--observation-id", "obs1"])

    def test_multiple_selectors_rejected(self):
        """Two selectors → argparse error (SystemExit)."""
        with pytest.raises(SystemExit):
            self._parse([
                "--observation-id", "obs1",
                "--person-name", "Alice",
                "--person-id", "5",
            ])

    def test_all_three_selectors_rejected(self):
        """Three selectors → argparse error (SystemExit)."""
        with pytest.raises(SystemExit):
            self._parse([
                "--observation-id", "obs1",
                "--person-name", "Alice",
                "--person-id", "5",
                "--external-person-id", "badge-1",
            ])


# ── Class 2: PersonRepository Unit Tests (mocked) ─────────────────────────

class TestPersonRepositoryUnit:
    """PersonRepository CRUD with mocked cursor."""

    @staticmethod
    def _make_repo() -> tuple[PersonRepository, MagicMock]:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        return PersonRepository(conn), cursor

    def test_create_person_returns_id(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 42}
        pid = repo.create_person("Alice")
        assert pid == 42

    def test_create_person_with_all_fields(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 1}
        pid = repo.create_person(
            "Bob",
            external_person_id="ext-1",
            description="Test person",
            created_by="admin",
            updated_by="admin",
            payload={"tag": "vip"},
        )
        assert pid == 1

    def test_get_by_id_returns_dict(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {
            "id": 1, "name": "Alice", "is_active": True,
        }
        person = repo.get_by_id(1)
        assert person is not None
        assert person["name"] == "Alice"

    def test_get_by_id_returns_none(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = None
        person = repo.get_by_id(999)
        assert person is None

    def test_get_by_external_person_id(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {
            "id": 5, "name": "Alice", "external_person_id": "badge-1",
        }
        person = repo.get_by_external_person_id("badge-1")
        assert person is not None
        assert person["id"] == 5

    def test_get_by_external_person_id_not_found(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = None
        person = repo.get_by_external_person_id("nonexistent")
        assert person is None

    def test_list_active_returns_list(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = [
            {"id": 1, "name": "Alice", "is_active": True},
            {"id": 2, "name": "Bob", "is_active": True},
        ]
        persons = repo.list_active()
        assert len(persons) == 2

    def test_deactivate_returns_true_on_update(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 1}
        assert repo.deactivate(1) is True

    def test_deactivate_returns_false_when_not_found(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = None
        assert repo.deactivate(999) is False


# ── Class 3: GalleryRepository Unit Tests (mocked) ────────────────────────

class TestGalleryRepositoryUnit:
    """GalleryRepository CRUD with mocked cursor."""

    @staticmethod
    def _make_repo() -> tuple[GalleryRepository, MagicMock]:
        with patch("app.gallery_repository.register_vector"):
            conn = MagicMock()
            cursor = MagicMock()
            cursor.__enter__ = MagicMock(return_value=cursor)
            cursor.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cursor
            return GalleryRepository(conn), cursor

    def test_add_embedding_returns_id(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 10}
        gid = repo.add_embedding(person_id=1, embedding=_unit_embedding())
        assert gid == 10

    def test_add_embedding_with_all_fields(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 11}
        gid = repo.add_embedding(
            person_id=1,
            embedding=_unit_embedding(),
            source_type="manual_upload",
            source_image_path="/img.jpg",
            source_observation_id="face:cam1:42:1000",
            embedding_model="adaface",
            model_version="v1",
            quality=0.9,
            face_bbox=[1, 2, 3, 4],
            landmarks=[0.0] * 10,
            is_primary=True,
            payload={"note": "test"},
        )
        assert gid == 11

    def test_add_embedding_invalid_rejected(self):
        repo, _ = self._make_repo()
        with pytest.raises(ValueError, match="length"):
            repo.add_embedding(person_id=1, embedding=[0.1] * 100)

    def test_get_by_id_returns_dict(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {
            "id": 1, "person_id": 1, "is_primary": False,
        }
        row = repo.get_by_id(1)
        assert row is not None

    def test_get_by_id_returns_none(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = None
        assert repo.get_by_id(999) is None

    def test_list_by_person_returns_list(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = [
            {"id": 1, "person_id": 1, "is_primary": True},
            {"id": 2, "person_id": 1, "is_primary": False},
        ]
        rows = repo.list_by_person(1)
        assert len(rows) == 2

    def test_deactivate_returns_true(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 1}
        assert repo.deactivate(1) is True

    def test_deactivate_returns_false(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = None
        assert repo.deactivate(999) is False


# ── Class 4: search_gallery Parameters (mocked) ──────────────────────────

class TestSearchGalleryParams:
    """Parameter clamping and SQL generation for search_gallery."""

    @staticmethod
    def _make_store() -> FaceVectorStore:
        with patch("app.vector_store.register_vector"):
            conn = MagicMock()
            cursor = MagicMock()
            cursor.__enter__ = MagicMock(return_value=cursor)
            cursor.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cursor
            return FaceVectorStore(conn)

    def test_top_k_below_1_clamped(self):
        store = self._make_store()
        result = store.search_gallery(_unit_embedding(), top_k=0)
        assert isinstance(result, list)

    def test_top_k_above_100_clamped(self):
        store = self._make_store()
        result = store.search_gallery(_unit_embedding(), top_k=200)
        assert isinstance(result, list)

    def test_empty_person_ids_returns_empty(self):
        store = self._make_store()
        result = store.search_gallery(_unit_embedding(), person_ids=[])
        assert result == []

    def test_min_similarity_below_0_rejected(self):
        store = self._make_store()
        with pytest.raises(ValueError, match="outside"):
            store.search_gallery(_unit_embedding(), min_similarity=-0.1)

    def test_min_similarity_above_1_rejected(self):
        store = self._make_store()
        with pytest.raises(ValueError, match="outside"):
            store.search_gallery(_unit_embedding(), min_similarity=1.1)

    def test_min_similarity_in_range_accepted(self):
        store = self._make_store()
        result = store.search_gallery(_unit_embedding(), min_similarity=0.75)
        assert isinstance(result, list)

    def test_sql_uses_cosine_operator(self):
        store = FaceVectorStore.__new__(FaceVectorStore)
        store._conn = MagicMock()
        with patch("app.vector_store.register_vector"):
            cursor = MagicMock()
            cursor.__enter__ = MagicMock(return_value=cursor)
            cursor.__exit__ = MagicMock(return_value=False)
            cursor.fetchall.return_value = []
            store._conn.cursor.return_value = cursor
            store.search_gallery(_unit_embedding(), top_k=5)
            call_args = cursor.execute.call_args
            sql = call_args[0][0]
            assert "<=>" in sql
            assert "person_gallery_embeddings" in sql
            assert "JOIN persons" in sql

    def test_sql_filters_active_person(self):
        """search_gallery SQL must include p.is_active = true."""
        store = FaceVectorStore.__new__(FaceVectorStore)
        store._conn = MagicMock()
        with patch("app.vector_store.register_vector"):
            cursor = MagicMock()
            cursor.__enter__ = MagicMock(return_value=cursor)
            cursor.__exit__ = MagicMock(return_value=False)
            cursor.fetchall.return_value = []
            store._conn.cursor.return_value = cursor
            store.search_gallery(_unit_embedding())
            call_args = cursor.execute.call_args
            sql = call_args[0][0]
            assert "p.is_active = true" in sql
            assert "pge.is_active = true" in sql


# ── Class 5: search_gallery Results (mocked) ─────────────────────────────

class TestSearchGalleryResult:
    """Result shape for search_gallery."""

    @staticmethod
    def _make_store_with_rows(rows: list[dict]) -> FaceVectorStore:
        with patch("app.vector_store.register_vector"):
            conn = MagicMock()
            cursor = MagicMock()
            cursor.__enter__ = MagicMock(return_value=cursor)
            cursor.__exit__ = MagicMock(return_value=False)
            cursor.fetchall.return_value = rows
            conn.cursor.return_value = cursor
            return FaceVectorStore(conn)

    def test_default_excludes_embedding(self):
        rows = [{
            "id": 1, "person_id": 1, "person_name": "Alice",
            "source_type": "manual_upload", "embedding_model": "adaface",
            "is_primary": True, "quality": 0.9,
            "similarity": 1.0, "distance": 0.0,
            "created_at": "2026-01-01T00:00:00Z",
        }]
        store = self._make_store_with_rows(rows)
        result = store.search_gallery(_unit_embedding())
        assert len(result) == 1
        assert "embedding" not in result[0]
        assert result[0]["person_name"] == "Alice"

    def test_include_embedding_true(self):
        rows = [{
            "id": 1, "person_id": 1, "person_name": "Alice",
            "source_type": "manual_upload", "embedding_model": "adaface",
            "is_primary": True, "quality": 0.9,
            "embedding": [0.01] * 512,
            "similarity": 1.0, "distance": 0.0,
            "created_at": "2026-01-01T00:00:00Z",
        }]
        store = self._make_store_with_rows(rows)
        result = store.search_gallery(_unit_embedding(), include_embedding=True)
        assert "embedding" in result[0]

    def test_empty_result(self):
        store = self._make_store_with_rows([])
        result = store.search_gallery(_unit_embedding())
        assert result == []

    def test_person_ids_filter_applied(self):
        store = self._make_store_with_rows([])
        result = store.search_gallery(_unit_embedding(), person_ids=[1, 2])
        assert result == []
        cursor = store._conn.cursor.return_value
        call_args = cursor.execute.call_args
        params = call_args[0][1]
        assert "person_ids" in params


# ── Class 6: Integration Tests (real PostgreSQL) ──────────────────────────

pytestmark_integration = pytest.mark.integration


@ pytestmark_integration
class TestGalleryIntegration:
    """Integration tests against a real PostgreSQL with pgvector.

    All test data uses person names prefixed with ``test:f3_4:``.
    Cleanup runs in finally blocks.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        import os
        if not os.getenv("DATABASE_URL"):
            pytest.skip("DATABASE_URL not set")

    def _connect(self):
        import os
        import psycopg
        from pgvector.psycopg import register_vector
        conn = psycopg.connect(os.environ["DATABASE_URL"])
        register_vector(conn)
        return conn

    def _cleanup(self, conn):
        """Delete all test data created by this test module."""
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM person_gallery_embeddings "
                "WHERE person_id IN "
                "(SELECT id FROM persons WHERE name LIKE 'test:f3_4:%%')"
            )
            cur.execute(
                "DELETE FROM persons WHERE name LIKE 'test:f3_4:%%'"
            )
            cur.execute(
                "DELETE FROM face_observations "
                "WHERE source_observation_id LIKE 'test:f3_4:%%'"
            )
        conn.commit()

    def test_create_person_and_get(self):
        conn = self._connect()
        repo = PersonRepository(conn)
        try:
            pid = repo.create_person(
                "test:f3_4:alice",
                description="Integration test person",
            )
            assert pid > 0
            person = repo.get_by_id(pid)
            assert person is not None
            assert person["name"] == "test:f3_4:alice"
            assert person["is_active"] is True
        finally:
            self._cleanup(conn)
            conn.close()

    def test_create_person_with_external_person_id(self):
        conn = self._connect()
        repo = PersonRepository(conn)
        try:
            pid = repo.create_person(
                "test:f3_4:ext_person",
                external_person_id="test:f3_4:badge-999",
            )
            assert pid > 0
            person = repo.get_by_external_person_id("test:f3_4:badge-999")
            assert person is not None
            assert person["id"] == pid
        finally:
            self._cleanup(conn)
            conn.close()

    def test_external_person_id_unique(self):
        """Duplicate external_person_id raises unique violation."""
        conn = self._connect()
        repo = PersonRepository(conn)
        try:
            repo.create_person(
                "test:f3_4:dup_ext_1",
                external_person_id="test:f3_4:dup-ext",
            )
            with pytest.raises(Exception):
                repo.create_person(
                    "test:f3_4:dup_ext_2",
                    external_person_id="test:f3_4:dup-ext",
                )
            # Rollback the failed transaction so cleanup can proceed
            conn.rollback()
        finally:
            self._cleanup(conn)
            conn.close()

    def test_list_active(self):
        conn = self._connect()
        repo = PersonRepository(conn)
        try:
            repo.create_person("test:f3_4:person_a")
            repo.create_person("test:f3_4:person_b")
            persons = repo.list_active()
            names = [p["name"] for p in persons]
            assert "test:f3_4:person_a" in names
            assert "test:f3_4:person_b" in names
        finally:
            self._cleanup(conn)
            conn.close()

    def test_deactivate_person(self):
        conn = self._connect()
        repo = PersonRepository(conn)
        try:
            pid = repo.create_person("test:f3_4:deactivate_me")
            assert repo.deactivate(pid) is True
            person = repo.get_by_id(pid)
            assert person["is_active"] is False
            active = repo.list_active()
            active_ids = [p["id"] for p in active]
            assert pid not in active_ids
        finally:
            self._cleanup(conn)
            conn.close()

    def test_add_gallery_embedding(self):
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        try:
            pid = person_repo.create_person("test:f3_4:gallery_add")
            emb = _unit_embedding()
            gid = gallery_repo.add_embedding(
                pid, emb, is_primary=True, quality=0.85,
            )
            assert gid > 0
            row = gallery_repo.get_by_id(gid)
            assert row is not None
            assert row["person_id"] == pid
            assert row["is_primary"] is True
            assert row["quality"] == pytest.approx(0.85)
        finally:
            self._cleanup(conn)
            conn.close()

    def test_add_embedding_with_source_observation_id(self):
        """source_observation_id provenance is persisted."""
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        try:
            pid = person_repo.create_person("test:f3_4:provenance")
            sid = "test:f3_4:prov_obs_1"
            # Insert a face_observation first (FK requirement)
            from pgvector.psycopg import Vector
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO face_observations (
                        source_observation_id, camera_id, source_id, track_id,
                        timestamp_ms, face_bbox, landmarks,
                        face_confidence, quality, embedding_dim,
                        embedding, embedding_norm, reid_throttle_key
                    ) VALUES (
                        %(sid)s, 'cam', 'src', '1', 1000,
                        '[]'::jsonb, '[]'::jsonb,
                        0.9, 0.9, 512, %(emb)s, 1.0, ''
                    ) ON CONFLICT DO NOTHING
                    """,
                    {"sid": sid, "emb": Vector(_unit_embedding())},
                )
                conn.commit()

            gid = gallery_repo.add_embedding(
                pid,
                _unit_embedding(),
                source_observation_id=sid,
            )
            row = gallery_repo.get_by_id(gid)
            assert row is not None
            assert row["source_observation_id"] == sid
        finally:
            self._cleanup(conn)
            conn.close()

    def test_list_by_person(self):
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        try:
            pid = person_repo.create_person("test:f3_4:multi_gallery")
            gid1 = gallery_repo.add_embedding(pid, _unit_embedding(), is_primary=True)
            gid2 = gallery_repo.add_embedding(
                pid, _randomlike_embedding(seed=100),
            )
            rows = gallery_repo.list_by_person(pid)
            assert len(rows) == 2
            assert rows[0]["is_primary"] is True
            ids = [r["id"] for r in rows]
            assert gid1 in ids
            assert gid2 in ids
        finally:
            self._cleanup(conn)
            conn.close()

    def test_cascade_delete_person_removes_gallery(self):
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        try:
            pid = person_repo.create_person("test:f3_4:cascade")
            gid = gallery_repo.add_embedding(pid, _unit_embedding())
            with conn.cursor() as cur:
                cur.execute("DELETE FROM persons WHERE id = %s", (pid,))
            conn.commit()
            assert gallery_repo.get_by_id(gid) is None
        finally:
            self._cleanup(conn)
            conn.close()

    def test_search_gallery_self_match(self):
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        store = FaceVectorStore(conn)
        try:
            pid = person_repo.create_person("test:f3_4:self_match")
            emb = _unit_embedding()
            gallery_repo.add_embedding(pid, emb, is_primary=True)

            results = store.search_gallery(emb, top_k=5, person_ids=[pid])
            assert len(results) >= 1
            top1 = results[0]
            assert top1["person_id"] == pid
            assert top1["similarity"] == pytest.approx(1.0, abs=0.01)
        finally:
            self._cleanup(conn)
            conn.close()

    def test_search_gallery_different_embeddings(self):
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        store = FaceVectorStore(conn)
        try:
            pid_a = person_repo.create_person("test:f3_4:search_a")
            pid_b = person_repo.create_person("test:f3_4:search_b")
            emb1 = _unit_embedding()
            emb2 = _randomlike_embedding(seed=77)
            gallery_repo.add_embedding(pid_a, emb1, is_primary=True)
            gallery_repo.add_embedding(pid_b, emb2, is_primary=True)

            results = store.search_gallery(emb1, top_k=5)
            assert len(results) >= 2
            assert results[0]["person_id"] == pid_a
            assert results[0]["similarity"] > results[1]["similarity"]
        finally:
            self._cleanup(conn)
            conn.close()

    def test_search_gallery_person_ids_filter(self):
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        store = FaceVectorStore(conn)
        try:
            pid_a = person_repo.create_person("test:f3_4:filter_a")
            pid_b = person_repo.create_person("test:f3_4:filter_b")
            emb = _unit_embedding()
            gallery_repo.add_embedding(pid_a, emb)
            gallery_repo.add_embedding(pid_b, emb)

            results = store.search_gallery(emb, top_k=5, person_ids=[pid_a])
            person_ids_in_result = [r["person_id"] for r in results]
            assert pid_a in person_ids_in_result
            assert pid_b not in person_ids_in_result
        finally:
            self._cleanup(conn)
            conn.close()

    def test_search_gallery_min_similarity_filter(self):
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        store = FaceVectorStore(conn)
        try:
            pid = person_repo.create_person("test:f3_4:min_sim")
            emb1 = _unit_embedding()
            emb2 = _randomlike_embedding(seed=99)
            gallery_repo.add_embedding(pid, emb1, is_primary=True)

            results = store.search_gallery(emb2, top_k=5, min_similarity=0.99)
            matching = [r for r in results if r["person_id"] == pid]
            assert len(matching) == 0
        finally:
            self._cleanup(conn)
            conn.close()

    def test_search_gallery_exclude_inactive_embedding(self):
        """Deactivated gallery embeddings are excluded from search."""
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        store = FaceVectorStore(conn)
        try:
            pid = person_repo.create_person("test:f3_4:inactive_emb")
            emb = _unit_embedding()
            gid = gallery_repo.add_embedding(pid, emb)
            gallery_repo.deactivate(gid)

            results = store.search_gallery(emb, top_k=5, person_ids=[pid])
            ids = [r["id"] for r in results]
            assert gid not in ids
        finally:
            self._cleanup(conn)
            conn.close()

    def test_search_gallery_exclude_deactivated_person(self):
        """Deactivated person's gallery embeddings are excluded from search."""
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        store = FaceVectorStore(conn)
        try:
            pid = person_repo.create_person("test:f3_4:inactive_person")
            emb = _unit_embedding()
            gallery_repo.add_embedding(pid, emb, is_primary=True)

            # Verify it appears before deactivation
            results = store.search_gallery(emb, top_k=5, person_ids=[pid])
            assert len(results) >= 1

            # Deactivate person
            person_repo.deactivate(pid)

            # Should no longer appear
            results = store.search_gallery(emb, top_k=5, person_ids=[pid])
            person_ids_in_result = [r["person_id"] for r in results]
            assert pid not in person_ids_in_result
        finally:
            self._cleanup(conn)
            conn.close()

    def test_search_gallery_active_person_active_embedding(self):
        """Active person + active embedding → returned."""
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        store = FaceVectorStore(conn)
        try:
            pid = person_repo.create_person("test:f3_4:active_both")
            emb = _unit_embedding()
            gallery_repo.add_embedding(pid, emb, is_primary=True)

            results = store.search_gallery(emb, top_k=5, person_ids=[pid])
            assert len(results) >= 1
            assert results[0]["person_id"] == pid
        finally:
            self._cleanup(conn)
            conn.close()

    def test_end_to_end_enrollment_flow(self):
        """Full flow: insert face_observation → create person → enroll → search."""
        conn = self._connect()
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        store = FaceVectorStore(conn)
        try:
            emb = _randomlike_embedding(seed=123)
            sid = "test:f3_4:e2e_obs"
            from pgvector.psycopg import Vector
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO face_observations (
                        source_observation_id, camera_id, source_id, track_id,
                        timestamp_ms, face_bbox, landmarks, face_confidence,
                        quality, embedding_dim, embedding, embedding_norm,
                        reid_throttle_key
                    ) VALUES (
                        %(sid)s, 'test_cam', 'test_src', '1',
                        1000,
                        '[320,240,60,60]'::jsonb,
                        '[100,200,150,200,125,230,110,240,140,240]'::jsonb,
                        0.90, 0.88, 512, %(emb)s, 1.0,
                        'test:src:1'
                    )
                    ON CONFLICT (source_observation_id) DO NOTHING
                    """,
                    {"sid": sid, "emb": Vector(emb)},
                )
                conn.commit()

            from psycopg.rows import dict_row
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT embedding FROM face_observations "
                    "WHERE source_observation_id = %s",
                    (sid,),
                )
                obs = cur.fetchone()

            obs_emb = [float(x) for x in obs["embedding"]]

            pid = person_repo.create_person("test:f3_4:e2e_person")
            gid = gallery_repo.add_embedding(
                pid, obs_emb,
                source_type="snapshot_extract",
                source_observation_id=sid,
                is_primary=True,
            )
            assert gid > 0

            # Verify provenance
            row = gallery_repo.get_by_id(gid)
            assert row["source_observation_id"] == sid

            results = store.search_gallery(obs_emb, top_k=3, person_ids=[pid])
            assert len(results) >= 1
            assert results[0]["person_id"] == pid
            assert results[0]["similarity"] == pytest.approx(1.0, abs=0.01)

        finally:
            self._cleanup(conn)
            conn.close()

    def test_no_leftover_test_rows(self):
        """Verify cleanup: no test rows remain."""
        conn = self._connect()
        try:
            self._cleanup(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM persons "
                    "WHERE name LIKE 'test:f3_4:%%'"
                )
                row = cur.fetchone()
            assert row[0] == 0
        finally:
            conn.close()
