"""C2.11 demo package contract tests."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BUILDER_PATH = ROOT / "scripts" / "tools" / "build_c2_11_demo_package.py"


def test_demo_manifest_requires_watchlist_hit(tmp_path: Path) -> None:
    builder = _load_builder()
    manifest = _manifest(tmp_path)
    builder.validate_demo_manifest(manifest)

    bad = copy.deepcopy(manifest)
    bad["event"]["event_type"] = "intrusion"
    try:
        builder.validate_demo_manifest(bad)
    except RuntimeError as exc:
        assert "watchlist_hit" in str(exc)
    else:
        raise AssertionError("non-watchlist event should fail")


def test_demo_manifest_requires_person_and_external_person_id() -> None:
    builder = _load_builder()
    manifest = _manifest()

    for key in ("person_id", "external_person_id"):
        bad = copy.deepcopy(manifest)
        bad["event"][key] = None
        try:
            builder.validate_demo_manifest(bad)
        except RuntimeError as exc:
            assert key in str(exc)
        else:
            raise AssertionError(f"missing {key} should fail")


def test_demo_manifest_requires_source_observation_id() -> None:
    builder = _load_builder()
    manifest = _manifest()
    manifest["event"]["source_observation_id"] = ""

    try:
        builder.validate_demo_manifest(manifest)
    except RuntimeError as exc:
        assert "source_observation_id" in str(exc)
    else:
        raise AssertionError("missing source_observation_id should fail")


def test_demo_manifest_requires_evidence_bundle_path() -> None:
    builder = _load_builder()
    manifest = _manifest()
    manifest["evidence"]["bundle_path"] = ""

    try:
        builder.validate_demo_manifest(manifest)
    except RuntimeError as exc:
        assert "bundle_path" in str(exc)
    else:
        raise AssertionError("missing bundle path should fail")


def test_demo_manifest_requires_workaround_flags(tmp_path: Path) -> None:
    builder = _load_builder()
    manifest = _manifest(tmp_path)
    builder.validate_demo_manifest(manifest)

    bad = copy.deepcopy(manifest)
    bad["evidence"]["capture_mode"] = "event_style_replay"
    try:
        builder.validate_demo_manifest(bad)
    except RuntimeError as exc:
        assert "capture_mode" in str(exc)
    else:
        raise AssertionError("bad capture mode should fail")


def test_demo_manifest_requires_event_style_replay_not_passed_limitation(tmp_path: Path) -> None:
    builder = _load_builder()
    manifest = _manifest(tmp_path)
    manifest["limitations"] = [item for item in manifest["limitations"] if item != "event_style_replay_not_passed"]

    try:
        builder.validate_demo_manifest(manifest)
    except RuntimeError as exc:
        assert "event_style_replay_not_passed" in str(exc)
    else:
        raise AssertionError("missing replay limitation should fail")


def test_unsafe_scan_rejects_embedding_vectors() -> None:
    builder = _load_builder()
    scan = builder.scan_for_unsafe_payload({"payload": {"embedding": [0.1] * 128}})

    assert scan["passed"] is False
    assert scan["payload_has_embedding"] is True
    assert scan["embedding_key_paths"] == ["payload.embedding"]


def test_unsafe_scan_rejects_base64_image_and_crop_bytes() -> None:
    builder = _load_builder()
    scan = builder.scan_for_unsafe_payload(
        {
            "payload": {
                "image_base64": "data:image/jpeg;base64,AAAA",
                "crop_bytes": "not-safe",
                "face_crop_bytes": "not-safe",
            }
        }
    )

    assert scan["passed"] is False
    assert scan["payload_has_image_bytes"] is True
    assert "payload.crop_bytes" in scan["image_byte_key_paths"]


def test_unsafe_scan_allows_bbox_and_landmarks() -> None:
    builder = _load_builder()
    scan = builder.scan_for_unsafe_payload(
        {
            "bbox": {"xyxy": [1.0, 2.0, 3.0, 4.0]},
            "landmarks": {"points": [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]},
            "gallery_embedding_id": 3,
            "embedding_included": False,
            "image_bytes_included": False,
            "crop_bytes_included": False,
        }
    )

    assert scan["passed"] is True
    assert scan["payload_has_embedding"] is False
    assert scan["payload_has_image_bytes"] is False
    assert scan["has_suspicious_numeric_vectors"] is False


def test_index_html_includes_caveats() -> None:
    builder = _load_builder()
    html = builder.render_index_html(_manifest())

    assert "C2 Watchlist Evidence Demo" in html
    assert "watchlist_hit" in html
    assert "event_style_replay_not_passed" in html
    assert "stable_post_savant_sink_time_crop" in html
    assert "deterministic fixed sample" in html
    assert "no_broad_accuracy_proof" in html


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location("build_c2_11_demo_package", BUILDER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest(tmp_path: Path | None = None) -> dict[str, Any]:
    bundle_path = "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035"
    if tmp_path is not None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        bundle_path = str(bundle_dir)
        operator_report = tmp_path / "operator_watchlist_evidence.html"
        api_by_event = tmp_path / "api_response_by_event_id.json"
        api_by_source = tmp_path / "api_response_by_source_event_id.json"
        sidecar_sample = tmp_path / "sidecar_annotation_sample.json"
        operator_report.write_text("<html>Watchlist Hit</html>", encoding="utf-8")
        api_by_event.write_text("{}", encoding="utf-8")
        api_by_source.write_text("{}", encoding="utf-8")
        sidecar_sample.write_text("{}", encoding="utf-8")
    else:
        operator_report = Path("/tmp/operator_watchlist_evidence.html")
        api_by_event = Path("/tmp/api_response_by_event_id.json")
        api_by_source = Path("/tmp/api_response_by_source_event_id.json")
        sidecar_sample = Path("/tmp/sidecar_annotation_sample.json")
    return {
        "schema_version": "1.0",
        "demo_name": "c2_watchlist_evidence_demo",
        "result_marker": "PASS_C2_REBASELINE_DEMO_READY",
        "c2_10_result_marker": "PASS_C2_10_RUNTIME_API_VIEWER_READY",
        "package_result_marker": "PASS_C2_11_DEMO_PACKAGE_READY",
        "source_branch": "c2/post-savant-poc",
        "source_commit": "06f8b4be753e4470306e5408c3263d742fe0c43c",
        "input_artifacts": {},
        "event": {
            "event_type": "watchlist_hit",
            "event_id": "b4cf4b6b-9d90-4282-9b7f-5e0c56e81a32",
            "source_event_id": "c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:c2_7_event_worker_persistence_20260607T223424",
            "person_id": 4,
            "external_person_id": "test:c2_4:person",
            "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
            "similarity": 1.0,
            "threshold": 0.99,
            "watchlist_rule_id": "c2_6r_test_watchlist_rule",
        },
        "evidence": {
            "bundle_path": bundle_path,
            "operator_report": str(operator_report),
            "api_response_by_event_id": str(api_by_event),
            "api_response_by_source_event_id": str(api_by_source),
            "sidecar_annotation_sample": str(sidecar_sample),
            "capture_mode": "stable_post_savant_sink_time_crop",
            "workaround_used": True,
            "event_style_replay_job_passed": False,
            "known_face_count": 1,
            "watchlist_hit_count": 1,
        },
        "safety": {
            "embedding_present": False,
            "image_base64_crop_present": False,
            "fallback_used": False,
            "legacy_used_for_visual_binding": False,
        },
        "limitations": [
            "event_style_replay_not_passed",
            "stable_post_savant_sink_time_crop_workaround",
            "deterministic_fixed_sample",
            "no_broad_accuracy_proof",
            "no_long_running_soak",
        ],
        "demo_steps": ["open_operator_report"],
        "runtime_semantics_changed": False,
    }
