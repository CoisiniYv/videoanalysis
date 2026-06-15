from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "services" / "evidence-viewer" / "app" / "static"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_8090_operator_has_runtime_control_tab_and_panel() -> None:
    html = _text(STATIC_ROOT / "index.html")

    assert 'data-view="runtime"' in html
    assert 'id="runtime-view"' in html
    assert 'id="runtime-health-summary"' in html
    assert 'id="runtime-source-table"' in html
    assert 'id="runtime-forwarder-table"' in html
    assert 'id="runtime-evidence-table"' in html
    assert 'id="runtime-container-table"' in html
    assert "运行控制" in html
    assert "摄像头性能" in html
    assert "证据生成" in html


def test_operator_runtime_overview_uses_api_proxy_only() -> None:
    js = _text(STATIC_ROOT / "operator.js")
    html = _text(STATIC_ROOT / "index.html")
    viewer_main = _text(ROOT / "services" / "evidence-viewer" / "app" / "main.py")

    assert "loadRuntimeOverview" in js
    assert "`${API}/runtime/overview`" in js
    assert "renderRuntimeOverview" in js
    assert "renderRuntimeForwarderTable" in js
    assert "renderRuntimeEvidenceTable" in js
    assert "waiting_proof" in js
    assert "recent_failures" in js
    assert "frames_dropped_total" in js
    assert "restart_rate_per_min" in js
    assert "restart_warning" in js
    assert "restarts/min" in js
    assert '"runtime"' in viewer_main
    assert "18080" not in js
    assert "savant-security:8080" not in js
    assert "docker" not in html.lower()


def test_operator_runtime_overview_has_stable_table_styles() -> None:
    css = _text(STATIC_ROOT / "style.css")

    assert ".runtime-workspace" in css
    assert ".runtime-table" in css
    assert ".runtime-health-grid" in css
    assert ".runtime-evidence-pane" in css
    assert ".runtime-failure-list" in css
    assert ".warn-row" in css
