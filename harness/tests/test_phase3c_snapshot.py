"""Tests for Phase 3C — Event Snapshot MVP."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

import pytest

MW_DIR = str(Path(__file__).resolve().parents[2] / "services" / "media-worker")
if MW_DIR not in sys.path:
    sys.path.insert(0, MW_DIR)

API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.append(API_DIR)

from app.snapshot import generate_snapshot, _ffmpeg_duration, _ffmpeg_extract, _get_ffmpeg
from app.worker import _snapshot_needed, _process_pending_snapshots, _update_snapshot_status
from app.config import Config


# ===========================================================================
# Snapshot offset policy
# ===========================================================================


def test_snapshot_offset_uses_pre_seconds():
    """Snapshot extraction uses pre_seconds, not random or first/last frame."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clip_path = os.path.join(tmpdir, "test.mkv")
        with open(clip_path, "wb") as f:
            f.write(b"dummy")

        with patch("app.snapshot._ffmpeg_duration", return_value=30.0):
            with patch("app.snapshot._ffmpeg_extract", return_value=True):
                with patch("app.snapshot._get_ffmpeg", return_value="/usr/bin/ffmpeg"):
                    result = generate_snapshot(
                        "ev-001", clip_path,
                        pre_seconds=5.0, output_dir=tmpdir,
                    )

        assert result["snapshot_status"] == "ready"
        assert result["snapshot_offset_seconds"] == 5.0
        assert "snapshot_fallback_reason" not in result


def test_snapshot_fallback_when_duration_short():
    """When clip duration <= pre_seconds, fallback to duration/2."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clip_path = os.path.join(tmpdir, "short.mkv")
        with open(clip_path, "wb") as f:
            f.write(b"dummy")

        with patch("app.snapshot._ffmpeg_duration", return_value=2.0):
            with patch("app.snapshot._ffmpeg_extract", return_value=True):
                with patch("app.snapshot._get_ffmpeg", return_value=True):
                    result = generate_snapshot(
                        "ev-002", clip_path,
                        pre_seconds=5.0, output_dir=tmpdir,
                    )

        assert result["snapshot_status"] == "ready"
        assert result["snapshot_offset_seconds"] == 1.0  # duration/2
        assert result["snapshot_fallback_reason"] is not None
        assert "clip_duration_shorter_than_pre_seconds" in result["snapshot_fallback_reason"]


# ===========================================================================
# generate_snapshot failure paths
# ===========================================================================


def test_snapshot_fails_when_clip_missing():
    """Missing clip_path -> snapshot_status=failed, clip_status unchanged."""
    result = generate_snapshot(
        "ev-003", "/nonexistent/clip.mkv",
        pre_seconds=5.0, output_dir="/tmp",
    )
    assert result["snapshot_status"] == "failed"
    assert result["snapshot_path"] is None
    assert "not found" in result["error_message"]


def test_snapshot_fails_when_ffmpeg_missing():
    """ffmpeg not available -> snapshot_status=failed with clear error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clip_path = os.path.join(tmpdir, "test.mkv")
        with open(clip_path, "wb") as f:
            f.write(b"dummy")

        with patch("app.snapshot._get_ffmpeg", side_effect=ImportError("no ffmpeg")):
            result = generate_snapshot(
                "ev-004", clip_path,
                pre_seconds=5.0, output_dir=tmpdir,
            )

    assert result["snapshot_status"] == "failed"
    assert "ffmpeg not available" in result["error_message"]


