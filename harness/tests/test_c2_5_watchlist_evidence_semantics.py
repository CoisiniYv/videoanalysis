"""C2.5 watchlist evidence semantics contract tests."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BUILDER_PATH = ROOT / "scripts" / "tools" / "build_c2_watchlist_evidence_bundle.py"


def test_watchlist_event_requires_source_observation_id() -> None:
    builder = _load_builder()
    event = _watchlist_event(builder)
    event["source_observation_id"] = ""

    try:
        builder._validate_output(_summary(), event, _watchlist_summary(builder))
    except RuntimeError as exc:
        assert "watchlist_event_missing_source_observation_id" in str(exc)
    else:
        raise AssertionError("missing source_observation_id should fail")


def test_watchlist_event_requires_person_gallery_and_match() -> None:
    builder = _load_builder()
    for key in ("person_id", "gallery_embedding_id", "match_result_id"):
        event = _watchlist_event(builder)
        event[key] = None
        try:
            builder._validate_output(_summary(), event, _watchlist_summary(builder))
        except RuntimeError as exc:
            assert f"watchlist_event_missing_{key}" in str(exc)
        else:
            raise AssertionError(f"missing {key} should fail")


def test_event_payload_must_not_include_embedding_or_image_bytes() -> None:
    builder = _load_builder()
    for key in ("embedding", "image_bytes", "base64", "crop_bytes"):
        event = _watchlist_event(builder)
        event["payload"][key] = "forbidden"
        try:
            builder.assert_no_forbidden_event_payload(event)
        except RuntimeError as exc:
            assert "watchlist_event_contains_forbidden_payload" in str(exc)
            assert key in str(exc)
        else:
            raise AssertionError(f"forbidden payload key {key} should fail")


def test_watchlist_sidecar_patch_preserves_geometry() -> None:
    builder = _load_builder()
    rows = [_sidecar_row()]
    binding = _binding(builder)
    original = builder._geometry_snapshot(rows[0]["objects"][0])

    patched = builder.patch_sidecar_watchlist_fields(
        rows,
        binding=binding,
        watchlist_event=_watchlist_event(builder),
        watchlist_rule_id="c2_5_test_watchlist_rule",
        watchlist_rule_name="C2.5 Test Watchlist Rule",
        severity="high",
    )

    assert builder._geometry_snapshot(patched[0]["objects"][0]) == original


def test_sidecar_known_face_has_watchlist_identity_fields() -> None:
    builder = _load_builder()
    patched = builder.patch_sidecar_watchlist_fields(
        [_sidecar_row()],
        binding=_binding(builder),
        watchlist_event=_watchlist_event(builder),
        watchlist_rule_id="c2_5_test_watchlist_rule",
        watchlist_rule_name="C2.5 Test Watchlist Rule",
        severity="high",
    )
    obj = patched[0]["objects"][0]

    assert obj["object_type"] == "known_face"
    assert obj["label"]["kind"] == "known_face"
    assert obj["label"]["event_type"] == "watchlist_hit"
    assert obj["identity"]["event_type"] == "watchlist_hit"
    assert obj["identity"]["person_id"] == 4
    assert obj["identity"]["external_person_id"] == "test:c2_4:person"
    assert obj["identity"]["similarity"] == 1.0
    assert obj["identity"]["threshold"] == 0.99
    assert obj["identity"]["watchlist_rule_id"] == "c2_5_test_watchlist_rule"
    assert obj["identity"]["identity_source"] == "match_results"
    assert obj["identity"]["source_observation_id"] == "face:c2_post_savant_fps_probe:4:17854:1"


def test_summary_has_watchlist_hit_count_and_preserves_workaround_flags() -> None:
    builder = _load_builder()
    patched = builder.patch_sidecar_watchlist_fields(
        [_sidecar_row()],
        binding=_binding(builder),
        watchlist_event=_watchlist_event(builder),
        watchlist_rule_id="c2_5_test_watchlist_rule",
        watchlist_rule_name="C2.5 Test Watchlist Rule",
        severity="high",
    )
    summary = builder.patch_summary(
        _summary(),
        rows=patched,
        binding=_binding(builder),
        watchlist_event=_watchlist_event(builder),
        watchlist_rule_id="c2_5_test_watchlist_rule",
        watchlist_rule_name="C2.5 Test Watchlist Rule",
        severity="high",
    )

    assert summary["watchlist_hit_count"] == 1
    assert summary["known_face_count"] == 1
    assert summary["evidence_capture_mode"] == "stable_post_savant_sink_time_crop"
    assert summary["workaround_used"] is True
    assert summary["event_style_replay_job_passed"] is False
    assert summary["fallback_used"] is False
    assert summary["legacy_used_for_visual_binding"] is False


def test_known_face_count_zero_fails_c2_5_input() -> None:
    builder = _load_builder()
    summary = _summary()
    summary["known_face_count"] = 0
    summary["object_counts"]["known_face"] = 0

    try:
        builder._validate_input_summary(summary)
    except RuntimeError as exc:
        assert "known_face_count_positive_required" in str(exc)
    else:
        raise AssertionError("known_face_count=0 should fail C2.5")


def test_viewer_502_does_not_fail_contract_when_files_exist(tmp_path: Path) -> None:
    builder = _load_builder()
    input_bundle = _make_input_bundle(tmp_path)
    output_bundle = tmp_path / "c2_5"

    result = builder.build_watchlist_evidence_bundle(
        input_bundle=input_bundle,
        output_dir=output_bundle,
        person_id=4,
        external_person_id="test:c2_4:person",
        source_observation_id="face:c2_post_savant_fps_probe:4:17854:1",
        watchlist_rule_id="c2_5_test_watchlist_rule",
        threshold=0.99,
    )

    assert result.result_marker == builder.RESULT_PASS
    assert (output_bundle / "watchlist_event.json").is_file()
    assert (output_bundle / "watchlist_evidence_summary.json").is_file()
    assert (output_bundle / "watchlist_evidence_report.html").is_file()


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location("build_c2_watchlist_evidence_bundle", BUILDER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _binding(builder: Any) -> Any:
    return builder.KnownFaceBinding(
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


def _watchlist_event(builder: Any) -> dict[str, Any]:
    return builder.build_watchlist_event(
        binding=_binding(builder),
        input_bundle=Path("/data/video-analytics/media/evidence/c2_4_identity_binding_20260607T211846"),
        output_bundle=Path("/data/video-analytics/media/evidence/c2_5_watchlist_hit_test"),
        source_event_id="c2_5:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4",
        watchlist_rule_id="c2_5_test_watchlist_rule",
        watchlist_rule_name="C2.5 Test Watchlist Rule",
        person_name="C2.5 Test Watchlist Person",
        threshold=0.99,
        severity="high",
        camera_id="c2_post_savant_fps_probe",
        source_id="c2_post_savant_fps_probe",
    )


def _watchlist_summary(builder: Any) -> dict[str, Any]:
    return {
        "result_marker": builder.RESULT_PASS,
        "event_type": "watchlist_hit",
        "source_event_id": "c2_5:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4",
        "watchlist_rule_id": "c2_5_test_watchlist_rule",
    }


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
                "object_type": "known_face",
                "track_id": "1",
                "object_id": "889031753",
                "label": {
                    "kind": "known_face",
                    "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
                    "person_id": 4,
                    "external_person_id": "test:c2_4:person",
                    "gallery_embedding_id": 3,
                    "match_result_id": 3,
                    "similarity": 1.0,
                    "threshold": 0.99,
                },
                "bbox": {
                    "format": "xyxy",
                    "coordinate_space": "pixel",
                    "xyxy": [699.7688903808594, 478.4892578125, 831.0499572753906, 637.2005615234375],
                    "confidence": 0.7833261489868164,
                    "source": "detection_box",
                },
                "landmarks": {
                    "format": "5_point",
                    "coordinate_space": "pixel",
                    "source": "native_metadata",
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
                    "person_name": "C2.4 Test Person",
                    "gallery_embedding_id": 3,
                    "match_result_id": 3,
                    "similarity": 1.0,
                    "threshold": 0.99,
                    "identity_source": "match_results",
                    "identity_binding_status": "matched",
                    "join_method": "frame_pts_bbox_iou_unique",
                    "status": "matched",
                    "match_status": "above_threshold",
                    "recognition_claim_allowed": True,
                },
            }
        ],
    }


def _summary() -> dict[str, Any]:
    return {
        "schema_version": "2.0-c2",
        "annotation_source": "sidecar",
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
        "object_counts": {"person": 0, "face": 0, "known_face": 1},
        "known_face_count": 1,
        "unknown_face_count": 0,
        "evidence_topology": "post_savant",
        "evidence_capture_mode": "stable_post_savant_sink_time_crop",
        "workaround_used": True,
        "event_style_replay_job_passed": False,
        "identity_binding_connected": True,
        "identity_patch_source": "match_results",
        "recognition_claim_allowed": True,
        "video_integrity": {"production_gate_passed": True, "integrity_status": "pass"},
    }


def _make_input_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "c2_4"
    bundle.mkdir()
    (bundle / "raw_clip.mov").write_bytes(b"fake video")
    (bundle / "sink_metadata.json").write_text("[]\n", encoding="utf-8")
    (bundle / "summary.json").write_text(json.dumps(_summary()), encoding="utf-8")
    (bundle / "summary.frame_cache.identity.json").write_text(json.dumps(_summary()), encoding="utf-8")
    (bundle / "identity_patches.jsonl").write_text(
        json.dumps(
            {
                "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
                "join_method": "frame_pts_bbox_iou_unique",
                "geometry_modified": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle / "annotations.frame_cache.identity.jsonl").write_text(json.dumps(_sidecar_row()) + "\n", encoding="utf-8")
    shutil.copyfile(bundle / "summary.json", bundle / "c2_4_identity_binding_summary.json")
    return bundle
