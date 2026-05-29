"""R3 algorithm registry and camera algorithm-rule endpoints."""

from __future__ import annotations

import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.algorithm_registry import get_algorithm, list_algorithms
from app.db import get_conn
from app.repositories.cameras import CameraRepository
from app.schemas.algorithms import (
    AlgorithmRuleCreate,
    AlgorithmRuleResponse,
    AlgorithmRuleUpdate,
)

router = APIRouter(prefix="/api/v1", tags=["algorithms"])


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


@router.get("/algorithms")
def algorithms_list(request_id: str = Depends(_request_id)) -> dict:
    return _ok(
        {"algorithms": [a.model_dump() for a in list_algorithms()]},
        request_id,
    )


@router.get("/algorithms/{algorithm_type}")
def algorithms_get(
    algorithm_type: str,
    request_id: str = Depends(_request_id),
) -> dict:
    definition = get_algorithm(algorithm_type)
    if definition is None:
        return _err_response(
            404, f"unknown algorithm_type: {algorithm_type}", request_id
        )
    return _ok(definition.model_dump(), request_id)


def _validate_zone_line(
    *,
    camera_id: str,
    body: AlgorithmRuleCreate | AlgorithmRuleUpdate,
    repo: CameraRepository,
) -> str:
    zones = repo.list_zones(camera_id)
    zone_names = {z["zone_name"] for z in zones}
    line_names = {
        z["zone_name"]
        for z in zones
        if z["zone_type"] in ("line", "direction_line")
    }

    zone_id = getattr(body, "zone_id", None)
    line_id = getattr(body, "line_id", None)

    if zone_id and zone_id not in zone_names:
        return f"zone_id does not belong to camera {camera_id!r}: {zone_id!r}"
    if line_id and line_id not in line_names:
        return f"line_id does not reference a line zone on camera {camera_id!r}: {line_id!r}"
    return ""


def _merge_and_validate_config(
    algorithm_type: str, config: Dict[str, Any]
) -> tuple[Dict[str, Any] | None, str]:
    definition = get_algorithm(algorithm_type)
    if definition is None:
        return None, f"unknown algorithm_type: {algorithm_type}"

    merged = {**definition.default_config, **(config or {})}
    schema = definition.config_schema or {}
    required = schema.get("required", [])
    for field in required:
        if field not in merged:
            return None, f"config.{field} is required for {algorithm_type}"

    properties = schema.get("properties", {})
    for field, spec in properties.items():
        if field not in merged:
            continue
        value = merged[field]
        if spec.get("type") == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return None, f"config.{field} must be an integer"
            if "minimum" in spec and value < spec["minimum"]:
                return None, f"config.{field} must be >= {spec['minimum']}"

    return merged, ""


@router.post("/cameras/{camera_id}/algorithm-rules")
def camera_algorithm_rules_create(
    camera_id: str,
    body: AlgorithmRuleCreate,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)

    definition = get_algorithm(body.algorithm_type)
    if definition is None:
        return _err_response(
            400, f"unknown algorithm_type: {body.algorithm_type}", request_id
        )
    if body.zone_id and not definition.supports_roi:
        return _err_response(
            400, f"{body.algorithm_type} does not support zone_id", request_id
        )
    if body.line_id and not definition.supports_line:
        return _err_response(
            400, f"{body.algorithm_type} does not support line_id", request_id
        )

    zone_error = _validate_zone_line(camera_id=camera_id, body=body, repo=repo)
    if zone_error:
        return _err_response(400, zone_error, request_id)

    config, error = _merge_and_validate_config(body.algorithm_type, body.config)
    if error:
        return _err_response(400, error, request_id)

    row = repo.create_algorithm_rule(
        camera_id=camera_id,
        algorithm_type=body.algorithm_type,
        enabled=body.enabled,
        zone_id=body.zone_id,
        line_id=body.line_id,
        config=config or {},
        evidence_policy=body.evidence_policy.model_dump(),
    )
    return _ok(AlgorithmRuleResponse.from_db_row(row).model_dump(), request_id)


@router.get("/cameras/{camera_id}/algorithm-rules")
def camera_algorithm_rules_list(
    camera_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    rows = repo.list_algorithm_rules(camera_id)
    return _ok(
        {
            "rules": [
                AlgorithmRuleResponse.from_db_row(row).model_dump()
                for row in rows
            ]
        },
        request_id,
    )


@router.put("/cameras/{camera_id}/algorithm-rules/{rule_id}")
def camera_algorithm_rules_update(
    camera_id: str,
    rule_id: int,
    body: AlgorithmRuleUpdate,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    existing = repo.get_algorithm_rule(camera_id, rule_id)
    if existing is None:
        return _err_response(404, f"algorithm rule not found: {rule_id}", request_id)

    zone_error = _validate_zone_line(camera_id=camera_id, body=body, repo=repo)
    if zone_error:
        return _err_response(400, zone_error, request_id)

    config = body.config
    if config is not None:
        merged, error = _merge_and_validate_config(existing["rule_type"], config)
        if error:
            return _err_response(400, error, request_id)
        config = merged

    row = repo.update_algorithm_rule(
        camera_id=camera_id,
        rule_id=rule_id,
        enabled=body.enabled,
        zone_id=body.zone_id,
        line_id=body.line_id,
        config=config,
        evidence_policy=(
            body.evidence_policy.model_dump()
            if body.evidence_policy is not None
            else None
        ),
    )
    return _ok(AlgorithmRuleResponse.from_db_row(row).model_dump(), request_id)


def _set_rule_enabled(
    camera_id: str,
    rule_id: int,
    enabled: bool,
    repo: CameraRepository,
    request_id: str,
) -> dict:
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)
    row = repo.set_algorithm_rule_enabled(camera_id, rule_id, enabled)
    if row is None:
        return _err_response(404, f"algorithm rule not found: {rule_id}", request_id)
    return _ok(AlgorithmRuleResponse.from_db_row(row).model_dump(), request_id)


@router.post("/cameras/{camera_id}/algorithm-rules/{rule_id}/enable")
def camera_algorithm_rules_enable(
    camera_id: str,
    rule_id: int,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    return _set_rule_enabled(camera_id, rule_id, True, repo, request_id)


@router.post("/cameras/{camera_id}/algorithm-rules/{rule_id}/disable")
def camera_algorithm_rules_disable(
    camera_id: str,
    rule_id: int,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    return _set_rule_enabled(camera_id, rule_id, False, repo, request_id)