def test_snapshot_fails_when_ffmpeg_extraction_fails():
    """ffmpeg extraction failure -> snapshot_status=failed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clip_path = os.path.join(tmpdir, "test.mkv")
        with open(clip_path, "wb") as f:
            f.write(b"dummy")

        with patch("app.snapshot._get_ffmpeg", return_value=True):
            with patch("app.snapshot._ffmpeg_duration", return_value=60.0):
                with patch("app.snapshot._ffmpeg_extract", return_value=False):
                    result = generate_snapshot(
                        "ev-005", clip_path,
                        pre_seconds=5.0, output_dir=tmpdir,
                    )

    assert result["snapshot_status"] == "failed"
    assert result["error_message"] is not None


# ===========================================================================
# _snapshot_needed query filtering
# ===========================================================================


def test_snapshot_needed_returns_clip_ready_not_snapped():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("ev-a", "/media/clip.mkv", 5.0, True, "not_implemented"),
    ]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    rows = _snapshot_needed(mock_conn)
    assert len(rows) == 1
    assert rows[0]["event_id"] == "ev-a"


def test_snapshot_needed_skips_already_ready():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("ev-b", "/media/clip.mkv", 5.0, True, "ready"),
    ]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    rows = _snapshot_needed(mock_conn)
    assert len(rows) == 0


def test_snapshot_needed_skips_not_required():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("ev-c", "/media/clip.mkv", 5.0, False, "not_implemented"),
    ]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    rows = _snapshot_needed(mock_conn)
    assert len(rows) == 0


def test_snapshot_needed_skips_not_required_status():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("ev-c2", "/media/clip.mkv", 5.0, True, "not_required"),
    ]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    rows = _snapshot_needed(mock_conn)
    assert len(rows) == 0


# ===========================================================================
# _update_snapshot_status
# ===========================================================================


def test_update_snapshot_status_success():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    ok = _update_snapshot_status(
        mock_conn, "ev-001",
        snapshot_path="/media/snapshots/ev-001.jpg",
        snapshot_status="ready",
        snapshot_offset_seconds=5.0,
    )
    assert ok is True


def test_update_snapshot_status_with_error():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    ok = _update_snapshot_status(
        mock_conn, "ev-002",
        snapshot_path=None,
        snapshot_status="failed",
        error_message="ffmpeg crashed",
    )
    assert ok is True


# ===========================================================================
# _process_pending_snapshots
# ===========================================================================


def test_process_pending_snapshots_generates():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    # First call: _snapshot_needed query
    # Second call: _update_snapshot_status
    mock_cursor.fetchall.return_value = [
        ("ev-x", "/media/clip.mkv", 5.0, True, "not_implemented"),
    ]
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("app.worker.generate_snapshot") as mock_gen:
        mock_gen.return_value = {
            "snapshot_path": "/media/snapshots/ev-x.jpg",
            "snapshot_status": "ready",
            "snapshot_offset_seconds": 5.0,
        }

        with tempfile.TemporaryDirectory() as snap_dir:
            updated = _process_pending_snapshots(
                mock_conn, snap_dir, default_pre_seconds=5.0,
            )

    assert updated >= 1
    mock_gen.assert_called_once()
    call_kwargs = mock_gen.call_args[1]
    assert call_kwargs["pre_seconds"] == 5.0


def test_process_pending_snapshots_idempotent_existing_file():
    """If snapshot file already exists, it should be promoted to ready without re-extraction."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("ev-y", "/media/clip.mkv", 5.0, True, "not_implemented"),
    ]
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with tempfile.TemporaryDirectory() as snap_dir:
        # Pre-create snapshot file
        snap_path = os.path.join(snap_dir, "ev-y.jpg")
        with open(snap_path, "wb") as f:
            f.write(b"existing snapshot")

        with patch("app.worker.generate_snapshot") as mock_gen:
            updated = _process_pending_snapshots(
                mock_conn, snap_dir, default_pre_seconds=5.0,
            )

    assert updated >= 1
    mock_gen.assert_not_called()  # no re-extraction


# ===========================================================================
# API snapshot_url exposure
# ===========================================================================


# ===========================================================================
# Clip status unchanged on snapshot failure
# ===========================================================================


def test_snapshot_failure_does_not_change_clip_status():
    """When snapshot extraction fails, clip_status should remain 'ready'."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("ev-z", "/media/clip.mkv", 5.0, True, "not_implemented"),
    ]
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("app.worker.generate_snapshot") as mock_gen:
        mock_gen.return_value = {
            "snapshot_path": None,
            "snapshot_status": "failed",
            "snapshot_offset_seconds": None,
            "error_message": "ffmpeg not found",
        }

        with tempfile.TemporaryDirectory() as snap_dir:
            _process_pending_snapshots(
                mock_conn, snap_dir, default_pre_seconds=5.0,
            )

    # _update_snapshot_status and _mark_not_required UPDATEs must not
    # SET clip_status.  (WHERE clauses may legitimately reference it.)
    calls = mock_cursor.execute.call_args_list
    for call_args in calls:
        sql = call_args[0][0]
        if sql.strip().upper().startswith("UPDATE"):
            set_part = sql.upper().split("WHERE")[0]
            assert "CLIP_STATUS" not in set_part


# ===========================================================================
# _ffprobe_duration edge cases
# ===========================================================================


def test_ffmpeg_duration_missing_binary():
    with patch("app.snapshot._get_ffmpeg", return_value="/fake/ffmpeg"):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _ffmpeg_duration("/some/clip.mkv")
            assert result is None
