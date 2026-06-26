"""Evidence viewer database-index contract tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "services" / "api"

if str(API_DIR) in sys.path:
    sys.path.remove(str(API_DIR))
sys.path.insert(0, str(API_DIR))

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.repositories.events import EventRepository  # noqa: E402
from app.routers.evidence import _bundle_summary_from_row  # noqa: E402


class _Cursor:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.query = ""
        self.params: dict[str, Any] = {}

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: str, params: dict[str, Any]) -> None:
        self.query = query
        self.params = params

    def fetchone(self) -> Any:
        return self.result

    def fetchall(self) -> Any:
        return self.result


class _Conn:
    def __init__(self) -> None:
        self.cursors: list[_Cursor] = []

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        result: Any
        if not self.cursors:
            result = {"total": 1}
        else:
            result = [
                {
                    "event_id": "11111111-1111-4111-8111-111111111111",
                    "source_event_id": "source-event-1",
                }
            ]
        cursor = _Cursor(result)
        self.cursors.append(cursor)
        return cursor


def test_evidence_bundle_list_query_is_database_backed() -> None:
    conn = _Conn()
    rows, total = EventRepository(conn).list_evidence_bundle_summaries(
        event_type="intrusion",
        event_category="perimeter",
        source_id="source-1",
        camera_id="camera-1",
        event_id="source-event",
        person="finch",
        clip_status="ready",
        limit=20,
        offset=40,
    )

    count_sql = conn.cursors[0].query
    data_sql = conn.cursors[1].query

    assert total == 1
    assert rows == [{"event_id": "11111111-1111-4111-8111-111111111111", "source_event_id": "source-event-1"}]
    assert "FROM events e" in data_sql
    assert "LEFT JOIN LATERAL" in data_sql
    assert "LEFT JOIN cameras c" in data_sql
    assert "c.name AS camera_name" in data_sql
    assert "c.name ILIKE %(source_id_like)s" in data_sql
    assert "e.payload->>'camera_name' ILIKE %(source_id_like)s" in data_sql
    assert "evidence_tasks" in data_sql
    assert "media_deleted" in data_sql
    assert "media_expired" in data_sql
    assert "deleted_at" in data_sql
    assert "clip_required IS TRUE" not in data_sql
    assert "->>'clip_required'" not in data_sql
    assert "metadata.json" not in count_sql + data_sql
    assert "evidence_root" not in count_sql + data_sql
    assert "iterdir" not in count_sql + data_sql
    assert conn.cursors[1].params["limit"] == 20
    assert conn.cursors[1].params["offset"] == 40
    assert conn.cursors[1].params["event_category_types"] == [
        "intrusion",
        "wall_climb_suspicious",
    ]


def test_database_row_maps_to_frontend_bundle_summary() -> None:
    created_at = datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc)
    row = {
        "event_id": "22222222-2222-4222-8222-222222222222",
        "source_event_id": "source-event-2",
        "event_type": "watchlist_hit",
        "camera_id": "camera-1",
        "source_id": "source-1",
        "media_status": "ready",
        "created_at": created_at,
        "start_ts": None,
        "event_ts_ms": 0,
        "clip_status": "ready",
        "raw_clip_path": "/data/video-analytics/media/evidence/event/raw_clip.mov",
        "annotations_jsonl_path": "/data/video-analytics/media/evidence/event/annotations.frame_cache.identity.jsonl",
        "evidence_task_count": 1,
        "latest_task_status": "ready",
        "payload": {
            "camera_name": "Lobby",
            "media": {
                "known_face_count": 2,
                "unknown_face_count": 1,
                "annotation_lines": 42,
                "visual_evidence_status": "verified_same_stream_metadata",
                "frontend_overlay_required": True,
            },
        },
    }

    summary = _bundle_summary_from_row(row)

    assert summary["event_id"] == "22222222-2222-4222-8222-222222222222"
    assert summary["camera_name"] == "Lobby"
    assert summary["alarm_machine_time"] == created_at.isoformat()
    assert summary["alarm_machine_time_source"] == "events.created_at"
    assert summary["raw_clip_available"] is True
    assert summary["raw_clip_name"] == "raw_clip.mov"
    assert summary["raw_clip_url"] == "/api/bundles/22222222-2222-4222-8222-222222222222/media/raw_clip"
    assert summary["annotations_available"] is True
    assert summary["annotation_lines"] == 42
    assert summary["matched_objects"] == 2
    assert summary["unknown_objects"] == 1
    assert summary["index_source"] == "database"


def test_database_row_uses_camera_table_name_when_payload_lacks_name() -> None:
    row = {
        "event_id": "22222222-2222-4222-8222-222222222223",
        "source_event_id": "source-event-3",
        "event_type": "intrusion",
        "camera_id": "camera-1",
        "source_id": "source-1",
        "camera_name": "Lab Camera",
        "media_status": "ready",
        "created_at": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "start_ts": None,
        "event_ts_ms": 0,
        "clip_status": "ready",
        "raw_clip_path": "/data/video-analytics/media/evidence/event/raw_clip.mov",
        "annotations_jsonl_path": None,
        "evidence_task_count": 1,
        "latest_task_status": "ready",
        "payload": {"media": {}},
    }

    summary = _bundle_summary_from_row(row)

    assert summary["camera_name"] == "Lab Camera"


def test_database_row_does_not_treat_legacy_annotations_as_available() -> None:
    row = {
        "event_id": "22222222-2222-4222-8222-222222222224",
        "source_event_id": "source-event-legacy-annotations",
        "event_type": "intrusion",
        "camera_id": "camera-1",
        "source_id": "source-1",
        "media_status": "ready",
        "created_at": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "start_ts": None,
        "event_ts_ms": 0,
        "clip_status": "ready",
        "raw_clip_path": "/data/video-analytics/media/evidence/event/raw_clip.mov",
        "annotations_jsonl_path": "/data/video-analytics/media/evidence/event/annotations.jsonl",
        "evidence_task_count": 1,
        "latest_task_status": "ready",
        "payload": {"media": {}},
    }

    summary = _bundle_summary_from_row(row)

    assert summary["annotations_available"] is False


def test_database_row_exposes_reviewable_raw_clip_when_path_exists() -> None:
    row = {
        "event_id": "33333333-3333-4333-8333-333333333333",
        "source_event_id": "source-event-3",
        "event_type": "intrusion",
        "camera_id": "camera-1",
        "source_id": "source-1",
        "media_status": "generated_unverified",
        "created_at": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "start_ts": None,
        "event_ts_ms": 0,
        "clip_status": "generated_unverified",
        "raw_clip_path": "/data/video-analytics/media/evidence/event/raw_clip.mov",
        "annotations_jsonl_path": None,
        "evidence_task_count": 1,
        "latest_task_status": "generated_unverified",
        "payload": {"media": {}},
    }

    summary = _bundle_summary_from_row(row)

    assert summary["raw_clip_available"] is True
    assert summary["raw_clip_name"] == "raw_clip.mov"
    assert summary["clip_status"] == "generated_unverified"


def test_database_row_hides_raw_clip_for_in_progress_evidence_states() -> None:
    for index, state in enumerate(("waiting_proof", "queued", "replaying", "finalizing"), start=1):
        row = {
            "event_id": f"33333333-3333-4333-8333-33333333333{index}",
            "source_event_id": f"source-event-{state}",
            "event_type": "intrusion",
            "camera_id": "camera-1",
            "source_id": "source-1",
            "media_status": state,
            "created_at": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
            "start_ts": None,
            "event_ts_ms": 0,
            "clip_status": state,
            "raw_clip_path": "/data/video-analytics/media/evidence/event/raw_clip.mov",
            "annotations_jsonl_path": None,
            "evidence_task_count": 1,
            "latest_task_status": state,
            "payload": {
                "media": {
                    "evidence_state": state,
                    "evidence_reason": "proof pending",
                },
            },
        }

        summary = _bundle_summary_from_row(row)

        assert summary["raw_clip_available"] is False
        assert summary["evidence_state"] == state
        assert summary["evidence_reason"] == "proof pending"


def test_database_row_marks_deleted_media_not_playable() -> None:
    row = {
        "event_id": "44444444-4444-4444-8444-444444444444",
        "source_event_id": "source-event-4",
        "event_type": "intrusion",
        "camera_id": "camera-1",
        "source_id": "source-1",
        "media_status": "media_deleted",
        "created_at": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "start_ts": None,
        "event_ts_ms": 0,
        "clip_status": "ready",
        "raw_clip_path": "/data/video-analytics/media/evidence/event/raw_clip.mov",
        "annotations_jsonl_path": "/data/video-analytics/media/evidence/event/annotations.frame_cache.identity.jsonl",
        "evidence_task_count": 1,
        "latest_task_status": "ready",
        "payload": {"maintenance": {"deleted_at": "2026-06-16T15:55:47Z"}, "media": {}},
    }

    summary = _bundle_summary_from_row(row)

    assert summary["media_status"] == "media_deleted"
    assert summary["raw_clip_available"] is False


def test_8090_evidence_frontend_uses_db_index_and_file_details() -> None:
    scripts = [
        ROOT / "services" / "evidence-viewer" / "app" / "static" / "evidence.js",
    ]

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert 'const EVIDENCE_INDEX_API = "/api/v1/evidence";' in text
        assert 'const EVIDENCE_BUNDLE_API = "/api";' in text
        assert "`${EVIDENCE_INDEX_API}/bundles?${bundleQueryString()}`" in text
        assert "`${EVIDENCE_INDEX_API}/health`" in text
        assert 'const CAMERA_INDEX_API = "/api/v1/cameras";' in text
        assert "function loadCameraNameLookup" in text
        assert "`${EVIDENCE_BUNDLE_API}/bundles/${encodeURIComponent(eventId)}`" in text
        assert "`${EVIDENCE_BUNDLE_API}/bundles/${encodeURIComponent(eventId)}/annotations?${annotationParams.toString()}`" in text
        assert "`${EVIDENCE_BUNDLE_API}/bundles/${encodeURIComponent(eventId)}/sink-metadata`" in text
        assert "`${EVIDENCE_API}/bundles?${bundleQueryString()}`" not in text
        assert "`/api/bundles?${bundleQueryString()}`" not in text
        assert "bundle.source_id || bundle.camera_id" not in text
        assert 'fetchJson("/health")' not in text
    assert not (ROOT / "services" / "evidence-viewer" / "app" / "static" / "app.js").exists()
    assert not (
        ROOT / "services" / "api" / "app" / "static" / "operator" / "evidence.js"
    ).exists()


def test_8090_operator_proxy_allows_evidence_index() -> None:
    main_py = (ROOT / "services" / "evidence-viewer" / "app" / "main.py").read_text(encoding="utf-8")

    assert "OPERATOR_PROXY_ALLOWED_PREFIXES" in main_py
    assert '"evidence",' in main_py


def test_evidence_click_clears_stale_video_and_ignores_late_responses() -> None:
    scripts = [
        ROOT / "services" / "evidence-viewer" / "app" / "static" / "evidence.js",
    ]

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert "selectionRequestId" in text
        assert "function evidenceStateLabel(bundle = {})" in text
        assert "bundle.evidence_reason" in text
        assert "function clearBundleDetailForLoading(eventId)" in text
        assert "dom.video.pause();" in text
        assert 'dom.video.removeAttribute("src");' in text
        assert "function selectionStillCurrent(requestId, eventId)" in text
        assert "if (!selectionStillCurrent(requestId, eventId))" in text
        assert 'const nextVideoHref = nextVideoSrc ? new URL(nextVideoSrc, window.location.href).href : "";' in text
        assert "previousVideoSrc !== new URL(nextVideoSrc" not in text
