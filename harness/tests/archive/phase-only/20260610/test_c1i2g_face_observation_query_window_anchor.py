"""C1I.2g face observation query window anchor and clip dedup tests."""

from __future__ import annotations

import importlib
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_DIR = str(ROOT / "services" / "media-worker")
SMOKE = (
    ROOT
    / "scripts"
    / "smoke"
    / "current"
    / "check_c1i2g_face_observation_query_window_anchor.sh"
)


def _activate_media_worker() -> Any:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if MEDIA_WORKER_DIR in sys.path:
        sys.path.remove(MEDIA_WORKER_DIR)
    sys.path.insert(0, MEDIA_WORKER_DIR)
    return importlib.import_module("app.continuous_annotation")


def _event_context(**overrides: Any) -> dict[str, Any]:
    event = {
        "event_id": "evt-c1i2g",
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
    created_at: str = "2026-06-04T03:11:20+00:00",
    face_confidence: float = 0.88,
) -> dict[str, Any]:
    return {
        "id": source_observation_id,
        "source_observation_id": source_observation_id,
        "source_id": source_id,
        "camera_id": camera_id,
        "track_id": track_id,
        "timestamp_ms": timestamp_ms,
        "frame_num": 42,
        "face_bbox": {"format": "xyxy", "values": [100, 100, 180, 180]},
        "landmarks": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "face_confidence": face_confidence,
        "quality": 0.91,
        "detector_model": "yolov8_face",
        "embedding_model": "adaface",
        "embedding_dim": 512,
        "embedding_norm": 1.0,
        "payload": {"media": {"frame_pts": 123456789}},
        "created_at": created_at,
    }


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


class _FilteringCursor:
    def __init__(self, conn: "_FilteringConn") -> None:
        self.conn = conn
        self.rows: list[dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql: str, params: dict[str, Any] | None = None):
        params = params or {}
        self.conn.calls.append((sql, params))
        if "FROM match_results" in sql or "FROM person_bbox_observations" in sql:
            self.rows = []
            return
        if "FROM face_observations" not in sql:
            self.rows = []
            return
        candidates = list(self.conn.rows)
        if "source_id = %(source_id)s" in sql:
            candidates = [
                row for row in candidates if row.get("source_id") == params["source_id"]
            ]
        if "camera_id = %(camera_id)s" in sql:
            candidates = [
                row for row in candidates if row.get("camera_id") == params["camera_id"]
            ]
        if "timestamp_ms BETWEEN" in sql:
            candidates = [
                row
                for row in candidates
                if params["start_ts_ms"] <= int(row.get("timestamp_ms") or 0) <= params["end_ts_ms"]
            ]
        if "created_at BETWEEN" in sql:
            created_from = _parse_dt(params["created_at_from"])
            created_to = _parse_dt(params["created_at_to"])
            candidates = [
                row
                for row in candidates
                if created_from <= _parse_dt(row.get("created_at")) <= created_to
            ]
        self.rows = candidates

    def fetchall(self):
        return self.rows


class _FilteringConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def cursor(self, *_, **__):
        return _FilteringCursor(self)


def _load_with_audit(annotations: Any, conn: _FilteringConn, **overrides: Any):
    event = _event_context(**overrides.pop("event_overrides", {}))
    return annotations._load_observations(
        conn,
        source_id=event["source_id"],
        camera_id=event["camera_id"],
        start_ts_ms=85_000,
        end_ts_ms=95_000,
        event_context=event,
        return_audit=True,
        **overrides,
    )


def test_face_query_includes_created_at_window_and_camera_id(monkeypatch) -> None:
    annotations = _activate_media_worker()
    monkeypatch.delenv("ALLOW_FACE_TIMESTAMP_ONLY_QUERY", raising=False)
    conn = _FilteringConn([])

    rows, audit = _load_with_audit(annotations, conn)

    assert rows == []
    first_sql = conn.calls[0][0]
    assert "timestamp_ms BETWEEN" in first_sql
    assert "created_at BETWEEN" in first_sql
    assert "camera_id = %(camera_id)s" in first_sql
    assert audit["created_at_margin_seconds"] == 15.0


