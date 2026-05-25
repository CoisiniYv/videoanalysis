"""Tests for Phase 3E.1 — Annotated Snapshot with bbox trust guard."""

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
    _metadata_has_detections,
    _process_pending_annotations,
    _update_annotation_status,
)


def _make_test_jpeg(filepath: str, size: tuple = (320, 240)) -> None:
    from PIL import Image
    img = Image.new("RGB", size, color=(60, 60, 80))
    img.save(filepath, "JPEG")


def _make_metadata_json(dirpath: str, objects_frames: int = 0) -> str:
    """Create a metadata.json with *objects_frames* non-empty objects frames."""
    meta_path = os.path.join(dirpath, "metadata.json")
    with open(meta_path, "w") as f:
        for i in range(10):
            if i < objects_frames:
                f.write('{"frame_num":%d,"metadata":{"objects":[{"bbox":{"x":100,"y":50,"w":80,"h":120}}]}}\n' % i)
            else:
                f.write('{"frame_num":%d,"metadata":{"objects":[]}}\n' % i)
    return meta_path


# ===========================================================================
# generate_annotated_snapshot — bbox trust guard
# ===========================================================================


def test_bbox_trusted_drawn():
    """Trusted bbox is drawn, bbox_overlay_status=ready."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "intrusion",
            "camera_id": "cam_01",
            "track_id": "t_001",
            "confidence": 0.92,
            "event_ts_ms": 1717000000000,
            "bbox": {"x": 100, "y": 50, "width": 200, "height": 150},
            "zone_id": "full_frame",
        }

        result = generate_annotated_snapshot(
            "ev-001", raw_path, payload, ann_dir, bbox_trusted=True,
        )

        assert result["annotated_snapshot_status"] == "ready"
        assert result["bbox_overlay_status"] == "ready"
        assert result["zone_overlay_status"] == "skipped_missing_polygon"
        assert os.path.isfile(result["annotated_snapshot_path"])


def test_bbox_untrusted_skipped():
    """Untrusted bbox is NOT drawn, bbox_overlay_status=skipped_untrusted_bbox."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "intrusion",
            "camera_id": "cam_01",
            "track_id": "t_002",
            "confidence": 0.92,
            "event_ts_ms": 1717000000000,
            "bbox": {"x": 100, "y": 50, "width": 200, "height": 150},
            "zone_id": "full_frame",
        }

        result = generate_annotated_snapshot(
            "ev-002", raw_path, payload, ann_dir, bbox_trusted=False,
        )

        assert result["annotated_snapshot_status"] == "ready"
        assert result["bbox_overlay_status"] == "skipped_untrusted_bbox"
        assert os.path.isfile(result["annotated_snapshot_path"])


def test_bbox_missing():
    """No bbox in payload → bbox_overlay_status=skipped_missing_bbox."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "loitering",
            "camera_id": "cam_02",
            "track_id": "t_003",
            "confidence": 0.75,
            "event_ts_ms": 1717000001000,
        }

        result = generate_annotated_snapshot(
            "ev-003", raw_path, payload, ann_dir,
        )

        assert result["annotated_snapshot_status"] == "ready"
        assert result["bbox_overlay_status"] == "skipped_missing_bbox"
        assert os.path.isfile(result["annotated_snapshot_path"])


def test_annotation_still_succeeds_label_only():
    """Annotation succeeds with label only, even when bbox is untrusted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "intrusion",
            "camera_id": "cam_03",
            "track_id": "t_004",
            "confidence": 0.5,
            "event_ts_ms": 1717000003000,
            "bbox": {"x": 10, "y": 10, "width": 5, "height": 5},
        }

        result = generate_annotated_snapshot(
            "ev-004", raw_path, payload, ann_dir, bbox_trusted=False,
        )

        assert result["annotated_snapshot_status"] == "ready"
        assert result["bbox_overlay_status"] == "skipped_untrusted_bbox"
        assert os.path.isfile(result["annotated_snapshot_path"])


# ===========================================================================
# generate_annotated_snapshot — bbox_source trust model (Phase 3F0.1)
# ===========================================================================


