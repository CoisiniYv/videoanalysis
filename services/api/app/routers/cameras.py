"""Camera / zone / rule configuration endpoints — /api/v1/cameras/* (camera config).

Reuses the {data, error, request_id} response envelope and the
psycopg-based repository pattern from app.routers.events.
"""

from __future__ import annotations

import uuid
import os
from typing import Any, Dict, List

import psycopg
import yaml
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from app.algorithm_ids import RULE_ALGORITHM_IDS, runtime_rule_type_for_algorithm_id
from app.db import get_conn
from app.repositories.cameras import CameraRepository
from app.runtime_config_export import (
    ExportOptions,
    ExportValidationError,
    export_runtime_config,
)
from app.schemas.cameras import (
    AlertPolicy,
    CameraConfigResponse,
    CameraCreate,
    CameraResponse,
    CameraUpdate,
    RuleCreate,
    RuleResponse,
    RuleUpdate,
    ZoneCreate,
    ZoneResponse,
    apply_intrusion_defaults,
    build_export_doc,
    validate_intrusion_config,
)
from app.services.runtime_apply import (
    RuntimeApplyError,
    apply_camera_runtime,
    restart_camera_runtime,
    sync_camera_runtime_config_and_sources,
)
from app.services.savant_supervisor import (
    SavantSupervisorError,
    get_savant_supervisor_snapshot,
    trigger_savant_recovery,
)


router = APIRouter(prefix="/api/v1/cameras", tags=["cameras"])


# ---------------------------------------------------------------------------
# Shared envelope helpers (kept local to avoid a circular import with events.py)
# ---------------------------------------------------------------------------


def _request_id(request: Request) -> str:
    return str(uuid.uuid4())


def _repo() -> CameraRepository:
    conn = get_conn()
    try:
        yield CameraRepository(conn)
    finally:
        conn.close()


def _ok(data: object, request_id: str) -> dict:
    return {"data": data, "error": None, "request_id": request_id}


def _err(message: str, request_id: str, status_code: int = 404) -> dict:
    return {
        "data": None,
        "error": {"message": message, "code": status_code},
        "request_id": request_id,
    }


def _err_response(status_code: int, message: str, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_err(message, request_id, status_code=status_code),
    )


def _runtime_source_apply_payload(repo: CameraRepository) -> dict[str, Any]:
    if not _env_bool("CAMERA_RUNTIME_APPLY_ENABLED", default=False):
        return {"ok": False, "skipped": "camera runtime control is disabled"}
    cameras, export_doc = _runtime_config_docs(repo)
    try:
        return {
            "ok": True,
            "result": sync_camera_runtime_config_and_sources(
                export_doc=export_doc,
                cameras=cameras,
            ),
        }
    except RuntimeApplyError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"runtime source apply filesystem error: {exc}"}


