"""C2.4 identity patch contract tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BUILDER_PATH = ROOT / "scripts" / "tools" / "build_c2_identity_patched_evidence_bundle.py"


def test_identity_patch_schema_and_summary_fields() -> None:
    builder = _load_builder()
    binding = _binding(builder)
    rows = [_sidecar_row()]

    patched_rows, patch = builder.patch_identity_rows(rows, binding)
    summary = builder.patch_summary(_summary(), patched_rows, binding, patch)

    target = patched_rows[0]["objects"][0]
    assert patch["message_type"] == "identity_patch"
    assert patch["source_observation_id"] == "face:c2_post_savant_fps_probe:4:17854:1"
    assert patch["person_id"] == 42
    assert patch["gallery_embedding_id"] == 84
    assert patch["match_result_id"] == 168
    assert patch["identity_source"] == "match_results"
    assert patch["geometry_modified"] is False
    assert patch["join_key"]["bbox_iou"] == 1.0

    assert target["object_type"] == "known_face"
    assert target["label"]["kind"] == "known_face"
    assert target["identity"]["status"] == "matched"
    assert target["identity"]["match_status"] == "above_threshold"
    assert target["identity"]["source_observation_id"] == patch["source_observation_id"]
    assert target["identity"]["recognition_claim_allowed"] is True

    assert summary["identity_binding_connected"] is True
    assert summary["identity_patch_source"] == "match_results"
    assert summary["recognition_claim_allowed"] is True
    assert summary["known_face_count"] == 1
    assert summary["unknown_face_count"] == 0
    assert summary["object_counts"] == {"person": 0, "face": 0, "known_face": 1}
    assert summary["fallback_used"] is False
    assert summary["legacy_used_for_visual_binding"] is False
    assert summary["allow_db_annotation_fallback"] is False
    assert summary["allow_legacy_annotation_fallback"] is False
    assert summary["evidence_capture_mode"] == "stable_post_savant_sink_time_crop"
    assert summary["workaround_used"] is True
    assert summary["event_style_replay_job_passed"] is False


def test_identity_patch_preserves_geometry() -> None:
    builder = _load_builder()
    rows = [_sidecar_row()]
    original = builder._geometry_snapshot(rows[0]["objects"][0])

    patched_rows, _patch = builder.patch_identity_rows(rows, _binding(builder))
    patched = builder._geometry_snapshot(patched_rows[0]["objects"][0])

    assert patched == original


def test_join_requires_unique_bbox_and_pts_match() -> None:
    builder = _load_builder()
    face = builder.SidecarFace(
        frame_index=194,
        frame_pts=17_854_288_888,
        source_id="c2-fps-probe-20260607T140420",
        sidecar_track_id="1",
        object_id="889031753",
        bbox_xyxy=[699.7689, 478.4893, 831.0499, 637.2001],
        confidence=0.78,
        row_index=0,
        object_index=0,
    )
    observation = builder.RedisObservation(
        data={},
        stream_id="1-0",
        source_observation_id="face:c2_post_savant_fps_probe:4:17854:1",
        redis_source_id="c2_post_savant_fps_probe",
        redis_track_id="4",
        redis_frame_pts=17_854_288_888,
        redis_timestamp_ms=17854,
        redis_frame_num=200,
        bbox_cxcywh=[765.409423828125, 557.8449096679688, 131.28106689453125, 158.7113037109375],
        face_confidence=0.78,
        quality=1.0,
    )

    candidate = builder.select_join_candidate(
        sidecar_faces=[face],
        observations=[observation],
        iou_threshold=0.99,
        pts_tolerance_ns=2_000_000,
    )

    assert candidate is not None
    assert candidate.observation.source_observation_id == observation.source_observation_id
    assert candidate.bbox_iou > 0.99
    assert candidate.frame_pts_delta_ns == 0


def test_join_missing_when_no_reliable_key() -> None:
    builder = _load_builder()
    face = builder.SidecarFace(
        frame_index=0,
        frame_pts=1_000_000_000,
        source_id="c2",
        sidecar_track_id="1",
        object_id="face-1",
        bbox_xyxy=[0, 0, 10, 10],
        confidence=0.9,
        row_index=0,
        object_index=0,
    )
    observation = builder.RedisObservation(
        data={},
        stream_id="1-0",
        source_observation_id="face:missing",
        redis_source_id="c2",
        redis_track_id="1",
        redis_frame_pts=1_000_000_000,
        redis_timestamp_ms=1000,
        redis_frame_num=1,
        bbox_cxcywh=[100, 100, 10, 10],
        face_confidence=0.9,
        quality=1.0,
    )

    candidate = builder.select_join_candidate(
        sidecar_faces=[face],
        observations=[observation],
        iou_threshold=0.99,
        pts_tolerance_ns=2_000_000,
    )

    assert candidate is None


def test_summary_does_not_allow_recognition_without_known_face() -> None:
    builder = _load_builder()
    summary = builder.patch_summary(_summary(), [_sidecar_row()], _binding(builder), builder.build_identity_patch(_binding(builder)))

    assert summary["known_face_count"] == 0
    assert summary["recognition_claim_allowed"] is False


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location("build_c2_identity_patched_evidence_bundle", BUILDER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _binding(builder: Any) -> Any:
    observation = builder.RedisObservation(
        data={},
        stream_id="1780836560000-0",
        source_observation_id="face:c2_post_savant_fps_probe:4:17854:1",
        redis_source_id="c2_post_savant_fps_probe",
        redis_track_id="4",
        redis_frame_pts=17_854_288_888,
        redis_timestamp_ms=17854,
        redis_frame_num=200,
        bbox_cxcywh=[765.409423828125, 557.8449096679688, 131.28106689453125, 158.7113037109375],
        face_confidence=0.7833,
        quality=1.0,
    )
    face = builder.SidecarFace(
        frame_index=194,
        frame_pts=17_854_288_888,
        source_id="c2-fps-probe-20260607T140420",
        sidecar_track_id="1",
        object_id="889031753",
        bbox_xyxy=[699.7688903808594, 478.4892578125, 831.0499572753906, 637.2000732421875],
        confidence=0.7833,
        row_index=0,
        object_index=0,
    )
    candidate = builder.JoinCandidate(
        face=face,
        observation=observation,
        bbox_iou=1.0,
        frame_pts_delta_ns=0,
        timestamp_delta_ms=0.288888,
    )
    return builder.IdentityBinding(
        candidate=candidate,
        observation_uuid="00000000-0000-4000-8000-000000000001",
        person_id=42,
        person_name="C2.4 Test Person",
        external_person_id="test:c2_4:person",
        gallery_embedding_id=84,
        match_result_id=168,
        search_request_id="c2400000-0000-4000-8000-000000000001",
        similarity=1.0,
        threshold=0.99,
    )


def _sidecar_row() -> dict[str, Any]:
    return {
        "schema_version": "2.0-c2-post-savant",
        "annotation_source": "post_savant_sink_metadata",
        "production_ready": True,
        "displayable": True,
        "frame_index": 194,
        "clip_frame_index": 194,
        "frame_pts": 17_854_288_888,
        "source_id": "c2-fps-probe-20260607T140420",
        "width": 1920,
        "height": 1080,
        "objects": [
            {
                "object_type": "face",
                "track_id": "1",
                "object_id": "889031753",
                "label": {"kind": "unknown_face"},
                "bbox": {
                    "format": "xyxy",
                    "coordinate_space": "pixel",
                    "xyxy": [699.7688903808594, 478.4892578125, 831.0499572753906, 637.2000732421875],
                    "confidence": 0.7833261489868164,
                    "source": "detection_box",
                },
                "landmarks": {
                    "format": "5_point",
                    "coordinate_space": "pixel",
                    "source": "native_metadata",
                    "points": [[730.6875, 543.6782], [795.1641, 540.0758], [767.2969, 573.3281], [746.7539, 603.2812], [795.7266, 600.3809]],
                },
                "identity": {
                    "source_observation_id": None,
                    "visual_evidence_status": "observation_only",
                    "status": "unknown",
                    "match_status": "not_searched",
                },
            }
        ],
    }


def _summary() -> dict[str, Any]:
    return {
        "schema_version": "2.0-c2",
        "annotation_source": "post_savant_sink_metadata",
        "annotation_source_kind": "production_sidecar",
        "sidecar_type": "production",
        "production_ready": True,
        "timeline_domain": "final_canonical_clip",
        "decoded_video_frame_count": 1,
        "original_metadata_frame_count": 1,
        "sidecar_frame_count": 1,
        "trim_occurred": False,
        "fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "allow_db_annotation_fallback": False,
        "allow_legacy_annotation_fallback": False,
        "object_counts": {"person": 0, "face": 1, "known_face": 0},
        "evidence_capture_mode": "stable_post_savant_sink_time_crop",
        "workaround_used": True,
        "event_style_replay_job_passed": False,
        "video_integrity": {"production_gate_passed": True, "integrity_status": "pass"},
    }