def test_timestamp_only_query_is_not_allowed_by_default(monkeypatch) -> None:
    annotations = _activate_media_worker()
    monkeypatch.delenv("ALLOW_FACE_TIMESTAMP_ONLY_QUERY", raising=False)
    conn = _FilteringConn([])

    _load_with_audit(annotations, conn)

    timestamp_only_calls = [
        sql
        for sql, _params in conn.calls
        if "timestamp_ms BETWEEN" in sql and "created_at BETWEEN" not in sql
    ]
    assert timestamp_only_calls == []


def test_historical_same_timestamp_old_created_at_is_excluded(monkeypatch) -> None:
    annotations = _activate_media_worker()
    monkeypatch.delenv("ALLOW_FACE_TIMESTAMP_ONLY_QUERY", raising=False)
    conn = _FilteringConn(
        [
            _face_row(source_observation_id="face:old", created_at="2026-06-01T09:47:21+00:00"),
            _face_row(source_observation_id="face:fresh", created_at="2026-06-04T03:11:20+00:00"),
        ]
    )

    rows, audit = _load_with_audit(annotations, conn)

    assert [row["source_observation_id"] for row in rows] == ["face:fresh"]
    assert audit["mode"] == "timestamp_and_created_at"
    assert audit["db_result_count"] == 1
    assert audit["cross_day_result_count"] == 0


def test_created_at_fallback_is_explicitly_marked(monkeypatch) -> None:
    annotations = _activate_media_worker()
    monkeypatch.delenv("ALLOW_FACE_TIMESTAMP_ONLY_QUERY", raising=False)
    conn = _FilteringConn(
        [
            _face_row(
                source_observation_id="face:created-only",
                timestamp_ms=120_000,
                created_at="2026-06-04T03:11:22+00:00",
            )
        ]
    )

    rows, audit = _load_with_audit(annotations, conn)

    assert [row["source_observation_id"] for row in rows] == ["face:created-only"]
    assert audit["mode"] == "created_at_fallback"
    assert audit["timestamp_only_fallback_used"] is False


def _identity(
    *,
    status: str = "unknown",
    match_status: str = "not_searched",
    name: str = "",
    similarity: float | None = None,
    person_id: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "person_id": person_id,
        "external_person_id": None,
        "display_name": name,
        "similarity": similarity,
        "rank": 1 if status == "matched" else None,
        "threshold": 0.5,
        "match_status": match_status,
    }


def _face(
    *,
    track_id: str = "7",
    bbox: list[float] | None = None,
    confidence: float = 0.8,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "object_type": "face",
        "object_id": f"face:{track_id or 'none'}:{confidence}",
        "track_id": track_id,
        "bbox": {
            "format": "xyxy",
            "values": bbox or [100, 100, 180, 180],
            "confidence": confidence,
            "source": "observation.face_bbox",
        },
        "identity": identity or _identity(),
        "style": {"label": "Unknown face", "bbox_color": "#9E9E9E"},
    }


def _grouped_line(
    *,
    source_id: str = "c1e_rtsp_replay",
    camera_id: str = "cam_c1e_rtsp_replay",
    time_offset_ms: int = 100,
    obj: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source_id": source_id,
        "camera_id": camera_id,
        "timestamp_ms": 85_000 + time_offset_ms,
        "time_offset_ms": time_offset_ms,
        "objects": [obj],
    }


def test_same_track_same_second_dedup_removes_duplicate_and_preserves_known() -> None:
    annotations = _activate_media_worker()
    known = _face(
        track_id="7",
        confidence=0.45,
        identity=_identity(
            status="matched",
            match_status="above_threshold",
            name="Reese",
            similarity=0.91,
            person_id="person-1",
        ),
    )
    unknown = _face(track_id="7", confidence=0.99)
    grouped = OrderedDict(
        {
            (1, ""): _grouped_line(time_offset_ms=100, obj=known),
            (2, ""): _grouped_line(time_offset_ms=800, obj=unknown),
        }
    )

    stats = annotations._dedupe_face_annotations_by_clip(
        grouped,
        event_context=_event_context(),
    )

    remaining = [obj for line in grouped.values() for obj in line["objects"]]
    assert stats["input_count"] == 2
    assert stats["output_count"] == 1
    assert stats["same_track_second_removed"] == 1
    assert remaining[0]["identity"]["display_name"] == "Reese"


