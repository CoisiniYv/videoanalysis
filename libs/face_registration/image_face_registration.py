"""External submitted image -> person gallery registration service."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

import psycopg
from pgvector.psycopg import register_vector

from .face_image_preprocess import (
    StorageResolution,
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
ERROR_DATABASE_WRITE_FAILED = "DATABASE_WRITE_FAILED"
ERROR_PERSON_NOT_FOUND = "PERSON_NOT_FOUND"
ERROR_EXTERNAL_PERSON_ID_CONFLICT = "EXTERNAL_PERSON_ID_CONFLICT"
ERROR_PRIMARY_GALLERY_CONSTRAINT_FAILED = "PRIMARY_GALLERY_CONSTRAINT_FAILED"

STATUS_REGISTERED = "REGISTERED"
STATUS_PARTIAL = "PARTIAL"
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
class BatchRegistrationRequest:
    """Common identity/settings for a bounded set of external face images.

    The batch intentionally keeps one person identity and produces one gallery
    embedding per accepted image.  It is a partial-success operation: an image
    with poor quality must not discard other valid images in the same upload.
    """

    image_paths: tuple[str, ...]
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
    image_names: tuple[str, ...] = ()

    def item_request(self, image_path: str) -> RegistrationRequest:
        return RegistrationRequest(
            image_path=image_path,
            external_person_id=self.external_person_id,
            name=self.name,
            person_id=self.person_id,
            description=self.description,
            source_type=self.source_type,
            is_primary=False,
            quality_threshold=self.quality_threshold,
            allow_multiple_faces=self.allow_multiple_faces,
            keep_crop=self.keep_crop,
            created_by=self.created_by,
            dev_mock_embedding_fixture=self.dev_mock_embedding_fixture,
            face_detector_onnx=self.face_detector_onnx,
            adaface_onnx=self.adaface_onnx,
            onnx_provider=self.onnx_provider,
        )


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


@dataclass
class BatchRegistrationItemResult:
    """The outcome for one submitted image, retained in client upload order."""

    index: int
    filename: str
    result: RegistrationResult

    def to_dict(self) -> dict[str, Any]:
        payload = self.result.to_dict()
        payload.update({"index": self.index, "filename": self.filename})
        return payload


@dataclass
class BatchRegistrationResult:
    """Serializable partial-success result for a multi-image registration."""

    status: str
    mode: str = MODE_EXTERNAL_IMAGE
    person_id: int | None = None
    person_reused: bool = False
    external_person_id: str | None = None
    name: str | None = None
    registered_count: int = 0
    failed_count: int = 0
    items: list[BatchRegistrationItemResult] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "mode": self.mode,
            "person_id": self.person_id,
            "person_reused": self.person_reused,
            "external_person_id": self.external_person_id,
            "name": self.name,
            "registered_count": self.registered_count,
            "failed_count": self.failed_count,
            "items": [item.to_dict() for item in self.items],
            "error_code": self.error_code,
            "error_message": self.error_message,
            "warnings": list(self.warnings),
        }


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


def _has_active_primary_gallery(conn: psycopg.Connection, *, person_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM person_gallery_embeddings
            WHERE person_id = %(person_id)s
              AND is_active = true
              AND is_primary = true
            LIMIT 1
            """,
            {"person_id": person_id},
        )
        return cur.fetchone() is not None


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
            promote_primary = request.is_primary or not _has_active_primary_gallery(
                conn,
                person_id=person_id,
            )
            if promote_primary:
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
        result.is_primary = promote_primary
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


@dataclass
class _PreparedBatchRegistration:
    """Validated/inferred image waiting to be written to the gallery."""

    image_path: str
    storage: StorageResolution
    candidate: EmbeddingCandidate
    item: BatchRegistrationItemResult


class _NoBatchImagesRegistered(Exception):
    """Abort the database transaction when no valid gallery row was written."""