def test_bbox_source_savant_detection_trusted():
    """bbox_source=savant_detection → bbox drawn, bbox_overlay_status=ready."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "intrusion",
            "camera_id": "cam_01",
            "track_id": "t_010",
            "confidence": 0.92,
            "event_ts_ms": 1717000000000,
            "bbox": {"x": 100, "y": 50, "width": 200, "height": 150},
            "bbox_source": "savant_detection",
        }

        result = generate_annotated_snapshot(
            "ev-010", raw_path, payload, ann_dir, bbox_trusted=True,
        )

        assert result["annotated_snapshot_status"] == "ready"
        assert result["bbox_overlay_status"] == "ready"
        assert os.path.isfile(result["annotated_snapshot_path"])


def test_bbox_source_missing_untrusted():
    """No bbox_source in payload → bbox untrusted even if bbox exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "intrusion",
            "camera_id": "cam_01",
            "track_id": "t_011",
            "confidence": 0.88,
            "event_ts_ms": 1717000000000,
            "bbox": {"x": 10, "y": 10, "width": 50, "height": 50},
        }

        result = generate_annotated_snapshot(
            "ev-011", raw_path, payload, ann_dir, bbox_trusted=False,
        )

        assert result["bbox_overlay_status"] == "skipped_untrusted_bbox"


def test_bbox_source_smoke_injected_untrusted():
    """bbox_source=smoke_injected → bbox untrusted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "intrusion",
            "camera_id": "cam_01",
            "track_id": "t_012",
            "confidence": 0.75,
            "event_ts_ms": 1717000000000,
            "bbox": {"x": 10, "y": 10, "width": 50, "height": 50},
            "bbox_source": "smoke_injected",
        }

        result = generate_annotated_snapshot(
            "ev-012", raw_path, payload, ann_dir, bbox_trusted=False,
        )

        assert result["bbox_overlay_status"] == "skipped_untrusted_bbox"


# ===========================================================================
# generate_annotated_snapshot — polygon overlay (unchanged logic)
# ===========================================================================


def test_annotated_snapshot_with_zone_polygon():
    """Polygon drawn, zone_overlay_status=ok, bbox_overlay_status handled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "intrusion",
            "camera_id": "cam_01",
            "track_id": "t_005",
            "confidence": 0.88,
            "event_ts_ms": 1717000002000,
            "zone_id": "perimeter",
            "polygon": [[50, 50], [250, 50], [250, 200], [50, 200]],
        }

        result = generate_annotated_snapshot(
            "ev-005", raw_path, payload, ann_dir,
        )

        assert result["annotated_snapshot_status"] == "ready"
        assert result["zone_overlay_status"] == "ok"
        assert result["bbox_overlay_status"] == "skipped_missing_bbox"


def test_annotated_snapshot_with_zone_polygon_alt_key():
    """Polygon from zone_polygon key accepted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        payload = {
            "event_type": "intrusion",
            "camera_id": "cam_01",
            "track_id": "t_006",
            "confidence": 0.88,
            "event_ts_ms": 1717000002000,
            "zone_id": "perimeter",
            "zone_polygon": [[10, 10], [100, 10], [100, 100]],
        }

        result = generate_annotated_snapshot(
            "ev-006", raw_path, payload, ann_dir,
        )

        assert result["annotated_snapshot_status"] == "ready"
        assert result["zone_overlay_status"] == "ok"


# ===========================================================================
# generate_annotated_snapshot — failure paths
# ===========================================================================


def test_annotated_snapshot_fails_missing_raw():
    """Missing raw snapshot → annotated_snapshot_status=failed."""
    result = generate_annotated_snapshot(
        "ev-007", "/nonexistent/snap.jpg", {}, "/tmp/ann",
    )
    assert result["annotated_snapshot_status"] == "failed"
    assert result["annotated_snapshot_path"] is None
    assert "not found" in result["annotated_snapshot_error_message"]
    assert result["bbox_overlay_status"] is None


def test_annotated_snapshot_fails_corrupt_image():
    """Corrupt raw snapshot → annotated_snapshot_status=failed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "corrupt.jpg")
        with open(raw_path, "wb") as f:
            f.write(b"not a jpeg image")
        ann_dir = os.path.join(tmpdir, "annotated")

        result = generate_annotated_snapshot(
            "ev-008", raw_path, {}, ann_dir,
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
        ("ev-a", "/media/snapshots/ev-a.jpg", payload, "/media/clip/video.mov"),
    ]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    rows = _annotation_needed(mock_conn)
    assert len(rows) == 1
    assert rows[0]["event_id"] == "ev-a"
    assert rows[0]["clip_path"] == "/media/clip/video.mov"


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
        ("ev-b", "/media/snapshots/ev-b.jpg", payload, None),
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
# _update_annotation_status — with bbox_overlay_status
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
        bbox_overlay_status="ready",
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

    calls = mock_cursor.execute.call_args_list
    sql = calls[0][0][0]
    assert "annotated_snapshot_status" in sql
    assert "annotated_snapshot_error_message" in sql


