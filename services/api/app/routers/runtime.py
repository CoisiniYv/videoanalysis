"""Runtime observability endpoints for the operator portal."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.services.runtime_control import (
    RuntimeControlBlockedError,
    RuntimeControlError,
    restart_single_runtime,
    runtime_control_status,
    start_single_runtime,
    stop_dual_runtime,
    stop_single_runtime,
)
from app.services.runtime_overview import RuntimeOverviewError, build_runtime_overview
from app.services.runtime_performance import (
    RuntimePerformanceError,
    apply_runtime_performance_config,
    get_runtime_performance_config,
    save_runtime_performance_config,
)


router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])


def _request_id(request: Request) -> str:
    return str(uuid.uuid4())


def _ok(data: object, request_id: str) -> dict:
    return {"data": data, "error": None, "request_id": request_id}


def _err(message: str, request_id: str, status_code: int = 503) -> dict:
    return {
        "data": None,
        "error": {"message": message, "code": status_code},
        "request_id": request_id,
    }


def _err_response(
    status_code: int,
    message: str,
    request_id: str,
    *,
    details: dict | None = None,
) -> JSONResponse:
    content = _err(message, request_id, status_code)
    if details is not None:
        content["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=content)


def _runtime_control_error_response(exc: RuntimeControlError, request_id: str) -> JSONResponse:
    if isinstance(exc, RuntimeControlBlockedError):
        return _err_response(
            exc.status_code,
            str(exc),
            request_id,
            details=exc.details,
        )
    return _err_response(503, str(exc), request_id)


def _runtime_performance_error_response(
    exc: RuntimePerformanceError,
    request_id: str,
) -> JSONResponse:
    return _err_response(
        exc.status_code,
        str(exc),
        request_id,
        details=exc.details or None,
    )


@router.get("/overview")
def runtime_overview(request_id: str = Depends(_request_id)):
    try:
        return _ok(build_runtime_overview(), request_id)
    except RuntimeOverviewError as exc:
        return _err_response(503, str(exc), request_id)


@router.get("/control")
def runtime_control(request_id: str = Depends(_request_id)):
    try:
        return _ok(runtime_control_status(), request_id)
    except RuntimeControlError as exc:
        return _runtime_control_error_response(exc, request_id)


@router.get("/performance-config")
def runtime_performance_config(request_id: str = Depends(_request_id)):
    try:
        return _ok(get_runtime_performance_config(), request_id)
    except RuntimePerformanceError as exc:
        return _runtime_performance_error_response(exc, request_id)


@router.put("/performance-config")
def runtime_performance_config_save(
    body: dict = Body(...),
    request_id: str = Depends(_request_id),
):
    try:
        return _ok(save_runtime_performance_config(body), request_id)
    except RuntimePerformanceError as exc:
        return _runtime_performance_error_response(exc, request_id)


@router.post("/performance-config/apply")
def runtime_performance_config_apply(
    force: bool = Query(
        False,
        description="Force performance config apply even when active evidence tasks would be interrupted.",
    ),
    request_id: str = Depends(_request_id),
):
    try:
        return _ok(apply_runtime_performance_config(force=force), request_id)
    except RuntimePerformanceError as exc:
        return _runtime_performance_error_response(exc, request_id)


@router.post("/control/single/start")
def runtime_control_single_start(request_id: str = Depends(_request_id)):
    try:
        return _ok(start_single_runtime(), request_id)
    except RuntimeControlError as exc:
        return _runtime_control_error_response(exc, request_id)


@router.post("/control/single/stop")
def runtime_control_single_stop(request_id: str = Depends(_request_id)):
    try:
        return _ok(stop_single_runtime(), request_id)
    except RuntimeControlError as exc:
        return _runtime_control_error_response(exc, request_id)


@router.post("/control/single/restart")
def runtime_control_single_restart(
    force: bool = Query(
        False,
        description="Force single-runtime restart even when active evidence tasks would be interrupted.",
    ),
    request_id: str = Depends(_request_id),
):
    try:
        return _ok(restart_single_runtime(force=force), request_id)
    except RuntimeControlError as exc:
        return _runtime_control_error_response(exc, request_id)


@router.post("/control/dual/stop")
def runtime_control_dual_stop(request_id: str = Depends(_request_id)):
    try:
        return _ok(stop_dual_runtime(), request_id)
    except RuntimeControlError as exc:
        return _runtime_control_error_response(exc, request_id)