def _batch_item_result(
    request: RegistrationRequest,
    *,
    index: int,
    filename: str,
    image_path: str,
    storage: StorageResolution | None = None,
) -> BatchRegistrationItemResult:
    return BatchRegistrationItemResult(
        index=index,
        filename=filename,
        result=RegistrationResult(
            status=STATUS_FAILED,
            mode=MODE_EXTERNAL_IMAGE,
            source_image_path=image_path,
            registered_crop_path=(storage.registered_crop_path if storage else None),
            source_type=request.source_type,
            external_person_id=request.external_person_id,
            name=request.name,
            storage_fallback_used=(storage.storage_fallback_used if storage else False),
            storage_fallback_reason=(storage.storage_fallback_reason if storage else None),
            dev_mock_used=bool(request.dev_mock_embedding_fixture),
            fallback_used=bool(request.dev_mock_embedding_fixture),
        ),
    )


def _set_registration_failure(
    result: RegistrationResult,
    error: RegistrationError,
) -> None:
    result.status = STATUS_FAILED
    result.error_code = error.error_code
    result.error_message = error.message
    result.gallery_embedding_id = None
    result.is_primary = False
    result.registered_crop_path = None


def _database_registration_error(exc: psycopg.Error) -> RegistrationError:
    """Expose stable, operator-safe codes for per-image write failures."""
    message = str(exc)
    if (
        "person_gallery_one_primary_idx" in message
        or "person_gallery_embeddings_one_primary_active" in message
    ):
        return RegistrationError(ERROR_PRIMARY_GALLERY_CONSTRAINT_FAILED, message)
    if "persons_external_person_id" in message:
        return RegistrationError(ERROR_EXTERNAL_PERSON_ID_CONFLICT, message)
    return RegistrationError(ERROR_DATABASE_WRITE_FAILED, message)


def _prepare_batch_registration(
    request: RegistrationRequest,
    *,
    index: int,
    filename: str,
    embedder: RealImageEmbedder,
) -> tuple[_PreparedBatchRegistration | None, BatchRegistrationItemResult]:
    """Validate and infer one image without writing a person or gallery row."""
    image_path = request.image_path
    try:
        _validate_image_extension(image_path)
        image_path = validate_image_path(image_path)
    except RegistrationError as exc:
        item = _batch_item_result(
            request, index=index, filename=filename, image_path=image_path
        )
        _set_registration_failure(item.result, exc)
        return None, item
    except FileNotFoundError as exc:
        item = _batch_item_result(
            request, index=index, filename=filename, image_path=str(exc)
        )
        _set_registration_failure(
            item.result,
            RegistrationError(ERROR_IMAGE_PATH_NOT_FOUND, str(exc)),
        )
        return None, item
    except (IsADirectoryError, OSError) as exc:
        item = _batch_item_result(
            request, index=index, filename=filename, image_path=image_path
        )
        _set_registration_failure(
            item.result,
            RegistrationError(ERROR_IMAGE_READ_FAILED, str(exc)),
        )
        return None, item

    try:
        storage = resolve_registration_storage(image_path)
    except OSError as exc:
        item = _batch_item_result(
            request, index=index, filename=filename, image_path=image_path
        )
        _set_registration_failure(
            item.result,
            RegistrationError(ERROR_IMAGE_READ_FAILED, str(exc)),
        )
        return None, item

    item = _batch_item_result(
        request,
        index=index,
        filename=filename,
        image_path=image_path,
        storage=storage,
    )
    try:
        if isinstance(embedder, OfflineFaceEmbedderAdapter):
            item.result.detector_providers = embedder.detector_providers
            item.result.embedder_providers = embedder.embedder_providers
        candidates = embedder.extract(
            image_path,
            allow_multiple_faces=request.allow_multiple_faces,
            quality_threshold=request.quality_threshold,
        )
        candidate = _select_candidate(
            candidates,
            allow_multiple_faces=request.allow_multiple_faces,
            quality_threshold=request.quality_threshold,
        )
    except RegistrationError as exc:
        _set_registration_failure(item.result, exc)
        return None, item
    except FileNotFoundError as exc:
        _set_registration_failure(item.result, _registration_error_from_file_not_found(exc))
        return None, item
    except ValueError as exc:
        _set_registration_failure(item.result, _registration_error_from_value_error(exc))
        return None, item

    return (
        _PreparedBatchRegistration(
            image_path=image_path,
            storage=storage,
            candidate=candidate,
            item=item,
        ),
        item,
    )


