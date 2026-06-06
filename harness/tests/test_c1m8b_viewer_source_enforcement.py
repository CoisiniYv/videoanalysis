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
    _make_bundle(evidence_root, production_ready=True)
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="auto")

    assert payload["annotation_source"] == "sidecar"
    assert payload["annotation_source_kind"] == "production_sidecar"
    assert payload["fallback_used"] is False
    assert payload["production_ready"] is True
    assert payload["visual_evidence_status"] == "verified"


def test_explicit_legacy_source_is_marked_debug(tmp_path: Path) -> None:
    evidence_root = _make_evidence_root(tmp_path)
    _make_bundle(evidence_root, production_ready=False)
    viewer = _activate_viewer(evidence_root)

    payload = _get_annotations(viewer, source="legacy")

    assert payload["annotation_source"] == "legacy"
    assert payload["annotation_source_kind"] == "legacy_debug"
    assert payload["fallback_used"] is False
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
    preview: bool = False,
) -> Path:
    bundle = evidence_root / "event-1"
    bundle.mkdir()
    _write_json(
        bundle / "metadata.json",
        {"event": {"event_id": "event-1", "event_type": "watchlist_hit"}},
    )
    _write_json(bundle / "summary.json", {"event_type": "watchlist_hit"})
    _write_json(bundle / "summary.frame_cache.identity.json", _sidecar_summary(production_ready=production_ready))
    _write_jsonl(bundle / "annotations.frame_cache.identity.jsonl", [_sidecar_face("Sidecar Reese")])
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


def _sidecar_summary(*, production_ready: bool) -> dict[str, Any]:
    return {
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
