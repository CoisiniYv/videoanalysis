"""Tests for Phase 3E — Annotated Snapshot MVP."""

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

from app.annotated_snapshot import (
    generate_annotated_snapshot,
    _draw_bbox,
    _draw_polygon,
    _draw_label_block,
    _extract_polygon,
)
from app.worker import (
    _annotation_needed,
    _process_pending_annotations,
    _update_annotation_status,
)


def _make_test_jpeg(filepath: str, size: tuple = (320, 240)) -> None:
    """Create a small solid-color JPEG for testing."""
    from PIL import Image
    img = Image.new("RGB", size, color=(60, 60, 80))
    img.save(filepath, "JPEG")


# ===========================================================================
# generate_annotated_snapshot — success paths
# ===========================================================================


def test_annotated_snapshot_generates_with_bbox():
    """Annotated snapshot draws bbox and label on raw snapshot."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "intrusion",
            "camera_id": "cam_01",
            "source_id": "phase3b",
            "track_id": "t_001",
            "confidence": 0.92,
            "event_ts_ms": 1717000000000,
            "bbox": {"x": 100, "y": 50, "width": 200, "height": 150},
            "zone_id": "full_frame",
        }

        result = generate_annotated_snapshot(
            "ev-001", raw_path, payload, ann_dir,
        )

        assert result["annotated_snapshot_status"] == "ready"
        assert result["annotated_snapshot_path"] is not None
        assert os.path.isfile(result["annotated_snapshot_path"])
        assert result["zone_overlay_status"] == "skipped_missing_polygon"


def test_annotated_snapshot_generates_without_bbox():
    """Annotated snapshot works when payload has no bbox (label only)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "loitering",
            "camera_id": "cam_02",
            "track_id": "t_002",
            "confidence": 0.75,
            "event_ts_ms": 1717000001000,
        }

        result = generate_annotated_snapshot(
            "ev-002", raw_path, payload, ann_dir,
        )

        assert result["annotated_snapshot_status"] == "ready"
        assert os.path.isfile(result["annotated_snapshot_path"])
        assert "zone_overlay_status" not in result


def test_annotated_snapshot_with_zone_polygon():
    """When polygon is present in payload, it is drawn and zone_overlay_status=ok."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "intrusion",
            "camera_id": "cam_01",
            "track_id": "t_003",
            "confidence": 0.88,
            "event_ts_ms": 1717000002000,
            "zone_id": "perimeter",
            "polygon": [[50, 50], [250, 50], [250, 200], [50, 200]],
        }

        result = generate_annotated_snapshot(
            "ev-003", raw_path, payload, ann_dir,
        )

        assert result["annotated_snapshot_status"] == "ready"
        assert result["zone_overlay_status"] == "ok"


def test_annotated_snapshot_with_zone_polygon_alt_key():
    """Polygon from zone_polygon key is also accepted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "intrusion",
            "camera_id": "cam_01",
            "track_id": "t_004",
            "confidence": 0.88,
            "event_ts_ms": 1717000002000,
            "zone_id": "perimeter",
            "zone_polygon": [[10, 10], [100, 10], [100, 100]],
        }

        result = generate_annotated_snapshot(
            "ev-004", raw_path, payload, ann_dir,
        )

        assert result["annotated_snapshot_status"] == "ready"
        assert result["zone_overlay_status"] == "ok"


# ===========================================================================
# generate_annotated_snapshot — failure paths
# ===========================================================================


def test_annotated_snapshot_fails_missing_raw():
    """Missing raw snapshot → annotated_snapshot_status=failed."""
    result = generate_annotated_snapshot(
        "ev-005", "/nonexistent/snap.jpg", {}, "/tmp/ann",
    )
    assert result["annotated_snapshot_status"] == "failed"
    assert result["annotated_snapshot_path"] is None
    assert "not found" in result["annotated_snapshot_error_message"]


