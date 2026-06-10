"""C2.7 event-worker persistence and API query contract tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BUILDER_PATH = ROOT / "scripts" / "tools" / "build_c2_7_persist_watchlist_event.py"


def test_watchlist_hit_event_requires_source_event_id() -> None:
    builder = _load_builder()
    event = _event()
    event["source_event_id"] = ""

    try:
        builder.validate_c2_7_output(
            summary=_summary(),
            event=event,
            db_row=_db_row(event),
            repository_response=_repository_response(event),
            api_response=_api_response(event),
        )
    except RuntimeError as exc:
        assert "event_missing_source_event_id" in str(exc)
    else:
        raise AssertionError("missing source_event_id should fail C2.7")


def test_watchlist_hit_event_requires_source_observation_id() -> None:
    builder = _load_builder()
    event = _event()
    event["source_observation_id"] = ""

    try:
        builder.validate_c2_7_output(
            summary=_summary(),
            event=event,
            db_row=_db_row(event),
            repository_response=_repository_response(event),
            api_response=_api_response(event),
        )
    except RuntimeError as exc:
        assert "event_missing_source_observation_id" in str(exc)
    else:
        raise AssertionError("missing source_observation_id should fail C2.7")


def test_watchlist_hit_event_requires_person_gallery_match_linkage() -> None:
    builder = _load_builder()
    for key in ("person_id", "gallery_embedding_id", "match_result_id"):
        event = _event()
        event[key] = None
        event["payload"][key] = None
        try:
            builder.validate_c2_7_output(
                summary=_summary(),
                event=event,
                db_row=_db_row(event),
                repository_response=_repository_response(event),
                api_response=_api_response(event),
            )
        except RuntimeError as exc:
            assert f"event_missing_{key}" in str(exc)
        else:
            raise AssertionError(f"missing {key} should fail C2.7")


def test_persisted_event_payload_must_not_contain_embedding() -> None:
    builder = _load_builder()
    for key in ("embedding", "embeddings", "embedding_vector", "embedding_list"):
        event = _event()
        event["payload"][key] = [0.1, 0.2]
        try:
            builder.assert_no_forbidden_persisted_payload(event)
        except RuntimeError as exc:
            assert "persisted_event_payload_contains_forbidden_keys" in str(exc)
            assert key in str(exc)
        else:
            raise AssertionError(f"forbidden key {key} should fail")


def test_persisted_event_payload_must_not_contain_image_base64_or_crop() -> None:
    builder = _load_builder()
    forbidden = {
        "image_bytes": "abc",
        "crop_bytes": "abc",
        "base64": "abc",
        "frame_bytes": "abc",
        "image_url_like": "data:image/jpeg;base64,abc",
    }
    for key, value in forbidden.items():
        event = _event()
        event["payload"][key] = value
        try:
            builder.assert_no_forbidden_persisted_payload(event)
        except RuntimeError as exc:
            assert "persisted_event_payload_contains_forbidden_keys" in str(exc)
        else:
            raise AssertionError(f"forbidden payload value {key} should fail")


def test_evidence_bundle_path_is_required() -> None:
    builder = _load_builder()
    event = _event()
    event["payload"]["evidence"]["bundle_path"] = ""

    try:
        builder.validate_c2_7_output(
            summary=_summary(),
            event=event,
            db_row=_db_row(event),
            repository_response=_repository_response(event),
            api_response=_api_response(event),
        )
    except RuntimeError as exc:
        assert "evidence_bundle_path_missing" in str(exc)
    else:
        raise AssertionError("missing evidence bundle path should fail")


def test_source_event_id_idempotency_prevents_duplicate_rows() -> None:
    builder = _load_builder()
    summary = _summary()
    summary["duplicate_count_after_replay"] = 2
    summary["idempotency_verified"] = False

    try:
        builder.validate_c2_7_output(
            summary=summary,
            event=_event(),
            db_row=_db_row(_event()),
            repository_response=_repository_response(_event()),
            api_response=_api_response(_event()),
        )
    except RuntimeError as exc:
        assert "idempotency_not_verified" in str(exc)
    else:
        raise AssertionError("duplicate row count should fail C2.7")


def test_repository_and_api_response_preserve_watchlist_hit() -> None:
    builder = _load_builder()
    event = _event()
    builder.validate_c2_7_output(
        summary=_summary(),
        event=event,
        db_row=_db_row(event),
        repository_response=_repository_response(event),
        api_response=_api_response(event),
    )

    assert _repository_response(event)["row"]["event_type"] == "watchlist_hit"
    assert _api_response(event)["json"]["data"]["event_type"] == "watchlist_hit"


def test_repository_api_response_preserves_workaround_and_track_warning() -> None:
    builder = _load_builder()
    event = _event()
    builder.validate_c2_7_output(
        summary=_summary(),
        event=event,
        db_row=_db_row(event),
        repository_response=_repository_response(event),
        api_response=_api_response(event),
    )

    payload = _api_response(event)["json"]["data"]["payload"]
    assert payload["evidence_capture_mode"] == "stable_post_savant_sink_time_crop"
    assert payload["event_style_replay_job_passed"] is False
    assert payload["track_id_join_warning"] is True


def test_fallback_and_legacy_flags_not_enabled() -> None:
    builder = _load_builder()
    summary = _summary()
    summary["fallback_used"] = True

    try:
        builder.validate_c2_7_output(
            summary=summary,
            event=_event(),
            db_row=_db_row(_event()),
            repository_response=_repository_response(_event()),
            api_response=_api_response(_event()),
        )
    except RuntimeError as exc:
        assert "summary_fallback_used" in str(exc)
    else:
        raise AssertionError("fallback_used=true should fail")


def test_empty_forbidden_key_generator_is_false_after_materialization() -> None:
    builder = _load_builder()
    payload = {"observation": {"landmarks": [1.0, 2.0], "face_bbox": [1, 2, 3, 4]}}

    assert list(builder._find_forbidden_keys(payload, only={"embedding"})) == []
    assert bool(list(builder._find_forbidden_keys(payload, only={"embedding"}))) is False


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location("build_c2_7_persist_watchlist_event", BUILDER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _event() -> dict[str, Any]:
    evidence = {
        "bundle_path": "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035",
        "audit_path": "/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035",
        "capture_mode": "stable_post_savant_sink_time_crop",
        "workaround_used": True,
        "event_style_replay_job_passed": False,
    }
    return {
        "schema_version": "1.0",
        "event_type": "watchlist_hit",
        "source_event_id": "c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:test",
        "producer": "face-worker-one-message-consumer",
        "camera_id": "c2_post_savant_fps_probe",
        "source_id": "c2_post_savant_fps_probe",
        "track_id": "4",
        "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
        "person_id": 4,
        "external_person_id": "test:c2_4:person",
        "gallery_embedding_id": 3,
        "match_result_id": 6,
        "similarity": 1.0,
        "threshold": 0.99,
        "watchlist_rule_id": "c2_6r_test_watchlist_rule",
        "event_ts_ms": 17_854,
        "start_ts_ms": 17_854,
        "end_ts_ms": 17_854,
        "frame_pts": 17_854_288_888,
        "frame_num": 200,
        "severity": "high",
        "evidence": evidence,
        "payload": {
            "identity_source": "face_worker_pgvector_match",
            "watchlist_match_source": "face_worker_consumer",
            "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
            "person_id": 4,
            "external_person_id": "test:c2_4:person",
            "gallery_embedding_id": 3,
            "match_result_id": 6,
            "similarity": 1.0,
            "threshold": 0.99,
            "watchlist_rule_id": "c2_6r_test_watchlist_rule",
            "embedding_included": False,
            "image_bytes_included": False,
            "crop_bytes_included": False,
            "primary_identity_join_key": "source_observation_id",
            "track_id_join_warning": True,
            "evidence": evidence,
            "evidence_bundle_path": evidence["bundle_path"],
            "evidence_capture_mode": "stable_post_savant_sink_time_crop",
            "workaround_used": True,
            "event_style_replay_job_passed": False,
            "fallback_used": False,
            "legacy_used_for_visual_binding": False,
            "allow_db_annotation_fallback": False,
            "allow_legacy_annotation_fallback": False,
        },
    }


def _db_row(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee7",
        "source_event_id": event.get("source_event_id"),
        "event_type": event.get("event_type"),
        "camera_id": event.get("camera_id"),
        "source_id": event.get("source_id"),
        "track_id": str(event.get("track_id") or ""),
        "person_id": event.get("person_id"),
        "status": "new",
        "payload": event.get("payload") or {},
    }


def _repository_response(event: dict[str, Any]) -> dict[str, Any]:
    return {"verified": True, "row": _db_row(event)}


def _api_response(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "verified": True,
        "status_code": 200,
        "json": {
            "data": {
                "source_event_id": event.get("source_event_id"),
                "event_type": event.get("event_type"),
                "payload": event.get("payload") or {},
            },
            "error": None,
            "request_id": "test",
        },
    }


def _summary() -> dict[str, Any]:
    event = _event()
    return {
        "result_marker": "PASS_C2_7_EVENT_WORKER_PERSISTENCE_API_QUERY_READY",
        "event_worker_persistence_verified": True,
        "redis_event_stream_verified": True,
        "event_worker_one_message_verified": True,
        "repository_query_verified": True,
        "api_query_verified": True,
        "live_api_runtime_verified": False,
        "source_event_id": event["source_event_id"],
        "event_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee7",
        "event_type": "watchlist_hit",
        "source_observation_id": event["source_observation_id"],
        "person_id": 4,
        "gallery_embedding_id": 3,
        "match_result_id": 6,
        "watchlist_rule_id": "c2_6r_test_watchlist_rule",
        "payload_has_embedding": False,
        "payload_has_image_bytes": False,
        "evidence_capture_mode": "stable_post_savant_sink_time_crop",
        "workaround_used": True,
        "event_style_replay_job_passed": False,
        "track_id_join_warning": True,
        "primary_identity_join_key": "source_observation_id",
        "fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "allow_db_annotation_fallback": False,
        "allow_legacy_annotation_fallback": False,
        "duplicate_replay_attempted": True,
        "duplicate_count_after_replay": 1,
        "idempotency_verified": True,
    }
