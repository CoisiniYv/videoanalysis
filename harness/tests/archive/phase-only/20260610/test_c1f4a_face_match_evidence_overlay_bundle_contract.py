"""C1F.4a face match evidence overlay bundle contract tests."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE = (
    REPO_ROOT
    / "scripts"
    / "smoke"
    / "check_c1f4a_face_match_evidence_overlay_bundle.sh"
)
COMPOSE = REPO_ROOT / "infra" / "docker-compose.c1-official-replay-dev.yml"
DOC = REPO_ROOT / "docs" / "c1f4a_face_match_evidence_overlay_bundle.md"
STYLE = REPO_ROOT / "services" / "media-worker" / "app" / "annotation_style.py"
CONTINUOUS = (
    REPO_ROOT / "services" / "media-worker" / "app" / "continuous_annotation.py"
)
WORKER = REPO_ROOT / "services" / "media-worker" / "app" / "worker.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _script() -> str:
    return _text(SMOKE)


def _script_lower() -> str:
    return _script().lower()


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _activate_media_worker_path() -> None:
    service_path = str(REPO_ROOT / "services" / "media-worker")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if service_path in sys.path:
        sys.path.remove(service_path)
    sys.path.insert(0, service_path)


def test_smoke_script_exists() -> None:
    assert SMOKE.exists()
    assert os.access(SMOKE, os.X_OK)
    mode = SMOKE.stat().st_mode
    assert mode & stat.S_IXUSR


def test_smoke_uses_existing_face_match_emitter() -> None:
    content = _script()
    assert "emit_face_match_events.py" in content
    assert "face_match_event_service" in content
    assert "--external-person-id" in content
    assert "test:archive:finch" in content
    assert "test:archive:reese" in content


def test_smoke_checks_event_worker() -> None:
    content = _script()
    assert "c1-official-event-worker" in content
    assert "wait_for_event_ingested" in content
    assert "FAIL_EVENT_NOT_INGESTED" in content


def test_smoke_checks_record_request() -> None:
    content = _script()
    assert 'RECORD_REQUEST_STREAM="security.record_requests"' in content
    assert "wait_for_record_request" in content
    assert "FAIL_RECORD_REQUEST_NOT_CREATED" in content


def test_smoke_checks_clip_worker_replay_job() -> None:
    content = _script()
    assert "c1-official-clip-worker" in content
    assert "wait_for_replay_job" in content
    assert "replay_job_id" in content
    assert "replay_job_request" in content
    assert "FAIL_REPLAY_JOB_NOT_CREATED" in content


def test_smoke_checks_video_file_sink_output() -> None:
    content = _script()
    assert "c1-official-video-file-sink" in content
    assert "sink_metadata_json" in content
    assert "raw_clip" in content


def test_smoke_checks_media_worker_evidence_bundle() -> None:
    content = _script()
    assert "c1-official-media-worker" in content
    assert "wait_for_evidence_bundle" in content
    assert "verify_evidence_bundle" in content
    assert "PASS_FACE_MATCH_EVIDENCE_BUNDLE_READY" in content


def test_smoke_does_not_directly_write_events_repository() -> None:
    content = _script()
    forbidden = [
        "EventRepository",
        "insert_event(",
        "INSERT INTO events",
        "create_evidence_task",
    ]
    for token in forbidden:
        assert token not in content


def test_smoke_does_not_directly_call_replay_as_pass_path() -> None:
    content = _script()
    assert "ReplayClient" not in content
    assert "/api/v1/job" not in content
    assert "curl " not in content
    assert "direct_replay_bypass" in content


def test_smoke_does_not_use_second_rtsp() -> None:
    content = _script()
    assert content.count("rtsp://10.37.57.112:8554/live/1080movie") == 1
    assert "SECOND_RTSP" not in content
    assert "second_rtsp=NO" in content


def test_smoke_does_not_use_source_extraction() -> None:
    content = _script_lower()
    assert "source extraction" not in content
    assert "source_extraction_fallback" not in content
    assert "source_extraction" in content


def test_smoke_does_not_use_ffmpeg_rtsp_clipping() -> None:
    content = _script_lower()
    assert "ffmpeg -i" not in content
    assert "ffmpeg" not in content.replace("ffmpeg_rtsp_clipping", "")
    assert "ffmpeg_rtsp_clipping=NO".lower() in content


def test_smoke_checks_raw_clip() -> None:
    content = _script()
    assert "raw_clip_path" in content
    assert "raw_clip_size" in content
    assert "FAIL_RAW_CLIP_NOT_GENERATED" in content or "raw_clip missing_or_empty" in content


def test_smoke_checks_annotations_jsonl() -> None:
    content = _script()
    assert "annotations.jsonl" in content
    assert "annotations_jsonl" in content
    assert "annotation_lines" in content
    assert "FAIL_ANNOTATIONS_NOT_GENERATED" in content


def test_smoke_checks_summary_json() -> None:
    content = _script()
    assert "summary.json" in content
    assert "summary_json" in content


def test_annotations_are_continuous_timeline_not_snapshot() -> None:
    content = _text(CONTINUOUS)
    assert "build_continuous_annotations" in content
    assert "time_offset_ms" in content
    assert "timestamp_ms" in content
    assert "OrderedDict" in content
    assert "event_frame" not in content


def test_annotations_include_frame_anchor_fields() -> None:
    content = _text(CONTINUOUS)
    assert "frame_uuid" in content
    assert "keyframe_uuid" in content
    assert "previous_keyframe_uuid" in content
    assert "frame_pts" in content


def test_annotations_include_bbox() -> None:
    content = _text(CONTINUOUS)
    assert '"bbox"' in content
    assert "_normalise_bbox" in content


def test_annotations_include_identity() -> None:
    content = _text(CONTINUOUS)
    assert '"identity"' in content
    assert "matched_person" in content
    assert "low_similarity_candidate" in content


def test_annotations_include_pose() -> None:
    content = _text(CONTINUOUS)
    assert '"pose"' in content
    assert "pose_observation_not_yet_persisted" in content


def test_annotations_include_action() -> None:
    content = _text(CONTINUOUS)
    assert '"action"' in content
    assert '"status": "none"' in content


def test_annotations_include_style() -> None:
    content = _text(CONTINUOUS)
    assert '"style"' in content
    assert "build_style" in content


def test_style_contains_bbox_color() -> None:
    content = _text(STYLE)
    assert "bbox_color" in content
    assert "label_color" in content


def test_style_policy_supports_identity_match() -> None:
    _activate_media_worker_path()
    from app.annotation_style import build_style

    style = build_style(identity_status="matched", display_name="Reese", similarity=0.51)
    assert style["reason"] == "identity_match"
    assert style["priority"] == 50


def test_style_policy_supports_low_similarity_candidate() -> None:
    _activate_media_worker_path()
    from app.annotation_style import build_style

    style = build_style(identity_status="low_similarity_candidate", similarity=0.35)
    assert style["reason"] == "low_similarity_candidate"
    assert style["priority"] == 30


def test_style_policy_supports_unknown_face() -> None:
    _activate_media_worker_path()
    from app.annotation_style import build_style

    style = build_style(identity_status="unknown")
    assert style["reason"] == "unknown_face"
    assert style["priority"] == 10


def test_style_policy_supports_behavior_event() -> None:
    _activate_media_worker_path()
    from app.annotation_style import build_style

    style = build_style(event_type="behavior_event", severity="high")
    assert style["reason"] == "behavior_event"
    assert style["priority"] == 90


def test_style_policy_supports_priority() -> None:
    content = _text(STYLE)
    assert "priority" in content
    assert "100" in content
    assert "90" in content
    assert "50" in content
    assert "30" in content
    assert "10" in content


def test_annotations_forbid_embedding_vector() -> None:
    content = _text(CONTINUOUS)
    assert '"embedding":' not in content
    assert "embedding_leaked" in content


def test_annotations_forbid_image_crop_base64_bytes() -> None:
    content = _text(CONTINUOUS).lower()
    assert "base64.b64encode" not in content
    assert "image_bytes_leaked" in content
    assert "crop_bytes" in content
    assert "raw_bytes" in content
    assert "data:image/" in content


def test_metadata_declares_no_annotated_clip_default() -> None:
    worker = _text(WORKER)
    assert '"annotated_clip_status": "not_generated"' in worker
    assert "frontend_overlay_required" in worker
    assert "continuous_jsonl" in worker


def test_compose_records_watchlist_hit_events() -> None:
    env = _compose()["services"]["event-worker"]["environment"]
    assert env["RECORDING_ENABLED"] == "true"
    assert "watchlist_hit" in env["RECORDING_EVENT_TYPES"]
    assert env["RECORD_REQUEST_STREAM"] == "security.record_requests"


def test_doc_declares_frontend_overlay_synthesis() -> None:
    content = _text(DOC)
    assert "frontend overlay" in content.lower()
    assert "composes the picture" in content.lower()


def test_doc_declares_raw_clip_annotations_are_default() -> None:
    content = _text(DOC)
    assert "raw_clip" in content
    assert "annotations.jsonl" in content
    assert "production default" in content.lower()


def test_doc_declares_annotated_clip_not_default() -> None:
    content = _text(DOC)
    assert "annotated_clip" in content
    assert "not default" in content.lower()


def test_doc_declares_pose_action_reserved_schema() -> None:
    content = _text(DOC)
    assert "pose" in content
    assert "action" in content
    assert "reserved" in content.lower()


def test_doc_declares_behavior_can_drive_color_changes() -> None:
    content = _text(DOC)
    assert "behavior" in content.lower()
    assert "color" in content.lower()
    assert "style policy" in content.lower()