def _finalize_batch_result(
    request: BatchRegistrationRequest,
    items: list[BatchRegistrationItemResult],
    *,
    person_id: int | None = None,
    person_reused: bool = False,
    external_person_id: str | None = None,
    name: str | None = None,
    error: RegistrationError | None = None,
    warnings: list[str] | None = None,
) -> BatchRegistrationResult:
    registered_count = sum(
        item.result.status == STATUS_REGISTERED for item in items
    )
    failed_count = len(items) - registered_count
    if registered_count == 0:
        status = STATUS_FAILED
    elif failed_count:
        status = STATUS_PARTIAL
    else:
        status = STATUS_REGISTERED
    return BatchRegistrationResult(
        status=status,
        person_id=person_id,
        person_reused=person_reused,
        external_person_id=external_person_id or request.external_person_id,
        name=name or request.name,
        registered_count=registered_count,
        failed_count=failed_count,
        items=items,
        error_code=error.error_code if error else None,
        error_message=error.message if error else None,
        warnings=list(warnings or []),
    )


def _unlink_crop_if_present(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("failed to remove unregistered face crop path=%s", path)


def register_external_images(
    request: BatchRegistrationRequest,
    *,
    embedder: RealImageEmbedder | None = None,
) -> BatchRegistrationResult:
    """Register several images for one person with per-image outcomes.

    Inference is deliberately sequential and reuses one detector/embedder
    instance.  A failed image is reported independently; valid images are
    retained.  This makes a batch usable for varied poses without turning one
    bad crop into a rollback of the whole person gallery.
    """
    if not request.image_paths:
        return BatchRegistrationResult(
            status=STATUS_FAILED,
            external_person_id=request.external_person_id,
            name=request.name,
            error_code=ERROR_IMAGE_PATH_NOT_FOUND,
            error_message="At least one image is required.",
        )

    template = request.item_request(request.image_paths[0])
    active_embedder: RealImageEmbedder = embedder or (
        DevMockEmbeddingFixtureEmbedder(request.dev_mock_embedding_fixture)
        if request.dev_mock_embedding_fixture
        else _resolve_default_embedder(template)
    )

    items: list[BatchRegistrationItemResult] = []
    prepared: list[_PreparedBatchRegistration] = []
    for index, image_path in enumerate(request.image_paths):
        filename = (
            request.image_names[index]
            if index < len(request.image_names) and request.image_names[index]
            else Path(image_path).name
        )
        item_request = request.item_request(image_path)
        candidate, item = _prepare_batch_registration(
            item_request,
            index=index,
            filename=filename,
            embedder=active_embedder,
        )
        items.append(item)
        if candidate is not None:
            prepared.append(candidate)

    if not prepared:
        return _finalize_batch_result(request, items)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        error = RegistrationError(
            ERROR_DATABASE_URL_MISSING,
            "DATABASE_URL not set",
        )
        for pending in prepared:
            _set_registration_failure(pending.item.result, error)
        return _finalize_batch_result(request, items, error=error)

    try:
        conn = _connect(database_url)
    except RegistrationError as exc:
        for pending in prepared:
            _set_registration_failure(pending.item.result, exc)
        return _finalize_batch_result(request, items, error=exc)

    successful: list[_PreparedBatchRegistration] = []
    person_id: int | None = None
    person_reused = False
    external_person_id: str | None = None
    person_name: str | None = None
    try:
        person_repo = PersonRepository(conn)
        gallery_repo = GalleryRepository(conn)
        with conn.transaction():
            (
                person_id,
                external_person_id,
                person_name,
                person_reused,
            ) = _resolve_person(person_repo, template)

            for pending in prepared:
                result = pending.item.result
                result.person_id = person_id
                result.person_reused = person_reused
                result.external_person_id = external_person_id
                result.name = person_name
                crop_path: str | None = None
                try:
                    crop_path = save_registered_crop(
                        pending.image_path,
                        pending.storage.registered_crop_path,
                        keep_crop=request.keep_crop,
                    )
                    payload = {
                        "registration_mode": MODE_EXTERNAL_IMAGE,
                        "real_embedding_used": request.dev_mock_embedding_fixture is None,
                        "dev_mock_used": bool(request.dev_mock_embedding_fixture),
                        "storage_fallback_used": pending.storage.storage_fallback_used,
                        "registered_crop_path": crop_path,
                        "batch_registration": {
                            "index": pending.item.index,
                            "size": len(request.image_paths),
                        },
                    }
                    gallery_id = gallery_repo.add_embedding(
                        person_id=person_id,
                        embedding=pending.candidate.embedding,
                        source_type=request.source_type,
                        source_image_path=pending.image_path,
                        embedding_model=pending.candidate.embedding_model,
                        model_version=pending.candidate.model_version,
                        quality=pending.candidate.quality,
                        face_bbox=pending.candidate.face_bbox,
                        landmarks=pending.candidate.landmarks,
                        is_primary=False,
                        payload=payload,
                    )
                except ValueError as exc:
                    _unlink_crop_if_present(crop_path)
                    _set_registration_failure(
                        result,
                        RegistrationError(
                            ERROR_EMBEDDING_DIM_INVALID
                            if "length=" in str(exc)
                            else ERROR_EMBEDDING_NORM_INVALID,
                            str(exc),
                        ),
                    )
                    continue
                except OSError as exc:
                    _unlink_crop_if_present(crop_path)
                    _set_registration_failure(
                        result,
                        RegistrationError(ERROR_IMAGE_READ_FAILED, str(exc)),
                    )
                    continue

                result.status = STATUS_REGISTERED
                result.gallery_embedding_id = gallery_id
                result.is_primary = False
                result.face_bbox = pending.candidate.face_bbox
                result.landmarks = pending.candidate.landmarks
                result.quality = pending.candidate.quality
                result.embedding_model = pending.candidate.embedding_model
                result.model_version = pending.candidate.model_version
                result.embedding_dim = len(pending.candidate.embedding)
                result.embedding_norm = _norm(pending.candidate.embedding)
                result.registered_crop_path = crop_path
                result.real_embedding_used = request.dev_mock_embedding_fixture is None
                result.dev_mock_used = bool(request.dev_mock_embedding_fixture)
                result.fallback_used = bool(request.dev_mock_embedding_fixture)
                successful.append(pending)

            if not successful:
                raise _NoBatchImagesRegistered()

            selected_primary = successful[0]
            has_primary = _has_active_primary_gallery(conn, person_id=person_id)
            if request.is_primary or not has_primary:
                _promote_gallery_primary(
                    conn,
                    person_id=person_id,
                    gallery_id=int(selected_primary.item.result.gallery_embedding_id),
                )
                selected_primary.item.result.is_primary = True
    except _NoBatchImagesRegistered:
        if not person_reused:
            for pending in prepared:
                pending.item.result.person_id = None
        return _finalize_batch_result(request, items)
    except RegistrationError as exc:
        for pending in prepared:
            _set_registration_failure(pending.item.result, exc)
        return _finalize_batch_result(request, items, error=exc)
    except psycopg.Error as exc:
        error = _database_registration_error(exc)
        for pending in prepared:
            _unlink_crop_if_present(pending.item.result.registered_crop_path)
            pending.item.result.gallery_embedding_id = None
            pending.item.result.is_primary = False
            pending.item.result.registered_crop_path = None
            if not person_reused:
                pending.item.result.person_id = None
            _set_registration_failure(pending.item.result, error)
        return _finalize_batch_result(request, items, error=error)
    else:
        return _finalize_batch_result(
            request,
            items,
            person_id=person_id,
            person_reused=person_reused,
            external_person_id=external_person_id,
            name=person_name,
        )
    finally:
        conn.close()
