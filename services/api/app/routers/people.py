"""People and external face registration endpoints."""

from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from app.db import get_conn
from app.repositories.people import PeopleRepository
from app.schemas.people import (
    GalleryEmbeddingResponse,
    PeopleListResponse,
    PersonDetailResponse,
    PersonSummaryResponse,
    PersonWithGalleryResponse,
)


def _find_libs_root(start: Path) -> Path:
    for path in (start, *start.parents):
        candidate = path / "libs"
        if (candidate / "face_registration").is_dir():
            return candidate
    return Path("/app/libs")


_LIBS_ROOT = _find_libs_root(Path(__file__).resolve())
if str(_LIBS_ROOT) not in sys.path:
    sys.path.insert(0, str(_LIBS_ROOT))

from face_registration.image_face_registration import (  # noqa: E402
    ERROR_DATABASE_CONNECTION_FAILED,
    ERROR_DATABASE_URL_MISSING,
    ERROR_EXTERNAL_PERSON_ID_CONFLICT,
    ERROR_FACE_TOO_SMALL,
    ERROR_IMAGE_FILE_TYPE_UNSUPPORTED,
    ERROR_IMAGE_PATH_NOT_FOUND,
    ERROR_IMAGE_READ_FAILED,
    ERROR_LANDMARKS_MISSING,
    ERROR_MODEL_FILE_NOT_FOUND,
    ERROR_MULTIPLE_FACES,
    ERROR_NO_FACE_DETECTED,
    ERROR_PERSON_NOT_FOUND,
    ERROR_PRIMARY_GALLERY_CONSTRAINT_FAILED,
    ERROR_QUALITY_TOO_LOW,
    ERROR_REAL_EMBEDDING_UNAVAILABLE,
    MODE_EXTERNAL_IMAGE,
    RegistrationRequest,
    RegistrationResult,
    register_external_image,
)


router = APIRouter(prefix="/api/v1/people", tags=["people"])

SUPPORTED_UPLOAD_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})
DEFAULT_UPLOAD_MAX_BYTES = 10 * 1024 * 1024


def _request_id(request: Request) -> str:
    return str(uuid.uuid4())


def _repo() -> PeopleRepository:
    conn = get_conn()
    try:
        yield PeopleRepository(conn)
    finally:
        conn.close()


def _registrar() -> Callable[[RegistrationRequest], RegistrationResult]:
    return register_external_image


def _ok(data: object, request_id: str) -> dict:
    return {"data": data, "error": None, "request_id": request_id}


def _err(
    message: str,
    request_id: str,
    *,
    status_code: int,
    registration_error_code: str | None = None,
) -> dict:
    error: dict[str, object] = {"message": message, "code": status_code}
    if registration_error_code:
        error["registration_error_code"] = registration_error_code
    return {"data": None, "error": error, "request_id": request_id}


def _err_response(
    status_code: int,
    message: str,
    request_id: str,
    *,
    registration_error_code: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_err(
            message,
            request_id,
            status_code=status_code,
            registration_error_code=registration_error_code,
        ),
    )


def _registration_status(error_code: str | None) -> int:
    if error_code in {
        ERROR_IMAGE_FILE_TYPE_UNSUPPORTED,
        ERROR_IMAGE_PATH_NOT_FOUND,
        ERROR_IMAGE_READ_FAILED,
        ERROR_NO_FACE_DETECTED,
        ERROR_MULTIPLE_FACES,
        ERROR_FACE_TOO_SMALL,
        ERROR_LANDMARKS_MISSING,
        ERROR_QUALITY_TOO_LOW,
    }:
        return 400
    if error_code == ERROR_PERSON_NOT_FOUND:
        return 404
    if error_code in {
        ERROR_EXTERNAL_PERSON_ID_CONFLICT,
        ERROR_PRIMARY_GALLERY_CONSTRAINT_FAILED,
    }:
        return 409
    if error_code in {
        ERROR_DATABASE_URL_MISSING,
        ERROR_DATABASE_CONNECTION_FAILED,
        ERROR_REAL_EMBEDDING_UNAVAILABLE,
        ERROR_MODEL_FILE_NOT_FOUND,
    }:
        return 500
    return 500


def _upload_root() -> Path:
    media_root = os.getenv("MEDIA_ROOT", "/data/video-analytics/media")
    return Path(os.getenv("FACE_UPLOAD_ROOT", str(Path(media_root) / "face_uploads")))


