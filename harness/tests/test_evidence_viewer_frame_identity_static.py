"""Evidence viewer frame-identity alignment contract tests."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VIEWER_JS = ROOT / "services" / "evidence-viewer" / "app" / "static" / "evidence.js"
VIEWER_CSS = ROOT / "services" / "evidence-viewer" / "app" / "static" / "style.css"
OBSOLETE_VIEWER_JS = ROOT / "services" / "evidence-viewer" / "app" / "static" / "app.js"
OBSOLETE_API_OPERATOR_JS = ROOT / "services" / "api" / "app" / "static" / "operator" / "evidence.js"


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


def test_evidence_viewer_detail_paths_use_database_index_api() -> None:
    text = _text(VIEWER_JS)

    assert 'const EVIDENCE_INDEX_API = "/api/v1/evidence";' in text
    assert "EVIDENCE_BUNDLE_API" not in text
    assert "`${EVIDENCE_INDEX_API}/bundles/${encodeURIComponent(eventId)}`" in text
    assert (
        "`${EVIDENCE_INDEX_API}/bundles/${encodeURIComponent(eventId)}/annotations?"
        "${annotationParams.toString()}`" in text
    )
    assert "`${EVIDENCE_INDEX_API}/bundles/${encodeURIComponent(eventId)}/sink-metadata`" in text
    assert "`${EVIDENCE_BUNDLE_API}/bundles/${encodeURIComponent(eventId)}`" not in text


def test_evidence_viewer_alarm_overlay_defaults_show_available_boxes() -> None:
    text = _text(VIEWER_JS)

    assert "const DEFAULT_SHOW_PERSON_BOXES = true;" in text
    assert "const DEFAULT_SHOW_MATCHED_FACES = true;" in text
    assert "const DEFAULT_SHOW_UNKNOWN_FACES = true;" in text
    assert "dom.showPersons.checked = DEFAULT_SHOW_PERSON_BOXES" in text
    assert "dom.showMatched.checked = DEFAULT_SHOW_MATCHED_FACES" in text
    assert "dom.showUnknown.checked = DEFAULT_SHOW_UNKNOWN_FACES" in text
    assert "function currentEvidenceAllowsFaceOverlay" not in text
    assert "function overlaySuppressionReason" not in text


def test_evidence_viewer_infers_bbox_format_when_values_has_no_format() -> None:
    text = _text(VIEWER_JS)

    assert "function inferBboxFormat" in text
    assert "coordinate_space || bbox.coordinateSpace" in text
    assert "values.every(value => value >= 0 && value <= 1)" in text
    assert "format = inferBboxFormat(values, sourceWidth, sourceHeight, usesNormalizedCoordinates)" in text


def test_evidence_viewer_accepts_legacy_bbox_shapes_and_seeks_to_first_overlay() -> None:
    text = _text(VIEWER_JS)
    html = _text(ROOT / "services" / "evidence-viewer" / "app" / "static" / "index.html")

    assert "function initialOverlayTimeSec" in text
    assert "function seekVideoToInitialOverlayTime" in text
    assert "seekVideoToInitialOverlayTime();" in text
    assert 'Array.isArray(bbox)' in text
    assert '["x1", "y1", "x2", "y2"].every' in text
    assert '["left", "top", "right", "bottom"].every' in text
    assert "bbox.width ?? bbox.w" in text
    assert "evidence-overlay-seek-20260709" in html


def test_hidden_image_evidence_does_not_expand_video_container() -> None:
    css = _text(VIEWER_CSS)

    assert ".image-evidence[hidden]" in css
    assert "display: none !important;" in css
