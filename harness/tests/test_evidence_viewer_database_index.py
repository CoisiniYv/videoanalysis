"""Evidence viewer database-index contract tests."""

from __future__ import annotations

import json
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
from app.routers.evidence import (  # noqa: E402
    _bundle_manifest_from_index_row,
    _bundle_summary_from_row,
    _load_artifact_records,
    _resolve_artifact_path,
    evidence_bundle_annotations,
    evidence_bundle_sink_metadata,
    router as evidence_router,
)


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


def test_evidence_bundle_list_supports_frontend_root_alias() -> None:
    paths = {route.path for route in evidence_router.routes}

    assert "/api/v1/evidence" in paths
    assert "/api/v1/evidence/bundles" in paths


def test_evidence_bundle_list_query_is_database_backed() -> None:
    conn = _Conn()
    rows, total = EventRepository(conn).list_evidence_bundle_summaries(
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
    assert "FROM evidence_bundles eb" in data_sql
    assert "WITH page AS MATERIALIZED" in data_sql
    assert data_sql.index("WITH page AS MATERIALIZED") < data_sql.index("overlay_artifact")
    assert "LEFT JOIN LATERAL" in data_sql
    assert "LEFT JOIN cameras c" in data_sql
    assert "COALESCE(eb.camera_name, c.name) AS camera_name" in data_sql
    assert "c.name ILIKE %(source_id_like)s" in data_sql
    assert "eb.camera_name ILIKE %(source_id_like)s" in data_sql
    assert "evidence_tasks" in data_sql
    assert "overlay_artifact" not in count_sql
    assert "task_counts" not in count_sql
    assert "COUNT(*)::int AS task_count" not in count_sql
    assert "media_deleted" in data_sql
    assert "media_expired" in data_sql
    assert "raw_clip_uri" in data_sql
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


def test_evidence_bundle_count_skips_camera_join_without_camera_name_filter() -> None:
    conn = _Conn()

    EventRepository(conn).list_evidence_bundle_summaries(
        event_category="evidence",
        limit=10,
    )

    assert "LEFT JOIN cameras c" not in conn.cursors[0].query
    assert "WITH page AS MATERIALIZED" in conn.cursors[1].query


def test_evidence_category_excludes_face_lookup_events_by_default() -> None:
    conn = _Conn()
    EventRepository(conn).list_evidence_bundle_summaries(event_category="evidence")

    event_types = conn.cursors[1].params["event_category_types"]
    assert "intrusion" in event_types
    assert "crowd_gathering" in event_types
    assert "watchlist_hit" not in event_types
    assert "live_search_hit" not in event_types


def test_explicit_evidence_event_type_bypasses_default_category_filter() -> None:
    conn = _Conn()
    EventRepository(conn).list_evidence_bundle_summaries(
        event_type="watchlist_hit",
        event_category="evidence",
    )

    params = conn.cursors[1].params
    assert params["event_type"] == "watchlist_hit"
    assert "event_category_types" not in params


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
                "summary": {
                    "matched_person": {
                        "person_id": 5,
                        "name": "Reese",
                        "external_person_id": "demo:f4_3:reese",
                    },
                    "match": {"source_observation_id": "face:source-1:frame-1:0"},
                    "observation": {"person_track_id": "736"},
                },
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
    assert summary["raw_clip_url"] == "/media/evidence/event/raw_clip.mov"
    assert summary["annotations_available"] is True
    assert summary["annotation_lines"] == 42
    assert summary["matched_objects"] == 2
    assert summary["unknown_objects"] == 1
    assert summary["matched_person"]["person_id"] == 5
    assert summary["person_name"] == "Reese"
    assert summary["external_person_id"] == "demo:f4_3:reese"
    assert summary["source_observation_id"] == "face:source-1:frame-1:0"
    assert summary["person_track_id"] == "736"
    assert summary["index_source"] == "database"


def test_database_bundle_manifest_does_not_require_sidecar_files() -> None:
    row = {
        "event_id": "22222222-2222-4222-8222-222222222222",
        "source_event_id": "source-event-2",
        "event_type": "watchlist_hit",
        "camera_id": "camera-1",
        "source_id": "source-1",
        "camera_name": "Lab",
        "event_created_at": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "alarm_machine_time": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "media_status": "materialized",
        "evidence_reason": "",
        "raw_clip_uri": "/data/video-analytics/media/evidence/event/raw_clip.mov",
        "overlay_artifact_uri": "/data/video-analytics/media/evidence/event/annotations.frame_cache.identity.jsonl",
        "frontend_overlay_required": True,
        "visual_evidence_status": "verified",
        "summary": {
            "matched_person": {
                "person_id": 5,
                "name": "Reese",
                "external_person_id": "demo:f4_3:reese",
            },
            "match": {"source_observation_id": "face:source-1:frame-1:0"},
            "observation": {"person_track_id": "736"},
            "sidecar_summary": {
                "production_ready": True,
                "timeline_domain": "final_canonical_clip",
                "frame_identity_method": "frame_uuid",
            }
        },
        "materialization": {},
    }

    manifest = _bundle_manifest_from_index_row(row)

    assert manifest["index_source"] == "database"
    assert manifest["raw_clip_url"] == "/media/evidence/event/raw_clip.mov"
    assert manifest["available_files"] == ["raw_clip.mov"]
    assert manifest["default_annotation_source"] == "database"
    assert manifest["default_annotation_file"] is None
    assert manifest["metadata"]["media"]["db_index_status"] == "ready"
    assert manifest["matched_person"]["person_id"] == 5
    assert manifest["metadata"]["event"]["person_name"] == "Reese"
    assert manifest["source_observation_id"] == "face:source-1:frame-1:0"
    assert manifest["person_track_id"] == "736"


def test_image_only_bundle_summary_and_manifest() -> None:
    row = {
        "event_id": "33333333-3333-4333-8333-333333333333",
        "source_event_id": "watchlist-hit-3",
        "event_type": "watchlist_hit",
        "camera_id": "camera-1",
        "source_id": "source-1",
        "camera_name": "Lab",
        "event_created_at": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "alarm_machine_time": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "media_status": "image_ready",
        "evidence_reason": "",
        "raw_clip_uri": None,
        "face_crop_uri": "/data/video-analytics/media/faces/crop.jpg",
        "full_frame_uri": "/data/video-analytics/media/faces/full.jpg",
        "annotated_frame_uri": None,
        "frontend_overlay_required": False,
        "visual_evidence_status": "verified",
        "summary": {
            "playback_kind": "image",
            "clip_status": "not_required",
            "face_crop_uri": "/data/video-analytics/media/faces/crop.jpg",
            "full_frame_uri": "/data/video-analytics/media/faces/full.jpg",
        },
        "materialization": {},
        "payload": {"media": {"playback_kind": "image"}},
        "created_at": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "clip_status": "not_required",
        "raw_clip_path": None,
        "annotations_jsonl_path": None,
        "evidence_task_count": 1,
        "latest_task_status": "materialized",
    }

    summary = _bundle_summary_from_row(row)
    manifest = _bundle_manifest_from_index_row(row)

    assert summary["playback_kind"] == "image"
    assert summary["raw_clip_available"] is False
    assert summary["face_crop_url"] == "/media/faces/crop.jpg"
    assert manifest["playback_kind"] == "image"
    assert manifest["raw_clip_url"] is None
    assert manifest["full_frame_url"] == "/media/faces/full.jpg"


def test_image_only_bundle_without_artifact_is_not_reported_available() -> None:
    row = {
        "event_id": "44444444-4444-4444-8444-444444444444",
        "source_event_id": "watchlist-hit-4",
        "event_type": "watchlist_hit",
        "camera_id": "camera-1",
        "source_id": "source-1",
        "camera_name": "Lab",
        "event_created_at": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "alarm_machine_time": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "media_status": "image_missing",
        "evidence_reason": "face_image_artifact_missing",
        "raw_clip_uri": None,
        "face_crop_uri": None,
        "full_frame_uri": None,
        "annotated_frame_uri": None,
        "frontend_overlay_required": False,
        "visual_evidence_status": "missing",
        "summary": {
            "playback_kind": "image",
            "clip_status": "not_required",
            "image_status": "image_missing",
            "image_missing_reason": "face_image_artifact_missing",
        },
        "materialization": {},
        "payload": {"media": {"playback_kind": "image"}},
        "created_at": datetime(2026, 6, 15, 4, 5, 6, tzinfo=timezone.utc),
        "clip_status": "not_required",
        "raw_clip_path": None,
        "annotations_jsonl_path": None,
        "evidence_task_count": 1,
        "latest_task_status": "failed",
    }

    summary = _bundle_summary_from_row(row)
    manifest = _bundle_manifest_from_index_row(row)

    assert summary["playback_kind"] == "image"
    assert summary["image_available"] is False
    assert summary["evidence_reason"] == "face_image_artifact_missing"
    assert summary["warnings"] == ["image_evidence_missing_artifact"]
    assert manifest["image_available"] is False
    assert manifest["warnings"] == ["image_evidence_missing_artifact"]


def test_bundle_annotations_fall_back_to_artifact_file_when_db_rows_missing(
    tmp_path: Path,
) -> None:
    annotations = tmp_path / "annotations.frame_cache.identity.jsonl"
    annotations.write_text(
        '{"clip_frame_index":0,"displayable":true,"objects":[{"label":"person"}]}\n',
        encoding="utf-8",
    )

    class _Repo:
        def get_evidence_bundle_index(self, _event_id: str) -> dict[str, Any]:
            return {
                "event_id": "22222222-2222-4222-8222-222222222222",
                "summary": {"sidecar_summary": {"production_ready": True}},
                "overlay_artifact_uri": str(annotations),
                "visual_evidence_status": "verified",
            }

        def list_evidence_overlay_records(self, _event_id: str) -> list[dict[str, Any]]:
            return []

    response = evidence_bundle_annotations(
        "22222222-2222-4222-8222-222222222222",
        repo=_Repo(),
        request_id="req-1",
    )

    data = response["data"]
    assert data["count"] == 1
    assert data["annotation_source"] == "filesystem"
    assert data["annotation_source_kind"] == "filesystem_overlay_artifact"
    assert data["fallback_used"] is True
    assert data["records"][0]["objects"][0]["label"] == "person"


def test_bundle_annotations_fallback_supports_count_only_requests(
    tmp_path: Path,
) -> None:
    annotations = tmp_path / "annotations.frame_cache.identity.jsonl"
    annotations.write_text(
        "\n".join(
            [
                '{"clip_frame_index":0,"displayable":true,"objects":[{"label":"person"}]}',
                '{"clip_frame_index":1,"displayable":false,"objects":[{"label":"person"}]}',
                '{"clip_frame_index":2,"objects":[{"label":"person"}]}',
            ]
        ),
        encoding="utf-8",
    )

    class _Repo:
        def get_evidence_bundle_index(self, _event_id: str) -> dict[str, Any]:
            return {
                "event_id": "22222222-2222-4222-8222-222222222222",
                "summary": {"sidecar_summary": {"production_ready": True}},
                "overlay_artifact_uri": str(annotations),
                "visual_evidence_status": "verified",
            }

        def list_evidence_overlay_records(self, _event_id: str) -> list[dict[str, Any]]:
            return []

    response = evidence_bundle_annotations(
        "22222222-2222-4222-8222-222222222222",
        include_records=False,
        repo=_Repo(),
        request_id="req-1",
    )

    data = response["data"]
    assert data["count"] == 2
    assert data["records"] == []
    assert data["fallback_used"] is True


def test_artifact_loader_maps_media_container_paths_to_api_media_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    media_root = tmp_path / "media"
    annotations = media_root / "evidence" / "event" / "annotations.frame_cache.identity.jsonl"
    annotations.parent.mkdir(parents=True)
    annotations.write_text(
        '{"clip_frame_index":0,"objects":[{"label":"person"}]}\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(_resolve_artifact_path.__globals__, "MEDIA_ROOT", media_root)

    assert _resolve_artifact_path(
        "/media/evidence/event/annotations.frame_cache.identity.jsonl"
    ) == annotations
    result = _load_artifact_records(
        "/media/evidence/event/annotations.frame_cache.identity.jsonl"
    )
    assert result["count"] == 1
    assert result["records"][0]["objects"][0]["label"] == "person"


def test_bundle_sink_metadata_falls_back_to_artifact_file_when_db_rows_missing(
    tmp_path: Path,
) -> None:
    timeline = tmp_path / "sink_metadata.json"
    timeline.write_text(
        '{"clip_frame_index":0,"frame_uuid":"frame-1","frame_pts":123}\n',
        encoding="utf-8",
    )

    class _Repo:
        def get_evidence_bundle_index(self, _event_id: str) -> dict[str, Any]:
            return {
                "event_id": "22222222-2222-4222-8222-222222222222",
                "timeline_artifact_uri": str(timeline),
            }

        def list_evidence_timeline_records(self, _event_id: str) -> list[dict[str, Any]]:
            return []

    response = evidence_bundle_sink_metadata(
        "22222222-2222-4222-8222-222222222222",
        repo=_Repo(),
        request_id="req-1",
    )

    data = response["data"]
    assert data["count"] == 1
    assert data["index_source"] == "filesystem"
    assert data["fallback_used"] is True
    assert data["records"][0]["frame_uuid"] == "frame-1"


def test_bundle_sink_metadata_fallback_reads_savant_frames_object(
    tmp_path: Path,
) -> None:
    timeline = tmp_path / "sink_metadata.json"
    timeline.write_text(
        json.dumps(
            {
                "source_id": "source-a",
                "frames": [
                    {"uuid": "frame-a", "pts": 100},
                    {"uuid": "frame-b", "pts": 200},
                ],
            }
        ),
        encoding="utf-8",
    )

    class _Repo:
        def get_evidence_bundle_index(self, _event_id: str) -> dict[str, Any]:
            return {
                "event_id": "22222222-2222-4222-8222-222222222222",
                "timeline_artifact_uri": str(timeline),
            }

        def list_evidence_timeline_records(self, _event_id: str) -> list[dict[str, Any]]:
            return []

    response = evidence_bundle_sink_metadata(
        "22222222-2222-4222-8222-222222222222",
        repo=_Repo(),
        request_id="req-1",
    )

    data = response["data"]
    assert data["count"] == 2
    assert data["records"][0]["uuid"] == "frame-a"


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


def test_8090_evidence_frontend_uses_db_index_for_list_and_details() -> None:
    scripts = [
        ROOT / "services" / "evidence-viewer" / "app" / "static" / "evidence.js",
    ]

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert 'const EVIDENCE_INDEX_API = "/api/v1/evidence";' in text
        assert "EVIDENCE_BUNDLE_API" not in text
        assert "`${EVIDENCE_INDEX_API}/bundles?${bundleQueryString()}`" in text
        assert "`${EVIDENCE_INDEX_API}/health`" in text
        assert 'const CAMERA_INDEX_API = "/api/v1/cameras";' in text
        assert "function loadCameraNameLookup" in text
        assert "`${EVIDENCE_INDEX_API}/bundles/${encodeURIComponent(eventId)}`" in text
        assert "`${EVIDENCE_INDEX_API}/bundles/${encodeURIComponent(eventId)}/annotations?${annotationParams.toString()}`" in text
        assert "`${EVIDENCE_INDEX_API}/bundles/${encodeURIComponent(eventId)}/sink-metadata`" in text
        assert "`${EVIDENCE_API}/bundles?${bundleQueryString()}`" not in text
        assert "`${EVIDENCE_BUNDLE_API}/bundles/${encodeURIComponent(eventId)}`" not in text
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
        assert "overlay_render_failed" in text
        assert "overlay_object_render_failed" in text
        assert 'const nextVideoHref = nextVideoSrc ? new URL(nextVideoSrc, window.location.href).href : "";' in text
        assert "previousVideoSrc !== new URL(nextVideoSrc" not in text
