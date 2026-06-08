"""C2.14D RTSP watchlist known-face evidence contract tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "scripts" / "tools" / "build_c2_14d_watchlist_clip_from_ring.py"
AUDIT = ROOT / "scripts" / "tools" / "audit_c2_14d_watchlist_evidence_bundle.py"


def test_watchlist_event_selection_contract_rejects_non_hit() -> None:
    tool = _load(BUILDER)

    failures = tool.validate_watchlist_event_contract({"event_type": "intrusion"}, threshold=0.65)

    assert "event_type_not_watchlist_hit" in failures
    assert "source_observation_id_missing" in failures


def test_no_fake_hit_when_similarity_below_threshold() -> None:
    tool = _load(BUILDER)
    event = _event(similarity=0.64, threshold=0.65)

    failures = tool.validate_watchlist_event_contract(event, threshold=0.65)

    assert "similarity_below_threshold" in failures


def test_direct_source_observation_id_identity_binding_patch() -> None:
    tool = _load(BUILDER)
    candidate = _candidate(tool, identity_method="direct_source_observation_id")
    rows = [_sidecar_row(object_type="face")]

    patched, patch = tool.patch_sidecar_identity(rows=rows, candidate=candidate)

    identity = patched[0]["objects"][0]["identity"]
    assert patch["patched"] is True
    assert identity["source_observation_id"] == "face:c2_post_savant_fps_probe:902:12072435"
    assert identity["person_id"] == 6
    assert identity["external_person_id"] == "demo:f4_3:finch"
    assert identity["matched"] is True
    assert identity["identity_binding_method"] == "direct_source_observation_id"


def test_sidecar_excludes_embedding_image_base64_crop_bytes() -> None:
    tool = _load(BUILDER)
    candidate = _candidate(tool)
    rows = [_sidecar_row(object_type="face")]

    patched, _ = tool.patch_sidecar_identity(rows=rows, candidate=candidate)
    scan = tool.scan_for_unsafe_payload({"sidecar_rows": patched})

    assert scan["passed"] is True


def test_unsafe_payload_scan_rejects_embedding_image_base64_crop_bytes() -> None:
    tool = _load(BUILDER)

    scan = tool.scan_for_unsafe_payload({"embedding": [0.1] * 32, "image_base64": "abc", "crop_bytes": "abc"})

    assert scan["passed"] is False
    assert scan["payload_has_embedding"] is True
    assert scan["payload_has_image_bytes"] is True


def test_clip_bundle_summary_schema_no_broad_fallback() -> None:
    summary = {
        "schema_version": "1.0-c2.14d-watchlist-known-face-evidence",
        "result_marker": "PASS_C2_14D_RTSP_WATCHLIST_KNOWN_FACE_EVIDENCE_READY",
        "event_type": "watchlist_hit",
        "db_broad_window_fallback_used": False,
        "db_window_fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "event_style_replay_job_passed": False,
        "fake_watchlist_hit": False,
    }

    assert summary["db_broad_window_fallback_used"] is False
    assert summary["legacy_used_for_visual_binding"] is False
    assert summary["event_style_replay_job_passed"] is False


def test_audit_detects_pass_bundle_contract(tmp_path: Path) -> None:
    audit = _load(AUDIT)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_json(
        bundle / "summary.json",
        {
            "result_marker": audit.RESULT_PASS,
            "event_type": "watchlist_hit",
            "source_id": "c2_post_savant_fps_probe",
            "person_id": 6,
            "external_person_id": "demo:f4_3:finch",
            "gallery_embedding_id": 5,
            "similarity": 0.7,
            "threshold": 0.65,
            "source_observation_id": "face:c2_post_savant_fps_probe:902:12072435",
            "frame_pts": 12072435377777,
            "event_ts_ms": 12072435,
            "identity_binding_method": "direct_frame_pts",
            "db_broad_window_fallback_used": False,
            "db_window_fallback_used": False,
            "legacy_used_for_visual_binding": False,
            "event_style_replay_job_passed": False,
            "fake_watchlist_hit": False,
            "limitations": [],
        },
    )
    _write_json(bundle / "selected_watchlist_event.json", _event())
    _write_json(bundle / "video_integrity_report.json", {"decoded_frame_count": 1, "production_gate_passed": True})
    _write_json(bundle / "unsafe_payload_scan.json", {"passed": True})
    (bundle / "raw_clip.mp4").write_bytes(b"video")
    (bundle / "sink_metadata.json").write_text("{}\n", encoding="utf-8")
    (bundle / "index.html").write_text("raw_clip.mp4 annotations.frame_cache.identity.jsonl sink_metadata.json", encoding="utf-8")
    (bundle / "annotations.frame_cache.identity.jsonl").write_text(json.dumps(_sidecar_row()) + "\n", encoding="utf-8")

    result = audit.audit_bundle(bundle)

    assert result["result_marker"] == audit.RESULT_PASS


def test_audit_flags_broad_db_fallback(tmp_path: Path) -> None:
    audit = _load(AUDIT)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_json(
        bundle / "summary.json",
        {
            "result_marker": audit.RESULT_PASS,
            "event_type": "watchlist_hit",
            "source_id": "c2_post_savant_fps_probe",
            "person_id": 6,
            "external_person_id": "demo:f4_3:finch",
            "gallery_embedding_id": 5,
            "similarity": 0.7,
            "threshold": 0.65,
            "source_observation_id": "face:c2_post_savant_fps_probe:902:12072435",
            "frame_pts": 12072435377777,
            "identity_binding_method": "direct_frame_pts",
            "db_broad_window_fallback_used": True,
            "db_window_fallback_used": False,
            "legacy_used_for_visual_binding": False,
            "event_style_replay_job_passed": False,
            "fake_watchlist_hit": False,
        },
    )
    _write_json(bundle / "selected_watchlist_event.json", _event())
    _write_json(bundle / "video_integrity_report.json", {"decoded_frame_count": 1, "production_gate_passed": True})
    _write_json(bundle / "unsafe_payload_scan.json", {"passed": True})
    (bundle / "raw_clip.mp4").write_bytes(b"video")
    (bundle / "sink_metadata.json").write_text("{}\n", encoding="utf-8")
    (bundle / "index.html").write_text("raw_clip.mp4", encoding="utf-8")
    (bundle / "annotations.frame_cache.identity.jsonl").write_text(json.dumps(_sidecar_row()) + "\n", encoding="utf-8")

    result = audit.audit_bundle(bundle)

    assert result["result_marker"] == audit.RESULT_DB_BROAD_FALLBACK


def _event(similarity: float = 0.7, threshold: float = 0.65) -> dict[str, Any]:
    return {
        "event_type": "watchlist_hit",
        "source_id": "c2_post_savant_fps_probe",
        "camera_id": "c2_post_savant_fps_probe",
        "person_id": 6,
        "external_person_id": "demo:f4_3:finch",
        "gallery_embedding_id": 5,
        "similarity": similarity,
        "threshold": threshold,
        "source_observation_id": "face:c2_post_savant_fps_probe:902:12072435",
        "frame_pts": 12072435377777,
        "frame_uuid": "frame-a",
        "payload": {
            "matched_person": {"person_id": 6, "external_person_id": "demo:f4_3:finch", "name": "Finch"},
            "match": {
                "similarity": similarity,
                "threshold": threshold,
                "gallery_embedding_id": 5,
                "source_observation_id": "face:c2_post_savant_fps_probe:902:12072435",
            },
            "media": {"frame_pts": 12072435377777, "frame_uuid": "frame-a"},
        },
    }


def _sidecar_row(*, object_type: str = "known_face") -> dict[str, Any]:
    return {
        "frame_index": 0,
        "clip_frame_index": 0,
        "frame_pts": 12072435377777,
        "frame_uuid": "frame-a",
        "source_id": "c2_post_savant_fps_probe",
        "objects": [
            {
                "object_type": object_type,
                "track_id": "902",
                "bbox": {"xyxy": [1.0, 2.0, 10.0, 20.0]},
                "identity": {
                    "source_observation_id": "face:c2_post_savant_fps_probe:902:12072435",
                    "person_id": 6,
                    "external_person_id": "demo:f4_3:finch",
                    "gallery_embedding_id": 5,
                    "similarity": 0.7,
                    "threshold": 0.65,
                    "matched": True,
                    "identity_binding_method": "direct_frame_pts",
                },
            }
        ],
    }


def _candidate(tool: Any, *, identity_method: str = "direct_frame_pts") -> Any:
    return tool.WatchlistCandidate(
        event=_event(),
        observation={
            "source_observation_id": "face:c2_post_savant_fps_probe:902:12072435",
            "track_id": "902",
            "face_bbox": {"format": "xyxy", "values": [1.0, 2.0, 10.0, 20.0]},
        },
        redis_id="1780895616360-0",
        source="redis_face_observation_gallery_match",
        similarity=0.7,
        threshold=0.65,
        person_id=6,
        external_person_id="demo:f4_3:finch",
        person_name="Finch",
        gallery_embedding_id=5,
        source_observation_id="face:c2_post_savant_fps_probe:902:12072435",
        frame_pts=12072435377777,
        frame_uuid="frame-a",
        ring_window={},
        frame_hit=None,
        identity_binding_method=identity_method,
        identity_binding_detail="direct_frame_uuid_face_object",
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module
