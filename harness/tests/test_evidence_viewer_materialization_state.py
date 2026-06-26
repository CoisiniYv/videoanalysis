"""Evidence viewer materialization-state serialization tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VIEWER_DIR = ROOT / "services" / "evidence-viewer"


def _activate_viewer() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    text = str(VIEWER_DIR)
    if text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)


def _write_bundle(root: Path, event_id: str, media: dict) -> Path:
    bundle = root / event_id
    bundle.mkdir(parents=True)
    (bundle / "raw_clip.mov").write_bytes(b"video")
    metadata = {
        "event": {
            "event_id": event_id,
            "event_type": "intrusion",
            "source_id": "source_00",
        },
        "media": media,
    }
    (bundle / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (bundle / "summary.json").write_text(
        json.dumps({"event_id": event_id, "clip_status": "ready"}),
        encoding="utf-8",
    )
    return bundle


def test_file_bundle_hides_raw_clip_for_deferred_materialization(tmp_path: Path) -> None:
    _activate_viewer()
    from app.evidence_index import bundle_manifest, bundle_summary

    bundle = _write_bundle(
        tmp_path,
        "event-deferred",
        {
            "materialization_status": "materialization_deferred",
            "materialization_reason": "pressure_warning",
            "materialization_deadline_at": "2026-06-20T15:30:00+00:00",
            "quota_decision": {"level": "warning"},
            "degrade_decision": {"level": "warning"},
        },
    )

    summary = bundle_summary(bundle)
    manifest = bundle_manifest(tmp_path, "event-deferred")

    assert summary["raw_clip_available"] is False
    assert summary["raw_clip_url"] is None
    assert summary["materialization_status"] == "materialization_deferred"
    assert summary["materialization_reason"] == "pressure_warning"
    assert summary["raw_clip_unavailable_reason"] == (
        "raw_clip_unavailable:materialization_deferred"
    )
    assert manifest["raw_clip_url"] is None
    assert "raw_clip_unavailable:materialization_deferred" in manifest["warnings"]


def test_file_bundle_defaults_existing_raw_clip_to_materialized(tmp_path: Path) -> None:
    _activate_viewer()
    from app.evidence_index import bundle_summary

    bundle = _write_bundle(tmp_path, "event-ready", {})

    summary = bundle_summary(bundle)

    assert summary["raw_clip_available"] is True
    assert summary["raw_clip_url"] == "/api/bundles/event-ready/media/raw_clip"
    assert summary["materialization_status"] == "materialized"
    assert summary["raw_clip_unavailable_reason"] is None