def test_update_annotation_status_with_bbox_overlay():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    ok = _update_annotation_status(
        mock_conn, "ev-003",
        annotated_snapshot_path="/media/ann/ev-003.jpg",
        annotated_snapshot_status="ready",
        bbox_overlay_status="skipped_untrusted_bbox",
    )
    assert ok is True

    calls = mock_cursor.execute.call_args_list
    sql = calls[0][0][0]
    assert "{media,bbox_overlay_status}" in sql


# ===========================================================================
# _process_pending_annotations
# ===========================================================================


def test_process_pending_annotations_generates():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    payload = {"event_type": "intrusion", "camera_id": "cam_01"}
    mock_cursor.fetchall.return_value = [
        ("ev-x", "/media/snapshots/ev-x.jpg", payload, "/media/clip/video.mov"),
    ]
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with tempfile.TemporaryDirectory() as tmpdir:
        clip_dir = os.path.join(tmpdir, "clip")
        os.makedirs(clip_dir)
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)

        ann_dir = os.path.join(tmpdir, "annotated")

        with patch("app.worker._annotation_needed") as mock_needed:
            mock_needed.return_value = [
                {
                    "event_id": "ev-x",
                    "snapshot_path": raw_path,
                    "payload": payload,
                    "clip_path": os.path.join(clip_dir, "video.mov"),
                },
            ]

            updated = _process_pending_annotations(mock_conn, ann_dir)

    assert updated >= 1


def test_process_pending_annotations_with_trusted_bbox_source():
    """When payload.bbox_source=savant_detection, bbox is trusted."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    payload = {
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "track_id": "t_010",
        "confidence": 0.92,
        "event_ts_ms": 1717000000000,
        "bbox": {"x": 100, "y": 50, "width": 200, "height": 150},
        "bbox_source": "savant_detection",
    }
    mock_cursor.fetchall.return_value = [
        ("ev-trusted", "/media/snapshots/ev-trusted.jpg", payload, "/media/clip/video.mov"),
    ]
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.jpg")
        _make_test_jpeg(raw_path)
        ann_dir = os.path.join(tmpdir, "annotated")

        with patch("app.worker._annotation_needed") as mock_needed:
            mock_needed.return_value = [
                {
                    "event_id": "ev-trusted",
                    "snapshot_path": raw_path,
                    "payload": payload,
                    "clip_path": None,
                },
            ]

            updated = _process_pending_annotations(mock_conn, ann_dir)

    assert updated >= 1
    # Verify bbox_overlay_status was written as ready
    calls = mock_cursor.execute.call_args_list
    found = False
    for call_args in calls:
        sql = call_args[0][0]
        if "bbox_overlay_status" in sql and "ready" in str(call_args[0][1]):
            found = True
    assert found, "bbox_overlay_status should be 'ready' for trusted bbox_source"


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

        ann_path = os.path.join(ann_dir, "ev-y.jpg")
        _make_test_jpeg(ann_path)

        with patch("app.worker._annotation_needed") as mock_needed:
            mock_needed.return_value = [
                {
                    "event_id": "ev-y",
                    "snapshot_path": raw_path,
                    "payload": {},
                    "clip_path": None,
                },
            ]

            with patch("app.worker.generate_annotated_snapshot") as mock_gen:
                updated = _process_pending_annotations(mock_conn, ann_dir)

    assert updated >= 1
    mock_gen.assert_not_called()


# ===========================================================================
# _metadata_has_detections
# ===========================================================================


def test_metadata_has_detections_true():
    """Returns True when metadata.json has non-empty objects."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clip_path = os.path.join(tmpdir, "video.mov")
        _make_metadata_json(tmpdir, objects_frames=1)
        assert _metadata_has_detections(clip_path) is True


def test_metadata_has_detections_false_when_all_empty():
    """Returns False when all frames have empty objects."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clip_path = os.path.join(tmpdir, "video.mov")
        _make_metadata_json(tmpdir, objects_frames=0)
        assert _metadata_has_detections(clip_path) is False


def test_metadata_has_detections_false_when_missing():
    """Returns False when metadata.json doesn't exist."""
    assert _metadata_has_detections("/nonexistent/clip/video.mov") is False


def test_metadata_has_detections_false_when_none():
    """Returns False when clip_path is None."""
    assert _metadata_has_detections(None) is False


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
                    "clip_path": None,
                },
            ]

            _process_pending_annotations(mock_conn, ann_dir)

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
# _draw_bbox / _draw_polygon edge cases
# ===========================================================================


def test_draw_bbox_skips_zero_size():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (100, 100), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    _draw_bbox(draw, {"x": 10, "y": 10, "width": 0, "height": 0})


def test_draw_polygon_skips_under_3_points():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (100, 100), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    _draw_polygon(draw, [(0, 0), (10, 10)])
