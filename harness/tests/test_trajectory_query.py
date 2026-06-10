"""Tests for midterm registered person trajectory query harness.

Test classes:
- TestTrajectoryCliArgs: argparse validation for query_trajectory.py CLI
- TestParseIso8601: ISO 8601 parsing
- TestFormatTable: table formatting
- TestTrajectoryRepositoryUnit: mocked DB, TrajectoryRepository
- TestQueryRuntime: real query_trajectory.query()/query_trajectory.main() with monkeypatched DB (no real PostgreSQL)
- TestTrajectoryQueryIntegration: real PostgreSQL + pgvector (integration mark)
"""

from __future__ import annotations

import json
import math
import uuid
import importlib.util
from datetime import datetime, timedelta, timezone
from io import StringIO
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

from app.trajectory_repository import (
    TrajectoryRepository,
    _DEFAULT_LIMIT,
    _MAX_LIMIT,
    _MIN_LIMIT,
)

QUERY_TRAJECTORY_PATH = FACE_WORKER_ROOT / "query_trajectory.py"
_query_spec = importlib.util.spec_from_file_location(
    "query_trajectory_under_test", QUERY_TRAJECTORY_PATH
)
assert _query_spec is not None and _query_spec.loader is not None
query_trajectory = importlib.util.module_from_spec(_query_spec)
_query_spec.loader.exec_module(query_trajectory)


# ── Helpers (pure Python, no DB) ───────────────────────────────────────────

_EMBEDDING_DIM = 512


def _unit_embedding() -> list[float]:
    val = 1.0 / math.sqrt(_EMBEDDING_DIM)
    return [val] * _EMBEDDING_DIM


# ── Class 1: CLI Argument Validation ───────────────────────────────────────

class TestTrajectoryCliArgs:
    """query_trajectory.py argparse validation."""

    @staticmethod
    def _parse(args: list[str]):
        """Build the same argparse as query_trajectory.main() and parse."""
        import argparse

        parser = argparse.ArgumentParser()
        person_group = parser.add_mutually_exclusive_group(required=True)
        person_group.add_argument("--person-id", type=int, default=None)
        person_group.add_argument("--external-person-id", default=None)
        parser.add_argument("--time-from", default=None)
        parser.add_argument("--time-to", default=None)
        parser.add_argument("--camera-id", default=None)
        parser.add_argument("--min-similarity", type=float, default=None)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--json", action="store_true", default=False, dest="output_json")
        return parser.parse_args(args)

    def test_person_id_required(self):
        """Missing both --person-id and --external-person-id → SystemExit."""
        with pytest.raises(SystemExit):
            self._parse([])

    def test_person_id_accepted(self):
        args = self._parse(["--person-id", "42"])
        assert args.person_id == 42
        assert args.external_person_id is None

    def test_external_person_id_accepted(self):
        args = self._parse(["--external-person-id", "EMP-00123"])
        assert args.external_person_id == "EMP-00123"
        assert args.person_id is None

    def test_mutually_exclusive(self):
        """Cannot specify both --person-id and --external-person-id."""
        with pytest.raises(SystemExit):
            self._parse(["--person-id", "1", "--external-person-id", "X"])

    def test_defaults(self):
        args = self._parse(["--person-id", "1"])
        assert args.time_from is None
        assert args.time_to is None
        assert args.camera_id is None
        assert args.min_similarity is None
        assert args.limit == 100
        assert args.output_json is False

    def test_all_options(self):
        args = self._parse([
            "--person-id", "42",
            "--time-from", "2026-05-01T00:00:00+08:00",
            "--time-to", "2026-05-28T23:59:59+08:00",
            "--camera-id", "cam-lobby",
            "--min-similarity", "0.7",
            "--limit", "50",
            "--json",
        ])
        assert args.person_id == 42
        assert args.time_from == "2026-05-01T00:00:00+08:00"
        assert args.time_to == "2026-05-28T23:59:59+08:00"
        assert args.camera_id == "cam-lobby"
        assert args.min_similarity == 0.7
        assert args.limit == 50
        assert args.output_json is True

    def test_json_flag(self):
        args = self._parse(["--person-id", "1", "--json"])
        assert args.output_json is True


# ── ISO 8601 Parsing Tests ─────────────────────────────────────────────────

