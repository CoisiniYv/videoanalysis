"""C1M.8b viewer source enforcement tests."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
VIEWER_ROOT = ROOT / "services" / "evidence-viewer"


def test_auto_unavailable_exposes_unavailable_source_kind_without_legacy_fallback(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=False)
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="auto")

    assert payload["annotation_source"] == "unavailable"
    assert payload["annotation_source_kind"] == "unavailable"
    assert payload["fallback_used"] is False
    assert payload["annotation_file"] is None
    assert payload["legacy_available"] is True


def test_auto_unavailable_when_sidecar_missing_and_legacy_exists_for_intrusion(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=False, event_type="intrusion", include_sidecar=False)
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="auto")
    manifest = _get_manifest(viewer)

    assert payload["annotation_source"] == "unavailable"
    assert payload["annotation_source_kind"] == "unavailable"
    assert payload["fallback_used"] is False
    assert payload["fallback_reason"] is None
    assert payload["reason"] == "production_sidecar_annotations_missing"
    assert payload["legacy_available"] is True
    assert payload["sidecar_available"] is False
    assert manifest["default_annotation_source"] == "unavailable"
    assert manifest["default_annotation_source_kind"] == "unavailable"
    assert manifest["auto_requires_production_sidecar"] is True


def test_auto_unavailable_when_sidecar_non_ready_and_legacy_exists_for_intrusion(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=False, event_type="intrusion")
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="auto")

    assert payload["annotation_source"] == "unavailable"
    assert payload["annotation_source_kind"] == "unavailable"
    assert payload["fallback_used"] is False
    assert payload["fallback_reason"] is None
    assert payload["reason"] == "cache_stale_or_epoch_mismatch"
    assert payload["legacy_available"] is True
    assert payload["sidecar_available"] is True


def test_manifest_default_source_kind_is_unavailable_for_not_ready_watchlist(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=False)
    viewer = _activate_viewer(evidence_root)

    payload = _get_manifest(viewer)

    assert payload["default_annotation_source"] == "unavailable"
    assert payload["default_annotation_source_kind"] == "unavailable"
    assert payload["production_sidecar_ready"] is False
    assert payload["legacy_available"] is True
    assert payload["event_status"] == "watchlist_hit"
    assert payload["visual_evidence_status"] == "unverified"
    assert payload["reason"] == "cache_stale_or_epoch_mismatch"


def test_ready_sidecar_exposes_production_source_kind(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=True, event_type="intrusion")
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="auto")

    assert payload["annotation_source"] == "sidecar"
    assert payload["annotation_source_kind"] == "production_sidecar"
    assert payload["fallback_used"] is False
    assert payload["production_ready"] is True
    assert payload["visual_evidence_status"] == "verified"


def test_ready_sidecar_keeps_unknown_face_observation_without_known_face_upgrade(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=True, event_type="intrusion")
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="auto")
    objects = [
        obj
        for record in payload["annotations"]
        for obj in record.get("objects", [])
        if isinstance(obj, dict)
    ]
    faces = [obj for obj in objects if obj.get("object_type") == "face"]

    assert payload["annotation_source"] == "sidecar"
    assert faces
    assert faces[0]["label"]["kind"] == "unknown_face"
    assert faces[0]["identity"]["visual_evidence_status"] == "observation_only"
    assert faces[0]["identity"]["match_status"] == "not_searched"
    assert faces[0].get("annotation_role") != "watchlist_trigger_face"
    assert not any((obj.get("label") or {}).get("kind") == "known_face" for obj in faces)


def test_explicit_legacy_source_is_marked_debug(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=False)
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="legacy")

    assert payload["annotation_source"] == "legacy"
    assert payload["annotation_source_kind"] == "legacy_debug"
    assert payload["fallback_used"] is False
    assert payload["production_ready"] is None
    assert payload["legacy_warning"] is not None
    assert payload["count"] == 1
    assert payload["legacy_known_face_blocked"] == 1
    face = payload["annotations"][0]
    assert face["identity"]["status"] == "unknown"
    assert face["label"]["kind"] == "unknown_face"


def test_explicit_preview_source_is_marked_debug(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=False, preview=True)
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="sidecar_preview")

    assert payload["annotation_source"] == "sidecar_preview"
    assert payload["annotation_source_kind"] == "preview_debug"
    assert payload["fallback_used"] is False
    assert payload["count"] == 1


def _activate_viewer(evidence_root: Path) -> Any:
    _clear_app_modules()
    viewer_root = str(VIEWER_ROOT)
    if viewer_root in sys.path:
        sys.path.remove(viewer_root)
    sys.path.insert(0, viewer_root)
    try:
        module = importlib.import_module("app.main")
        module.settings = module.settings.__class__(
            evidence_root=evidence_root,
            host=module.settings.host,
            port=module.settings.port,
            max_bundles=200,
        )
        return module
    finally:
        try:
            sys.path.remove(viewer_root)
        except ValueError:
            pass


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _get_annotations(viewer: Any, *, source: str) -> dict[str, Any]:
    response = viewer.api_annotations("event-1", source=source)
    return json.loads(response.body.decode("utf-8"))


def _get_manifest(viewer: Any) -> dict[str, Any]:
    response = viewer.api_bundle_manifest("event-1")
    return json.loads(response.body.decode("utf-8"))


def _make_evidence_root(tmp_path: Path) -> Path:
    root = tmp_path / "evidence"
    root.mkdir()
    return root


def _make_bundle(
    evidence_root: Path,
    *,
    production_ready: bool,
    event_type: str = "watchlist_hit",
    include_sidecar: bool = True,
    preview: bool = False,
) -> Path:
    bundle = evidence_root / "event-1"
    bundle.mkdir()
    _write_json(
        bundle / "metadata.json",
        {"event": {"event_id": "event-1", "event_type": event_type}},
    )
    _write_json(bundle / "summary.json", {"event_type": event_type})
    if include_sidecar:
        _write_json(
            bundle / "summary.frame_cache.identity.json",
            _sidecar_summary(production_ready=production_ready, event_type=event_type),
        )
        _write_jsonl(
            bundle / "annotations.frame_cache.identity.jsonl",
            [_sidecar_face("Sidecar Reese"), _sidecar_unknown_face()],
        )
    _write_jsonl(bundle / "annotations.jsonl", [_legacy_face("Legacy Reese")])
    if preview:
        _write_jsonl(
            bundle / "annotations.frame_cache.identity.rebased.preview.jsonl",
            [_sidecar_face("Preview Reese")],
        )
        _write_json(
            bundle / "summary.frame_cache.identity.rebased.preview.json",
            {"sidecar_type": "preview", "production_ready": False},
        )
    return bundle


def _sidecar_summary(*, production_ready: bool, event_type: str) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "sidecar_type": "production",
        "timeline_domain": "final_canonical_clip",
        "annotation_status": "ready" if production_ready else "cache_stale_or_epoch_mismatch",
        "production_ready": production_ready,
        "canonical_clip": True,
        "legacy_fallback_allowed": False,
        "rows_displayable": 1 if production_ready else 0,
        "production_ready_failures": [] if production_ready else ["trigger_row_stale_or_epoch_mismatch"],
        "source_observation_id": "face:event-1:1",
        "visual_binding_status": "verified" if production_ready else "unverified",
        "visual_binding_reason": "production_sidecar_trigger_bound"
        if production_ready
        else "cache_stale_or_epoch_mismatch",
        "frame_identity_method": "frame_uuid" if production_ready else "frame_pts_fallback",
        "frame_identity_confidence": "high" if production_ready else "none",
        "trigger_face_row_exists": production_ready,
        "trigger_face_row_passed_freshness_guard": production_ready,
    }


def _sidecar_face(name: str) -> dict[str, Any]:
    return {
        "object_type": "face",
        "annotation_role": "watchlist_trigger_face",
        "displayable": True,
        "frame_pts": 1_000_000_000,
        "frame_uuid": "frame-event-1",
        "t_ms": 1000,
        "source_observation_id": "face:event-1:1",
        "label": {"kind": "known_face", "display_name": name},
    }


def _sidecar_unknown_face() -> dict[str, Any]:
    return {
        "schema_version": "2.0-c2-post-savant",
        "annotation_source": "post_savant_sink_metadata",
        "production_ready": True,
        "displayable": True,
        "frame_index": 0,
        "clip_frame_index": 0,
        "frame_pts": 1_000_000_000,
        "frame_uuid": "frame-event-1",
        "objects": [
            {
                "object_type": "face",
                "namespace": "yolov8_face",
                "label": {"kind": "unknown_face"},
                "track_id": "41",
                "bbox": {
                    "format": "xyxy",
                    "coordinate_space": "pixel",
                    "xyxy": [10.0, 20.0, 40.0, 60.0],
                    "confidence": 0.9,
                    "source": "detection_box",
                },
                "landmarks": {
                    "format": "5_point",
                    "coordinate_space": "pixel",
                    "points": [[15.0, 25.0], [30.0, 25.0]],
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


def _legacy_face(name: str) -> dict[str, Any]:
    return {
        "record_type": "object_annotation",
        "object_type": "face",
        "timestamp": 1.0,
        "annotation_role": "watchlist_trigger_face",
        "identity": {"status": "matched", "display_name": name, "match_status": "above_threshold"},
        "label": {"kind": "known_face", "display_name": name},
        "bbox": {"x": 10, "y": 10, "width": 100, "height": 100},
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
