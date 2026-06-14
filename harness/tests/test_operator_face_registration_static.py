from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "services" / "evidence-viewer" / "app" / "static"
API_OPERATOR_STATIC_ROOT = ROOT / "services" / "api" / "app" / "static" / "operator"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_operator_keeps_camera_registration_controls() -> None:
    html = _text(STATIC_ROOT / "index.html")
    assert 'data-view="cameras"' in html
    assert "new-camera" in html
    assert "camera-form" in html
    assert "save-camera" in html
    assert "open-rules-panel" in html
    assert "open-recording-settings" in html
    assert "apply-runtime" in html
    assert "algorithm-controls" in html
    assert "save-quick-algorithms" in html
    assert "算法开关" in html
    assert "录制设置" in html
    assert "应用运行时" in html
    assert "算法开关与录像" in html


def test_operator_adds_people_face_registration_controls() -> None:
    html = _text(STATIC_ROOT / "index.html")
    assert 'data-view="people"' in html
    assert "face-registration-form" in html
    assert "submit-face-registration" in html
    assert "external_person_id" in html
    assert "allow_multiple_faces" in html
    assert "keep_crop" in html
    assert "quality_threshold" in html
    assert 'value="0.65"' in html


def test_operator_page_is_chinese_console_ui() -> None:
    html = _text(STATIC_ROOT / "index.html")
    assert "视频分析操作台" in html
    assert "统一配置台" in html
    assert "运行概览" in html
    assert "摄像头管理" in html
    assert "人员与人脸" in html
    assert "告警证据" in html
    assert "报警机器时间" in html
    assert "人脸注册" in html
    assert "人脸图库" in html
    assert "人脸图片" in html
    assert "开始注册" in html
    assert "theme-toggle" in html
    assert "黑夜" in html


def test_operator_customer_view_hides_internal_debug_fields() -> None:
    html = _text(STATIC_ROOT / "index.html")
    assert "摄像头ID" not in html
    assert "数据源ID" not in html
    assert "GPU 编号" not in html
    assert "RTSP传输" not in html
    assert "配置预览" in html and "internal-config" in html
    assert "质量阈值" not in html
    assert "允许多人脸" not in html
    assert "操作员" not in html
    assert "向量范数" not in html
    assert "图库向量" not in html


def test_operator_js_uses_real_camera_and_people_apis() -> None:
    js = _text(STATIC_ROOT / "operator.js")
    assert "/api/v1" in js
    assert "`${API}/cameras`" in js
    assert "`${API}/people" in js
    assert "`${API}/people/register-face`" in js
    assert "FormData(faceRegistrationForm)" in js
    assert "dev_mock" not in js


def test_operator_gallery_renders_registered_face_images() -> None:
    js = _text(STATIC_ROOT / "operator.js")
    css = _text(STATIC_ROOT / "style.css")
    assert "source_image_url" in js
    assert "registered_crop_url" in js
    assert "gallery-thumb" in js
    assert ".gallery-thumb" in css


def test_operator_new_camera_defaults_to_uuid_compatible_id() -> None:
    js = _text(STATIC_ROOT / "operator.js")
    assert "makeCameraId" in js
    assert "randomUUID" in js
    assert "source_${cameraId}" in js


def test_operator_exposes_algorithm_rules_and_recording_window_controls() -> None:
    html = _text(STATIC_ROOT / "index.html")
    js = _text(STATIC_ROOT / "operator.js")

    assert '<section class="pane rules">' in html
    assert 'data-tab="rules"' in html
    assert "算法规则" in html
    assert "rule-form" in html
    assert 'name="algorithm_id"' in html
    assert 'name="enabled"' in html
    assert 'name="clip_required"' in html
    assert 'name="pre_seconds"' in html
    assert 'name="post_seconds"' in html
    assert "/api/v1/algorithms" in js or "`${API}/algorithms`" in js
    assert "algorithm-rules" in js
    assert "/rules/" in js
    assert "quickAlgorithmIds" in js
    assert "saveQuickAlgorithmControls" in js
    assert "quickRuleBodyFromCard" in js
    assert "switchCameraTab(\"rules\")" in js
    assert "配置并启用算法规则" in js
    assert "cameras/runtime/apply" in js
    assert "运行时已应用" in js
    assert "restart-runtime" in html
    assert "受控重启运行时" in html
    assert "cameras/runtime/restart" in js
    assert "运行时已受控重启" in js


def test_operator_smoke_prepares_camera_schema() -> None:
    migration = ROOT / "db" / "migrations" / "012_operator_camera_schema_compat.sql"
    smoke = ROOT / "scripts" / "smoke" / "current" / "check_operator_camera_and_face_registration.sh"
    assert migration.exists()
    smoke_text = smoke.read_text(encoding="utf-8")
    assert "012_operator_camera_schema_compat.sql" in smoke_text
    assert "ensuring camera operator schema" in smoke_text
    assert 'OPERATOR_SMOKE_CAMERA_ENABLED:-false' in smoke_text
    assert '"enabled": ${CAMERA_ENABLED}' in smoke_text
    assert "http://0.0.0.0:8090" in smoke_text
    assert '"${API_BASE_URL}/operator"' not in smoke_text
    assert '"${API_BASE_URL}/"' in smoke_text


def test_operator_portal_is_served_by_evidence_viewer_8090() -> None:
    html = _text(STATIC_ROOT / "index.html")
    js = _text(STATIC_ROOT / "operator.js")
    assert "/static/operator.js" in html
    assert "/static/evidence.js" in html
    assert "/operator/static" not in html
    assert "evidence-viewer" not in (html + js).lower()


def test_operator_theme_toggle_is_frontend_only_and_persistent() -> None:
    html = _text(STATIC_ROOT / "index.html")
    js = _text(STATIC_ROOT / "operator.js")
    css = _text(STATIC_ROOT / "style.css")
    assert "style.css?v=dark-mode-20260611" in html
    assert "operator.js?v=runtime-overview-20260614" in html
    assert "operator-theme" in html
    assert "operator-theme" in js
    assert "theme-toggle" in js
    assert 'data-theme="dark"' not in html
    assert ':root[data-theme="dark"]' in css


def test_operator_evidence_page_shows_alarm_machine_time() -> None:
    html = _text(STATIC_ROOT / "index.html")
    evidence_js = _text(STATIC_ROOT / "evidence.js")
    assert 'id="alarmMachineTime"' in html
    assert "/static/evidence.js?v=" in html
    assert "formatAlarmMachineTime" in evidence_js
    assert "alarm_machine_time" in evidence_js
    assert "报警" in evidence_js


def test_operator_evidence_prefers_camera_name_and_keeps_source_id_detail() -> None:
    for static_root in (STATIC_ROOT, API_OPERATOR_STATIC_ROOT):
        html = _text(static_root / "index.html")
        evidence_js = _text(static_root / "evidence.js")
        assert 'id="sourceRawId"' in html
        assert "function cameraDisplayName" in evidence_js
        assert "camera_name" in evidence_js
        assert "setText(\"sourceRawId\"" in evidence_js