class TestParseIso8601:
    """query_trajectory._parse_iso8601 validation."""

    @staticmethod
    def _call(value: str) -> int:
        return query_trajectory._parse_iso8601(value, "test")

    def test_valid_aware_datetime(self):
        result = self._call("2026-05-01T00:00:00+08:00")
        # 2026-05-01 00:00:00+08:00 = 2026-04-30 16:00:00 UTC
        dt = datetime(2026, 4, 30, 16, 0, 0, tzinfo=timezone.utc)
        assert result == int(dt.timestamp() * 1000)

    def test_valid_utc(self):
        result = self._call("2026-05-01T00:00:00+00:00")
        dt = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert result == int(dt.timestamp() * 1000)

    def test_naive_datetime_rejected(self):
        """Naive datetime (no tzinfo) must be rejected."""
        with pytest.raises(SystemExit):
            query_trajectory._parse_iso8601("2026-05-01T00:00:00", "test")

    def test_invalid_format_rejected(self):
        with pytest.raises(SystemExit):
            query_trajectory._parse_iso8601("not-a-date", "test")


# ── Table Formatting Tests ─────────────────────────────────────────────────

class TestFormatTable:
    """query_trajectory._format_table output."""

    @staticmethod
    def _call(rows: list[dict]) -> str:
        return query_trajectory._format_table(rows)

    def test_empty_rows(self):
        assert self._call([]) == "(no results)"

    def test_single_row(self):
        rows = [{
            "rank": 1,
            "matched_timestamp_ms": 1717000000000,
            "matched_camera_id": "cam1",
            "matched_track_id": "t1",
            "similarity": 0.95,
            "person_name": "Alice",
            "external_person_id": "EXT-1",
            "snapshot_path": "/snap.jpg",
        }]
        output = self._call(rows)
        assert "cam1" in output
        assert "0.9500" in output
        assert "Alice" in output
        assert "1 rows" in output

    def test_missing_fields_show_question_mark(self):
        rows = [{"rank": 1}]
        output = self._call(rows)
        assert "?" in output


# ── Class 2: TrajectoryRepository Unit Tests (mocked DB) ───────────────────

