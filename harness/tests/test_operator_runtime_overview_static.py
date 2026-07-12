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
    assert 'id="runtime-control-status"' in html
    assert 'id="runtime-performance-form"' in html
    assert 'id="runtime-performance-status"' in html
    assert 'id="runtime-performance-diff"' in html
    assert 'id="save-runtime-performance"' in html
    assert 'id="apply-runtime-performance"' in html
    assert 'id="start-single-runtime"' in html
    assert 'id="stop-single-runtime"' in html
    assert 'id="restart-single-runtime"' in html
    assert 'id="stop-dual-runtime"' in html
    assert "运行控制" in html
    assert "启动单路链路" in html
    assert "关闭双路扩展" in html
    assert "推理性能" in html
    assert "保存并应用" in html
    assert "Savant 最大 FPS" in html
    assert "转发队列上限" in html
    assert "发送超时 ms" in html
    assert "发送重试次数" in html
    assert "发送高水位" in html
    assert "Savant Redis 超时 ms" in html
    assert "Savant Redis 写入重试" in html
    assert "Frame annotation 写入超时 ms" in html
    assert "集中查看推理链路、视频源、证据任务和管理服务状态" in html
    assert "摄像头性能" in html
    assert "证据生成" in html


def test_operator_runtime_overview_uses_api_proxy_only() -> None:
    js = _text(STATIC_ROOT / "operator.js")
    html = _text(STATIC_ROOT / "index.html")
    viewer_main = _text(ROOT / "services" / "evidence-viewer" / "app" / "main.py")

    assert "loadRuntimeOverview" in js
    assert "`${API}/runtime/overview`" in js
    assert "`${API}/runtime/control`" in js
    assert "`${API}/runtime/performance-config`" in js
    assert "runtime/performance-config/apply" in js
    assert "renderRuntimePerformanceConfig" in js
    assert "runtimePerformanceFormBody" in js
    assert "runtime/control/single/start" in js
    assert "runtime/control/single/stop" in js
    assert "runtime/control/single/restart" in js
    assert "runtime/control/dual/stop" in js
    assert "renderRuntimeOverview" in js
    assert "renderRuntimeControlStatus" in js
    assert "renderRuntimeForwarderTable" in js
    assert "renderRuntimeEvidenceTable" in js
    assert "waiting_proof" in js
    assert "recent_failures" in js
    assert "frames_dropped_total" in js
    assert "restart_rate_per_min" in js
    assert "restart_warning" in js
    assert "runtimeIssueLabel" in js
    assert "containerStateText" in js
    assert "每分钟重启" in js
    assert "固定源容器未运行" in js
    assert "推理指标" in js
    assert "运行判断" in js
    assert "发送失败" in js
    assert '"runtime"' in viewer_main
    assert "18080" not in js
    assert "savant-security:8080" not in js
    assert "docker" not in html.lower()


def test_operator_runtime_restart_displays_evidence_guard_details() -> None:
    js = _text(STATIC_ROOT / "operator.js")

    assert "error.details = body.error?.details || body.detail || null" in js
    assert "function apiErrorMessage" in js
    assert "仍有 ${count} 个证据任务在生成中" in js
    assert "active_count" in js
    assert "blocking_state" in js
    assert "apiErrorMessage(e)" in js


def test_operator_runtime_overview_has_stable_table_styles() -> None:
    css = _text(STATIC_ROOT / "style.css")

    assert ".runtime-workspace" in css
    assert ".runtime-table" in css
    assert ".runtime-health-grid" in css
    assert ".runtime-performance-form" in css
    assert ".runtime-performance-table" in css
    assert ".runtime-evidence-pane" in css
    assert ".runtime-failure-list" in css
    assert ".warn-row" in css
