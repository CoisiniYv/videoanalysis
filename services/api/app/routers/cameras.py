"""Camera / zone / rule configuration endpoints — /api/v1/cameras/* (Phase C1).

Reuses the {data, error, request_id} response envelope and the
psycopg-based repository pattern from app.routers.events.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import psycopg
import yaml
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from app.db import get_conn
from app.repositories.cameras import CameraRepository
from app.schemas.cameras import (
    CameraConfigResponse,
    CameraCreate,
    CameraResponse,
    RuleCreate,
    RuleResponse,
    ZoneCreate,
    ZoneResponse,
    apply_intrusion_defaults,
    build_export_doc,
    validate_intrusion_config,
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
        row = repo.create_camera(
            camera_id=body.id,
            source_id=body.source_id,
            name=body.name,
            rtsp_url=body.rtsp_url,
            site_id=body.site_id,
            location=body.location,
            gpu_id=body.gpu_id,
            enabled=body.enabled,
        )
    except psycopg.errors.UniqueViolation as exc:
        return _err_response(409, f"unique constraint violation: {exc}", request_id)
    return _ok(CameraResponse.from_db_row(row).model_dump(), request_id)


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
        row = repo.create_zone(
            camera_id=camera_id,
            zone_name=body.zone_name,
            zone_type=body.zone_type,
            points=body.points,
            payload=body.payload,
        )
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

    # Reject duplicate (camera, rule_type) for C1-lite.
    existing_types = {r["rule_type"] for r in repo.list_rules(camera_id)}
    if body.rule_type in existing_types:
        return _err_response(
            409,
            f"rule_type already exists for camera {camera_id!r}: {body.rule_type!r}",
            request_id,
        )

    config = dict(body.config or {})
    if body.rule_type == "intrusion":
        config = apply_intrusion_defaults(config)
        ok, err = validate_intrusion_config(config, repo.get_zone_names(camera_id))
        if not ok:
            return _err_response(400, err, request_id)

    try:
        row = repo.create_rule(
            camera_id=camera_id,
            rule_type=body.rule_type,
            enabled=body.enabled,
            config=config,
        )
    except psycopg.errors.UniqueViolation as exc:
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
    )
    return _ok(payload.model_dump(), request_id)
