from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "services" / "evidence-viewer" / "app" / "static"
VIEWER_MAIN = ROOT / "services" / "evidence-viewer" / "app" / "main.py"
COMPOSE = ROOT / "infra" / "docker-compose.midterm.yml"
ENV_FILE = ROOT / "infra" / "env" / "midterm.env"
MIGRATION = ROOT / "db" / "migrations" / "013_storage_maintenance_audit.sql"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_operator_has_chinese_storage_maintenance_entry() -> None:
    html = _text(STATIC_ROOT / "index.html")
    js = _text(STATIC_ROOT / "maintenance.js")
    assert 'data-view="maintenance"' in html
    assert "存储维护" in html
    assert "/static/maintenance.js" in html
    assert "/static/operator.js?v=" in html
    assert "operator-trajectory-ssd-cache-20260712" in html
    assert "maintenance-execute-control-20260627" in html
    assert "删除后不会自动重新生成证据" in html
    assert "删除必须先 preview" in html
    assert "删除必须先 preview" in js
    assert 'id="maintenance-execute-toggle"' in html
    assert 'id="apply-maintenance-execute-control"' in html
    assert "/execution-control" in js
    assert "删除执行未开启" in js
    assert "8000" not in html
    assert "8000" not in js


def test_maintenance_delete_requires_preview_in_frontend() -> None:
    html = _text(STATIC_ROOT / "index.html")
    js = _text(STATIC_ROOT / "maintenance.js")
    assert 'id="execute-evidence-delete"' in html
    assert 'name="time_from"' in html
    assert 'name="time_to"' in html
    assert "按时间删除证据" in html
    assert "人脸清理" not in html
    assert "Job 详情" not in html
    assert "allow_stale_pending_tasks" in js
    assert 'id="preview-delete-current-evidence"' in html
    assert "删除当前证据" in html
    assert 'id="delete-dialog"' in html
    assert 'id="execute-delete-dialog"' in html
    assert 'id="delete-dialog-pending-action"' in html
    assert 'id="repreview-delete-dialog"' in html
    assert "允许待处理后重新预览" in html
    assert "executeActiveDelete" in js
    assert 'disabled>确认删除' in html
    assert "确认删除预览对象" not in html
    assert "maintenanceState.preview" in js
    assert "maintenanceState.facePreview" in js
    assert "preview_id" in js
    assert "confirm_token" in js
    assert "candidate_hash" in js
    assert "window.confirm" in js
    assert '"/people/delete"' in js
    assert '"/people/gallery-delete"' in js
    assert '"/face-media/orphans-cleanup"' not in js
    assert "active_write_guard" in js
    assert "写入保护期" in js
    assert "task_pending" in js
    assert "待处理" in js
    assert "shouldOfferPendingRepreview" in js
    assert "allow_stale_pending_tasks: true" in js


def test_evidence_and_people_pages_open_maintenance_delete_preview() -> None:
    html = _text(STATIC_ROOT / "index.html")
    operator_js = _text(STATIC_ROOT / "operator.js")
    evidence_js = _text(STATIC_ROOT / "evidence.js")
    maintenance_js = _text(STATIC_ROOT / "maintenance.js")
    assert 'id="preview-delete-selected-person"' in html
    assert "删除当前人员" in html
    assert "openDeleteDialog" in operator_js
    assert "loadEvidenceCount" in operator_js
    assert '"/storage/summary"' in maintenance_js
    assert "ACTIVE_VIEW_STORAGE_KEY" in operator_js
    assert "EVIDENCE_COUNT_STORAGE_KEY" in operator_js
    assert "initialTopView" in operator_js
    assert "restoreEvidenceCount" in operator_js
    assert "persistTopView(normalized)" in operator_js
    assert "window.operatorEvidence?.pause" in operator_js
    assert "selectedPersonDeleteRequest" in operator_js
    assert "galleryDeleteRequest" in operator_js
    assert "删除照片" in operator_js
    assert "系统 ID" in operator_js
    assert "图库 ID" in operator_js
    assert "previewDeleteCurrentEvidence" in evidence_js
    assert "currentEvidenceDeleteRequest" in evidence_js
    assert "initPromise" in evidence_js
    assert "selectFirstAvailableBundle" in evidence_js
    assert "bundle_detail_load_failed" in evidence_js
    assert "EVIDENCE_STATE_STORAGE_KEY" in evidence_js
    assert "persistEvidenceState" in evidence_js
    assert "restoreEvidenceState" in evidence_js
    assert "syncCategoryButtons" in evidence_js
    assert "FILTER_INPUT_IDS" in evidence_js
    assert "BUNDLE_PAGE_SIZE = 50" in evidence_js
    assert "startOverlayLoop" in evidence_js
    assert "stopOverlayLoop" in evidence_js
    assert "cancelAnimationFrame" in evidence_js
    assert 'window.location.hash === "#evidence"' in evidence_js
    assert "openDeleteDialog" in maintenance_js
    assert "ensureMaintenanceEventsBound" in maintenance_js
    assert "void loadMaintenanceSummary()" in maintenance_js
    assert "showActiveDelete(request)" in maintenance_js
    assert "previewActiveDelete(request)" in maintenance_js
    assert "activePreviewBody" in maintenance_js
    assert "maintenanceDom.activeResult" in maintenance_js
    assert "activateTopView(\"maintenance\", true)" not in operator_js


