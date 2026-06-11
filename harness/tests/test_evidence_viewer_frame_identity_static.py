"""Evidence viewer frame-identity alignment contract tests."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VIEWER_JS = ROOT / "services" / "evidence-viewer" / "app" / "static" / "evidence.js"
OPERATOR_JS = ROOT / "services" / "api" / "app" / "static" / "operator" / "evidence.js"


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


def test_operator_overlay_uses_frame_identity_before_time_offsets() -> None:
    _assert_frame_identity_first(_text(OPERATOR_JS))
