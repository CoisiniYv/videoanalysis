"""Storage maintenance endpoints exposed through the 8090 operator proxy."""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.db import get_conn
from app.repositories.maintenance import MaintenanceRepository
from app.schemas.maintenance import (
    EvidenceDeleteExecuteRequest,
    EvidenceDeletePreviewRequest,
    FaceMediaCleanupExecuteRequest,
    FaceMediaOrphansPreviewRequest,
    GalleryDeleteExecuteRequest,
    GalleryDeletePreviewRequest,
    PeopleDeleteExecuteRequest,
    PeopleDeletePreviewRequest,
)
from app.services.storage_maintenance import (
    MaintenanceError,
    StorageMaintenanceService,
)


router = APIRouter(prefix="/api/v1/maintenance", tags=["maintenance"])


def _request_id(request: Request) -> str:
    return str(uuid.uuid4())


def _repo() -> MaintenanceRepository:
    conn = get_conn()
    try:
        yield MaintenanceRepository(conn)
    finally:
        conn.close()


def _service(repo: MaintenanceRepository = Depends(_repo)) -> StorageMaintenanceService:
    return StorageMaintenanceService(repo)


def _ok(data: object, request_id: str) -> dict:
    return {"data": data, "error": None, "request_id": request_id}


def _err(message: str, request_id: str, status_code: int = 404) -> dict:
    return {
        "data": None,
        "error": {"message": message, "code": status_code},
        "request_id": request_id,
    }


def _err_response(status_code: int, message: str, request_id: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=_err(message, request_id, status_code))


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _summary_enabled() -> bool:
    return _flag("STORAGE_MAINTENANCE_SUMMARY_ENABLED", True)


def _preview_enabled() -> bool:
    return _flag("STORAGE_MAINTENANCE_PREVIEW_ENABLED", True)


def _execute_enabled() -> bool:
    return _flag("STORAGE_MAINTENANCE_EXECUTE_ENABLED", False)


def _require_summary(request_id: str):
    if not _summary_enabled():
        return _err_response(403, "storage maintenance summary disabled", request_id)
    return None


def _require_preview(request_id: str):
    if not _preview_enabled():
        return _err_response(403, "storage maintenance preview disabled", request_id)
    return None


def _require_execute(request_id: str):
    if not _execute_enabled():
        return _err_response(403, "storage maintenance execute disabled", request_id)
    return None


def _run(request_id: str, func):
    try:
        return _ok(func(), request_id)
    except MaintenanceError as exc:
        return _err_response(exc.status_code, str(exc), request_id)


@router.get("/storage/summary")
def storage_summary(
    service: StorageMaintenanceService = Depends(_service),
    request_id: str = Depends(_request_id),
):
    blocked = _require_summary(request_id)
    if blocked:
        return blocked
    return _run(request_id, service.storage_summary)


@router.post("/evidence/delete-preview")
def evidence_delete_preview(
    body: EvidenceDeletePreviewRequest,
    service: StorageMaintenanceService = Depends(_service),
    request_id: str = Depends(_request_id),
):
    blocked = _require_preview(request_id)
    if blocked:
        return blocked
    return _run(request_id, lambda: service.create_evidence_delete_preview(body))


@router.post("/evidence/delete")
def evidence_delete_execute(
    body: EvidenceDeleteExecuteRequest,
    service: StorageMaintenanceService = Depends(_service),
    request_id: str = Depends(_request_id),
):
    blocked = _require_execute(request_id)
    if blocked:
        return blocked
    return _run(
        request_id,
        lambda: service.execute_job(
            preview_id=body.preview_id,
            confirm_token=body.confirm_token,
            delete_mode=body.delete_mode,
            reason=body.reason,
            operator=body.operator,
            requested_candidate_hash=body.candidate_hash,
        ),
    )


@router.post("/people/delete-preview")
def people_delete_preview(
    body: PeopleDeletePreviewRequest,
    service: StorageMaintenanceService = Depends(_service),
    request_id: str = Depends(_request_id),
):
    blocked = _require_preview(request_id)
    if blocked:
        return blocked
    return _run(request_id, lambda: service.create_people_delete_preview(body))


@router.post("/people/delete")
def people_delete_execute(
    body: PeopleDeleteExecuteRequest,
    service: StorageMaintenanceService = Depends(_service),
    request_id: str = Depends(_request_id),
):
    blocked = _require_execute(request_id)
    if blocked:
        return blocked
    return _run(
        request_id,
        lambda: service.execute_job(
            preview_id=body.preview_id,
            confirm_token=body.confirm_token,
            delete_mode="soft_delete",
            reason=body.reason,
            operator=body.operator,
            requested_candidate_hash=body.candidate_hash,
        ),
    )


@router.post("/people/gallery-delete-preview")
def gallery_delete_preview(
    body: GalleryDeletePreviewRequest,
    service: StorageMaintenanceService = Depends(_service),
    request_id: str = Depends(_request_id),
):
    blocked = _require_preview(request_id)
    if blocked:
        return blocked
    return _run(request_id, lambda: service.create_gallery_delete_preview(body))


@router.post("/people/gallery-delete")
def gallery_delete_execute(
    body: GalleryDeleteExecuteRequest,
    service: StorageMaintenanceService = Depends(_service),
    request_id: str = Depends(_request_id),
):
    blocked = _require_execute(request_id)
    if blocked:
        return blocked
    return _run(
        request_id,
        lambda: service.execute_job(
            preview_id=body.preview_id,
            confirm_token=body.confirm_token,
            delete_mode="soft_delete",
            reason=body.reason,
            operator=body.operator,
            requested_candidate_hash=body.candidate_hash,
        ),
    )


@router.post("/face-media/orphans-preview")
def face_media_orphans_preview(
    body: FaceMediaOrphansPreviewRequest,
    service: StorageMaintenanceService = Depends(_service),
    request_id: str = Depends(_request_id),
):
    blocked = _require_preview(request_id)
    if blocked:
        return blocked
    return _run(request_id, lambda: service.create_face_media_orphans_preview(body))


@router.post("/face-media/orphans-cleanup")
def face_media_orphans_cleanup(
    body: FaceMediaCleanupExecuteRequest,
    service: StorageMaintenanceService = Depends(_service),
    request_id: str = Depends(_request_id),
):
    blocked = _require_execute(request_id)
    if blocked:
        return blocked
    return _run(
        request_id,
        lambda: service.execute_job(
            preview_id=body.preview_id,
            confirm_token=body.confirm_token,
            delete_mode=body.delete_mode,
            reason=body.reason,
            operator=body.operator,
            requested_candidate_hash=body.candidate_hash,
        ),
    )


@router.get("/jobs/{job_id}")
def maintenance_job_detail(
    job_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    service: StorageMaintenanceService = Depends(_service),
    request_id: str = Depends(_request_id),
):
    return _run(
        request_id,
        lambda: service.job_detail(job_id, limit=limit, offset=offset, status=status),
    )
