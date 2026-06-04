"""C1I.2i face overlay frame-domain time alignment."""

from __future__ import annotations

import importlib
import json
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_DIR = str(ROOT / "services" / "media-worker")
FACE_WORKER_DIR = str(ROOT / "services" / "face-worker")
FACE_OBS_EXPORTER = (
    ROOT
    / "modules"
    / "savant_security"
    / "custom"
    / "pyfuncs"
    / "face_observation_exporter.py"
)
FACE_REPOSITORY = ROOT / "services" / "face-worker" / "app" / "repository.py"
SMOKE = (
    ROOT
    / "scripts"
    / "smoke"
    / "current"
    / "check_c1i2i_face_frame_domain_alignment.sh"
)


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _activate_media_worker() -> Any:
    _clear_app_modules()
    if MEDIA_WORKER_DIR in sys.path:
        sys.path.remove(MEDIA_WORKER_DIR)
    sys.path.insert(0, MEDIA_WORKER_DIR)
    return importlib.import_module("app.continuous_annotation")


def _activate_face_worker_module(module_name: str) -> Any:
    _clear_app_modules()
    if FACE_WORKER_DIR in sys.path:
        sys.path.remove(FACE_WORKER_DIR)
    sys.path.insert(0, FACE_WORKER_DIR)
    return importlib.import_module(module_name)


def _event_context(**overrides: Any) -> dict[str, Any]:
    event = {
        "event_id": "evt-c1i2i",
        "event_type": "watchlist_hit",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "track_id": "7",
        "event_ts_ms": 90_000,
        "created_at": "2026-06-04T03:11:20+00:00",
        "severity": "high",
        "payload": {
            "match": {
                "source_observation_id": "face:fresh",
                "similarity": 0.91,
                "threshold": 0.5,
            },
            "matched_person": {"person_id": "person-1", "name": "Reese"},
            "media": {"pre_seconds": 5, "post_seconds": 5},
        },
        "evidence_policy": {"pre_seconds": 5, "post_seconds": 5},
    }
    event.update(overrides)
    return event


def _face_row(
    *,
    source_observation_id: str = "face:fresh",
    source_id: str = "c1e_rtsp_replay",
    camera_id: str = "cam_c1e_rtsp_replay",
    track_id: str = "7",
    timestamp_ms: int = 90_000,
    frame_num: int = 42,
    frame_pts: int = 123_000_000,
    face_bbox: Any | None = None,
    created_at: str = "2026-06-04T03:11:20+00:00",
) -> dict[str, Any]:
    return {
        "id": source_observation_id,
        "source_observation_id": source_observation_id,
        "source_id": source_id,
        "camera_id": camera_id,
        "track_id": track_id,
        "timestamp_ms": timestamp_ms,
        "frame_num": frame_num,
        "face_bbox": face_bbox
        if face_bbox is not None
        else {"format": "cxcywh", "values": [140, 140, 80, 80]},
        "landmarks": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "face_confidence": 0.88,
        "quality": 0.91,
        "detector_model": "yolov8_face",
        "embedding_model": "adaface",
        "embedding_dim": 512,
        "embedding_norm": 1.0,
        "payload": {
            "media": {
                "frame_num": frame_num,
                "frame_pts": frame_pts,
                "ntp_timestamp": "2026-06-04T03:11:20.000000Z",
            }
        },
        "created_at": created_at,
    }


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


