"""8090 operator portal static asset contract tests."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "services" / "evidence-viewer" / "app" / "static"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_8090_operator_portal_contains_camera_face_and_evidence_views() -> None:
    html = _text(STATIC_ROOT / "index.html")
    assert 'lang="zh-CN"' in html
    assert "视频分析操作台" in html
    assert 'data-view="cameras"' in html
    assert 'data-view="people"' in html
    assert 'data-view="evidence"' in html
    assert "摄像头管理" in html
    assert "人员与人脸" in html
    assert "告警证据" in html
    assert "人脸注册" in html
    assert "人脸图库" in html
    assert "证据复核" in html
    assert "/static/operator.js" in html
    assert "/static/evidence.js" in html
    assert "/operator/static" not in html


def test_operator_portal_keeps_customer_fields_simple() -> None:
    html = _text(STATIC_ROOT / "index.html")
    assert "摄像头ID" not in html
    assert "数据源ID" not in html
    assert "GPU 编号" not in html
    assert "RTSP传输" not in html
    assert "质量阈值" not in html
    assert "允许多人脸" not in html
    assert "操作员" not in html
    assert "internal-config" in html
    assert "internal-debug" in html
    assert "allow_multiple_faces" in html
    assert "quality_threshold" in html
    assert "keep_crop" in html


def test_operator_js_uses_same_origin_proxy_for_camera_and_people_apis() -> None:
    js = _text(STATIC_ROOT / "operator.js")
    assert 'const API = "/api/v1"' in js
    assert "`${API}/cameras`" in js
    assert "`${API}/people" in js
    assert "`${API}/people/register-face`" in js
    assert "FormData(faceRegistrationForm)" in js
    assert "dev_mock" not in js
    assert "localhost" not in js
    assert "127.0.0.1" not in js


def test_operator_evidence_js_uses_8090_native_evidence_api_and_categories() -> None:
    js = _text(STATIC_ROOT / "evidence.js")
    assert 'const EVIDENCE_API = "/api"' in js
    assert 'fetchJson("/health")' in js
    assert "`${EVIDENCE_API}/bundles?${bundleQueryString()}`" in js
    assert "categoryFilteredBundles" in js
    assert "eventCategoryForType(bundle.event_type)" in js
    assert "event_category" not in js
    assert "/api/v1/evidence" not in js
    assert "名单布控" in js
    assert "周界入侵" in js
    assert "行为异常" in js
    assert "聚集风险" in js


def test_operator_portal_css_matches_unified_console_layout() -> None:
    css = _text(STATIC_ROOT / "style.css")
    for token in (
        ".app-shell",
        ".sidebar",
        ".top-tab.active",
        ".dashboard-summary",
        ".camera-workspace",
        ".people-workspace",
        ".evidence-workspace",
        ".category-chip.active",
        ".gallery-thumb",
    ):
        assert token in css


def test_operator_portal_static_has_no_hardcoded_local_addresses() -> None:
    joined = "\n".join(
        _text(STATIC_ROOT / name)
        for name in ("index.html", "operator.js", "evidence.js", "style.css")
    )
    assert "localhost" not in joined
    assert "127.0.0.1" not in joined
    assert "http://" not in joined
    assert "https://" not in joined
