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
    assert 'id="runtime-latency-summary"' in html
    assert 'id="refresh-runtime-latency"' in html
    assert 'id="quick-runtime-stop"' in html
    assert 'id="runtime-source-table"' in html
    assert 'id="runtime-forwarder-table"' in html
    assert 'id="runtime-evidence-table"' in html
    assert 'id="runtime-container-table"' in html
    assert 'id="runtime-control-status"' in html
    assert 'id="runtime-decision-panel"' in html
    assert 'id="toggle-runtime-advanced"' in html
    assert 'id="runtime-performance-pane"' in html
    assert 'id="runtime-topology-pane"' in html
    assert 'id="runtime-container-pane"' in html
    assert 'id="recover-runtime-sources"' in html
    assert 'id="apply-saved-runtime-topology"' in html
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
    assert "建议下一步" in html
    assert "启动基础单路链路" in html
    assert "停止双路扩展" in html
    assert "推理性能" in html
    assert "确认并应用" in html
    assert "保存草稿" in html
    assert "识别最大帧率" in html
    assert "转发队列上限" in html
    assert "发送超时 ms" in html
    assert "发送重试次数" in html
    assert "发送高水位" in html
    assert "识别数据读取超时（毫秒）" in html
    assert "识别数据写入重试" in html
    assert "画面标注写入超时（毫秒）" in html
    assert "先判断真实运行态和配置差异，再执行恢复或切换操作" in html
    assert "摄像头性能" in html
    assert "证据生成" in html
    assert "高级运维" in html
    assert "运维控制" in html
    assert "startup-callout" not in html
    runtime_html = html.split('id="runtime-view"', 1)[1].split(
        'id="evidence-view"', 1
    )[0]
    assert 'id="quick-runtime-start"' in runtime_html


def test_operator_runtime_overview_uses_api_proxy_only() -> None:
    js = _text(STATIC_ROOT / "operator.js")
    html = _text(STATIC_ROOT / "index.html")
    viewer_main = _text(ROOT / "services" / "evidence-viewer" / "app" / "main.py")

    assert "loadRuntimeOverview" in js
    assert "`${API}/runtime/overview`" in js
    assert "`${API}/runtime/control`" in js
    assert "`${API}/runtime/latency`" in js
    assert "function formatDate(value)" in js
    assert "formatDate(data.generated_at)" in js
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
    assert "renderRuntimeDecision" in js
    assert "recoverRuntimeSources" in js
    assert "applySavedRuntimeTopology" in js
    assert "Promise.allSettled" in js
    assert "renderRuntimeForwarderTable" in js
    assert "renderRuntimeEvidenceTable" in js
    assert "setRuntimeAdvancedVisible" in js
    assert "runtimeEvidenceStateIsFailure" in js
    assert "materialization_failed: \"生成失败\"" in js
    assert "waiting_proof" in js
    assert "recent_failures" in js
    assert "frames_dropped_total" in js
    assert "restart_rate_per_min" in js
    assert "restart_warning" in js
    assert "runtimeIssueLabel" in js
    assert "containerStateText" in js
    assert "每分钟重启" in js
    assert "固定视频源未运行" in js
    assert "推理指标" in js
    assert "运行判断" in js
    assert "发送失败" in js
    assert "累计发送失败" in js
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
    assert ".runtime-decision-panel" in css
    assert ".runtime-drift-summary" in css
    assert ".runtime-performance-form" in css
    assert ".runtime-performance-table" in css
    assert ".runtime-evidence-pane" in css
    assert ".runtime-advanced-pane[hidden]" in css
    assert ".runtime-failure-list" in css
    assert ".warn-row" in css
