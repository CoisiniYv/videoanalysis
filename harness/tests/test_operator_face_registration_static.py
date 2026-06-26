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
    assert "apply-runtime" in html
    assert "algorithm-controls" in html
    assert "save-quick-algorithms" in html
    assert "告警算法" in html
    assert "入侵检测与名单命中" in html
    assert "应用视频源" in html
    assert "保存并应用" in html


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
    assert "prepareFaceRegistrationFormData" in js
    assert "selectedExternalPersonId" in js
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
    assert "高级规则" in html
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


def test_operator_primary_algorithm_controls_are_limited_to_live_alarm_paths() -> None:
    html = _text(STATIC_ROOT / "index.html")
    js = _text(STATIC_ROOT / "operator.js")
    css = _text(STATIC_ROOT / "style.css")

    assert 'data-template="behavior.intrusion"' in html
    assert 'data-template="face.watchlist"' in html
    assert 'data-template="behavior.loitering"' not in html
    assert 'data-template="behavior.running"' not in html
    assert 'data-template="behavior.fall"' not in html
    assert 'data-template="behavior.crowd_gathering"' not in html
    assert 'data-template="face.live_search"' not in html
    assert 'const quickAlgorithmIds = [\n  "behavior.intrusion",\n  "face.watchlist",\n];' in js
    assert "operatorAlgorithmMeta" in js
    assert "按摄像头名单" in js
    assert "target_person_ids" in js
    assert "target_external_person_ids" in js
    assert "target_names" in js
    assert 'data-action="save-quick-rule"' in js
    assert "saveQuickAlgorithmCard" in js
    assert "upsertCurrentRule(savedRule)" in js
    assert "await selectCamera(cameraId, { clear: false })" in js
    assert "sourceApplyPayloadStatus" in js
    assert "showCameraSourceApplyResult" in js
    assert "runtime_source_apply" in js
    assert "operator.js?v=source-apply-once-20260626" in html
    assert "watchlist-target-list" in css
    assert "匹配阈值" in js
    assert "停留毫秒" in js


def test_operator_exposes_algorithm_support_and_runtime_apply_visibility() -> None:
    html = _text(STATIC_ROOT / "index.html")
    js = _text(STATIC_ROOT / "operator.js")
    css = _text(STATIC_ROOT / "style.css")

    assert "algorithms/support-matrix" in js
    assert "supportStatusLabels" in js
    assert "applyStateLabels" in js
    assert "algorithmDebugModeEnabled" in js
    assert "renderRuntimeApplyResult" in js
    assert "loadSelectedRuntimeConfig" in js
    assert "generated-runtime-config" in html
    assert "runtime-apply-result" in html
    assert "refresh-runtime-config" in html
    assert "support-badge" in css
    assert "runtime-warning-list" in css
    assert "support-config_only" in css
    assert "support-unsupported" in css
    assert "support-deferred" in css


def test_operator_smoke_prepares_camera_schema() -> None:
    migration = ROOT / "db" / "migrations" / "012_operator_camera_schema_compat.sql"
    zone_migration = ROOT / "db" / "migrations" / "016_camera_rule_zone_id_text_compat.sql"
    smoke = ROOT / "scripts" / "smoke" / "current" / "check_operator_camera_and_face_registration.sh"
    assert migration.exists()
    assert zone_migration.exists()
    smoke_text = smoke.read_text(encoding="utf-8")
    assert "012_operator_camera_schema_compat.sql" in smoke_text
    assert "016_camera_rule_zone_id_text_compat.sql" in smoke_text
    assert "ensuring camera operator schema" in smoke_text
    assert 'OPERATOR_SMOKE_CAMERA_ENABLED:-false' in smoke_text
    assert '"enabled": ${CAMERA_ENABLED}' in smoke_text
    assert "http://0.0.0.0:8090" in smoke_text
    assert '"${API_BASE_URL}/operator"' not in smoke_text
    assert '"${API_BASE_URL}/"' in smoke_text
    assert "cleanup_smoke_camera" in smoke_text
    assert "OPERATOR_SMOKE_KEEP_CAMERA" in smoke_text
    assert "DELETE FROM cameras" in smoke_text
    zone_text = zone_migration.read_text(encoding="utf-8")
    assert "ALTER COLUMN zone_id TYPE TEXT" in zone_text
    assert "camera_rules_zone_id_fkey" in zone_text or "DROP CONSTRAINT IF EXISTS" in zone_text


def test_operator_portal_is_served_by_evidence_viewer_8090() -> None:
    html = _text(STATIC_ROOT / "index.html")
    js = _text(STATIC_ROOT / "operator.js")
    viewer_main = _text(ROOT / "services" / "evidence-viewer" / "app" / "main.py")
    assert "/static/operator.js" in html
    assert "/static/evidence.js" in html
    assert '@app.get("/operator")' in viewer_main
    assert "def operator_index" in viewer_main
    assert "/operator/static" not in html
    assert "evidence-viewer" not in (html + js).lower()


def test_operator_theme_toggle_is_frontend_only_and_persistent() -> None:
    html = _text(STATIC_ROOT / "index.html")
    js = _text(STATIC_ROOT / "operator.js")
    css = _text(STATIC_ROOT / "style.css")
    assert "/static/style.css" in html
    assert "/static/operator.js" in html
    assert "operator-theme" in html
    assert "operator-theme" in js
    assert "theme-toggle" in js
    assert 'data-theme="dark"' not in html
    assert ':root[data-theme="dark"]' in css


def test_operator_registration_clears_conflicting_selected_person_id() -> None:
    js = _text(STATIC_ROOT / "operator.js")
    assert "prepareFaceRegistrationFormData" in js
    assert "fd.delete(\"person_id\")" in js
    assert "selectedExternalPersonId" in js
    assert "fillRegistrationForPerson" in js


def test_operator_face_registration_requires_explicit_append_mode() -> None:
    js = _text(STATIC_ROOT / "operator.js")
    assert "setFaceRegistrationMode" in js
    assert "clearFaceRegistrationIdentityFields" in js
    assert 'faceRegistrationMode === "append"' in js
    assert '点击“追加到当前人员”后再上传新照片' in js
    assert 'registerNewPersonBtn?.addEventListener("click"' in js
    assert 'appendSelectedPersonBtn?.addEventListener("click"' in js


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
        assert 'const CAMERA_INDEX_API = "/api/v1/cameras";' in evidence_js
        assert "function loadCameraNameLookup" in evidence_js
        assert "camera_name" in evidence_js
        assert "cameraNameLookup.get(`source_id:${sourceId}`)" in evidence_js
        assert "|| textOrNull(value.source_id)" not in evidence_js
        assert "|| textOrNull(value.camera_id)" not in evidence_js
        assert "setText(\"sourceRawId\"" in evidence_js
