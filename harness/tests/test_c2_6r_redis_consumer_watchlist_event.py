"""C2.6R Redis one-message watchlist consumer contract tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BUILDER_PATH = ROOT / "scripts" / "tools" / "build_c2_6r_redis_watchlist_evidence_bundle.py"


def test_process_one_message_event_requires_source_observation_id() -> None:
    builder = _load_builder()
    event = _event(builder)
    event["source_observation_id"] = ""

    try:
        builder.validate_c2_6r_output(
            summary=_summary(),
            event=event,
            c2_6r_summary=_c2_6r_summary(builder),
        )
    except RuntimeError as exc:
        assert "redis_event_missing_source_observation_id" in str(exc)
    else:
        raise AssertionError("missing source_observation_id should fail")


def test_event_producer_is_face_worker_consumer_entrypoint() -> None:
    builder = _load_builder()
    event = _event(builder)

    builder.validate_c2_6r_output(
        summary=_summary(),
        event=event,
        c2_6r_summary=_c2_6r_summary(builder),
    )

    assert event["producer"] == "face-worker-one-message-consumer"


def test_event_payload_has_no_embedding_or_image_bytes() -> None:
    builder = _load_builder()
    for key in ("embedding", "embedding_vector", "embedding_list", "image_bytes", "base64", "crop_bytes"):
        event = _event(builder)
        event["payload"][key] = "forbidden"
        try:
            builder.assert_no_forbidden_event_payload(event)
        except RuntimeError as exc:
            assert "redis_watchlist_event_contains_forbidden_payload" in str(exc)
            assert key in str(exc)
        else:
            raise AssertionError(f"forbidden key {key} should fail")


def test_event_links_person_gallery_match_and_rule() -> None:
    builder = _load_builder()
    for key in ("person_id", "gallery_embedding_id", "match_result_id", "watchlist_rule_id"):
        event = _event(builder)
        event[key] = None
        try:
            builder.validate_c2_6r_output(
                summary=_summary(),
                event=event,
                c2_6r_summary=_c2_6r_summary(builder),
            )
        except RuntimeError as exc:
            assert f"redis_event_missing_{key}" in str(exc)
        else:
            raise AssertionError(f"missing {key} should fail")


def test_track_id_alone_is_not_identity_join_key() -> None:
    builder = _load_builder()
    event = _event(builder)
    event["payload"]["primary_identity_join_key"] = "track_id"
    event["payload"]["track_id_join_warning"] = False

    try:
        builder.validate_c2_6r_output(
            summary=_summary(),
            event=event,
            c2_6r_summary=_c2_6r_summary(builder),
        )
    except RuntimeError as exc:
        assert "primary_identity_join_key_not_source_observation_id" in str(exc)
    else:
        raise AssertionError("track_id-only join should fail")


def test_summary_records_redis_consumer_or_one_message_verification() -> None:
    builder = _load_builder()
    summary = _summary()

    builder.validate_c2_6r_output(
        summary=summary,
        event=_event(builder),
        c2_6r_summary=_c2_6r_summary(builder),
    )

    assert summary["redis_consumer_loop_verified"] is True
    assert summary["face_worker_one_message_entrypoint_verified"] is True
    assert summary["primary_identity_join_key"] == "source_observation_id"
    assert summary["track_id_join_warning"] is True


def test_known_face_count_zero_fails_c2_6r() -> None:
    builder = _load_builder()
    summary = _summary()
    summary["known_face_count"] = 0
    summary["object_counts"]["known_face"] = 0

    try:
        builder.validate_c2_6r_output(
            summary=summary,
            event=_event(builder),
            c2_6r_summary=_c2_6r_summary(builder),
        )
    except RuntimeError as exc:
        assert "known_face_count_positive_required" in str(exc)
    else:
        raise AssertionError("known_face_count=0 should fail C2.6R")


def test_workaround_and_fallback_flags_preserved() -> None:
    builder = _load_builder()
    summary = _summary()

    builder.validate_c2_6r_output(
        summary=summary,
        event=_event(builder),
        c2_6r_summary=_c2_6r_summary(builder),
    )

    assert summary["evidence_capture_mode"] == "stable_post_savant_sink_time_crop"
    assert summary["event_style_replay_job_passed"] is False
    assert summary["fallback_used"] is False
    assert summary["legacy_used_for_visual_binding"] is False


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location("build_c2_6r_redis_watchlist_evidence_bundle", BUILDER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _event(builder: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_type": "watchlist_hit",
        "source_event_id": "c2_6r:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4",
        "producer": "face-worker-one-message-consumer",
        "camera_id": "c2_post_savant_fps_probe",
        "source_id": "c2_post_savant_fps_probe",
        "track_id": "4",
        "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
        "person_id": 4,
        "external_person_id": "test:c2_4:person",
        "gallery_embedding_id": 3,
        "match_result_id": 14,
        "similarity": 1.0,
        "threshold": 0.99,
        "watchlist_rule_id": "c2_6r_test_watchlist_rule",
        "event_ts_ms": 17854,
        "frame_pts": 17_854_288_888,
        "frame_num": 194,
        "payload": {
            "identity_source": "face_worker_pgvector_match",
            "watchlist_match_source": "face_worker_consumer",
            "embedding_included": False,
            "image_bytes_included": False,
            "crop_bytes_included": False,
            "primary_identity_join_key": "source_observation_id",
            "track_id_join_warning": True,
            "evidence_capture_mode": "stable_post_savant_sink_time_crop",
            "workaround_used": True,
            "event_style_replay_job_passed": False,
        },
    }


def _summary() -> dict[str, Any]:
    return {
        "event_type": "watchlist_hit",
        "watchlist_hit_count": 1,
        "known_face_count": 1,
        "object_counts": {"person": 0, "face": 0, "known_face": 1},
        "redis_consumer_loop_verified": True,
        "face_worker_one_message_entrypoint_verified": True,
        "face_worker_match_verified": True,
        "live_watchlist_from_face_worker": True,
        "identity_binding_connected": True,
        "primary_identity_join_key": "source_observation_id",
        "track_id_join_warning": True,
        "evidence_capture_mode": "stable_post_savant_sink_time_crop",
        "workaround_used": True,
        "event_style_replay_job_passed": False,
        "fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "allow_db_annotation_fallback": False,
        "allow_legacy_annotation_fallback": False,
        "production_ready": True,
        "video_integrity": {"production_gate_passed": True, "integrity_status": "pass"},
    }


def _c2_6r_summary(builder: Any) -> dict[str, Any]:
    return {
        "result_marker": builder.RESULT_PASS,
        "execution_mode": "Mode A Redis stream one-message consumer",
        "event_type": "watchlist_hit",
    }
