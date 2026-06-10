"""C2.10 runtime API/viewer recovery report contract tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BUILDER_PATH = ROOT / "scripts" / "tools" / "build_c2_10_runtime_api_viewer_report.py"


def test_operator_report_includes_watchlist_hit() -> None:
    builder = _load_builder()
    html = builder.render_operator_report(_context())

    assert "Watchlist Hit" in html
    assert "watchlist_hit" in html


def test_operator_report_includes_external_person_id() -> None:
    builder = _load_builder()
    html = builder.render_operator_report(_context())

    assert "test:c2_4:person" in html
    assert "Person id" in html


def test_operator_report_includes_similarity_and_threshold() -> None:
    builder = _load_builder()
    html = builder.render_operator_report(_context())

    assert "Similarity" in html
    assert "1.0" in html
    assert "Threshold" in html
    assert "0.99" in html


def test_operator_report_includes_source_observation_id() -> None:
    builder = _load_builder()
    html = builder.render_operator_report(_context())

    assert "face:c2_post_savant_fps_probe:4:17854:1" in html


def test_operator_report_includes_evidence_bundle_path() -> None:
    builder = _load_builder()
    html = builder.render_operator_report(_context())

    assert "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035" in html
    assert "raw_clip.mov" in html


def test_operator_report_includes_workaround_flags() -> None:
    builder = _load_builder()
    html = builder.render_operator_report(_context())

    assert "stable_post_savant_sink_time_crop" in html
    assert "Workaround used" in html
    assert "true" in html


def test_operator_report_includes_event_style_replay_false() -> None:
    builder = _load_builder()
    html = builder.render_operator_report(_context())

    assert "Event-style Replay passed" in html
    assert "false" in html
    assert "Event-style Replay is not passed" in html


def test_report_and_response_do_not_include_embedding() -> None:
    builder = _load_builder()
    context = _context()
    html = builder.render_operator_report(context)
    scan = builder.scan_for_unsafe_payload({"detail": context["detail"], "html": html})

    assert scan["payload_has_embedding"] is False
    assert scan["forbidden_key_paths"] == []


def test_report_and_response_do_not_include_image_base64_or_crop_bytes() -> None:
    builder = _load_builder()
    context = _context()
    html = builder.render_operator_report(context)
    scan = builder.scan_for_unsafe_payload({"detail": context["detail"], "html": html})

    assert scan["payload_has_image_bytes"] is False
    assert scan["forbidden_key_paths"] == []


def test_live_api_failure_can_be_partial_not_false_pass(tmp_path: Path) -> None:
    builder = _load_builder()
    context = _context()
    context["live_api_by_event_id_verified"] = False
    context["live_api_by_source_event_id_verified"] = False
    scan = builder.scan_for_unsafe_payload({"detail": context["detail"]})
    summary = builder.build_summary(context, scan, tmp_path)
    summary["operator_report_generated"] = True

    assert builder.determine_result_marker(summary) == builder.RESULT_STATIC_ONLY
    assert summary["live_api_http_verified"] is False


def test_viewer_502_can_be_partial_not_false_pass(tmp_path: Path) -> None:
    builder = _load_builder()
    context = _context()
    context["viewer_health_verified"] = False
    context["viewer_manifest_verified"] = False
    context["viewer_annotations_verified"] = False
    context["viewer_health_status_code"] = 502
    context["viewer_manifest_status_code"] = 502
    context["viewer_annotations_status_code"] = 502
    scan = builder.scan_for_unsafe_payload({"detail": context["detail"]})
    summary = builder.build_summary(context, scan, tmp_path)
    summary["operator_report_generated"] = True

    assert builder.determine_result_marker(summary) == builder.RESULT_LIVE_API_STATIC
    assert summary["evidence_viewer_verified"] is False


def test_summary_validation_requires_no_unsafe_payload(tmp_path: Path) -> None:
    builder = _load_builder()
    context = _context()
    scan = {"payload_has_embedding": True, "payload_has_image_bytes": False, "forbidden_key_paths": ["embedding"]}
    (tmp_path / "operator_watchlist_evidence.html").write_text("ok", encoding="utf-8")
    summary = builder.build_summary(context, scan, tmp_path)

    try:
        builder.validate_summary(summary)
    except RuntimeError as exc:
        assert "unsafe_embedding_present" in str(exc)
    else:
        raise AssertionError("unsafe embedding should fail C2.10")


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location("build_c2_10_runtime_api_viewer_report", BUILDER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _context() -> dict[str, Any]:
    return {
        "detail": _detail(),
        "known_face": {
            "object_type": "known_face",
            "identity": {
                "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
                "watchlist_rule_id": "c2_6r_test_watchlist_rule",
            },
        },
        "audit_index_path": "/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035/index.html",
        "contact_sheet_path": "/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035/contact_sheet.jpg",
        "live_api_by_event_id_verified": True,
        "live_api_by_source_event_id_verified": True,
        "live_api_status_code_by_event_id": 200,
        "live_api_status_code_by_source_event_id": 200,
        "live_api_status": "verified:200",
        "api_runtime_mode": "temporary_local_uvicorn",
        "api_base_url": "http://127.0.0.1:45678",
        "temp_api_started": True,
        "viewer_health_verified": True,
        "viewer_manifest_verified": True,
        "viewer_annotations_verified": True,
        "viewer_health_status_code": 200,
        "viewer_manifest_status_code": 200,
        "viewer_annotations_status_code": 200,
        "viewer_status": "verified:200",
        "viewer_base_url": "http://127.0.0.1:8090",
        "viewer_manifest_url": "http://127.0.0.1:8090/api/bundles/c2_6r_redis_watchlist_20260607T221035",
        "viewer_annotations_url": "http://127.0.0.1:8090/api/bundles/c2_6r_redis_watchlist_20260607T221035/annotations?source=sidecar",
        "viewer_raw_clip_url": "http://127.0.0.1:8090/api/bundles/c2_6r_redis_watchlist_20260607T221035/media/raw_clip",
    }


def _detail() -> dict[str, Any]:
    return {
        "event_id": "b4cf4b6b-9d90-4282-9b7f-5e0c56e81a32",
        "source_event_id": (
            "c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:"
            "c2_7_event_worker_persistence_20260607T223424"
        ),
        "event_type": "watchlist_hit",
        "status": "new",
        "camera_id": "c2_post_savant_fps_probe",
        "source_id": "c2_post_savant_fps_probe",
        "track_id": "4",
        "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
        "person": {"person_id": 4, "external_person_id": "test:c2_4:person"},
        "watchlist": {
            "watchlist_rule_id": "c2_6r_test_watchlist_rule",
            "similarity": 1.0,
            "threshold": 0.99,
            "match_result_id": 6,
            "gallery_embedding_id": 3,
        },
        "evidence": {
            "bundle_path": "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035",
            "raw_clip_path": "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035/raw_clip.mov",
            "summary_path": "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035/summary.json",
            "sidecar_path": "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035/annotations.frame_cache.identity.jsonl",
            "watchlist_event_path": "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035/redis_watchlist_event.json",
            "video_integrity_status": "pass",
            "production_ready": True,
            "known_face_count": 1,
            "watchlist_hit_count": 1,
            "capture_mode": "stable_post_savant_sink_time_crop",
            "workaround_used": True,
            "event_style_replay_job_passed": False,
        },
    }
