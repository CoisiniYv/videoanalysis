"""C1M.9 watchlist visual binding guard tests."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
VIEWER_ROOT = ROOT / "services" / "evidence-viewer"


def test_watchlist_sidecar_stale_manifest_is_unverified(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=False)
    viewer = _activate_viewer(evidence_root)

    manifest = _get_manifest(viewer)

    assert manifest["event_status"] == "watchlist_hit"
    assert manifest["visual_evidence_status"] == "unverified"
    assert manifest["reason"] == "cache_stale_or_epoch_mismatch"
    assert manifest["source_observation_id"] == "face:event-1:1"
    assert manifest["frame_identity_method"] == "frame_pts_fallback"
    assert manifest["frame_identity_confidence"] == "none"


def test_watchlist_auto_unavailable_is_not_visually_confirmed(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=False)
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="auto")

    assert payload["annotation_source"] == "unavailable"
    assert payload["visual_evidence_status"] == "unverified"
    assert payload["visual_binding_reason"] == "cache_stale_or_epoch_mismatch"
    assert payload["records"] == []
    assert payload["fallback_used"] is False


def test_watchlist_legacy_debug_blocks_known_face_confirmation(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=False)
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="legacy")
    row = payload["annotations"][0]

    assert payload["annotation_source_kind"] == "legacy_debug"
    assert payload["legacy_known_face_blocked"] == 1
    assert payload["visual_evidence_status"] == "unverified"
    assert row["legacy_known_face_blocked"] is True
    assert row["visual_binding_status"] == "unverified"
    assert row["identity"]["status"] == "unknown"
    assert row["identity"]["match_status"] == "not_searched"
    assert row["label"]["kind"] == "unknown_face"
    assert "annotation_role" not in row


def test_watchlist_ready_sidecar_manifest_is_verified(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=True)
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="auto")

    assert payload["annotation_source"] == "sidecar"
    assert payload["visual_evidence_status"] == "verified"
    assert payload["visual_binding_reason"] == "production_sidecar_trigger_bound"
    assert payload["frame_identity_method"] == "frame_uuid"
    assert payload["frame_identity_confidence"] == "high"


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


def _make_bundle(evidence_root: Path, *, production_ready: bool) -> Path:
    bundle = evidence_root / "event-1"
    bundle.mkdir()
    _write_json(
        bundle / "metadata.json",
        {"event": {"event_id": "event-1", "event_type": "watchlist_hit"}},
    )
    _write_json(bundle / "summary.json", {"event_type": "watchlist_hit"})
    _write_json(bundle / "summary.frame_cache.identity.json", _sidecar_summary(production_ready=production_ready))
    _write_jsonl(bundle / "annotations.frame_cache.identity.jsonl", [_sidecar_face()])
    _write_jsonl(bundle / "annotations.jsonl", [_legacy_known_face()])
    return bundle


def _sidecar_summary(*, production_ready: bool) -> dict[str, Any]:
    return {
        "event_type": "watchlist_hit",
        "sidecar_type": "production",
        "timeline_domain": "final_canonical_clip",
        "annotation_status": "complete" if production_ready else "cache_stale_or_epoch_mismatch",
        "production_ready": production_ready,
        "source_observation_id": "face:event-1:1",
        "visual_binding_status": "verified" if production_ready else "unverified",
        "visual_binding_reason": "production_sidecar_trigger_bound"
        if production_ready
        else "cache_stale_or_epoch_mismatch",
        "evidence_visual_status": "verified" if production_ready else "unverified",
        "frame_identity_method": "frame_uuid" if production_ready else "frame_pts_fallback",
        "frame_identity_confidence": "high" if production_ready else "none",
        "trigger_face_row_exists": production_ready,
        "trigger_face_row_passed_freshness_guard": production_ready,
        "legacy_fallback_allowed": False,
    }


def _sidecar_face() -> dict[str, Any]:
    return {
        "object_type": "face",
        "annotation_role": "watchlist_trigger_face",
        "displayable": True,
        "source_observation_id": "face:event-1:1",
        "frame_uuid": "frame-event-1",
        "t_ms": 1000,
        "label": {"kind": "known_face", "display_name": "Reese"},
    }


def _legacy_known_face() -> dict[str, Any]:
    return {
        "record_type": "object_annotation",
        "object_type": "face",
        "annotation_role": "watchlist_trigger_face",
        "source_observation_id": "face:event-1:1",
        "timestamp": 1.0,
        "identity": {
            "status": "matched",
            "match_status": "above_threshold",
            "display_name": "Reese",
        },
        "label": {"kind": "known_face", "display_name": "Reese"},
        "bbox": {"x": 10, "y": 10, "width": 100, "height": 100},
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