class _FilteringCursor:
    def __init__(self, conn: "_FilteringConn") -> None:
        self.conn = conn
        self.rows: list[dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        self.conn.calls.append((sql, params))
        if "FROM match_results" in sql or "FROM person_bbox_observations" in sql:
            self.rows = []
            return
        if "FROM face_observations" not in sql:
            self.rows = []
            return
        rows = list(self.conn.rows)
        if "source_id = %(source_id)s" in sql:
            rows = [row for row in rows if row.get("source_id") == params["source_id"]]
        if "camera_id = %(camera_id)s" in sql:
            rows = [row for row in rows if row.get("camera_id") == params["camera_id"]]
        if "timestamp_ms BETWEEN" in sql:
            rows = [
                row
                for row in rows
                if params["start_ts_ms"]
                <= int(row.get("timestamp_ms") or 0)
                <= params["end_ts_ms"]
            ]
        if "created_at BETWEEN" in sql:
            start = _parse_dt(params["created_at_from"])
            end = _parse_dt(params["created_at_to"])
            rows = [
                row
                for row in rows
                if start <= _parse_dt(row.get("created_at")) <= end
            ]
        self.rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _FilteringConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def cursor(self, *args: Any, **kwargs: Any) -> _FilteringCursor:
        return _FilteringCursor(self)


def _write_sink_metadata(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                json.dumps({"frame_num": 40, "pts": 120_000_000}),
                json.dumps({"frame_num": 41, "pts": 121_000_000}),
                json.dumps({"frame_num": 42, "pts": 123_000_000}),
                json.dumps({"frame_num": 43, "pts": 124_000_000}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_build_sink_frame_index_uses_sink_metadata_frame_num_offsets(tmp_path) -> None:
    annotations = _activate_media_worker()
    metadata = tmp_path / "sink_metadata.jsonl"
    _write_sink_metadata(metadata)

    first_pts, frame_index = annotations._build_sink_frame_index(metadata)

    assert first_pts == 120_000_000
    assert frame_index == {40: 0, 41: 1, 42: 3, 43: 4}


def test_line_base_uses_frame_num_match_before_timestamp_estimate() -> None:
    annotations = _activate_media_worker()

    line = annotations._line_base(
        observation=_face_row(timestamp_ms=90_000, frame_num=42),
        start_ts_ms=85_000,
        time_alignment_status="estimated",
        sink_first_pts=120_000_000,
        sink_frame_offset_index={42: 3},
    )

    assert line["time_offset_ms"] == 3
    assert line["time_offset_ms"] != 5_000
    assert line["time_basis"] == "frame_num"
    assert line["time_alignment_status"] == "exact_frame_num"
    assert line["frame_num"] == 42
    assert line["frame_pts"] == 123_000_000
    assert line["ntp_timestamp"] == "2026-06-04T03:11:20.000000Z"


def test_line_base_uses_frame_pts_fallback_when_frame_num_missing() -> None:
    annotations = _activate_media_worker()

    line = annotations._line_base(
        observation=_face_row(timestamp_ms=90_000, frame_num=52, frame_pts=133_000_000),
        start_ts_ms=85_000,
        time_alignment_status="estimated",
        sink_first_pts=120_000_000,
        sink_frame_offset_index={42: 3},
    )

    assert line["time_offset_ms"] == 13
    assert line["time_basis"] == "frame_pts"
    assert line["time_alignment_status"] == "exact_frame_pts"


def test_line_base_only_uses_timestamp_estimated_when_no_frame_anchor() -> None:
    annotations = _activate_media_worker()

    line = annotations._line_base(
        observation=_face_row(timestamp_ms=90_000, frame_num=52, frame_pts=133_000_000),
        start_ts_ms=85_000,
        time_alignment_status="estimated",
        sink_first_pts=None,
        sink_frame_offset_index={},
    )

    assert line["time_offset_ms"] == 5_000
    assert line["time_basis"] == "timestamp_estimated"
    assert line["time_alignment_status"] == "estimated"


def test_estimated_bundle_keeps_frame_anchored_face_offsets() -> None:
    annotations = _activate_media_worker()
    summary = {
        "clip_start_ts_ms": 85_000,
        "clip_end_ts_ms": 95_000,
        "time_anchor": {
            "actual_clip_duration_ms": 10_000,
            "time_alignment_status": "estimated",
        },
    }
    lines = [
        {
            "time_offset_ms": 3,
            "time_basis": "frame_num",
            "time_alignment_status": "exact_frame_num",
            "frame_num": 42,
            "frame_pts": 123_000_000,
            "objects": [{"object_type": "face"}],
        }
    ]

    records = annotations._prepare_annotation_records(lines, summary)

    assert records[0]["time_offset_ms"] == 3
    assert records[0]["face_overlay_time_alignment_status"] == "aligned_frame_based"
    assert records[0]["face_overlay_time_basis"] == "frame_num"
    assert summary["face_overlay_time_alignment_status"] == "aligned_frame_based"
    assert summary["estimated_face_time_offset_disabled_count"] == 0
    assert summary["frame_anchored_face_count"] == 1


def test_timestamp_estimated_face_line_is_marked_unreliable() -> None:
    annotations = _activate_media_worker()
    summary = {
        "clip_start_ts_ms": 85_000,
        "clip_end_ts_ms": 95_000,
        "time_anchor": {
            "actual_clip_duration_ms": 10_000,
            "time_alignment_status": "estimated",
        },
    }
    lines = [
        {
            "time_offset_ms": 5_000,
            "time_basis": "timestamp_estimated",
            "time_alignment_status": "estimated",
            "frame_pts": 123_000_000,
            "objects": [{"object_type": "face"}],
        }
    ]

    records = annotations._prepare_annotation_records(lines, summary)

    assert records[0]["time_offset_ms"] is None
    assert records[0]["estimated_time_offset_ms"] == 5_000
    assert records[0]["face_overlay_time_alignment_status"] == "estimated_unreliable"
    assert summary["face_overlay_time_alignment_status"] == "estimated_unreliable"
    assert summary["estimated_face_time_offset_disabled_count"] == 1
    assert summary["face_frame_alignment"]["face_timestamp_estimated_count"] == 1
    assert summary["face_timestamp_estimated_reason"]


def test_build_summary_contains_face_frame_alignment(tmp_path) -> None:
    annotations = _activate_media_worker()
    metadata = tmp_path / "sink_metadata.jsonl"
    _write_sink_metadata(metadata)

    lines, summary = annotations.build_continuous_annotations(
        _FilteringConn([_face_row()]),
        _event_context(),
        replay_metadata_path=str(metadata),
    )

    alignment = summary["face_frame_alignment"]
    assert lines[0]["time_basis"] == "frame_num"
    assert alignment["sink_first_pts_present"] is True
    assert alignment["sink_frame_index_count"] == 4
    assert alignment["face_frame_num_present_count"] == 1
    assert alignment["face_frame_num_matched_count"] == 1
    assert alignment["face_frame_pts_present_count"] == 1
    assert alignment["face_timestamp_estimated_count"] == 0
    assert alignment["frame_anchored_face_count"] == 1


def test_known_face_frame_offset_is_inside_raw_clip_duration(tmp_path) -> None:
    annotations = _activate_media_worker()
    metadata = tmp_path / "sink_metadata.jsonl"
    _write_sink_metadata(metadata)

    lines, summary = annotations.build_continuous_annotations(
        _FilteringConn([_face_row()]),
        _event_context(),
        replay_metadata_path=str(metadata),
    )
    records = annotations._prepare_annotation_records(lines, summary)
    duration_ms = summary["time_anchor"]["actual_clip_duration_ms"]
    known_offsets = [
        int(line["time_offset_ms"])
        for line in records
        for obj in line.get("objects", [])
        if obj.get("object_type") == "face"
        and obj.get("identity", {}).get("status") == "matched"
    ]

    assert known_offsets
    assert all(0 <= offset <= duration_ms for offset in known_offsets)


def test_face_query_created_at_window_does_not_regress(tmp_path) -> None:
    annotations = _activate_media_worker()
    metadata = tmp_path / "sink_metadata.jsonl"
    _write_sink_metadata(metadata)
    conn = _FilteringConn([_face_row(timestamp_ms=50_000)])

    lines, summary = annotations.build_continuous_annotations(
        conn,
        _event_context(),
        replay_metadata_path=str(metadata),
    )

    assert lines
    assert summary["face_observation_query"]["mode"] == "created_at_fallback"
    assert summary["face_observation_query"]["timestamp_only_fallback_used"] is False
    executed_sql = "\n".join(sql for sql, _params in conn.calls)
    assert "created_at BETWEEN" in executed_sql


def test_face_bbox_format_and_track_semantics_do_not_regress() -> None:
    assert '"format": "cxcywh"' in FACE_OBS_EXPORTER.read_text(encoding="utf-8")
    repository = _activate_face_worker_module("app.repository")
    normalized = repository._normalize_face_bbox([10, 20, 30, 40])
    assert normalized == {
        "format": "cxcywh",
        "values": [10.0, 20.0, 30.0, 40.0],
        "coordinate_space": "pixel",
    }

    annotations = _activate_media_worker()
    lines, summary = annotations.build_continuous_annotations(
        _FilteringConn([_face_row(face_bbox=[140, 140, 80, 80])]),
        _event_context(),
    )
    obj = next(obj for line in lines for obj in line.get("objects", []))
    assert obj["bbox"]["format"] == "cxcywh"
    assert isinstance(obj["bbox"]["xyxy"], list)
    assert obj["track_id"] == "7"
    assert obj["person_track_id"] == "7"
    assert obj["face_track_id"] is None
    assert obj["track_id_semantics"] == "person_track_id"
    assert summary["face_bbox_format"]["legacy_list_count"] == 1
    assert summary["face_track_semantics"]["track_id_semantics"] == "person_track_id"


def test_c1i2i_smoke_contract_exists() -> None:
    assert SMOKE.exists()
    text = SMOKE.read_text(encoding="utf-8")
    assert "face_frame_domain_alignment_summary.json" in text
    assert "PASS_C1I2I_FACE_FRAME_DOMAIN_ALIGNMENT" in text
    assert "PARTIAL_C1I2I_FRAME_PTS_FALLBACK_ALIGNMENT" in text
    assert "FAIL_C1I2I_FACE_STILL_TIMESTAMP_ESTIMATED" in text
