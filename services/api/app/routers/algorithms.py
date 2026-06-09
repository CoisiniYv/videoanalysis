"""Algorithm registry and camera algorithm-rule endpoints."""

from __future__ import annotations

import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.algorithm_ids import (
    BEHAVIOR_ALGORITHM_IDS,
    FACE_RULE_ALGORITHM_IDS,
    behavior_rule_type_for_algorithm_id,
    family_algorithm_id_for_rule,
    is_face_rule_algorithm_id,
    normalize_algorithm_id,
)
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
    algorithms = []
    for definition in list_algorithms():
        item = definition.model_dump()
        item["algorithm_type"] = item["algorithm_id"]
        algorithms.append(item)
    return _ok(
        {"algorithms": algorithms},
        request_id,
    )


@router.get("/algorithms/{algorithm_id}")
def algorithms_get(
    algorithm_id: str,
    request_id: str = Depends(_request_id),
) -> dict:
    definition = get_algorithm(algorithm_id)
    if definition is None:
        return _err_response(
            404, f"unknown algorithm_id: {algorithm_id}", request_id
        )
    data = definition.model_dump()
    data["algorithm_type"] = data["algorithm_id"]
    return _ok(data, request_id)


def _validate_zone_line(
    *,
    camera_id: str,
    body: AlgorithmRuleCreate | AlgorithmRuleUpdate,
    repo: CameraRepository,
) -> str:
    zones = repo.list_zones(camera_id)
    zone_names = {z.get("zone_id") or z["zone_name"] for z in zones}
    polygon_zone_names = {
        z.get("zone_id") or z["zone_name"]
        for z in zones
        if z["zone_type"] == "polygon"
    }
    line_names = {
        z.get("zone_id") or z["zone_name"]
        for z in zones
        if z["zone_type"] in ("line", "direction_line")
    }

    zone_id = getattr(body, "zone_id", None)
    line_id = getattr(body, "line_id", None)

    if zone_id and zone_id not in zone_names:
        return f"zone_id does not belong to camera {camera_id!r}: {zone_id!r}"
    if zone_id and zone_id not in polygon_zone_names:
        return f"zone_id must reference a polygon zone on camera {camera_id!r}: {zone_id!r}"
    if line_id and line_id not in line_names:
        return f"line_id does not reference a line zone on camera {camera_id!r}: {line_id!r}"
    return ""


def _merge_and_validate_config(
    algorithm_id: str,
    config: Dict[str, Any],
    *,
    zone_id: str | None = None,
    line_id: str | None = None,
    severity: str | None = None,
) -> tuple[Dict[str, Any] | None, str]:
    normalized = normalize_algorithm_id(algorithm_id)
    definition = get_algorithm(family_algorithm_id_for_rule(normalized))
    if definition is None:
        return None, f"unknown algorithm_id: {algorithm_id}"

    merged = {**definition.default_config, **(config or {})}
    if zone_id:
        merged["zone_id"] = zone_id
        merged.setdefault("zone", zone_id)
    if line_id:
        merged["line_id"] = line_id
    if severity:
        merged["severity"] = severity
    schema = definition.config_schema or {}
    required = schema.get("required", [])
    for field in required:
        if field not in merged:
            return None, f"config.{field} is required for {normalized}"

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


def _validate_algorithm_bindings(
    algorithm_id: str,
    *,
    zone_id: str | None,
    line_id: str | None,
) -> str:
    normalized = normalize_algorithm_id(algorithm_id)
    definition = get_algorithm(family_algorithm_id_for_rule(normalized))
    if definition is None:
        return f"unknown algorithm_id: {algorithm_id}"
    if normalized in BEHAVIOR_ALGORITHM_IDS and not normalized.startswith("behavior."):
        return f"behavior algorithm_id must use behavior.*: {algorithm_id}"
    if normalized in FACE_RULE_ALGORITHM_IDS and not is_face_rule_algorithm_id(normalized):
        return f"face algorithm_id must use face.*: {algorithm_id}"
    if normalized == "behavior.wall_climb_suspicious":
        if not line_id:
            return "line_id is required for behavior.wall_climb_suspicious"
    elif normalized in BEHAVIOR_ALGORITHM_IDS:
        if not zone_id:
            return f"zone_id is required for {normalized}"
    if zone_id and not definition.supports_roi:
        return f"{normalized} does not support zone_id"
    if line_id and not definition.supports_line:
        return f"{normalized} does not support line_id"
    return ""


@router.post("/cameras/{camera_id}/algorithm-rules")
def camera_algorithm_rules_create(
    camera_id: str,
    body: AlgorithmRuleCreate,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    if repo.get_camera(camera_id) is None:
        return _err_response(404, f"camera not found: {camera_id}", request_id)

    algorithm_id = body.algorithm_id or ""
    definition = get_algorithm(family_algorithm_id_for_rule(algorithm_id))
    if definition is None:
        return _err_response(
            400, f"unknown algorithm_id: {algorithm_id}", request_id
        )

    binding_error = _validate_algorithm_bindings(
        algorithm_id, zone_id=body.zone_id, line_id=body.line_id
    )
    if binding_error:
        return _err_response(400, binding_error, request_id)

    zone_error = _validate_zone_line(camera_id=camera_id, body=body, repo=repo)
    if zone_error:
        return _err_response(400, zone_error, request_id)

    config, error = _merge_and_validate_config(
        algorithm_id,
        body.config,
        zone_id=body.zone_id,
        line_id=body.line_id,
        severity=body.severity,
    )
    if error:
        return _err_response(400, error, request_id)

    row = repo.create_algorithm_rule(
        camera_id=camera_id,
        rule_id=body.rule_id,
        algorithm_id=algorithm_id,
        rule_type=behavior_rule_type_for_algorithm_id(algorithm_id) or algorithm_id,
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
    rule_id: str,
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

    algorithm_id = normalize_algorithm_id(
        body.algorithm_id
        or existing.get("algorithm_id")
        or existing.get("algorithm_type")
        or existing.get("rule_type", "")
    )
    zone_id = existing.get("zone_id") if body.zone_id is None else body.zone_id
    line_id = existing.get("line_id") if body.line_id is None else body.line_id

    binding_error = _validate_algorithm_bindings(
        algorithm_id, zone_id=zone_id, line_id=line_id
    )
    if binding_error:
        return _err_response(400, binding_error, request_id)

    config = body.config
    if config is not None:
        merged, error = _merge_and_validate_config(
            algorithm_id,
            config,
            zone_id=zone_id,
            line_id=line_id,
            severity=body.severity,
        )
        if error:
            return _err_response(400, error, request_id)
        config = merged
    elif body.severity is not None:
        existing_config = existing.get("config") or {}
        config = {**existing_config, "severity": body.severity}

    row = repo.update_algorithm_rule(
        camera_id=camera_id,
        rule_id=rule_id,
        algorithm_id=algorithm_id if body.algorithm_id else None,
        rule_type=(
            behavior_rule_type_for_algorithm_id(algorithm_id) or algorithm_id
            if body.algorithm_id
            else None
        ),
        enabled=body.enabled,
        zone_id=zone_id,
        line_id=line_id,
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
    rule_id: str,
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
    rule_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    return _set_rule_enabled(camera_id, rule_id, True, repo, request_id)


@router.post("/cameras/{camera_id}/algorithm-rules/{rule_id}/disable")
def camera_algorithm_rules_disable(
    camera_id: str,
    rule_id: str,
    repo: CameraRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    return _set_rule_enabled(camera_id, rule_id, False, repo, request_id)