def _runtime_config_docs(
    repo: CameraRepository,
    *,
    include_disabled: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cameras = repo.list_cameras() if include_disabled else repo.list_cameras(enabled=True)
    camera_ids = [c["id"] for c in cameras]
    zones_by_camera = repo.list_zones_for_cameras(camera_ids)
    rules_by_camera = repo.list_rules_for_cameras(camera_ids)
    return cameras, build_export_doc(cameras, zones_by_camera, rules_by_camera)


def _camera_response_with_runtime(row: dict[str, Any], runtime_payload: dict[str, Any]) -> dict[str, Any]:
    payload = CameraResponse.from_db_row(row).model_dump()
    payload["runtime_source_apply"] = runtime_payload
    return payload


def _supports_kw(callable_obj: object, name: str) -> bool:
    import inspect

    try:
        params = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# /api/v1/cameras/config/export  (yaml)
#
# IMPORTANT: this route is declared BEFORE the parameterised
# ``/{camera_id}/...`` routes so FastAPI's longest-static-match resolution
# does not capture "config" as a camera_id.
# ---------------------------------------------------------------------------


@router.get("/config/export")
def cameras_export(
    include_disabled: bool = Query(
        False, description="When true, disabled cameras are exported with enabled=false."
    ),
    repo: CameraRepository = Depends(_repo),
):
    if include_disabled:
        cameras = repo.list_cameras()
    else:
        cameras = repo.list_cameras(enabled=True)

    camera_ids = [c["id"] for c in cameras]
    zones_by_camera = repo.list_zones_for_cameras(camera_ids)
    rules_by_camera = repo.list_rules_for_cameras(camera_ids)

    doc = build_export_doc(cameras, zones_by_camera, rules_by_camera)
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    return Response(content=text, media_type="text/yaml")


@router.post("/runtime/apply")
def cameras_runtime_apply(
    include_disabled: bool = Query(
        True, description="Include disabled cameras in exported runtime files."
    ),
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    cameras = repo.list_cameras() if include_disabled else repo.list_cameras(enabled=True)
    camera_ids = [c["id"] for c in cameras]
    zones_by_camera = repo.list_zones_for_cameras(camera_ids)
    rules_by_camera = repo.list_rules_for_cameras(camera_ids)
    export_doc = build_export_doc(cameras, zones_by_camera, rules_by_camera)
    try:
        result = apply_camera_runtime(export_doc=export_doc, cameras=cameras)
    except RuntimeApplyError as exc:
        return _err_response(503, str(exc), request_id)
    except OSError as exc:
        return _err_response(503, f"runtime apply filesystem error: {exc}", request_id)
    return _ok(result, request_id)


@router.post("/runtime/restart")
def cameras_runtime_restart(
    include_disabled: bool = Query(
        True, description="Include disabled cameras in exported runtime files."
    ),
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    cameras = repo.list_cameras() if include_disabled else repo.list_cameras(enabled=True)
    camera_ids = [c["id"] for c in cameras]
    zones_by_camera = repo.list_zones_for_cameras(camera_ids)
    rules_by_camera = repo.list_rules_for_cameras(camera_ids)
    export_doc = build_export_doc(cameras, zones_by_camera, rules_by_camera)
    try:
        result = restart_camera_runtime(export_doc=export_doc, cameras=cameras)
    except RuntimeApplyError as exc:
        return _err_response(503, str(exc), request_id)
    except OSError as exc:
        return _err_response(503, f"runtime restart filesystem error: {exc}", request_id)
    return _ok(result, request_id)


@router.post("/runtime/sources/apply")
def cameras_runtime_sources_apply(
    include_disabled: bool = Query(
        True, description="Include disabled cameras so stale source adapters are stopped."
    ),
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    cameras, export_doc = _runtime_config_docs(repo, include_disabled=include_disabled)
    try:
        result = sync_camera_runtime_config_and_sources(
            export_doc=export_doc,
            cameras=cameras,
        )
    except RuntimeApplyError as exc:
        return _err_response(503, str(exc), request_id)
    except OSError as exc:
        return _err_response(503, f"runtime source apply filesystem error: {exc}", request_id)
    return _ok(result, request_id)


@router.get("/{camera_id}/runtime-config")
def cameras_runtime_config_preview(
    camera_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    try:
        result = export_runtime_config(
            repo,
            ExportOptions(camera_id=camera_id, include_disabled=True, dry_run=True),
        )
    except ExportValidationError as exc:
        return _err_response(
            400,
            f"runtime config validation failed: {exc.errors}",
            request_id,
        )

    runtime_cameras = result.algorithm_runtime_config.get("cameras") or []
    runtime_camera = next(
        (
            row
            for row in runtime_cameras
            if str(row.get("camera_id") or "") == str(camera_id)
        ),
        None,
    )
    summary_cameras = result.export_summary.get("cameras") or []
    summary_camera = next(
        (
            row
            for row in summary_cameras
            if str(row.get("camera_id") or "") == str(camera_id)
        ),
        None,
    )
    cameras_doc = result.cameras_generated_doc.get("cameras") or {}
    return _ok(
        {
            "camera_id": camera_id,
            "generated_at": result.export_summary.get("generated_at"),
            "paths": result.paths_dict(),
            "cameras_midterm_yml": cameras_doc.get(camera_id),
            "algorithm_runtime_config": runtime_camera,
            "export_summary": summary_camera,
            "apply_plan": result.apply_plan,
        },
        request_id,
    )


@router.get("/runtime/supervisor")
def cameras_runtime_supervisor_status(
    request_id: str = Depends(_request_id),
):
    try:
        return _ok(get_savant_supervisor_snapshot(), request_id)
    except (RuntimeApplyError, SavantSupervisorError) as exc:
        return _err_response(503, str(exc), request_id)


@router.post("/runtime/supervisor/recover")
def cameras_runtime_supervisor_recover(
    request_id: str = Depends(_request_id),
):
    try:
        return _ok(trigger_savant_recovery(), request_id)
    except (RuntimeApplyError, SavantSupervisorError) as exc:
        return _err_response(503, str(exc), request_id)


# ---------------------------------------------------------------------------
# POST /api/v1/cameras
# ---------------------------------------------------------------------------


@router.post("")
def cameras_create(
    body: CameraCreate,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(body.id) is not None:
        return _err_response(409, f"camera already exists: {body.id}", request_id)
    try:
        kwargs = {
            "camera_id": body.id,
            "source_id": body.source_id,
            "name": body.name,
            "rtsp_url": body.rtsp_url,
            "site_id": body.site_id,
            "location": body.location,
            "gpu_id": body.gpu_id,
            "enabled": body.enabled,
        }
        if _supports_kw(repo.create_camera, "input_type"):
            kwargs.update(
                {
                    "input_type": body.input_type,
                    "rtsp_transport": body.rtsp_transport,
                    "fps_policy": body.fps_policy,
                    "alert_policy": body.alert_policy,
                }
            )
        row = repo.create_camera(**kwargs)
    except psycopg.errors.UniqueViolation as exc:
        return _err_response(409, f"unique constraint violation: {exc}", request_id)
    runtime_payload = _runtime_source_apply_payload(repo)
    return _ok(_camera_response_with_runtime(row, runtime_payload), request_id)


# ---------------------------------------------------------------------------
# GET /api/v1/cameras
# ---------------------------------------------------------------------------


@router.get("")
def cameras_list(
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    rows = repo.list_cameras()
    cameras = [CameraResponse.from_db_row(r).model_dump() for r in rows]
    return _ok({"cameras": cameras, "total": len(cameras)}, request_id)


# ---------------------------------------------------------------------------
# GET /api/v1/cameras/{camera_id}
# ---------------------------------------------------------------------------


@router.get("/{camera_id}")
def cameras_get(
    camera_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    row = repo.get_camera(camera_id)
    if row is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    return _ok(CameraResponse.from_db_row(row).model_dump(), request_id)


@router.put("/{camera_id}")
def cameras_update(
    camera_id: str,
    body: CameraUpdate,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    if not hasattr(repo, "update_camera"):
        return _err_response(501, "camera update is not supported by repository", request_id)
    try:
        row = repo.update_camera(camera_id, **body.model_dump(exclude_unset=True))
    except ValueError as exc:
        return _err_response(409, str(exc), request_id)
    runtime_payload = _runtime_source_apply_payload(repo)
    return _ok(_camera_response_with_runtime(row, runtime_payload), request_id)


def _set_camera_enabled(
    camera_id: str,
    enabled: bool,
    repo: CameraRepository,
    request_id: str,
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    if hasattr(repo, "set_camera_enabled"):
        row = repo.set_camera_enabled(camera_id, enabled)
    else:
        row = repo.get_camera(camera_id)
        row["enabled"] = enabled
    runtime_payload = _runtime_source_apply_payload(repo)
    return _ok(_camera_response_with_runtime(row, runtime_payload), request_id)


@router.post("/{camera_id}/enable")
def cameras_enable(
    camera_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    return _set_camera_enabled(camera_id, True, repo, request_id)


@router.post("/{camera_id}/disable")
def cameras_disable(
    camera_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    return _set_camera_enabled(camera_id, False, repo, request_id)


# ---------------------------------------------------------------------------
# POST /api/v1/cameras/{camera_id}/zones
# ---------------------------------------------------------------------------


@router.post("/{camera_id}/zones")
def cameras_create_zone(
    camera_id: str,
    body: ZoneCreate,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    try:
        body.validate_for_zone_type()
    except ValueError as exc:
        return _err_response(400, str(exc), request_id)

    if body.zone_name in repo.get_zone_names(camera_id):
        return _err_response(
            409,
            f"zone_name already exists for camera {camera_id!r}: {body.zone_name!r}",
            request_id,
        )

    try:
        kwargs = {
            "camera_id": camera_id,
            "zone_name": body.zone_name,
            "zone_type": body.zone_type,
            "points": body.points,
            "payload": body.payload,
        }
        if _supports_kw(repo.create_zone, "zone_id"):
            kwargs.update(
                {
                    "zone_id": body.zone_id,
                    "coordinate_space": body.coordinate_space,
                    "enabled": body.enabled,
                }
            )
        row = repo.create_zone(**kwargs)
    except psycopg.errors.UniqueViolation as exc:
        return _err_response(409, f"unique constraint violation: {exc}", request_id)
    return _ok(ZoneResponse.from_db_row(row).model_dump(), request_id)


# ---------------------------------------------------------------------------
# GET /api/v1/cameras/{camera_id}/zones
# ---------------------------------------------------------------------------


@router.get("/{camera_id}/zones")
def cameras_list_zones(
    camera_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    rows = repo.list_zones(camera_id)
    return _ok(
        {"zones": [ZoneResponse.from_db_row(r).model_dump() for r in rows]},
        request_id,
    )


@router.put("/{camera_id}/zones/{zone_id}")
def cameras_update_zone(
    camera_id: str,
    zone_id: str,
    body: ZoneCreate,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    try:
        body.validate_for_zone_type()
    except ValueError as exc:
        return _err_response(400, str(exc), request_id)
    if not hasattr(repo, "update_zone"):
        return _err_response(501, "zone update is not supported by repository", request_id)
    row = repo.update_zone(
        camera_id=camera_id,
        zone_id=zone_id,
        new_zone_id=body.zone_id,
        zone_name=body.zone_name,
        zone_type=body.zone_type,
        coordinate_space=body.coordinate_space,
        points=body.points,
        enabled=body.enabled,
        payload=body.payload,
    )
    if row is None:
        return _err_response(404, f"zone not found: {zone_id}", request_id)
    return _ok(ZoneResponse.from_db_row(row).model_dump(), request_id)


@router.delete("/{camera_id}/zones/{zone_id}")
def cameras_delete_zone(
    camera_id: str,
    zone_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    if not hasattr(repo, "delete_zone") or not repo.delete_zone(camera_id, zone_id):
        return _err_response(404, f"zone not found: {zone_id}", request_id)
    return _ok({"deleted": True, "zone_id": zone_id}, request_id)


# ---------------------------------------------------------------------------
# POST /api/v1/cameras/{camera_id}/rules
# ---------------------------------------------------------------------------


@router.post("/{camera_id}/rules")
def cameras_create_rule(
    camera_id: str,
    body: RuleCreate,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)

    existing = repo.list_rules(camera_id)
    existing_rule_ids = {str(r.get("rule_id") or r.get("id")) for r in existing}
    existing_types = {r["rule_type"] for r in existing}
    if body.rule_id in existing_rule_ids or (
        body.algorithm_id not in RULE_ALGORITHM_IDS and body.rule_type in existing_types
    ):
        return _err_response(
            409,
            f"rule already exists for camera {camera_id!r}: {body.rule_id!r}",
            request_id,
        )

    config = dict(body.config or {})
    if body.rule_type == "intrusion":
        if "zone_id" in config and "zone" not in config:
            config["zone"] = config["zone_id"]
        config = apply_intrusion_defaults(config)
        ok, err = validate_intrusion_config(config, repo.get_zone_names(camera_id))
        if not ok:
            return _err_response(400, err, request_id)

    try:
        kwargs = {
            "camera_id": camera_id,
            "rule_type": body.rule_type,
            "enabled": body.enabled,
            "config": config,
        }
        if _supports_kw(repo.create_rule, "rule_id"):
            kwargs.update({"rule_id": body.rule_id, "algorithm_id": body.algorithm_id})
        row = repo.create_rule(**kwargs)
    except Exception as exc:
        if exc.__class__.__name__ != "UniqueViolation":
            raise
        return _err_response(409, f"unique constraint violation: {exc}", request_id)
    return _ok(RuleResponse.from_db_row(row).model_dump(), request_id)


# ---------------------------------------------------------------------------
# GET /api/v1/cameras/{camera_id}/rules
# ---------------------------------------------------------------------------


@router.get("/{camera_id}/rules")
def cameras_list_rules(
    camera_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    rows = repo.list_rules(camera_id)
    return _ok(
        {"rules": [RuleResponse.from_db_row(r).model_dump() for r in rows]},
        request_id,
    )


@router.get("/{camera_id}/rules/{rule_id}")
def cameras_get_rule(
    camera_id: str,
    rule_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    if hasattr(repo, "get_rule"):
        row = repo.get_rule(camera_id, rule_id)
    else:
        row = next(
            (
                r
                for r in repo.list_rules(camera_id)
                if str(r.get("id")) == rule_id
                or str(r.get("rule_id", "")) == rule_id
                or r.get("rule_type") == rule_id
            ),
            None,
        )
    if row is None:
        return _err_response(404, f"rule not found: {rule_id}", request_id)
    return _ok(RuleResponse.from_db_row(row).model_dump(), request_id)


@router.put("/{camera_id}/rules/{rule_id}")
def cameras_update_rule(
    camera_id: str,
    rule_id: str,
    body: RuleUpdate,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    if not hasattr(repo, "update_rule"):
        return _err_response(501, "rule update is not supported by repository", request_id)

    algorithm_id = body.algorithm_id
    rule_type = body.rule_type or (
        runtime_rule_type_for_algorithm_id(algorithm_id) if algorithm_id else None
    )
    row = repo.update_rule(
        camera_id=camera_id,
        rule_id=rule_id,
        new_rule_id=body.rule_id,
        algorithm_id=algorithm_id,
        rule_type=rule_type,
        enabled=body.enabled,
        config=body.config,
    )
    if row is None:
        return _err_response(404, f"rule not found: {rule_id}", request_id)
    return _ok(RuleResponse.from_db_row(row).model_dump(), request_id)


@router.delete("/{camera_id}/rules/{rule_id}")
def cameras_delete_rule(
    camera_id: str,
    rule_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    if not hasattr(repo, "delete_rule") or not repo.delete_rule(camera_id, rule_id):
        return _err_response(404, f"rule not found: {rule_id}", request_id)
    return _ok({"deleted": True, "rule_id": rule_id}, request_id)


def _set_rule_enabled(
    camera_id: str,
    rule_id: str,
    enabled: bool,
    repo: CameraRepository,
    request_id: str,
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    if hasattr(repo, "set_rule_enabled"):
        row = repo.set_rule_enabled(camera_id, rule_id, enabled)
    else:
        row = None
    if row is None:
        return _err_response(404, f"rule not found: {rule_id}", request_id)
    return _ok(RuleResponse.from_db_row(row).model_dump(), request_id)


@router.post("/{camera_id}/rules/{rule_id}/enable")
def cameras_enable_rule(
    camera_id: str,
    rule_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    return _set_rule_enabled(camera_id, rule_id, True, repo, request_id)


@router.post("/{camera_id}/rules/{rule_id}/disable")
def cameras_disable_rule(
    camera_id: str,
    rule_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    return _set_rule_enabled(camera_id, rule_id, False, repo, request_id)


@router.get("/{camera_id}/alert-policy")
def cameras_get_alert_policy(
    camera_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    row = repo.get_camera(camera_id)
    if row is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    return _ok(row.get("alert_policy") or {}, request_id)


@router.put("/{camera_id}/alert-policy")
def cameras_put_alert_policy(
    camera_id: str,
    body: AlertPolicy,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    policy = body.model_dump()
    if hasattr(repo, "set_alert_policy"):
        row = repo.set_alert_policy(camera_id, policy)
    else:
        row = repo.get_camera(camera_id)
        row["alert_policy"] = policy
    return _ok(CameraResponse.from_db_row(row).alert_policy, request_id)


# ---------------------------------------------------------------------------
# GET /api/v1/cameras/{camera_id}/config
# ---------------------------------------------------------------------------


@router.get("/{camera_id}/config")
def cameras_get_config(
    camera_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    cam = repo.get_camera(camera_id)
    if cam is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)

    zones = repo.list_zones(camera_id)
    rules = repo.list_rules(camera_id)

    payload = CameraConfigResponse(
        camera=CameraResponse.from_db_row(cam),
        zones=[ZoneResponse.from_db_row(z) for z in zones],
        rules=[RuleResponse.from_db_row(r) for r in rules],
        alert_policy=cam.get("alert_policy") or {},
    )
    return _ok(payload.model_dump(), request_id)
