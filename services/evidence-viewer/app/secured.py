"""Secured ASGI entrypoint for the school/operator deployment.

The existing application remains unchanged. This wrapper adds a fail-closed
Basic Auth boundary in front of every operator, API, media, and evidence route
while keeping /health available for local supervision. Credentials are read on
every request so they can be rotated without restarting the container.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.main import app
from app.operator_auth import (
    auth_file_path,
    auth_required,
    authorization_valid,
    load_credentials,
)


DEFAULT_BASELINE_FILE = "/evidence/.deployment-baseline.json"
REALM = os.getenv("OPERATOR_AUTH_REALM", "Video Analytics Operator")


def _baseline_path() -> Path:
    return Path(os.getenv("DEPLOYMENT_BASELINE_FILE", DEFAULT_BASELINE_FILE))


def _auth_unavailable_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": "operator authentication is required but no valid credential file is configured",
            "auth_file": str(auth_file_path()),
        },
    )


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": "operator authentication required"},
        headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
    )


class OperatorAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        required = auth_required()
        credentials = load_credentials()

        # Health stays probeable without credentials, but a missing credential
        # file makes the service unhealthy so insecure deployments fail closed.
        if request.url.path == "/health":
            if required and credentials is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "degraded",
                        "auth_required": True,
                        "auth_configured": False,
                    },
                )
            response = await call_next(request)
            response.headers["X-Operator-Auth"] = "required" if required else "disabled"
            return response

        if not required:
            return await call_next(request)
        if credentials is None:
            return _auth_unavailable_response()
        if not authorization_valid(request.headers.get("authorization"), credentials):
            return _unauthorized_response()
        return await call_next(request)


@app.get("/system/deployment-baseline")
def deployment_baseline() -> JSONResponse:
    path = _baseline_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return JSONResponse(
            status_code=404,
            content={"detail": "deployment baseline has not been captured"},
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return JSONResponse(
            status_code=503,
            content={"detail": f"deployment baseline is unreadable: {exc}"},
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=503,
            content={"detail": "deployment baseline has an invalid format"},
        )
    return JSONResponse(payload)


app.add_middleware(OperatorAuthMiddleware)