def _max_upload_bytes() -> int:
    raw = os.getenv("FACE_UPLOAD_MAX_BYTES")
    if not raw:
        return DEFAULT_UPLOAD_MAX_BYTES
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_UPLOAD_MAX_BYTES


def _safe_stem(filename: str) -> str:
    stem = Path(filename).stem or "face"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._")
    return safe[:80] or "face"


def _save_upload(upload: UploadFile, request_id: str) -> str:
    original = upload.filename or ""
    suffix = Path(original).suffix.lower()
    if suffix not in SUPPORTED_UPLOAD_EXTENSIONS:
        raise ValueError(f"unsupported_image_extension:{suffix or '<none>'}")

    root = _upload_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = root / f"{stamp}_{_safe_stem(original)}_{uuid.uuid4().hex[:8]}{suffix}"
    tmp = target.with_suffix(target.suffix + ".tmp")
    max_bytes = _max_upload_bytes()
    written = 0
    try:
        with tmp.open("wb") as fh:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"upload_too_large:{max_bytes}")
                fh.write(chunk)
        if written == 0:
            raise ValueError("empty_upload")
        tmp.replace(target)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    finally:
        upload.file.close()
    return str(target.resolve())


@router.get("")
def people_list(
    include_inactive: bool = Query(False),
    q: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: PeopleRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    rows, total = repo.list_people(
        include_inactive=include_inactive,
        q=q,
        limit=limit,
        offset=offset,
    )
    payload = PeopleListResponse(
        people=[PersonSummaryResponse.from_db_row(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
    return _ok(payload.model_dump(), request_id)


@router.get("/{person_id}")
def people_get(
    person_id: int,
    repo: PeopleRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
):
    person = repo.get_person(person_id)
    if person is None:
        return _err_response(404, f"person not found: {person_id}", request_id)
    gallery = repo.list_gallery(person_id)
    payload = PersonWithGalleryResponse(
        person=PersonDetailResponse.from_db_row(person),
        gallery=[GalleryEmbeddingResponse.from_db_row(row) for row in gallery],
    )
    return _ok(payload.model_dump(), request_id)


@router.post("/register-face")
def people_register_face(
    image: UploadFile = File(...),
    external_person_id: str = Form(...),
    name: str = Form(...),
    person_id: int | None = Form(None),
    description: str | None = Form(None),
    is_primary: bool = Form(False),
    quality_threshold: float = Form(0.65),
    allow_multiple_faces: bool = Form(False),
    keep_crop: bool = Form(True),
    operator: str | None = Form(None),
    registrar: Callable[[RegistrationRequest], RegistrationResult] = Depends(_registrar),
    request_id: str = Depends(_request_id),
):
    if not external_person_id.strip():
        return _err_response(400, "external_person_id is required", request_id)
    if not name.strip():
        return _err_response(400, "name is required", request_id)
    if not 0.0 <= quality_threshold <= 1.0:
        return _err_response(
            400,
            "quality_threshold must be between 0.0 and 1.0",
            request_id,
        )

    try:
        image_path = _save_upload(image, request_id)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("unsupported_image_extension"):
            return _err_response(
                400,
                message,
                request_id,
                registration_error_code=ERROR_IMAGE_FILE_TYPE_UNSUPPORTED,
            )
        if message.startswith("upload_too_large"):
            return _err_response(400, message, request_id)
        return _err_response(400, message, request_id)
    except OSError as exc:
        return _err_response(500, str(exc), request_id)

    request = RegistrationRequest(
        image_path=image_path,
        external_person_id=external_person_id.strip(),
        name=name.strip(),
        person_id=person_id,
        description=description,
        source_type="manual_upload",
        is_primary=is_primary,
        quality_threshold=quality_threshold,
        allow_multiple_faces=allow_multiple_faces,
        keep_crop=keep_crop,
        created_by=operator or "operator",
        dev_mock_embedding_fixture=None,
        face_detector_onnx=os.getenv("YOLOV8_FACE_ONNX"),
        adaface_onnx=os.getenv("ADAFACE_ONNX"),
        onnx_provider=os.getenv("FACE_REGISTRATION_ONNX_PROVIDER"),
    )
    result = registrar(request)
    payload = result.to_dict()
    if result.status != "REGISTERED":
        return _err_response(
            _registration_status(result.error_code),
            result.error_message or result.error_code or "registration failed",
            request_id,
            registration_error_code=result.error_code,
        )
    return _ok(payload, request_id)
