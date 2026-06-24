"""Runtime observability endpoints for the operator portal."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.services.runtime_control import (
    RuntimeControlError,
    restart_single_runtime,
    runtime_control_status,
    start_single_runtime,
    stop_dual_runtime,
    stop_single_runtime,
)
from app.services.runtime_overview import RuntimeOverviewError, build_runtime_overview


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


@router.get("/overview")
def runtime_overview(request_id: str = Depends(_request_id)):
    try:
        return _ok(build_runtime_overview(), request_id)
    except RuntimeOverviewError as exc:
        return JSONResponse(status_code=503, content=_err(str(exc), request_id))


@router.get("/control")
def runtime_control(request_id: str = Depends(_request_id)):
    try:
        return _ok(runtime_control_status(), request_id)
    except RuntimeControlError as exc:
        return JSONResponse(status_code=503, content=_err(str(exc), request_id))


@router.post("/control/single/start")
def runtime_control_single_start(request_id: str = Depends(_request_id)):
    try:
        return _ok(start_single_runtime(), request_id)
    except RuntimeControlError as exc:
        return JSONResponse(status_code=503, content=_err(str(exc), request_id))


@router.post("/control/single/stop")
def runtime_control_single_stop(request_id: str = Depends(_request_id)):
    try:
        return _ok(stop_single_runtime(), request_id)
    except RuntimeControlError as exc:
        return JSONResponse(status_code=503, content=_err(str(exc), request_id))


@router.post("/control/single/restart")
def runtime_control_single_restart(request_id: str = Depends(_request_id)):
    try:
        return _ok(restart_single_runtime(), request_id)
    except RuntimeControlError as exc:
        return JSONResponse(status_code=503, content=_err(str(exc), request_id))


@router.post("/control/dual/stop")
def runtime_control_dual_stop(request_id: str = Depends(_request_id)):
    try:
        return _ok(stop_dual_runtime(), request_id)
    except RuntimeControlError as exc:
        return JSONResponse(status_code=503, content=_err(str(exc), request_id))
