"""External submitted image -> person gallery registration service."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

import psycopg
from pgvector.psycopg import register_vector

from .face_image_preprocess import (
    resolve_registration_storage,
    save_registered_crop,
    validate_image_path,
)
from .gallery_repository import GalleryRepository
from .person_repository import PersonRepository

def _find_repo_root(start: Path) -> Path:
    for path in (start, *start.parents):
        if (path / "modules" / "savant_security").is_dir():
            return path
    return start.parents[2]


_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _find_repo_root(_THIS_FILE)
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SAVANT_SECURITY_ROOT = _REPO_ROOT / "modules" / "savant_security"
if str(_SAVANT_SECURITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_SAVANT_SECURITY_ROOT))

try:
    from custom.models.faces import FaceDetection
    from custom.services.face_quality import evaluate_face_quality
except ModuleNotFoundError:
    from modules.savant_security.custom.models.faces import FaceDetection
    from modules.savant_security.custom.services.face_quality import (
        evaluate_face_quality,
    )


ERROR_IMAGE_PATH_NOT_FOUND = "IMAGE_PATH_NOT_FOUND"
ERROR_IMAGE_READ_FAILED = "IMAGE_READ_FAILED"
ERROR_IMAGE_FILE_TYPE_UNSUPPORTED = "IMAGE_FILE_TYPE_UNSUPPORTED"
ERROR_NO_FACE_DETECTED = "NO_FACE_DETECTED"
ERROR_MULTIPLE_FACES = "MULTIPLE_FACES_DETECTED"
ERROR_FACE_TOO_SMALL = "FACE_TOO_SMALL"
ERROR_LANDMARKS_MISSING = "LANDMARKS_MISSING"
ERROR_QUALITY_TOO_LOW = "QUALITY_TOO_LOW"
ERROR_REAL_EMBEDDING_UNAVAILABLE = "REAL_IMAGE_EMBEDDING_UNAVAILABLE"
ERROR_MODEL_FILE_NOT_FOUND = "MODEL_FILE_NOT_FOUND"
ERROR_EMBEDDING_DIM_INVALID = "EMBEDDING_DIM_INVALID"
ERROR_EMBEDDING_NORM_INVALID = "EMBEDDING_NORM_INVALID"
ERROR_DATABASE_URL_MISSING = "DATABASE_URL_MISSING"
ERROR_DATABASE_CONNECTION_FAILED = "DATABASE_CONNECTION_FAILED"
ERROR_PERSON_NOT_FOUND = "PERSON_NOT_FOUND"
ERROR_EXTERNAL_PERSON_ID_CONFLICT = "EXTERNAL_PERSON_ID_CONFLICT"
ERROR_PRIMARY_GALLERY_CONSTRAINT_FAILED = "PRIMARY_GALLERY_CONSTRAINT_FAILED"

STATUS_REGISTERED = "REGISTERED"
STATUS_FAILED = "FAILED"
MODE_EXTERNAL_IMAGE = "external_image"
SOURCE_TYPE_MANUAL_UPLOAD = "manual_upload"


_SUPPORTED_IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
)
_UNSUPPORTED_VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm"}
)


@dataclass(frozen=True)
class RegistrationRequest:
    image_path: str
    external_person_id: str | None
    name: str | None
    person_id: int | None
    description: str | None
    source_type: str
    is_primary: bool
    quality_threshold: float
    allow_multiple_faces: bool
    keep_crop: bool
    created_by: str | None
    dev_mock_embedding_fixture: str | None
    face_detector_onnx: str | None = None
    adaface_onnx: str | None = None
    onnx_provider: str | None = None


@dataclass(frozen=True)
class EmbeddingCandidate:
    embedding: list[float]
    face_bbox: list[float]
    landmarks: list[list[float]]
    quality: float
    embedding_model: str
    model_version: str | None
    source_image_path: str
    detection_confidence: float


@dataclass
class RegistrationResult:
    status: str
    mode: str
    error_code: str | None = None
    error_message: str | None = None
    person_id: int | None = None
    person_reused: bool = False
    external_person_id: str | None = None
    name: str | None = None
    gallery_embedding_id: int | None = None
    is_primary: bool = False
    face_bbox: list[float] | None = None
    landmarks: list[list[float]] | None = None
    quality: float | None = None
    embedding_model: str = "adaface"
    model_version: str | None = None
    embedding_dim: int | None = None
    embedding_norm: float | None = None
    source_image_path: str | None = None
    registered_crop_path: str | None = None
    source_type: str = SOURCE_TYPE_MANUAL_UPLOAD
    storage_fallback_used: bool = False
    storage_fallback_reason: str | None = None
    real_embedding_used: bool = False
    dev_mock_used: bool = False
    fallback_used: bool = False
    detector_providers: list[str] | None = None
    embedder_providers: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RegistrationError(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class RealImageEmbedder(Protocol):
    def extract(
        self,
        image_path: str,
        *,
        allow_multiple_faces: bool = False,
        quality_threshold: float = 0.65,
    ) -> list[EmbeddingCandidate]:
        ...


class NotImplementedRealImageEmbedder:
    """Placeholder until offline YOLOv8-Face + AdaFace Python runner exists."""

    def extract(
        self,
        image_path: str,
        *,
        allow_multiple_faces: bool = False,
        quality_threshold: float = 0.65,
    ) -> list[EmbeddingCandidate]:
        raise RegistrationError(
            ERROR_REAL_EMBEDDING_UNAVAILABLE,
            (
                "Offline external image face detection + AdaFace embedding "
                "runner is not wired in this repo yet."
            ),
        )


class DevMockEmbeddingFixtureEmbedder:
    """Dev-only fixture loader for contract and local dry-runs."""

    def __init__(self, fixture_path: str) -> None:
        self._fixture_path = fixture_path

    def extract(
        self,
        image_path: str,
        *,
        allow_multiple_faces: bool = False,
        quality_threshold: float = 0.65,
    ) -> list[EmbeddingCandidate]:
        data = json.loads(Path(self._fixture_path).read_text(encoding="utf-8"))
        candidates = data.get("faces")
        if not isinstance(candidates, list) or not candidates:
            raise RegistrationError(
                ERROR_NO_FACE_DETECTED,
                "Dev mock fixture did not provide any faces.",
            )
        parsed: list[EmbeddingCandidate] = []
        for item in candidates:
            parsed.append(
                EmbeddingCandidate(
                    embedding=[float(x) for x in item["embedding"]],
                    face_bbox=[float(x) for x in item["face_bbox"]],
                    landmarks=[
                        [float(v) for v in pair] for pair in item["landmarks"]
                    ],
                    quality=float(item["quality"]),
                    embedding_model=str(item.get("embedding_model", "adaface")),
                    model_version=item.get("model_version"),
                    source_image_path=image_path,
                    detection_confidence=float(
                        item.get("detection_confidence", 1.0)
                    ),
                )
            )
        return parsed


def _validate_image_extension(image_path: str) -> None:
    """Reject video file types. Raises RegistrationError if unsupported."""
    ext = Path(image_path).suffix.lower()
    if ext in _UNSUPPORTED_VIDEO_EXTENSIONS:
        raise RegistrationError(
            ERROR_IMAGE_FILE_TYPE_UNSUPPORTED,
            f"Video file type '{ext}' is not supported. Use jpg/jpeg/png/bmp/webp.",
        )


def _norm(vec: list[float]) -> float:
    return sum(x * x for x in vec) ** 0.5


def _registration_error_from_value_error(exc: ValueError) -> RegistrationError:
    """Map offline inference ValueError prefixes to CLI-facing error codes."""
    message = str(exc)
    prefix = message.split(":", 1)[0].strip()
    mapping = {
        ERROR_NO_FACE_DETECTED: ERROR_NO_FACE_DETECTED,
        ERROR_MULTIPLE_FACES: ERROR_MULTIPLE_FACES,
        ERROR_FACE_TOO_SMALL: ERROR_FACE_TOO_SMALL,
        ERROR_LANDMARKS_MISSING: ERROR_LANDMARKS_MISSING,
        ERROR_QUALITY_TOO_LOW: ERROR_QUALITY_TOO_LOW,
        ERROR_EMBEDDING_DIM_INVALID: ERROR_EMBEDDING_DIM_INVALID,
        ERROR_EMBEDDING_NORM_INVALID: ERROR_EMBEDDING_NORM_INVALID,
    }
    return RegistrationError(mapping.get(prefix, ERROR_IMAGE_READ_FAILED), message)


def _registration_error_from_file_not_found(exc: FileNotFoundError) -> RegistrationError:
    message = str(exc)
    if "ONNX model not found" in message:
        return RegistrationError(ERROR_MODEL_FILE_NOT_FOUND, message)
    return RegistrationError(ERROR_IMAGE_READ_FAILED, message)


def _connect(database_url: str) -> psycopg.Connection:
    try:
        conn = psycopg.connect(database_url, autocommit=True)
    except psycopg.Error as exc:
        raise RegistrationError(
            ERROR_DATABASE_CONNECTION_FAILED,
            str(exc),
        ) from exc
    register_vector(conn)
    return conn


def _resolve_person(
    repo: PersonRepository,
    request: RegistrationRequest,
) -> tuple[int, str | None, str | None, bool]:
    if request.person_id is not None:
        person = repo.get_by_id(request.person_id)
        if person is None:
            raise RegistrationError(
                ERROR_PERSON_NOT_FOUND,
                f"person_id={request.person_id} not found",
            )
        if (
            request.external_person_id is not None
            and person.get("external_person_id") not in (None, request.external_person_id)
        ):
            current_external_id = person.get("external_person_id") or "not set"
            raise RegistrationError(
                ERROR_EXTERNAL_PERSON_ID_CONFLICT,
                (
                    f"当前选中的人员 ID {request.person_id} 已绑定人员编号 "
                    f"{current_external_id}，但这次提交的人员编号是 "
                    f"{request.external_person_id}。请选中匹配的人员，或者先清空当前"
                    "选中人员后再注册这张脸。"
                ),
            )
        return (
            int(person["id"]),
            person.get("external_person_id"),
            person.get("name"),
            True,
        )

    if request.external_person_id is not None:
        person = repo.get_by_external_person_id(request.external_person_id)
        if person is not None:
            return (
                int(person["id"]),
                person.get("external_person_id"),
                person.get("name"),
                True,
            )
        person_name = request.name or request.external_person_id
        person_id = repo.create_person(
            name=person_name,
            external_person_id=request.external_person_id,
            description=request.description,
            created_by=request.created_by,
            updated_by=request.created_by,
            payload={"registration_mode": MODE_EXTERNAL_IMAGE},
        )
        return person_id, request.external_person_id, person_name, False

    if request.name is None:
        raise RegistrationError(
            ERROR_PERSON_NOT_FOUND,
            "Either --person-id or --external-person-id is required.",
        )
    person_id = repo.create_person(
        name=request.name,
        description=request.description,
        created_by=request.created_by,
        updated_by=request.created_by,
        payload={"registration_mode": MODE_EXTERNAL_IMAGE},
    )
    return person_id, None, request.name, False


def _promote_gallery_primary(
    conn: psycopg.Connection,
    *,
    person_id: int,
    gallery_id: int,
) -> None:
    """Make gallery_id the only active primary embedding for person_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE person_gallery_embeddings
            SET is_primary = false, updated_at = now()
            WHERE person_id = %(person_id)s
              AND id <> %(gallery_id)s
              AND is_primary = true
              AND is_active = true
            """,
            {"person_id": person_id, "gallery_id": gallery_id},
        )
        cur.execute(
            """
            UPDATE person_gallery_embeddings
            SET is_primary = true, updated_at = now()
            WHERE id = %(gallery_id)s
              AND person_id = %(person_id)s
              AND is_active = true
            """,
            {"person_id": person_id, "gallery_id": gallery_id},
        )


def _ensure_candidate_valid(
    candidate: EmbeddingCandidate,
    quality_threshold: float,
) -> None:
    if len(candidate.face_bbox) < 4:
        raise RegistrationError(
            ERROR_FACE_TOO_SMALL,
            "face_bbox must contain at least 4 values",
        )
    if not candidate.landmarks or len(candidate.landmarks) < 5:
        raise RegistrationError(
            ERROR_LANDMARKS_MISSING,
            "5-point landmarks are required",
        )

    quality_result = evaluate_face_quality(
        FaceDetection(
            face_bbox=candidate.face_bbox,
            landmarks=candidate.landmarks,
            confidence=candidate.detection_confidence,
        ),
        {"face_quality_threshold": quality_threshold},
    )
    if "face_too_narrow" in quality_result.reasons or "face_too_short" in quality_result.reasons:
        raise RegistrationError(ERROR_FACE_TOO_SMALL, "Detected face is too small")
    if not quality_result.passed or candidate.quality < quality_threshold:
        raise RegistrationError(
            ERROR_QUALITY_TOO_LOW,
            f"quality={candidate.quality:.3f} below threshold={quality_threshold:.3f}",
        )
    if len(candidate.embedding) != 512:
        raise RegistrationError(
            ERROR_EMBEDDING_DIM_INVALID,
            f"embedding_dim={len(candidate.embedding)}, expected 512",
        )
    norm = _norm(candidate.embedding)
    if not (0.90 <= norm <= 1.10):
        raise RegistrationError(
            ERROR_EMBEDDING_NORM_INVALID,
            f"embedding_norm={norm:.6f} outside [0.90, 1.10]",
        )


def _select_candidate(
    candidates: list[EmbeddingCandidate],
    allow_multiple_faces: bool,
    quality_threshold: float,
) -> EmbeddingCandidate:
    if not candidates:
        raise RegistrationError(ERROR_NO_FACE_DETECTED, "No face detected")
    if len(candidates) > 1 and not allow_multiple_faces:
        raise RegistrationError(
            ERROR_MULTIPLE_FACES,
            f"Detected {len(candidates)} faces; rerun with --allow-multiple-faces",
        )
    selected = max(candidates, key=lambda item: item.quality)
    _ensure_candidate_valid(selected, quality_threshold)
    return selected


class OfflineFaceEmbedderAdapter:
    """Adapter that wraps OfflineFaceEmbedder to implement RealImageEmbedder protocol."""

    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder

    def extract(
        self,
        image_path: str,
        *,
        allow_multiple_faces: bool = False,
        quality_threshold: float = 0.65,
    ) -> list[EmbeddingCandidate]:
        try:
            result = self._embedder.extract(
                image_path,
                allow_multiple_faces=allow_multiple_faces,
                quality_threshold=quality_threshold,
            )
        except ValueError as exc:
            raise _registration_error_from_value_error(exc) from exc
        return [
            EmbeddingCandidate(
                embedding=result.embedding,
                face_bbox=result.face_bbox,
                landmarks=result.landmarks,
                quality=result.quality,
                embedding_model=result.embedding_model,
                model_version=result.model_version,
                source_image_path=image_path,
                detection_confidence=result.face_confidence,
            )
        ]

    @property
    def detector_providers(self) -> list[str]:
        try:
            return self._embedder.detector_providers
        except FileNotFoundError as exc:
            raise _registration_error_from_file_not_found(exc) from exc

    @property
    def embedder_providers(self) -> list[str]:
        try:
            return self._embedder.embedder_providers
        except FileNotFoundError as exc:
            raise _registration_error_from_file_not_found(exc) from exc


def _resolve_default_embedder(
    request: RegistrationRequest,
) -> RealImageEmbedder:
    """Resolve the default embedder: OfflineFaceEmbedder if available."""
    try:
        from .offline_face_embedder import OfflineFaceEmbedder

        embedder = OfflineFaceEmbedder(
            face_detector_onnx=request.face_detector_onnx,
            adaface_onnx=request.adaface_onnx,
            onnx_provider=request.onnx_provider,
        )
        return OfflineFaceEmbedderAdapter(embedder)
    except ImportError:
        logger.warning(
            "onnxruntime not available, falling back to NotImplementedRealImageEmbedder"
        )
        return NotImplementedRealImageEmbedder()


def register_external_image(
    request: RegistrationRequest,
    *,
    embedder: RealImageEmbedder | None = None,
) -> RegistrationResult:
    image_path = request.image_path
    try:
        _validate_image_extension(image_path)
        image_path = validate_image_path(request.image_path)
    except RegistrationError as exc:
        return RegistrationResult(
            status=STATUS_FAILED,
            mode=MODE_EXTERNAL_IMAGE,
            error_code=exc.error_code,
            error_message=exc.message,
            source_image_path=image_path,
            source_type=request.source_type,
        )
    except FileNotFoundError as exc:
        return RegistrationResult(
            status=STATUS_FAILED,
            mode=MODE_EXTERNAL_IMAGE,
            error_code=ERROR_IMAGE_PATH_NOT_FOUND,
            error_message=str(exc),
            source_image_path=str(exc),
            source_type=request.source_type,
        )
    except (IsADirectoryError, OSError) as exc:
        return RegistrationResult(
            status=STATUS_FAILED,
            mode=MODE_EXTERNAL_IMAGE,
            error_code=ERROR_IMAGE_READ_FAILED,
            error_message=str(exc),
            source_image_path=image_path,
            source_type=request.source_type,
        )

    storage = resolve_registration_storage(image_path)
    result = RegistrationResult(
        status=STATUS_FAILED,
        mode=MODE_EXTERNAL_IMAGE,
        source_image_path=image_path,
        registered_crop_path=storage.registered_crop_path,
        source_type=request.source_type,
        external_person_id=request.external_person_id,
        name=request.name,
        storage_fallback_used=storage.storage_fallback_used,
        storage_fallback_reason=storage.storage_fallback_reason,
        dev_mock_used=bool(request.dev_mock_embedding_fixture),
        fallback_used=bool(request.dev_mock_embedding_fixture),
    )

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        result.error_code = ERROR_DATABASE_URL_MISSING
        result.error_message = "DATABASE_URL not set"
        return result

    active_embedder: RealImageEmbedder = embedder or (
        DevMockEmbeddingFixtureEmbedder(request.dev_mock_embedding_fixture)
        if request.dev_mock_embedding_fixture
        else _resolve_default_embedder(request)
    )

    # Capture provider info if available
    try:
        if isinstance(active_embedder, OfflineFaceEmbedderAdapter):
            result.detector_providers = active_embedder.detector_providers
            result.embedder_providers = active_embedder.embedder_providers
        candidates = active_embedder.extract(
            image_path,
            allow_multiple_faces=request.allow_multiple_faces,
            quality_threshold=request.quality_threshold,
        )
        candidate = _select_candidate(
            candidates,
            allow_multiple_faces=request.allow_multiple_faces,
            quality_threshold=request.quality_threshold,
        )
        conn = _connect(database_url)
    except RegistrationError as exc:
        result.error_code = exc.error_code
        result.error_message = exc.message
        return result
    except FileNotFoundError as exc:
        reg_error = _registration_error_from_file_not_found(exc)
        result.error_code = reg_error.error_code
        result.error_message = reg_error.message
        return result

    try:
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        person_id, external_person_id, person_name, person_reused = _resolve_person(
            person_repo, request
        )
        crop_path = save_registered_crop(
            image_path,
            storage.registered_crop_path,
            keep_crop=request.keep_crop,
        )
        payload = {
            "registration_mode": MODE_EXTERNAL_IMAGE,
            "real_embedding_used": request.dev_mock_embedding_fixture is None,
            "dev_mock_used": bool(request.dev_mock_embedding_fixture),
            "storage_fallback_used": storage.storage_fallback_used,
            "registered_crop_path": crop_path,
        }
        try:
            gallery_id = gallery_repo.add_embedding(
                person_id=person_id,
                embedding=candidate.embedding,
                source_type=request.source_type,
                source_image_path=image_path,
                embedding_model=candidate.embedding_model,
                model_version=candidate.model_version,
                quality=candidate.quality,
                face_bbox=candidate.face_bbox,
                landmarks=candidate.landmarks,
                is_primary=False,
                payload=payload,
            )
            if request.is_primary:
                _promote_gallery_primary(
                    conn,
                    person_id=person_id,
                    gallery_id=gallery_id,
                )
        except ValueError as exc:
            message = str(exc)
            error_code = (
                ERROR_EMBEDDING_DIM_INVALID
                if "length=" in message
                else ERROR_EMBEDDING_NORM_INVALID
            )
            raise RegistrationError(error_code, message) from exc
        except psycopg.Error as exc:
            message = str(exc)
            if "person_gallery_embeddings_one_primary_active" in message:
                raise RegistrationError(
                    ERROR_PRIMARY_GALLERY_CONSTRAINT_FAILED,
                    message,
                ) from exc
            raise

        emb_norm = _norm(candidate.embedding)
        result.status = STATUS_REGISTERED
        result.person_id = person_id
        result.person_reused = person_reused
        result.external_person_id = external_person_id
        result.name = person_name
        result.gallery_embedding_id = gallery_id
        result.is_primary = request.is_primary
        result.face_bbox = candidate.face_bbox
        result.landmarks = candidate.landmarks
        result.quality = candidate.quality
        result.embedding_model = candidate.embedding_model
        result.model_version = candidate.model_version
        result.embedding_dim = len(candidate.embedding)
        result.embedding_norm = emb_norm
        result.registered_crop_path = crop_path
        result.real_embedding_used = request.dev_mock_embedding_fixture is None
        result.dev_mock_used = bool(request.dev_mock_embedding_fixture)
        result.fallback_used = bool(request.dev_mock_embedding_fixture)
        return result
    except RegistrationError as exc:
        result.error_code = exc.error_code
        result.error_message = exc.message
        return result
    finally:
        conn.close()
