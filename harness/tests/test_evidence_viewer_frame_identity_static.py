"""Evidence viewer frame-identity alignment contract tests."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VIEWER_JS = ROOT / "services" / "evidence-viewer" / "app" / "static" / "evidence.js"
OBSOLETE_VIEWER_JS = ROOT / "services" / "evidence-viewer" / "app" / "static" / "app.js"
OBSOLETE_API_OPERATOR_JS = ROOT / "services" / "api" / "app" / "static" / "operator" / "evidence.js"
VIEWER_MAIN = ROOT / "services" / "evidence-viewer" / "app" / "main.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert_frame_identity_first(js: str) -> None:
    assert "function buildSinkFrameLookup" in js
    assert "frameLookup.byUuid.get(frameUuid)" in js
    assert 'mode: "frame_uuid"' in js
    assert 'mode: "time_offset_ms_fallback"' in js
    assert js.index("frameLookup.byUuid.get(frameUuid)") < js.index("const timeOffsetMs")
    assert js.index("frameLookup?.byPts?.get") < js.index("const timeOffsetMs")


def test_evidence_viewer_overlay_uses_frame_identity_before_time_offsets() -> None:
    _assert_frame_identity_first(_text(VIEWER_JS))


def test_obsolete_duplicate_evidence_frontends_are_removed() -> None:
    assert not OBSOLETE_VIEWER_JS.exists()
    assert not OBSOLETE_API_OPERATOR_JS.exists()


def test_evidence_viewer_detail_paths_try_database_index_first() -> None:
    text = _text(VIEWER_MAIN)
    annotations_body = text.split("def api_annotations(", 1)[1].split("\n\n@app.get", 1)[0]
    sink_body = text.split("def api_sink_metadata(", 1)[1].split("\n\n@app.get", 1)[0]
    manifest_body = text.split("def api_bundle_manifest(", 1)[1].split("\n\ndef annotation_source_summary", 1)[0]

    assert '_operator_api_json(f"evidence/bundles/{event_id}")' in text
    assert '_operator_api_json(f"evidence/bundles/{event_id}/annotations")' in text
    assert '_operator_api_json(f"evidence/bundles/{event_id}/sink-metadata")' in text
    assert manifest_body.index('_operator_api_json(f"evidence/bundles/{event_id}")') < manifest_body.index("bundle_manifest(")
    assert annotations_body.index('_operator_api_json(f"evidence/bundles/{event_id}/annotations")') < annotations_body.index("select_annotation_file(")
    assert sink_body.index('_operator_api_json(f"evidence/bundles/{event_id}/sink-metadata")') < sink_body.index('parse_json_or_jsonl_records(bundle_dir / "sink_metadata.json")')
