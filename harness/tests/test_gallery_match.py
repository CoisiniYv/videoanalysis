"""Tests for midterm gallery match harness.

Test classes:
- TestMatchCliArgs: argparse validation for match_gallery.py CLI
- TestMatchResultRepositoryUnit: mocked DB, MatchResultRepository
- TestMatchGalleryIntegration: real PostgreSQL + pgvector (integration mark)
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FACE_WORKER_ROOT = REPO_ROOT / "services" / "face-worker"
FACE_WORKER_ROOT_STR = str(FACE_WORKER_ROOT)
if FACE_WORKER_ROOT_STR in sys.path:
    sys.path.remove(FACE_WORKER_ROOT_STR)
sys.path.insert(0, FACE_WORKER_ROOT_STR)
for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.match_repository import MatchResultRepository
from app.vector_store import _EMBEDDING_DIM


# ── Helpers (pure Python, no DB) ───────────────────────────────────────────

def _unit_embedding() -> list[float]:
    val = 1.0 / math.sqrt(_EMBEDDING_DIM)
    return [val] * _EMBEDDING_DIM


def _randomlike_embedding(seed: int = 42) -> list[float]:
    import random as _random
    rng = _random.Random(seed)
    raw = [rng.uniform(-1.0, 1.0) for _ in range(_EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


# ── Class 1: CLI Argument Validation ───────────────────────────────────────

class TestMatchCliArgs:
    """match_gallery.py argparse validation."""

    @staticmethod
    def _parse(args: list[str]):
        """Build the same argparse as match_gallery.main() and parse."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--observation-id", required=True)
        parser.add_argument("--search-request-id", default=None)
        parser.add_argument("--top-k", type=int, default=10)
        parser.add_argument("--min-similarity", type=float, default=None)
        parser.add_argument(
            "--search-mode",
            default="gallery_match",
            choices=["gallery_match"],
        )
        parser.add_argument("--person-ids", default=None)
        parser.add_argument("--expires-in-seconds", type=int, default=3600)
        parser.add_argument("--dry-run", action="store_true", default=False)
        return parser.parse_args(args)

    def test_observation_id_required(self):
        """Missing --observation-id → SystemExit."""
        with pytest.raises(SystemExit):
            self._parse([])

    def test_defaults(self):
        args = self._parse(["--observation-id", "obs1"])
        assert args.observation_id == "obs1"
        assert args.search_request_id is None
        assert args.top_k == 10
        assert args.min_similarity is None
        assert args.search_mode == "gallery_match"
        assert args.person_ids is None
        assert args.expires_in_seconds == 3600
        assert args.dry_run is False

    def test_custom_top_k(self):
        args = self._parse(["--observation-id", "obs1", "--top-k", "5"])
        assert args.top_k == 5

    def test_custom_min_similarity(self):
        args = self._parse([
            "--observation-id", "obs1", "--min-similarity", "0.75",
        ])
        assert args.min_similarity == 0.75

    def test_gallery_match_accepted(self):
        args = self._parse([
            "--observation-id", "obs1", "--search-mode", "gallery_match",
        ])
        assert args.search_mode == "gallery_match"

    def test_watchlist_rejected(self):
        """watchlist is a future release, not allowed in midterm."""
        with pytest.raises(SystemExit):
            self._parse([
                "--observation-id", "obs1", "--search-mode", "watchlist",
            ])

    def test_live_search_rejected(self):
        """live_search is a future release, not allowed in midterm."""
        with pytest.raises(SystemExit):
            self._parse([
                "--observation-id", "obs1", "--search-mode", "live_search",
            ])

    def test_arbitrary_mode_rejected(self):
        """Arbitrary search_mode strings are not allowed."""
        with pytest.raises(SystemExit):
            self._parse([
                "--observation-id", "obs1", "--search-mode", "bogus",
            ])

    def test_custom_person_ids(self):
        args = self._parse([
            "--observation-id", "obs1", "--person-ids", "1,2,3",
        ])
        assert args.person_ids == "1,2,3"

    def test_custom_expires(self):
        args = self._parse([
            "--observation-id", "obs1", "--expires-in-seconds", "7200",
        ])
        assert args.expires_in_seconds == 7200

    def test_dry_run_flag(self):
        args = self._parse(["--observation-id", "obs1", "--dry-run"])
        assert args.dry_run is True

    def test_search_request_id(self):
        req_id = str(uuid.uuid4())
        args = self._parse([
            "--observation-id", "obs1", "--search-request-id", req_id,
        ])
        assert args.search_request_id == req_id


