from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
LIBS = str(ROOT / "libs")
if LIBS not in sys.path:
    sys.path.insert(0, LIBS)

from face_registration import image_face_registration as registration


def _candidate() -> registration.EmbeddingCandidate:
    value = 1.0 / math.sqrt(512)
    return registration.EmbeddingCandidate(
        embedding=[value] * 512,
        face_bbox=[10.0, 10.0, 80.0, 80.0],
        landmarks=[[20.0, 20.0]] * 5,
        quality=0.95,
        embedding_model="adaface",
        model_version="test",
        source_image_path="",
        detection_confidence=0.99,
    )


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Conn:
    def __init__(self) -> None:
        self.transaction_count = 0
        self.closed = False

    def transaction(self):
        self.transaction_count += 1
        return _Transaction()

    def close(self) -> None:
        self.closed = True


class _PersonRepo:
    created_count = 0

    def __init__(self, _conn) -> None:
        pass

    def get_by_external_person_id(self, _external_person_id):
        return None

    def create_person(self, **_kwargs):
        type(self).created_count += 1
        return 41


class _GalleryRepo:
    calls: list[dict] = []

    def __init__(self, _conn) -> None:
        pass

    def add_embedding(self, **kwargs):
        type(self).calls.append(kwargs)
        return 700 + len(type(self).calls)


class _Embedder:
    def extract(self, image_path, **_kwargs):
        if Path(image_path).name.startswith("bad"):
            raise registration.RegistrationError(
                registration.ERROR_NO_FACE_DETECTED,
                "No face detected",
            )
        return [_candidate()]


def _request(paths: list[Path]) -> registration.BatchRegistrationRequest:
    return registration.BatchRegistrationRequest(
        image_paths=tuple(str(path) for path in paths),
        image_names=tuple(path.name for path in paths),
        external_person_id="person:batch:reese",
        name="Reese",
        person_id=None,
        description="batch registration",
        source_type="manual_upload",
        is_primary=False,
        quality_threshold=0.65,
        allow_multiple_faces=False,
        keep_crop=True,
        created_by="pytest",
        dev_mock_embedding_fixture=None,
    )


def test_batch_registration_reuses_one_embedder_and_keeps_valid_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = tmp_path / "good.jpg"
    bad = tmp_path / "bad.jpg"
    good.write_bytes(b"good")
    bad.write_bytes(b"bad")
    conn = _Conn()
    promoted: list[int] = []
    _PersonRepo.created_count = 0
    _GalleryRepo.calls = []

    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(registration, "_connect", lambda _url: conn)
    monkeypatch.setattr(registration, "PersonRepository", _PersonRepo)
    monkeypatch.setattr(registration, "GalleryRepository", _GalleryRepo)
    monkeypatch.setattr(
        registration,
        "resolve_registration_storage",
        lambda image_path: registration.StorageResolution(
            registration_root=str(tmp_path / "crops"),
            registered_crop_path=str(tmp_path / "crops" / f"{Path(image_path).stem}.jpg"),
            storage_fallback_used=False,
            storage_fallback_reason=None,
        ),
    )
    monkeypatch.setattr(
        registration,
        "save_registered_crop",
        lambda image_path, destination_path, *, keep_crop: destination_path
        if keep_crop
        else None,
    )
    monkeypatch.setattr(
        registration,
        "_select_candidate",
        lambda candidates, **_kwargs: candidates[0],
    )
    monkeypatch.setattr(
        registration,
        "_has_active_primary_gallery",
        lambda _conn, *, person_id: False,
    )
    monkeypatch.setattr(
        registration,
        "_promote_gallery_primary",
        lambda _conn, *, person_id, gallery_id: promoted.append(gallery_id),
    )

    result = registration.register_external_images(_request([good, bad]), embedder=_Embedder())

    assert result.status == registration.STATUS_PARTIAL
    assert result.person_id == 41
    assert result.registered_count == 1
    assert result.failed_count == 1
    assert result.items[0].result.status == registration.STATUS_REGISTERED
    assert result.items[0].result.is_primary is True
    assert result.items[1].result.error_code == registration.ERROR_NO_FACE_DETECTED
    assert _PersonRepo.created_count == 1
    assert len(_GalleryRepo.calls) == 1
    assert promoted == [701]
    assert conn.transaction_count == 1
    assert conn.closed is True


def test_batch_registration_does_not_create_person_when_every_image_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"bad")
    _PersonRepo.created_count = 0

    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(
        registration,
        "resolve_registration_storage",
        lambda image_path: registration.StorageResolution(
            registration_root=str(tmp_path / "crops"),
            registered_crop_path=str(tmp_path / "crops" / f"{Path(image_path).stem}.jpg"),
            storage_fallback_used=False,
            storage_fallback_reason=None,
        ),
    )
    monkeypatch.setattr(
        registration,
        "_select_candidate",
        lambda candidates, **_kwargs: candidates[0],
    )

    result = registration.register_external_images(_request([bad]), embedder=_Embedder())

    assert result.status == registration.STATUS_FAILED
    assert result.person_id is None
    assert result.registered_count == 0
    assert result.failed_count == 1
    assert result.items[0].result.error_code == registration.ERROR_NO_FACE_DETECTED
    assert _PersonRepo.created_count == 0
