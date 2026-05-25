"""FastAPI application — Video Analytics event query API."""

from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.db import get_conn
from app.routers.cameras import router as cameras_router
from app.routers.events import router as events_router
from app.routers.ws_alerts import router as ws_alerts_router

app = FastAPI(title="Video Analytics API", version="1.0.0")

app.include_router(events_router)
app.include_router(cameras_router)
app.include_router(ws_alerts_router)

# Mount /media for serving clip/snapshot files
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/media")
if os.path.isdir(MEDIA_ROOT):
    app.mount("/media", StaticFiles(directory=MEDIA_ROOT), name="media")


# ---------------------------------------------------------------------------
# Exception handler — wrap HTTPException in structured error envelope
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def structured_error_handler(request: Request, exc: Exception) -> JSONResponse:
    from fastapi import HTTPException

    if isinstance(exc, HTTPException):
        # detail might be a dict (our _err output) or a plain string
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("request_id"):
            return JSONResponse(status_code=exc.status_code, content=detail)

    request_id = str(uuid.uuid4())
    message = str(exc)
    return JSONResponse(
        status_code=500,
        content={
            "data": None,
            "error": {"message": message, "code": 500},
            "request_id": request_id,
        },
    )


# ---------------------------------------------------------------------------
# Health / Readiness
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    checks: dict[str, bool] = {"postgres": False}
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.close()
        checks["postgres"] = True
    except Exception:
        pass

    all_ok = all(checks.values())
    return {
        "status": "ready" if all_ok else "not_ready",
        "checks": checks,
    }
