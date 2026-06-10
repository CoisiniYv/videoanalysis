"""C2.1C viewer face overlay visibility contracts."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "services" / "evidence-viewer" / "app" / "static"


def test_unknown_face_overlay_is_enabled_by_default() -> None:
    index_html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="showUnknown" type="checkbox" checked' in index_html
    assert "DEFAULT_SHOW_UNKNOWN_FACES = true" in app_js
    assert "dom.showUnknown.checked = DEFAULT_SHOW_UNKNOWN_FACES" in app_js


def test_c2_face_objects_use_existing_unknown_face_renderer() -> None:
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "return dom.showUnknown.checked" in app_js
    assert "未知人脸" in app_js
    assert "obj.landmarks?.points" in app_js