# ── Class 2: MatchResultRepository Unit Tests (mocked) ─────────────────────

class TestMatchResultRepositoryUnit:
    """MatchResultRepository with mocked cursor."""

    @staticmethod
    def _make_repo() -> tuple[MatchResultRepository, MagicMock]:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        return MatchResultRepository(conn), cursor

    @staticmethod
    def _sample_row() -> dict:
        """Minimal valid gallery_match result data."""
        return {
            "search_request_id": str(uuid.uuid4()),
            "search_mode": "gallery_match",
            "query_observation_id": str(uuid.uuid4()),
            "query_source_observation_id": "face:cam1:42:1000",
            "query_gallery_embedding_id": 10,
            "rank": 1,
            "similarity": 0.95,
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        }

    def test_insert_returns_id(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 100}
        row_id = repo.insert_gallery_match_result(self._sample_row())
        assert row_id == 100

    def test_insert_duplicate_returns_none(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = None
        row_id = repo.insert_gallery_match_result(self._sample_row())
        assert row_id is None

    def test_insert_with_all_fields(self):
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 101}
        data = self._sample_row()
        data.update({
            "query_person_id": 1,
            "query_embedding_model": "adaface",
            "similarity_threshold": 0.6,
            "time_from": datetime.now(timezone.utc),
            "time_to": datetime.now(timezone.utc),
            "camera_scope": ["cam1", "cam2"],
            "face_confidence": 0.9,
            "quality": 0.85,
            "snapshot_path": "/snap.jpg",
            "crop_path": "/crop.jpg",
            "nvr_reference": {"url": "rtsp://..."},
            "payload": {"note": "test"},
        })
        row_id = repo.insert_gallery_match_result(data)
        assert row_id == 101

    def test_insert_sql_uses_on_conflict(self):
        """Verify ON CONFLICT DO NOTHING for idempotency."""
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 1}
        repo.insert_gallery_match_result(self._sample_row())
        sql = cursor.execute.call_args[0][0]
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql

    def test_insert_sql_conflicts_on_gallery_embedding(self):
        """Verify the conflict target is (search_request_id, query_gallery_embedding_id)."""
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 1}
        repo.insert_gallery_match_result(self._sample_row())
        sql = cursor.execute.call_args[0][0]
        assert "search_request_id, query_gallery_embedding_id" in sql

    def test_insert_sets_matched_observation_id_null(self):
        """gallery_match must set matched_observation_id = NULL."""
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 1}
        repo.insert_gallery_match_result(self._sample_row())
        params = cursor.execute.call_args[0][1]
        assert params["matched_observation_id"] is None

    def test_insert_sets_matched_context_null(self):
        """gallery_match must set matched_camera_id/source_id/track_id = NULL."""
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 1}
        repo.insert_gallery_match_result(self._sample_row())
        params = cursor.execute.call_args[0][1]
        assert params["matched_camera_id"] is None
        assert params["matched_source_id"] is None
        assert params["matched_track_id"] is None

    def test_insert_populates_query_observation_id(self):
        """query_observation_id must be set from input data."""
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 1}
        data = self._sample_row()
        obs_id = data["query_observation_id"]
        repo.insert_gallery_match_result(data)
        params = cursor.execute.call_args[0][1]
        assert params["query_observation_id"] == obs_id

    def test_get_by_search_request_returns_list(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = [
            {"id": 1, "rank": 1, "similarity": 0.95},
            {"id": 2, "rank": 2, "similarity": 0.80},
        ]
        results = repo.get_by_search_request(str(uuid.uuid4()))
        assert len(results) == 2
        assert results[0]["rank"] == 1

    def test_get_by_search_request_empty(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        results = repo.get_by_search_request(str(uuid.uuid4()))
        assert results == []

    def test_delete_expired_returns_count(self):
        repo, cursor = self._make_repo()
        cursor.rowcount = 5
        deleted = repo.delete_expired()
        assert deleted == 5

    def test_delete_expired_none(self):
        repo, cursor = self._make_repo()
        cursor.rowcount = 0
        deleted = repo.delete_expired()
        assert deleted == 0

    def test_insert_required_fields_only(self):
        """Insert with only the required fields (defaults fill the rest)."""
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 102}
        data = {
            "search_request_id": str(uuid.uuid4()),
            "query_gallery_embedding_id": 5,
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        row_id = repo.insert_gallery_match_result(data)
        assert row_id == 102

    def test_insert_camera_scope_serialized_as_jsonb(self):
        """camera_scope list is serialized to JSON for JSONB column."""
        repo, cursor = self._make_repo()
        cursor.fetchone.return_value = {"id": 103}
        data = self._sample_row()
        data["camera_scope"] = ["cam1", "cam2"]
        repo.insert_gallery_match_result(data)
        params = cursor.execute.call_args[0][1]
        assert isinstance(params["camera_scope"], str)
        assert "cam1" in params["camera_scope"]


# ── Class 3: Integration Tests (real PostgreSQL) ───────────────────────────

@pytest.mark.integration
class TestMatchGalleryIntegration:
    """Integration tests against a real PostgreSQL with pgvector.

    All test data uses names prefixed with ``test:midterm_match:``.
    Helper methods create real FK-satisfying rows.  Cleanup runs in finally
    blocks with rollback-first to avoid InFailedSqlTransaction.
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
        """Delete all test data.  Safe to call after rollback."""
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM match_results "
                "WHERE query_source_observation_id LIKE 'test:midterm_match:%%'"
            )
            cur.execute(
                "DELETE FROM person_gallery_embeddings "
                "WHERE person_id IN "
                "(SELECT id FROM persons WHERE name LIKE 'test:midterm_match:%%')"
            )
            cur.execute(
                "DELETE FROM persons WHERE name LIKE 'test:midterm_match:%%'"
            )
            cur.execute(
                "DELETE FROM face_observations "
                "WHERE source_observation_id LIKE 'test:midterm_match:%%'"
            )
        conn.commit()

    def _create_observation(
        self, conn, suffix: str, emb: list[float],
    ) -> tuple:
        """Create a test face_observation. Returns (observation_uuid, source_observation_id)."""
        from pgvector.psycopg import Vector
        sid = f"test:midterm_match:{suffix}"
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO face_observations (
                    source_observation_id, camera_id, source_id, track_id,
                    timestamp_ms, face_bbox, landmarks,
                    face_confidence, quality, embedding_dim,
                    embedding, embedding_norm, reid_throttle_key
                ) VALUES (
                    %(sid)s, 'cam1', 'src1', '1',
                    1000,
                    '[320,240,60,60]'::jsonb,
                    '[100,200,150,200,125,230,110,240,140,240]'::jsonb,
                    0.90, 0.88, 512,
                    %(emb)s, 1.0, 'src1:1'
                )
                ON CONFLICT (source_observation_id) DO NOTHING
                RETURNING id
                """,
                {"sid": sid, "emb": Vector(emb)},
            )
            row = cur.fetchone()
            conn.commit()
            if row is None:
                # Already exists, fetch it
                with conn.cursor() as cur2:
                    cur2.execute(
                        "SELECT id FROM face_observations "
                        "WHERE source_observation_id = %s",
                        (sid,),
                    )
                    obs_id = cur2.fetchone()[0]
            else:
                obs_id = row[0]
        return obs_id, sid

    def _create_person(self, conn, suffix: str) -> int:
        """Create a test person. Returns person_id."""
        name = f"test:midterm_match:{suffix}"
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO persons (name, created_by)
                VALUES (%(name)s, 'test')
                RETURNING id
                """,
                {"name": name},
            )
            pid = cur.fetchone()[0]
            conn.commit()
        return pid

    def _create_gallery_embedding(
        self, conn, person_id: int, emb: list[float],
    ) -> int:
        """Create a gallery embedding for a person. Returns gallery_embedding_id."""
        from pgvector.psycopg import Vector
        norm = math.sqrt(sum(x * x for x in emb))
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO person_gallery_embeddings (
                    person_id, embedding, embedding_dim, embedding_norm,
                    source_type, is_active
                ) VALUES (
                    %(pid)s, %(emb)s, 512, %(norm)s,
                    'test', true
                )
                RETURNING id
                """,
                {"pid": person_id, "emb": Vector(emb), "norm": norm},
            )
            gid = cur.fetchone()[0]
            conn.commit()
        return gid

    def test_insert_and_query_gallery_match(self):
        """Insert a gallery_match result and retrieve by search_request_id."""
        conn = self._connect()
        repo = MatchResultRepository(conn)
        try:
            obs_id, sid = self._create_observation(conn, "ins_query", _unit_embedding())
            pid = self._create_person(conn, "ins_query")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())

            req_id = str(uuid.uuid4())
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            data = {
                "search_request_id": req_id,
                "search_mode": "gallery_match",
                "query_observation_id": str(obs_id),
                "query_source_observation_id": sid,
                "query_gallery_embedding_id": gid,
                "rank": 1,
                "similarity": 0.95,
                "expires_at": expires,
            }
            row_id = repo.insert_gallery_match_result(data)
            assert row_id is not None
            assert row_id > 0

            results = repo.get_by_search_request(req_id)
            assert len(results) == 1
            assert results[0]["rank"] == 1
            assert results[0]["similarity"] == pytest.approx(0.95)
            assert results[0]["query_source_observation_id"] == sid
            assert results[0]["query_observation_id"] == obs_id
            assert results[0]["matched_observation_id"] is None
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_idempotent_insert(self):
        """Duplicate (search_request_id, query_gallery_embedding_id) is skipped."""
        conn = self._connect()
        repo = MatchResultRepository(conn)
        try:
            obs_id, sid = self._create_observation(conn, "idempotent", _unit_embedding())
            pid = self._create_person(conn, "idempotent")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())

            req_id = str(uuid.uuid4())
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            data = {
                "search_request_id": req_id,
                "search_mode": "gallery_match",
                "query_observation_id": str(obs_id),
                "query_source_observation_id": sid,
                "query_gallery_embedding_id": gid,
                "rank": 1,
                "similarity": 0.95,
                "expires_at": expires,
            }

            # First insert
            row_id_1 = repo.insert_gallery_match_result(data)
            assert row_id_1 is not None

            # Second insert (duplicate) — returns None
            row_id_2 = repo.insert_gallery_match_result(data)
            assert row_id_2 is None

            # Only one row exists
            results = repo.get_by_search_request(req_id)
            assert len(results) == 1
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_topk_multiple_gallery_results_all_persisted(self):
        """topK=3 with 3 different gallery embeddings → 3 match_results rows.

        This is the key regression test for the topK collapse bug.
        All rows share the same (search_request_id, query_observation_id)
        but have different query_gallery_embedding_id values.
        """
        conn = self._connect()
        repo = MatchResultRepository(conn)
        try:
            obs_id, sid = self._create_observation(conn, "topk3", _unit_embedding())
            pid = self._create_person(conn, "topk3")

            # Create 3 real gallery embeddings with different vectors
            embeddings = [
                _unit_embedding(),
                _randomlike_embedding(seed=10),
                _randomlike_embedding(seed=20),
            ]
            gallery_ids = []
            for emb in embeddings:
                gid = self._create_gallery_embedding(conn, pid, emb)
                gallery_ids.append(gid)

            req_id = str(uuid.uuid4())
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            similarities = [0.95, 0.80, 0.70]

            for rank, (gid, sim) in enumerate(
                zip(gallery_ids, similarities), 1
            ):
                data = {
                    "search_request_id": req_id,
                    "search_mode": "gallery_match",
                    "query_observation_id": str(obs_id),
                    "query_source_observation_id": sid,
                    "query_gallery_embedding_id": gid,
                    "rank": rank,
                    "similarity": sim,
                    "expires_at": expires,
                }
                row_id = repo.insert_gallery_match_result(data)
                assert row_id is not None, (
                    f"gallery_embedding_id={gid} should insert successfully"
                )

            # All 3 rows must be present
            results = repo.get_by_search_request(req_id)
            assert len(results) == 3, (
                f"Expected 3 results, got {len(results)}. "
                "topK collapse bug may have regressed."
            )

            # Verify ordering and content
            assert results[0]["rank"] == 1
            assert results[1]["rank"] == 2
            assert results[2]["rank"] == 3
            assert results[0]["similarity"] > results[1]["similarity"]
            assert results[1]["similarity"] > results[2]["similarity"]

            # Each has distinct gallery_embedding_id
            result_gids = {r["query_gallery_embedding_id"] for r in results}
            assert result_gids == set(gallery_ids)

            # All share same query observation
            for r in results:
                assert r["query_observation_id"] == obs_id
                assert r["query_source_observation_id"] == sid
                assert r["matched_observation_id"] is None
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_different_requests_independent(self):
        """Different search_request_ids are independent."""
        conn = self._connect()
        repo = MatchResultRepository(conn)
        try:
            obs_id, sid = self._create_observation(conn, "independent", _unit_embedding())
            pid = self._create_person(conn, "independent")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())

            req_a = str(uuid.uuid4())
            req_b = str(uuid.uuid4())
            expires = datetime.now(timezone.utc) + timedelta(hours=1)

            for req_id, sim in [(req_a, 0.95), (req_b, 0.80)]:
                repo.insert_gallery_match_result({
                    "search_request_id": req_id,
                    "search_mode": "gallery_match",
                    "query_observation_id": str(obs_id),
                    "query_source_observation_id": sid,
                    "query_gallery_embedding_id": gid,
                    "rank": 1,
                    "similarity": sim,
                    "expires_at": expires,
                })

            results_a = repo.get_by_search_request(req_a)
            results_b = repo.get_by_search_request(req_b)
            assert len(results_a) == 1
            assert len(results_b) == 1
            assert results_a[0]["similarity"] == pytest.approx(0.95)
            assert results_b[0]["similarity"] == pytest.approx(0.80)
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_full_match_flow(self):
        """End-to-end: observation → gallery search → match_results written.

        Verifies:
        - All gallery results are persisted (not collapsed to 1)
        - query_observation_id is set correctly
        - matched_observation_id is NULL
        - rank ordering is correct
        """
        conn = self._connect()
        from app.vector_store import FaceVectorStore
        try:
            emb_a = _randomlike_embedding(seed=55)
            emb_b = _randomlike_embedding(seed=77)

            obs_id, obs_sid = self._create_observation(conn, "full_flow", emb_a)
            pid_a = self._create_person(conn, "full_flow_a")
            pid_b = self._create_person(conn, "full_flow_b")
            gid_a = self._create_gallery_embedding(conn, pid_a, emb_a)
            gid_b = self._create_gallery_embedding(conn, pid_b, emb_b)

            store = FaceVectorStore(conn)
            match_repo = MatchResultRepository(conn)

            obs_emb = emb_a  # We already have the embedding

            # Search gallery — should find both, emb_a first (self-match)
            results = store.search_gallery(obs_emb, top_k=5)
            assert len(results) >= 2
            assert results[0]["person_id"] == pid_a
            assert results[0]["similarity"] == pytest.approx(1.0, abs=0.01)

            # Write match_results — must persist ALL results
            req_id = str(uuid.uuid4())
            expires = datetime.now(timezone.utc) + timedelta(hours=1)

            for rank, result in enumerate(results, 1):
                data = {
                    "search_request_id": req_id,
                    "search_mode": "gallery_match",
                    "query_observation_id": str(obs_id),
                    "query_source_observation_id": obs_sid,
                    "query_person_id": result.get("person_id"),
                    "query_gallery_embedding_id": result["id"],
                    "rank": rank,
                    "similarity": result["similarity"],
                    "expires_at": expires,
                }
                match_repo.insert_gallery_match_result(data)

            # Verify ALL results persisted (not just >= 1)
            stored = match_repo.get_by_search_request(req_id)
            assert len(stored) == len(results), (
                f"Expected {len(results)} match_results, got {len(stored)}. "
                "topK collapse bug may have regressed."
            )

            # Verify semantics
            for r in stored:
                assert r["query_observation_id"] == obs_id
                assert r["query_source_observation_id"] == obs_sid
                assert r["matched_observation_id"] is None
                assert r["query_gallery_embedding_id"] is not None

            assert stored[0]["query_person_id"] == pid_a
            assert stored[0]["similarity"] == pytest.approx(1.0, abs=0.01)
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_matched_observation_id_null_for_gallery_match(self):
        """gallery_match rows must have matched_observation_id = NULL."""
        conn = self._connect()
        repo = MatchResultRepository(conn)
        try:
            obs_id, sid = self._create_observation(conn, "null_match", _unit_embedding())
            pid = self._create_person(conn, "null_match")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())

            req_id = str(uuid.uuid4())
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            repo.insert_gallery_match_result({
                "search_request_id": req_id,
                "search_mode": "gallery_match",
                "query_observation_id": str(obs_id),
                "query_source_observation_id": sid,
                "query_gallery_embedding_id": gid,
                "rank": 1,
                "similarity": 0.9,
                "expires_at": expires,
            })

            results = repo.get_by_search_request(req_id)
            assert len(results) == 1
            assert results[0]["matched_observation_id"] is None
            assert results[0]["matched_camera_id"] is None
            assert results[0]["matched_source_id"] is None
            assert results[0]["matched_track_id"] is None
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_delete_expired(self):
        """Expired match_results are deleted by delete_expired."""
        conn = self._connect()
        repo = MatchResultRepository(conn)
        try:
            obs_id, sid = self._create_observation(conn, "expired", _unit_embedding())
            pid = self._create_person(conn, "expired")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())

            req_id = str(uuid.uuid4())
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            repo.insert_gallery_match_result({
                "search_request_id": req_id,
                "search_mode": "gallery_match",
                "query_observation_id": str(obs_id),
                "query_source_observation_id": sid,
                "query_gallery_embedding_id": gid,
                "rank": 1,
                "similarity": 0.95,
                "expires_at": past,
            })

            results = repo.get_by_search_request(req_id)
            assert len(results) == 1

            deleted = repo.delete_expired()
            assert deleted >= 1

            results = repo.get_by_search_request(req_id)
            assert len(results) == 0
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_no_leftover_test_rows(self):
        """Verify cleanup: no test rows remain."""
        conn = self._connect()
        try:
            self._cleanup(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM face_observations "
                    "WHERE source_observation_id LIKE 'test:midterm_match:%%'"
                )
                assert cur.fetchone()[0] == 0
                cur.execute(
                    "SELECT count(*) FROM persons "
                    "WHERE name LIKE 'test:midterm_match:%%'"
                )
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()