class TestTrajectoryRepositoryUnit:
    """TrajectoryRepository with mocked psycopg.Connection."""

    @staticmethod
    def _make_repo() -> tuple[TrajectoryRepository, MagicMock]:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        return TrajectoryRepository(conn), cursor

    def test_basic_query(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = [
            {"rank": 1, "similarity": 0.95, "matched_camera_id": "cam1"},
        ]
        rows = repo.get_person_trajectory(42)
        assert len(rows) == 1
        assert rows[0]["similarity"] == 0.95

    def test_empty_result(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        rows = repo.get_person_trajectory(42)
        assert rows == []

    def test_sql_filters_by_registered_person_history(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42)
        sql = cursor.execute.call_args[0][0]
        assert "registered_person_history" in sql

    def test_sql_filters_by_person_id(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42)
        params = cursor.execute.call_args[0][1]
        assert params["person_id"] == 42

    def test_sql_requires_matched_observation_id(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42)
        sql = cursor.execute.call_args[0][0]
        assert "matched_observation_id IS NOT NULL" in sql

    def test_sql_joins_persons(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42)
        sql = cursor.execute.call_args[0][0]
        assert "JOIN persons" in sql

    def test_sql_joins_face_observations(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42)
        sql = cursor.execute.call_args[0][0]
        assert "LEFT JOIN face_observations" in sql

    def test_sql_orders_by_timestamp_desc(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42)
        sql = cursor.execute.call_args[0][0]
        assert "matched_timestamp_ms DESC" in sql

    def test_time_from_filter(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42, time_from_ms=1000)
        params = cursor.execute.call_args[0][1]
        assert params["time_from_ms"] == 1000
        sql = cursor.execute.call_args[0][0]
        assert "matched_timestamp_ms >= %(time_from_ms)s" in sql

    def test_time_to_filter(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42, time_to_ms=2000)
        params = cursor.execute.call_args[0][1]
        assert params["time_to_ms"] == 2000
        sql = cursor.execute.call_args[0][0]
        assert "matched_timestamp_ms <= %(time_to_ms)s" in sql

    def test_camera_id_filter(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42, camera_id="cam-lobby")
        params = cursor.execute.call_args[0][1]
        assert params["camera_id"] == "cam-lobby"
        sql = cursor.execute.call_args[0][0]
        assert "matched_camera_id = %(camera_id)s" in sql

    def test_min_similarity_filter(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42, min_similarity=0.7)
        params = cursor.execute.call_args[0][1]
        assert params["min_similarity"] == 0.7
        sql = cursor.execute.call_args[0][0]
        assert "similarity >= %(min_similarity)s" in sql

    def test_min_similarity_validation_below(self):
        repo, _ = self._make_repo()
        with pytest.raises(ValueError, match="outside"):
            repo.get_person_trajectory(42, min_similarity=-0.1)

    def test_min_similarity_validation_above(self):
        repo, _ = self._make_repo()
        with pytest.raises(ValueError, match="outside"):
            repo.get_person_trajectory(42, min_similarity=1.1)

    def test_min_similarity_boundary_zero(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42, min_similarity=0.0)
        params = cursor.execute.call_args[0][1]
        assert params["min_similarity"] == 0.0

    def test_min_similarity_boundary_one(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42, min_similarity=1.0)
        params = cursor.execute.call_args[0][1]
        assert params["min_similarity"] == 1.0

    def test_limit_default(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42)
        params = cursor.execute.call_args[0][1]
        assert params["limit"] == _DEFAULT_LIMIT

    def test_limit_custom(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42, limit=50)
        params = cursor.execute.call_args[0][1]
        assert params["limit"] == 50

    def test_limit_clamped_below(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42, limit=0)
        params = cursor.execute.call_args[0][1]
        assert params["limit"] == _MIN_LIMIT

    def test_limit_clamped_above(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42, limit=9999)
        params = cursor.execute.call_args[0][1]
        assert params["limit"] == _MAX_LIMIT

    def test_no_time_filter_when_none(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42)
        sql = cursor.execute.call_args[0][0]
        assert "matched_timestamp_ms >=" not in sql
        assert "matched_timestamp_ms <=" not in sql

    def test_no_camera_filter_when_none(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42)
        sql = cursor.execute.call_args[0][0]
        assert "matched_camera_id = %(camera_id)s" not in sql

    def test_no_similarity_filter_when_none(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(42)
        sql = cursor.execute.call_args[0][0]
        assert "similarity >= %(min_similarity)s" not in sql

    def test_all_filters_combined(self):
        repo, cursor = self._make_repo()
        cursor.fetchall.return_value = []
        repo.get_person_trajectory(
            42,
            time_from_ms=1000,
            time_to_ms=2000,
            camera_id="cam1",
            min_similarity=0.8,
            limit=25,
        )
        params = cursor.execute.call_args[0][1]
        assert params["person_id"] == 42
        assert params["time_from_ms"] == 1000
        assert params["time_to_ms"] == 2000
        assert params["camera_id"] == "cam1"
        assert params["min_similarity"] == 0.8
        assert params["limit"] == 25


# ── Class 3: Real CLI Runtime Tests (monkeypatched DB) ─────────────────────

class TestQueryRuntime:
    """Exercise real query()/main() with monkeypatched DB connections."""

    @staticmethod
    def _mock_connect(mock_conn, mock_person=None, mock_rows=None):
        """Set up mock psycopg.connect and register_vector."""
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor

        mock_cursor.fetchone.return_value = mock_person
        mock_cursor.fetchall.return_value = mock_rows or []

        return mock_cursor

    def test_external_person_id_resolves_and_queries(self, capsys):
        """external-person-id resolves to person_id, queries trajectory."""
        mock_person = {"id": 7, "name": "Alice", "external_person_id": "EXT-7"}
        mock_rows = [
            {
                "rank": 1,
                "similarity": 0.9,
                "matched_camera_id": "cam1",
                "matched_timestamp_ms": 100000,
                "matched_track_id": "t1",
                "person_name": "Alice",
                "external_person_id": "EXT-7",
                "snapshot_path": None,
                "crop_path": None,
            },
        ]

        with patch.object(query_trajectory.os, "environ", {"DATABASE_URL": "mock://db"}), \
             patch.object(query_trajectory, "psycopg") as mock_psycopg, \
             patch.object(query_trajectory, "register_vector"):
            mock_conn = MagicMock()
            mock_psycopg.connect.return_value = mock_conn
            self._mock_connect(mock_conn, mock_person=mock_person, mock_rows=mock_rows)

            rows = query_trajectory.query(
                person_id=None,
                external_person_id="EXT-7",
                time_from_ms=None,
                time_to_ms=None,
                camera_id=None,
                min_similarity=None,
                limit=100,
                output_json=False,
            )

        assert len(rows) == 1
        captured = capsys.readouterr()
        assert "Resolved external_person_id=EXT-7" in captured.out
        assert "person_id=7" in captured.out

    def test_external_person_id_json_clean_output(self, capsys):
        """--external-person-id --json produces pure JSON on stdout."""
        mock_person = {"id": 7, "name": "Alice", "external_person_id": "EXT-7"}
        mock_rows = [
            {
                "rank": 1,
                "similarity": 0.9,
                "matched_camera_id": "cam1",
                "matched_timestamp_ms": 100000,
                "matched_track_id": "t1",
                "person_name": "Alice",
                "external_person_id": "EXT-7",
                "snapshot_path": None,
                "crop_path": None,
            },
        ]

        with patch.object(query_trajectory.os, "environ", {"DATABASE_URL": "mock://db"}), \
             patch.object(query_trajectory, "psycopg") as mock_psycopg, \
             patch.object(query_trajectory, "register_vector"):
            mock_conn = MagicMock()
            mock_psycopg.connect.return_value = mock_conn
            self._mock_connect(mock_conn, mock_person=mock_person, mock_rows=mock_rows)

            query_trajectory.query(
                person_id=None,
                external_person_id="EXT-7",
                time_from_ms=None,
                time_to_ms=None,
                camera_id=None,
                min_similarity=None,
                limit=100,
                output_json=True,
            )

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert len(parsed) == 1
        assert parsed[0]["person_name"] == "Alice"
        assert "Resolved" not in captured.out

    def test_external_person_id_not_found_exits(self):
        """Missing external_person_id raises SystemExit."""
        with patch.object(query_trajectory.os, "environ", {"DATABASE_URL": "mock://db"}), \
             patch.object(query_trajectory, "psycopg") as mock_psycopg, \
             patch.object(query_trajectory, "register_vector"):
            mock_conn = MagicMock()
            mock_psycopg.connect.return_value = mock_conn
            self._mock_connect(mock_conn, mock_person=None)

            with pytest.raises(SystemExit):
                query_trajectory.query(
                    person_id=None,
                    external_person_id="NONEXISTENT",
                    time_from_ms=None,
                    time_to_ms=None,
                    camera_id=None,
                    min_similarity=None,
                    limit=100,
                    output_json=False,
                )

    def test_json_output_no_embedding(self, capsys):
        """JSON output must not contain raw embedding vectors."""
        mock_rows = [
            {
                "rank": 1,
                "similarity": 0.9,
                "matched_camera_id": "cam1",
                "matched_timestamp_ms": 100000,
                "matched_track_id": "t1",
                "person_name": "Alice",
                "external_person_id": None,
                "snapshot_path": None,
                "crop_path": None,
                "search_request_id": "req-1",
            },
        ]

        with patch.object(query_trajectory.os, "environ", {"DATABASE_URL": "mock://db"}), \
             patch.object(query_trajectory, "psycopg") as mock_psycopg, \
             patch.object(query_trajectory, "register_vector"):
            mock_conn = MagicMock()
            mock_psycopg.connect.return_value = mock_conn
            self._mock_connect(mock_conn, mock_rows=mock_rows)

            query_trajectory.query(
                person_id=1,
                external_person_id=None,
                time_from_ms=None,
                time_to_ms=None,
                camera_id=None,
                min_similarity=None,
                limit=100,
                output_json=True,
            )

        parsed = json.loads(capsys.readouterr().out)
        for row in parsed:
            assert "embedding" not in row

    def test_table_output_shows_person_id(self, capsys):
        """Non-JSON output shows person_id header."""
        mock_rows = [
            {
                "rank": 1,
                "similarity": 0.9,
                "matched_camera_id": "cam1",
                "matched_timestamp_ms": 100000,
                "matched_track_id": "t1",
                "person_name": "Alice",
                "external_person_id": None,
                "snapshot_path": None,
                "crop_path": None,
            },
        ]

        with patch.object(query_trajectory.os, "environ", {"DATABASE_URL": "mock://db"}), \
             patch.object(query_trajectory, "psycopg") as mock_psycopg, \
             patch.object(query_trajectory, "register_vector"):
            mock_conn = MagicMock()
            mock_psycopg.connect.return_value = mock_conn
            self._mock_connect(mock_conn, mock_rows=mock_rows)

            query_trajectory.query(
                person_id=42,
                external_person_id=None,
                time_from_ms=None,
                time_to_ms=None,
                camera_id=None,
                min_similarity=None,
                limit=100,
                output_json=False,
            )

        assert "person_id=42" in capsys.readouterr().out


# ── Class 4: CLI main() Validation Tests ───────────────────────────────────

class TestMainValidation:
    """Test main() argument validation."""

    def test_reversed_time_range_exits(self):
        """--time-from after --time-to raises SystemExit."""
        with patch("sys.argv", [
            "query_trajectory.py",
            "--person-id", "1",
            "--time-from", "2026-05-28T00:00:00+08:00",
            "--time-to", "2026-05-01T00:00:00+08:00",
        ]):
            with pytest.raises(SystemExit):
                query_trajectory.main()

    def test_min_similarity_below_zero_rejected(self):
        """--min-similarity -0.1 raises SystemExit."""
        with patch("sys.argv", [
            "query_trajectory.py",
            "--person-id", "1",
            "--min-similarity", "-0.1",
        ]):
            with pytest.raises(SystemExit):
                query_trajectory.main()

    def test_min_similarity_above_one_rejected(self):
        """--min-similarity 1.1 raises SystemExit."""
        with patch("sys.argv", [
            "query_trajectory.py",
            "--person-id", "1",
            "--min-similarity", "1.1",
        ]):
            with pytest.raises(SystemExit):
                query_trajectory.main()

    def test_min_similarity_boundary_zero_accepted(self):
        """--min-similarity 0.0 is accepted."""
        with patch("sys.argv", [
            "query_trajectory.py",
            "--person-id", "1",
            "--min-similarity", "0.0",
        ]), patch.object(query_trajectory.os, "environ", {"DATABASE_URL": "mock://db"}), \
             patch.object(query_trajectory, "psycopg") as mock_psycopg, \
             patch.object(query_trajectory, "register_vector"):
            mock_conn = MagicMock()
            mock_psycopg.connect.return_value = mock_conn
            mock_cursor = MagicMock()
            mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
            mock_cursor.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = []

            query_trajectory.main()

    def test_min_similarity_boundary_one_accepted(self):
        """--min-similarity 1.0 is accepted."""
        with patch("sys.argv", [
            "query_trajectory.py",
            "--person-id", "1",
            "--min-similarity", "1.0",
        ]), patch.object(query_trajectory.os, "environ", {"DATABASE_URL": "mock://db"}), \
             patch.object(query_trajectory, "psycopg") as mock_psycopg, \
             patch.object(query_trajectory, "register_vector"):
            mock_conn = MagicMock()
            mock_psycopg.connect.return_value = mock_conn
            mock_cursor = MagicMock()
            mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
            mock_cursor.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = []

            query_trajectory.main()


# ── Class 5: Integration Tests (real PostgreSQL) ───────────────────────────

@pytest.mark.integration
class TestTrajectoryQueryIntegration:
    """Integration tests against a real PostgreSQL with pgvector.

    All test data uses names prefixed with ``test:midterm_trajectory:``.
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
                "WHERE query_source_observation_id LIKE 'test:midterm_trajectory:%%' "
                "OR matched_source_observation_id LIKE 'test:midterm_trajectory:%%'"
            )
            cur.execute(
                "DELETE FROM person_gallery_embeddings "
                "WHERE person_id IN "
                "(SELECT id FROM persons WHERE name LIKE 'test:midterm_trajectory:%%')"
            )
            cur.execute(
                "DELETE FROM persons WHERE name LIKE 'test:midterm_trajectory:%%'"
            )
            cur.execute(
                "DELETE FROM face_observations "
                "WHERE source_observation_id LIKE 'test:midterm_trajectory:%%'"
            )
        conn.commit()

    def _create_person(self, conn, suffix: str, *, external_id: str | None = None) -> int:
        """Create a test person. Returns person_id."""
        name = f"test:midterm_trajectory:{suffix}"
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO persons (name, external_person_id, created_by)
                VALUES (%(name)s, %(ext)s, 'test')
                RETURNING id
                """,
                {"name": name, "ext": external_id},
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

    def _create_observation(
        self, conn, suffix: str, emb: list[float], *,
        camera_id: str = "cam1",
        track_id: str = "1",
        timestamp_ms: int = 1000,
        snapshot_path: str | None = None,
        crop_path: str | None = None,
    ) -> tuple:
        """Create a test face_observation. Returns (observation_uuid, source_observation_id)."""
        from pgvector.psycopg import Vector
        sid = f"test:midterm_trajectory:{suffix}"
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO face_observations (
                    source_observation_id, camera_id, source_id, track_id,
                    timestamp_ms, face_bbox, landmarks,
                    face_confidence, quality, embedding_dim,
                    embedding, embedding_norm, reid_throttle_key,
                    snapshot_path, crop_path
                ) VALUES (
                    %(sid)s, %(cam)s, 'src1', %(tid)s,
                    %(ts)s,
                    '[320,240,60,60]'::jsonb,
                    '[100,200,150,200,125,230,110,240,140,240]'::jsonb,
                    0.90, 0.88, 512,
                    %(emb)s, 1.0, %(tid)s,
                    %(snap)s, %(crop)s
                )
                ON CONFLICT (source_observation_id) DO NOTHING
                RETURNING id
                """,
                {
                    "sid": sid,
                    "cam": camera_id,
                    "tid": track_id,
                    "ts": timestamp_ms,
                    "emb": Vector(emb),
                    "snap": snapshot_path,
                    "crop": crop_path,
                },
            )
            row = cur.fetchone()
            conn.commit()
            if row is None:
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

    def _seed_trajectory_match(
        self, conn, *,
        person_id: int,
        gallery_embedding_id: int,
        matched_observation_id,
        matched_source_observation_id: str,
        matched_camera_id: str,
        matched_track_id: str,
        matched_timestamp_ms: int,
        similarity: float,
        rank: int = 1,
        search_request_id: str | None = None,
    ) -> int:
        """Insert a registered_person_history match_result row.

        Returns the match_result id.
        """
        req_id = search_request_id or str(uuid.uuid4())
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO match_results (
                    search_request_id, search_mode,
                    query_person_id, query_gallery_embedding_id,
                    matched_observation_id, matched_source_observation_id,
                    matched_camera_id, matched_source_id, matched_track_id,
                    matched_timestamp_ms, rank, similarity,
                    expires_at
                ) VALUES (
                    %(req_id)s, 'registered_person_history',
                    %(person_id)s, %(gallery_embedding_id)s,
                    %(matched_observation_id)s, %(matched_source_observation_id)s,
                    %(matched_camera_id)s, 'src1', %(matched_track_id)s,
                    %(matched_timestamp_ms)s, %(rank)s, %(similarity)s,
                    %(expires_at)s
                )
                ON CONFLICT (search_request_id, matched_observation_id) DO NOTHING
                RETURNING id
                """,
                {
                    "req_id": req_id,
                    "person_id": person_id,
                    "gallery_embedding_id": gallery_embedding_id,
                    "matched_observation_id": matched_observation_id,
                    "matched_source_observation_id": matched_source_observation_id,
                    "matched_camera_id": matched_camera_id,
                    "matched_track_id": matched_track_id,
                    "matched_timestamp_ms": matched_timestamp_ms,
                    "rank": rank,
                    "similarity": similarity,
                    "expires_at": expires,
                },
            )
            row = cur.fetchone()
            conn.commit()
            return row[0] if row is not None else None

    def test_basic_trajectory_query(self):
        """Seed one trajectory entry, query it back."""
        conn = self._connect()
        repo = TrajectoryRepository(conn)
        try:
            pid = self._create_person(conn, "basic")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())
            obs_id, obs_sid = self._create_observation(
                conn, "basic", _unit_embedding(),
                camera_id="cam-lobby", track_id="t1", timestamp_ms=100000,
            )

            self._seed_trajectory_match(
                conn,
                person_id=pid,
                gallery_embedding_id=gid,
                matched_observation_id=obs_id,
                matched_source_observation_id=obs_sid,
                matched_camera_id="cam-lobby",
                matched_track_id="t1",
                matched_timestamp_ms=100000,
                similarity=0.92,
            )

            rows = repo.get_person_trajectory(pid)
            assert len(rows) == 1
            assert rows[0]["matched_camera_id"] == "cam-lobby"
            assert rows[0]["similarity"] == pytest.approx(0.92)
            assert rows[0]["person_name"] == f"test:midterm_trajectory:basic"
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_trajectory_ordered_by_timestamp_desc(self):
        """Results ordered by matched_timestamp_ms DESC."""
        conn = self._connect()
        repo = TrajectoryRepository(conn)
        try:
            pid = self._create_person(conn, "order")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())

            timestamps = [300000, 100000, 200000]
            for i, ts in enumerate(timestamps):
                obs_id, obs_sid = self._create_observation(
                    conn, f"order_{i}", _unit_embedding(),
                    camera_id="cam1", track_id=f"t{i}", timestamp_ms=ts,
                )
                self._seed_trajectory_match(
                    conn,
                    person_id=pid,
                    gallery_embedding_id=gid,
                    matched_observation_id=obs_id,
                    matched_source_observation_id=obs_sid,
                    matched_camera_id="cam1",
                    matched_track_id=f"t{i}",
                    matched_timestamp_ms=ts,
                    similarity=0.9,
                    rank=i + 1,
                )

            rows = repo.get_person_trajectory(pid)
            assert len(rows) == 3
            # DESC order: 300000, 200000, 100000
            assert rows[0]["matched_timestamp_ms"] == 300000
            assert rows[1]["matched_timestamp_ms"] == 200000
            assert rows[2]["matched_timestamp_ms"] == 100000
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_trajectory_time_range_filter(self):
        """Filter by time_from_ms and time_to_ms."""
        conn = self._connect()
        repo = TrajectoryRepository(conn)
        try:
            pid = self._create_person(conn, "trange")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())

            for i, ts in enumerate([50000, 150000, 250000]):
                obs_id, obs_sid = self._create_observation(
                    conn, f"trange_{i}", _unit_embedding(),
                    timestamp_ms=ts,
                )
                self._seed_trajectory_match(
                    conn,
                    person_id=pid,
                    gallery_embedding_id=gid,
                    matched_observation_id=obs_id,
                    matched_source_observation_id=obs_sid,
                    matched_camera_id="cam1",
                    matched_track_id=f"t{i}",
                    matched_timestamp_ms=ts,
                    similarity=0.9,
                )

            # Only 150000 should match
            rows = repo.get_person_trajectory(
                pid, time_from_ms=100000, time_to_ms=200000,
            )
            assert len(rows) == 1
            assert rows[0]["matched_timestamp_ms"] == 150000
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_trajectory_camera_filter(self):
        """Filter by camera_id."""
        conn = self._connect()
        repo = TrajectoryRepository(conn)
        try:
            pid = self._create_person(conn, "camfilter")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())

            for i, cam in enumerate(["cam-lobby", "cam-parking", "cam-lobby"]):
                obs_id, obs_sid = self._create_observation(
                    conn, f"camfilter_{i}", _unit_embedding(),
                    camera_id=cam, timestamp_ms=100000 + i * 1000,
                )
                self._seed_trajectory_match(
                    conn,
                    person_id=pid,
                    gallery_embedding_id=gid,
                    matched_observation_id=obs_id,
                    matched_source_observation_id=obs_sid,
                    matched_camera_id=cam,
                    matched_track_id=f"t{i}",
                    matched_timestamp_ms=100000 + i * 1000,
                    similarity=0.9,
                )

            rows = repo.get_person_trajectory(pid, camera_id="cam-lobby")
            assert len(rows) == 2
            assert all(r["matched_camera_id"] == "cam-lobby" for r in rows)
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_trajectory_min_similarity_filter(self):
        """Filter by min_similarity."""
        conn = self._connect()
        repo = TrajectoryRepository(conn)
        try:
            pid = self._create_person(conn, "simfilter")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())

            similarities = [0.6, 0.8, 0.95]
            for i, sim in enumerate(similarities):
                obs_id, obs_sid = self._create_observation(
                    conn, f"simfilter_{i}", _unit_embedding(),
                    timestamp_ms=100000 + i * 1000,
                )
                self._seed_trajectory_match(
                    conn,
                    person_id=pid,
                    gallery_embedding_id=gid,
                    matched_observation_id=obs_id,
                    matched_source_observation_id=obs_sid,
                    matched_camera_id="cam1",
                    matched_track_id=f"t{i}",
                    matched_timestamp_ms=100000 + i * 1000,
                    similarity=sim,
                )

            rows = repo.get_person_trajectory(pid, min_similarity=0.75)
            assert len(rows) == 2
            assert all(r["similarity"] >= 0.75 for r in rows)
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_trajectory_limit(self):
        """Limit caps the number of results."""
        conn = self._connect()
        repo = TrajectoryRepository(conn)
        try:
            pid = self._create_person(conn, "limit")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())

            for i in range(5):
                obs_id, obs_sid = self._create_observation(
                    conn, f"limit_{i}", _unit_embedding(),
                    timestamp_ms=100000 + i * 1000,
                )
                self._seed_trajectory_match(
                    conn,
                    person_id=pid,
                    gallery_embedding_id=gid,
                    matched_observation_id=obs_id,
                    matched_source_observation_id=obs_sid,
                    matched_camera_id="cam1",
                    matched_track_id=f"t{i}",
                    matched_timestamp_ms=100000 + i * 1000,
                    similarity=0.9,
                )

            rows = repo.get_person_trajectory(pid, limit=3)
            assert len(rows) == 3
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_trajectory_empty_for_unknown_person(self):
        """No results for a person with no trajectory entries."""
        conn = self._connect()
        repo = TrajectoryRepository(conn)
        try:
            pid = self._create_person(conn, "empty")
            # No match_results seeded
            rows = repo.get_person_trajectory(pid)
            assert rows == []
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_trajectory_excludes_gallery_match_rows(self):
        """gallery_match rows (matched_observation_id=NULL) must not appear."""
        conn = self._connect()
        repo = TrajectoryRepository(conn)
        try:
            pid = self._create_person(conn, "nogallery")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())

            # Insert a gallery_match row (matched_observation_id is NULL)
            req_id = str(uuid.uuid4())
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO match_results (
                        search_request_id, search_mode,
                        query_person_id, query_gallery_embedding_id,
                        matched_observation_id,
                        rank, similarity, expires_at
                    ) VALUES (
                        %(req_id)s, 'gallery_match',
                        %(pid)s, %(gid)s,
                        NULL,
                        1, 0.95, %(expires)s
                    )
                    """,
                    {"req_id": req_id, "pid": pid, "gid": gid, "expires": expires},
                )
                conn.commit()

            rows = repo.get_person_trajectory(pid)
            assert rows == []
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_trajectory_joins_persons_for_name(self):
        """Result includes person_name from persons table."""
        conn = self._connect()
        repo = TrajectoryRepository(conn)
        try:
            pid = self._create_person(conn, "joinname")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())
            obs_id, obs_sid = self._create_observation(
                conn, "joinname", _unit_embedding(),
            )
            self._seed_trajectory_match(
                conn,
                person_id=pid,
                gallery_embedding_id=gid,
                matched_observation_id=obs_id,
                matched_source_observation_id=obs_sid,
                matched_camera_id="cam1",
                matched_track_id="t1",
                matched_timestamp_ms=100000,
                similarity=0.9,
            )

            rows = repo.get_person_trajectory(pid)
            assert len(rows) == 1
            assert rows[0]["person_name"] == "test:midterm_trajectory:joinname"
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_trajectory_snapshot_crop_path_coalesce(self):
        """snapshot_path and crop_path fall back to face_observations when match_results is NULL."""
        conn = self._connect()
        repo = TrajectoryRepository(conn)
        try:
            pid = self._create_person(conn, "coalesce")
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())
            obs_id, obs_sid = self._create_observation(
                conn, "coalesce", _unit_embedding(),
                snapshot_path="/obs/snap.jpg",
                crop_path="/obs/crop.jpg",
            )
            self._seed_trajectory_match(
                conn,
                person_id=pid,
                gallery_embedding_id=gid,
                matched_observation_id=obs_id,
                matched_source_observation_id=obs_sid,
                matched_camera_id="cam1",
                matched_track_id="t1",
                matched_timestamp_ms=100000,
                similarity=0.9,
            )

            rows = repo.get_person_trajectory(pid)
            assert len(rows) == 1
            # match_results.snapshot_path and crop_path are NULL;
            # COALESCE falls back to face_observations values
            assert rows[0]["snapshot_path"] == "/obs/snap.jpg"
            assert rows[0]["crop_path"] == "/obs/crop.jpg"
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()

    def test_trajectory_external_person_id_resolution(self):
        """Query via external_person_id resolves to correct person."""
        conn = self._connect()
        try:
            ext_id = f"test:midterm_trajectory:ext:{uuid.uuid4().hex[:8]}"
            pid = self._create_person(conn, "extrun", external_id=ext_id)
            gid = self._create_gallery_embedding(conn, pid, _unit_embedding())
            obs_id, obs_sid = self._create_observation(
                conn, "extrun", _unit_embedding(),
                camera_id="cam-ext", timestamp_ms=200000,
            )
            self._seed_trajectory_match(
                conn,
                person_id=pid,
                gallery_embedding_id=gid,
                matched_observation_id=obs_id,
                matched_source_observation_id=obs_sid,
                matched_camera_id="cam-ext",
                matched_track_id="t1",
                matched_timestamp_ms=200000,
                similarity=0.88,
            )
            conn.commit()

            # query_trajectory.query() opens its own connection, so seed data is visible
            rows = query_trajectory.query(
                person_id=None,
                external_person_id=ext_id,
                time_from_ms=None, time_to_ms=None,
                camera_id=None, min_similarity=None,
                limit=100, output_json=True,
            )

            assert len(rows) == 1
            assert rows[0]["matched_camera_id"] == "cam-ext"
            assert rows[0]["similarity"] == pytest.approx(0.88)
        finally:
            conn.rollback()
            self._cleanup(conn)
            conn.close()
