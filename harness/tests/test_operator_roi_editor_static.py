from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "services" / "evidence-viewer" / "app" / "static"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_operator_camera_page_embeds_roi_preview_editor() -> None:
    html = _text(STATIC_ROOT / "index.html")

    assert 'id="roi-zone-id"' in html
    assert 'id="roi-zone-type"' in html
    assert 'id="roi-preview-image"' in html
    assert 'id="roi-canvas"' in html
    assert 'id="refresh-roi-preview"' in html
    assert 'id="add-roi-center-point"' in html
    assert 'id="undo-roi-point"' in html
    assert 'id="clear-roi-points"' in html
    assert 'id="save-roi-zone"' in html
    assert 'class="roi-workbench"' in html
    assert 'class="zone-list-panel"' in html
    assert 'class="zone-json-details"' in html
    assert "高级 JSON" in html
    assert html.index('id="zones"') < html.index('id="zone-json"')
    assert "保存 ROI" in html
    assert "operator-trajectory-ssd-cache-20260712" in html


def test_operator_roi_editor_uses_camera_preview_and_existing_zone_api() -> None:
    js = _text(STATIC_ROOT / "operator.js")

    assert "roiState" in js
    assert "refreshRoiPreview" in js
    assert "/preview.jpg?max_width=1280" in js
    assert "X-Camera-Source-Width" in js
    assert "X-Camera-Source-Height" in js
    assert "displayPointToSource" in js
    assert "sourcePointToDisplay" in js
    assert "roiImageDisplayRect" in js
    assert "nextRoiZoneId" in js
    assert "loadZoneIntoRoiEditor" in js
    assert "preferredFinalRoiZone" in js
    assert "prepareRoiEditorForCamera" in js
    assert "zoneRuleBindings" in js
    assert "function validZoneRows" in js
    assert "zoneOptionId(zone)" in js
    assert "validZoneRows(zones)" in js
    assert "appendZoneGroup" in js
    assert "已保存区域" in js
    assert "已保存检测线" in js
    assert "最终检测区域" in js
    assert "未绑定算法" in js
    assert "addRoiPointFromEvent" in js
    assert "ensureRoiImageReadyForInput" in js
    assert "roiInputPoint" in js
    assert "shouldIgnoreRoiInput" in js
    assert "lastPointerKey" not in js
    assert "Math.hypot(dx, dy) <= 10" in js
    assert "now - roiState.lastPointerAt < 600" in js
    assert "addRoiCenterPoint" in js
    assert "roiCanvasWrapEl" in js
    assert "bind_rules=true" in js
    assert 'const bindRuleRefs = bindRules && body.zone_type === "polygon"' in js
    assert 'addEventListener("pointerdown", addRoiPointFromEvent)' in js
    assert 'addEventListener("mousedown", addRoiPointFromEvent)' in js
    assert 'addEventListener("click", addRoiPointFromEvent)' in js
    assert "saveRoiZone" in js
    assert "await saveZone({ bindRules: true })" in js
    assert 'saveZone({ bindRules: true }).catch((e) => showError(e.message))' in js
    assert "coordinate_space: \"pixel\"" in js


def test_operator_roi_editor_has_stable_canvas_styles() -> None:
    css = _text(STATIC_ROOT / "style.css")

    assert ".roi-editor" in css
    assert ".roi-workbench" in css
    assert "grid-template-columns: 208px minmax(0, 1fr)" in css
    assert "grid-template-columns: 1fr" in css
    assert "max-height: 320px" in css
    assert ".roi-toolbar" in css
    assert ".roi-canvas-wrap" in css
    assert "#roi-preview-image" in css
    assert "#roi-canvas" in css
    assert "aspect-ratio: 16 / 9" in css
    assert ".roi-preview-empty" in css
    assert ".roi-editor-status" in css
    assert ".zone-list-group" in css
    assert ".zone-list-title" in css
    assert ".zone-list-panel" in css
    assert ".zone-json-details" in css
    assert ".zone-item-header" in css
    assert ".zone-binding-list" in css
    assert ".zone-chip.final" in css
    assert "cursor: crosshair" in css
    assert "touch-action: none" in css