def test_viewer_proxy_allowlist_includes_maintenance() -> None:
    main = _text(VIEWER_MAIN)
    assert "OPERATOR_PROXY_ALLOWED_PREFIXES" in main
    assert '"maintenance"' in main


def test_midterm_keeps_8090_only_and_no_8000_host_port() -> None:
    compose = yaml.safe_load(_text(COMPOSE))
    api = compose["services"]["api"]
    viewer = compose["services"]["evidence-viewer"]
    assert viewer["ports"] == ["8090:8090"]
    assert api["expose"] == ["8000"]
    assert "ports" not in api
    assert "/data/video-analytics/media/evidence:/evidence:ro" in viewer["volumes"]
    assert "../modules/savant_security/config:/app/modules/savant_security/config:ro" in viewer["volumes"]
    assert "../infra/generated:/app/infra/generated:ro" in viewer["volumes"]
    assert viewer["environment"]["OPERATOR_API_BASE_URL"] == "http://api:8000"
    assert viewer["environment"]["EVIDENCE_CAMERA_CONFIG_PATH"] == (
        "/app/modules/savant_security/config/cameras.midterm.yml"
    )
    assert viewer["environment"]["EVIDENCE_SOURCES_CONFIG_PATH"] == (
        "/app/infra/generated/sources.generated.yml"
    )


def test_midterm_preview_maintenance_flags_are_explicit_defaults() -> None:
    compose = yaml.safe_load(_text(COMPOSE))
    api_env = compose["services"]["api"]["environment"]
    env_text = _text(ENV_FILE)
    assert api_env["STORAGE_MAINTENANCE_SUMMARY_ENABLED"] == "${STORAGE_MAINTENANCE_SUMMARY_ENABLED:-true}"
    assert api_env["STORAGE_MAINTENANCE_PREVIEW_ENABLED"] == "${STORAGE_MAINTENANCE_PREVIEW_ENABLED:-true}"
    assert api_env["STORAGE_MAINTENANCE_EXECUTE_ENABLED"] == "${STORAGE_MAINTENANCE_EXECUTE_ENABLED:-false}"
    assert "STORAGE_MAINTENANCE_SUMMARY_ENABLED=true" in env_text
    assert "STORAGE_MAINTENANCE_PREVIEW_ENABLED=true" in env_text
    assert "STORAGE_MAINTENANCE_EXECUTE_ENABLED=true" in env_text


def test_maintenance_schema_has_explicit_candidate_hash_and_preview_expiry() -> None:
    migration = _text(MIGRATION)
    assert "candidate_hash TEXT" in migration
    assert "preview_expires_at TIMESTAMPTZ" in migration
    assert "CREATE TABLE IF NOT EXISTS maintenance_job_items" in migration


def test_maintenance_event_delete_sql_casts_jsonb_parameters() -> None:
    repository = _text(ROOT / "services" / "api" / "app" / "repositories" / "maintenance.py")
    assert "media_status = %(media_status)s::text" in repository
    assert "'deleted_by', %(operator)s::text" in repository
    assert "'delete_reason', %(reason)s::text" in repository
    assert "'delete_job_id', %(job_id)s::text" in repository
    assert "error_message = %(message)s::text" in repository
