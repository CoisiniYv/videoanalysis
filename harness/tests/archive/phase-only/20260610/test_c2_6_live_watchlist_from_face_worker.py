"""C2.6 live watchlist-from-face-worker harness contract tests."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BUILDER_PATH = ROOT / "scripts" / "tools" / "build_c2_live_watchlist_evidence_bundle.py"


def test_live_watchlist_event_requires_source_observation_id() -> None:
    builder = _load_builder()
    event = _live_event(builder)
    event["source_observation_id"] = ""

    try:
        builder.validate_c2_6_output(
            summary=_summary(),
            live_event=event,
            c2_6_summary=_c2_6_summary(builder),
        )
    except RuntimeError as exc:
        assert "live_event_missing_source_observation_id" in str(exc)
    else:
        raise AssertionError("missing source_observation_id should fail")


def test_event_producer_must_be_face_worker_or_c2_6_harness() -> None:
    builder = _load_builder()
    event = _live_event(builder)
    event["producer"] = "offline_fake_tool"

    try:
        builder.validate_c2_6_output(
            summary=_summary(),
            live_event=event,
            c2_6_summary=_c2_6_summary(builder),
        )
    except RuntimeError as exc:
        assert "live_event_producer_invalid" in str(exc)
    else:
        raise AssertionError("invalid producer should fail")


def test_event_payload_must_not_include_embedding_or_image_bytes() -> None:
    builder = _load_builder()
    for key in ("embedding", "embedding_vector", "image_bytes", "base64", "crop_bytes"):
        event = _live_event(builder)
        event["payload"][key] = "forbidden"
        try:
            builder.assert_no_forbidden_live_event_payload(event)
        except RuntimeError as exc:
            assert "live_watchlist_event_contains_forbidden_payload" in str(exc)
            assert key in str(exc)
        else:
            raise AssertionError(f"forbidden key {key} should fail")


def test_event_requires_person_gallery_match_and_rule_linkage() -> None:
    builder = _load_builder()
    for key in ("person_id", "gallery_embedding_id", "match_result_id", "watchlist_rule_id"):
        event = _live_event(builder)
        event[key] = None
        try:
            builder.validate_c2_6_output(
                summary=_summary(),
                live_event=event,
                c2_6_summary=_c2_6_summary(builder),
            )
        except RuntimeError as exc:
            assert f"live_event_missing_{key}" in str(exc)
        else:
            raise AssertionError(f"missing {key} should fail")


def test_evidence_summary_sets_live_face_worker_flags() -> None:
    builder = _load_builder()
    summary = _summary()

    builder.validate_c2_6_output(
        summary=summary,
        live_event=_live_event(builder),
        c2_6_summary=_c2_6_summary(builder),
    )

    assert summary["live_watchlist_from_face_worker"] is True
    assert summary["face_worker_match_verified"] is True
    assert summary["evidence_capture_mode"] == "stable_post_savant_sink_time_crop"
    assert summary["event_style_replay_job_passed"] is False
    assert summary["fallback_used"] is False
    assert summary["legacy_used_for_visual_binding"] is False


def test_known_face_count_zero_fails_c2_6() -> None:
    builder = _load_builder()
    summary = _summary()
    summary["known_face_count"] = 0
    summary["object_counts"]["known_face"] = 0

    try:
        builder.validate_c2_6_output(
            summary=summary,
            live_event=_live_event(builder),
            c2_6_summary=_c2_6_summary(builder),
        )
    except RuntimeError as exc:
        assert "known_face_count_positive_required" in str(exc)
    else:
        raise AssertionError("known_face_count=0 should fail C2.6")


def test_c2_6_sidecar_patch_preserves_geometry() -> None:
    builder = _load_builder()
    rows = [_sidecar_row()]
    binding = _binding(builder.c25)
    original = builder.c25._geometry_snapshot(rows[0]["objects"][0])

    patched = builder.patch_c2_6_sidecar_fields(
        rows,
        binding=binding,
        live_event=_live_event(builder),
        watchlist_rule_id="c2_6_test_watchlist_rule",
        watchlist_rule_name="C2.6 Test Watchlist Rule",
        severity="high",
    )

    assert builder.c25._geometry_snapshot(patched[0]["objects"][0]) == original
    assert patched[0]["objects"][0]["identity"]["identity_source"] == "face_worker_pgvector_match"
    assert patched[0]["objects"][0]["identity"]["match_result_id"] == 13


def test_file_contract_can_pass_without_live_viewer_or_redis_loop(tmp_path: Path) -> None:
    builder = _load_builder()
    input_bundle = _make_input_bundle(tmp_path)
    output_bundle = tmp_path / "c2_6"
    shutil.copytree(input_bundle, output_bundle)
    event = _live_event(builder)
    summary = _summary()
    c2_6_summary = _c2_6_summary(builder)
    builder.validate_c2_6_output(
        summary=summary,
        live_event=event,
        c2_6_summary=c2_6_summary,
    )

    (output_bundle / "live_watchlist_event.json").write_text(json.dumps(event), encoding="utf-8")
    (output_bundle / "c2_6_live_watchlist_summary.json").write_text(json.dumps(c2_6_summary), encoding="utf-8")

    assert (output_bundle / "live_watchlist_event.json").is_file()
    assert (output_bundle / "c2_6_live_watchlist_summary.json").is_file()
    assert event["face_worker_consumer_loop_exercised"] is False


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location("build_c2_live_watchlist_evidence_bundle", BUILDER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _binding(c25: Any) -> Any:
    return c25.KnownFaceBinding(
        row_index=0,
        object_index=0,
        frame_index=194,
        frame_pts=17_854_288_888,
        track_id="1",
        object_id="889031753",
        face_bbox=[699.7688903808594, 478.4892578125, 831.0499572753906, 637.2005615234375],
        source_observation_id="face:c2_post_savant_fps_probe:4:17854:1",
        person_id=4,
        external_person_id="test:c2_4:person",
        person_name="C2.4 Test Person",
        gallery_embedding_id=3,
        match_result_id=3,
        similarity=1.0,
        threshold=0.99,
        join_method="frame_pts_bbox_iou_unique",
    )


def _live_event(builder: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_type": "watchlist_hit",
        "source_event_id": "c2_6:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4",
        "producer": "c2_6_face_worker_harness",
        "camera_id": "c2_post_savant_fps_probe",
        "source_id": "c2_post_savant_fps_probe",
        "track_id": "1",
        "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
        "person_id": 4,
        "external_person_id": "test:c2_4:person",
        "gallery_embedding_id": 3,
        "match_result_id": 13,
        "similarity": 1.0,
        "threshold": 0.99,
        "watchlist_rule_id": "c2_6_test_watchlist_rule",
        "watchlist_rule_name": "C2.6 Test Watchlist Rule",
        "event_ts_ms": 17854,
        "frame_pts": 17_854_288_888,
        "frame_num": 194,
        "face_worker_execution_mode": "shared_matching_harness",
        "face_worker_consumer_loop_exercised": False,
        "redis_event_published": False,
        "payload": {
            "identity_source": "face_worker_pgvector_match",
            "watchlist_match_source": "face_worker_shared_matching_logic",
            "embedding_included": False,
            "image_bytes_included": False,
            "crop_bytes_included": False,
            "evidence_capture_mode": "stable_post_savant_sink_time_crop",
            "workaround_used": True,
            "event_style_replay_job_passed": False,
            "geometry_modified": False,
            "join_method": "frame_pts_bbox_iou_unique",
        },
    }


def _c2_6_summary(builder: Any) -> dict[str, Any]:
    return {
        "result_marker": builder.RESULT_HARNESS_ONLY,
        "event_type": "watchlist_hit",
        "source_event_id": "c2_6:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4",
        "producer": "c2_6_face_worker_harness",
        "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
    }


def _summary() -> dict[str, Any]:
    return {
        "event_type": "watchlist_hit",
        "watchlist_hit_count": 1,
        "known_face_count": 1,
        "unknown_face_count": 0,
        "object_counts": {"person": 0, "face": 0, "known_face": 1},
        "live_watchlist_from_face_worker": True,
        "face_worker_match_verified": True,
        "identity_binding_connected": True,
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


def _sidecar_row() -> dict[str, Any]:
    return {
        "frame_index": 194,
        "frame_pts": 17_854_288_888,
        "objects": [
            {
                "object_type": "known_face",
                "track_id": "1",
                "object_id": "889031753",
                "label": {"kind": "known_face"},
                "bbox": {
                    "format": "xyxy",
                    "xyxy": [699.7688903808594, 478.4892578125, 831.0499572753906, 637.2005615234375],
                    "confidence": 0.7833261489868164,
                },
                "landmarks": {
                    "format": "5_point",
                    "points": [
                        [730.6875, 543.67822265625],
                        [795.1640625, 540.0758056640625],
                        [767.296875, 573.328125],
                        [746.75390625, 603.28125],
                        [795.7265625, 600.380859375],
                    ],
                },
                "identity": {
                    "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
                    "person_id": 4,
                    "external_person_id": "test:c2_4:person",
                    "gallery_embedding_id": 3,
                    "match_result_id": 3,
                    "similarity": 1.0,
                    "threshold": 0.99,
                    "join_method": "frame_pts_bbox_iou_unique",
                },
            }
        ],
    }


def _make_input_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "c2_5"
    bundle.mkdir()
    (bundle / "raw_clip.mov").write_bytes(b"fake video")
    (bundle / "sink_metadata.json").write_text("[]\n", encoding="utf-8")
    (bundle / "summary.json").write_text(json.dumps(_summary()), encoding="utf-8")
    (bundle / "summary.frame_cache.identity.json").write_text(json.dumps(_summary()), encoding="utf-8")
    (bundle / "identity_patches.jsonl").write_text(
        json.dumps({"source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1"}) + "\n",
        encoding="utf-8",
    )
    (bundle / "annotations.frame_cache.identity.jsonl").write_text(json.dumps(_sidecar_row()) + "\n", encoding="utf-8")
    return bundle
