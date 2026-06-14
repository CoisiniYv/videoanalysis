"""Runtime observability endpoints for the operator portal."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

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