def test_annotated_snapshot_fails_corrupt_image():
    """Corrupt raw snapshot → annotated_snapshot_status=failed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "corrupt.jpg")
        with open(raw_path, "wb") as f:
            f.write(b"not a jpeg image")
        ann_dir = os.path.join(tmpdir, "annotated")

        result = generate_annotated_snapshot(
            "ev-006", raw_path, {}, ann_dir,
        )

        assert result["annotated_snapshot_status"] == "failed"
        assert "failed to open" in result["annotated_snapshot_error_message"]


# ===========================================================================
# _annotation_needed query filtering
# ===========================================================================


def test_annotation_needed_returns_snapshot_ready_not_annotated():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    payload = json.dumps({
        "media": {
            "snapshot_status": "ready",
            "clip_status": "ready",
        },
    })
    mock_cursor.fetchall.return_value = [
        ("ev-a", "/media/snapshots/ev-a.jpg", payload),
    ]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    rows = _annotation_needed(mock_conn)
    assert len(rows) == 1
    assert rows[0]["event_id"] == "ev-a"
    assert rows[0]["snapshot_path"] == "/media/snapshots/ev-a.jpg"


def test_annotation_needed_skips_already_annotated():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    payload = json.dumps({
        "media": {
            "snapshot_status": "ready",
            "annotated_snapshot_status": "ready",
        },
    })
    mock_cursor.fetchall.return_value = [
        ("ev-b", "/media/snapshots/ev-b.jpg", payload),
    ]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    rows = _annotation_needed(mock_conn)
    assert len(rows) == 0


def test_annotation_needed_skips_no_snapshot():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    rows = _annotation_needed(mock_conn)
    assert len(rows) == 0


# ===========================================================================
# _update_annotation_status
# ===========================================================================


def test_update_annotation_status_success():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    ok = _update_annotation_status(
        mock_conn, "ev-001",
        annotated_snapshot_path="/media/snapshots/annotated/ev-001.jpg",
        annotated_snapshot_status="ready",
        zone_overlay_status="ok",
    )
    assert ok is True


def test_update_annotation_status_with_error():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    ok = _update_annotation_status(
        mock_conn, "ev-002",
        annotated_snapshot_path=None,
        annotated_snapshot_status="failed",
        error_message="PIL decode error",
    )
    assert ok is True

    # Verify error_message and status are in the SQL
    calls = mock_cursor.execute.call_args_list
    sql = calls[0][0][0]
    assert "annotated_snapshot_status" in sql
    assert "annotated_snapshot_error_message" in sql


# ===========================================================================
# _process_pending_annotations
# ===========================================================================


def test_process_pending_annotations_generates():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    payload = {"event_type": "intrusion", "camera_id": "cam_01"}
    mock_cursor.fetchall.return_value = [
        ("ev-x", "/media/snapshots/ev-x.jpg", payload),
    ]
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)

        # Patch _annotation_needed to return our test data
        with patch("app.worker._annotation_needed") as mock_needed:
            mock_needed.return_value = [
                {
                    "event_id": "ev-x",
                    "snapshot_path": raw_path,
                    "payload": payload,
                },
            ]

            ann_dir = os.path.join(tmpdir, "annotated")
            updated = _process_pending_annotations(mock_conn, ann_dir)

    assert updated >= 1


def test_process_pending_annotations_idempotent_existing_file():
    """If annotated file already exists, promote to ready without re-generation."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")
        os.makedirs(ann_dir, exist_ok=True)

        # Pre-create annotated snapshot file
        ann_path = os.path.join(ann_dir, "ev-y.jpg")
        _make_test_jpeg(ann_path)

        with patch("app.worker._annotation_needed") as mock_needed:
            mock_needed.return_value = [
                {
                    "event_id": "ev-y",
                    "snapshot_path": raw_path,
                    "payload": {},
                },
            ]

            with patch("app.worker.generate_annotated_snapshot") as mock_gen:
                updated = _process_pending_annotations(mock_conn, ann_dir)

    assert updated >= 1
    mock_gen.assert_not_called()  # no re-generation


# ===========================================================================
# snapshot_status unchanged on annotation failure
# ===========================================================================


def test_annotation_failure_does_not_change_snapshot_status():
    """When annotation fails, snapshot_status must not be modified."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with tempfile.TemporaryDirectory() as tmpdir:
        ann_dir = os.path.join(tmpdir, "annotated")

        with patch("app.worker._annotation_needed") as mock_needed:
            mock_needed.return_value = [
                {
                    "event_id": "ev-z",
                    "snapshot_path": "/nonexistent/raw.jpg",
                    "payload": {},
                },
            ]

            _process_pending_annotations(mock_conn, ann_dir)

    # Check that UPDATE SQL does not touch {media,snapshot_status}
    calls = mock_cursor.execute.call_args_list
    for call_args in calls:
        sql = call_args[0][0]
        if sql.strip().upper().startswith("UPDATE"):
            assert "{media,snapshot_status}" not in sql


# ===========================================================================
# _extract_polygon edge cases
# ===========================================================================


def test_extract_polygon_none_when_missing():
    assert _extract_polygon({}) is None
    assert _extract_polygon({"zone_id": "zone1"}) is None


def test_extract_polygon_none_when_too_few_points():
    assert _extract_polygon({"polygon": [[0, 0], [10, 10]]}) is None


def test_extract_polygon_from_polygon_key():
    result = _extract_polygon({"polygon": [[0, 0], [10, 0], [10, 10]]})
    assert result == [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]


def test_extract_polygon_from_zone_polygon_key():
    result = _extract_polygon({"zone_polygon": [[1, 2], [3, 4], [5, 6]]})
    assert result == [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]


# ===========================================================================
# _draw_bbox edge cases
# ===========================================================================


def test_draw_bbox_skips_zero_size():
    """bbox with zero width/height should not crash."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (100, 100), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    _draw_bbox(draw, {"x": 10, "y": 10, "width": 0, "height": 0})
    # No exception = pass


def test_draw_polygon_skips_under_3_points():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (100, 100), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    _draw_polygon(draw, [(0, 0), (10, 10)])
    # No exception = pass