def test_iou_dedup_removes_same_second_overlapping_untracked_faces() -> None:
    annotations = _activate_media_worker()
    grouped = OrderedDict(
        {
            (1, ""): _grouped_line(
                time_offset_ms=100,
                obj=_face(track_id="", bbox=[100, 100, 180, 180], confidence=0.7),
            ),
            (2, ""): _grouped_line(
                time_offset_ms=200,
                obj=_face(track_id="", bbox=[104, 104, 184, 184], confidence=0.9),
            ),
        }
    )

    stats = annotations._dedupe_face_annotations_by_clip(
        grouped,
        event_context=_event_context(),
    )

    remaining = [obj for line in grouped.values() for obj in line["objects"]]
    assert stats["input_count"] == 2
    assert stats["output_count"] == 1
    assert stats["iou_duplicate_removed"] == 1
    assert remaining[0]["bbox"]["confidence"] == 0.9


def test_identity_propagation_does_not_cross_track_source_or_camera(monkeypatch) -> None:
    annotations = _activate_media_worker()
    monkeypatch.delenv("ANNOTATION_TRACK_IDENTITY_PROPAGATION", raising=False)
    known = _face(
        track_id="7",
        identity=_identity(
            status="matched",
            match_status="above_threshold",
            name="Reese",
            similarity=0.91,
            person_id="person-1",
        ),
    )
    same_track_different_source = _face(track_id="7")
    same_track_different_camera = _face(track_id="7")
    grouped = OrderedDict(
        {
            (1, ""): _grouped_line(source_id="source-a", camera_id="camera-a", obj=known),
            (2, ""): _grouped_line(
                source_id="source-b",
                camera_id="camera-a",
                time_offset_ms=1100,
                obj=same_track_different_source,
            ),
            (3, ""): _grouped_line(
                source_id="source-a",
                camera_id="camera-b",
                time_offset_ms=2100,
                obj=same_track_different_camera,
            ),
        }
    )

    propagated = annotations._propagate_identities_by_track(
        grouped,
        event_context=_event_context(source_id="source-a", camera_id="camera-a"),
    )

    assert propagated == 0
    assert same_track_different_source["identity"]["status"] == "unknown"
    assert same_track_different_camera["identity"]["status"] == "unknown"


def test_build_summary_includes_face_query_dedup_and_propagation_audit(monkeypatch) -> None:
    annotations = _activate_media_worker()
    monkeypatch.delenv("ALLOW_FACE_TIMESTAMP_ONLY_QUERY", raising=False)
    conn = _FilteringConn([_face_row()])

    _lines, summary = annotations.build_continuous_annotations(
        conn,
        _event_context(),
    )

    assert summary["face_observation_query"]["mode"] == "timestamp_and_created_at"
    assert summary["face_observation_query"]["timestamp_only_fallback_used"] is False
    assert summary["face_observation_query"]["cross_day_result_count"] == 0
    assert summary["face_dedup"]["input_count"] == 1
    assert summary["face_dedup"]["output_count"] == 1
    assert summary["identity_propagation"]["cross_track"] is False
    assert summary["identity_propagation"]["cross_source"] is False


def test_smoke_contract_fails_cross_day_results_and_writes_summary() -> None:
    assert SMOKE.exists()
    text = SMOKE.read_text(encoding="utf-8")
    assert "face_observation_query_window_anchor_summary.json" in text
    assert "PASS_C1I2G_FACE_OBSERVATION_QUERY_WINDOW_ANCHOR" in text
    assert "FAIL_C1I2G_TIMESTAMP_ONLY_FACE_QUERY" in text
    assert "FAIL_C1I2G_CROSS_DAY_FACE_OBSERVATION_CONTAMINATION" in text
    assert "PARTIAL_C1I2G_QUERY_FIXED_DEDUP_PENDING" in text
    assert "cross_day_result_count" in text
    assert "timestamp_only_fallback_used" in text
    assert "face_annotation_count < 80" in text
