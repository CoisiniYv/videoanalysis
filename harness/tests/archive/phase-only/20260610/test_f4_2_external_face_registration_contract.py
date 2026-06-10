from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "services" / "face-worker" / "register_face_image.py"
SERVICE = ROOT / "services" / "face-worker" / "app" / "image_face_registration.py"
PREPROCESS = ROOT / "services" / "face-worker" / "app" / "face_image_preprocess.py"
DOC = ROOT / "docs" / "phase_f4_2_external_face_registration_mvp.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_cli_exists() -> None:
    assert CLI.exists()


def test_service_exists() -> None:
    assert SERVICE.exists()


def test_preprocess_exists() -> None:
    assert PREPROCESS.exists()


def test_cli_supports_required_flags() -> None:
    content = _read(CLI)
    assert "--image" in content
    assert "--external-person-id" in content
    assert "--name" in content
    assert "--person-id" in content
    assert "--output-json" in content
    assert "--is-primary" in content
    assert "--quality-threshold" in content
    assert "--allow-multiple-faces" in content
    assert "--keep-crop" in content
    assert "--dev-mock-embedding-fixture" in content


def test_cli_does_not_support_fallback_observation_id() -> None:
    assert "--fallback-observation-id" not in _read(CLI)
    assert "--fallback-observation-id" not in _read(SERVICE)


def test_service_writes_person_gallery_embeddings_only() -> None:
    content = _read(SERVICE)
    assert "GalleryRepository" in content
    assert "add_embedding(" in content
    assert "source_type=request.source_type" in content
    assert "source_image_path=image_path" in content
    assert "face_observations" not in content
    assert "match_results" not in content
    assert "security.events" not in content


def test_source_type_manual_upload_is_enforced() -> None:
    content = _read(CLI) + _read(SERVICE)
    assert "manual_upload" in content
    assert "choices=[\"manual_upload\"]" in content


def test_embedding_dim_and_norm_validation_present() -> None:
    content = _read(SERVICE)
    assert "len(candidate.embedding) != 512" in content
    assert "ERROR_EMBEDDING_DIM_INVALID" in content
    assert "0.90 <= norm <= 1.10" in content
    assert "ERROR_EMBEDDING_NORM_INVALID" in content


def test_failure_modes_present() -> None:
    content = _read(SERVICE)
    for token in [
        "NO_FACE_DETECTED",
        "MULTIPLE_FACES_DETECTED",
        "FACE_TOO_SMALL",
        "LANDMARKS_MISSING",
        "QUALITY_TOO_LOW",
        "REAL_IMAGE_EMBEDDING_UNAVAILABLE",
        "DATABASE_URL_MISSING",
        "DATABASE_CONNECTION_FAILED",
        "PERSON_NOT_FOUND",
        "EXTERNAL_PERSON_ID_CONFLICT",
        "PRIMARY_GALLERY_CONSTRAINT_FAILED",
    ]:
        assert token in content


def test_real_path_defaults_to_not_implemented_embedder() -> None:
    content = _read(SERVICE)
    assert "NotImplementedRealImageEmbedder" in content
    assert "REAL_IMAGE_EMBEDDING_UNAVAILABLE" in content


def test_no_face_observation_embedding_copy_fallback() -> None:
    content = _read(SERVICE)
    assert "--fallback-observation-id" not in content
    assert "face_observations" not in content


def test_storage_policy_uses_media_root_with_fallback() -> None:
    content = _read(PREPROCESS)
    assert 'MEDIA_ROOT' in content
    assert 'FACE_REGISTRATION_ROOT' in content
    assert "/data/video-analytics/media" in content
    assert "./tmp/face_registration" in content or 'tmp", "face_registration' in content
    assert "storage_fallback_used" in _read(SERVICE) or "storage_fallback_used" in _read(CLI)


def test_output_fields_cover_required_json_contract() -> None:
    content = _read(SERVICE)
    for token in [
        "status",
        "mode",
        "person_id",
        "external_person_id",
        "name",
        "gallery_embedding_id",
        "face_bbox",
        "landmarks",
        "quality",
        "embedding_model",
        "model_version",
        "embedding_dim",
        "embedding_norm",
        "source_image_path",
        "registered_crop_path",
        "storage_fallback_used",
        "real_embedding_used",
        "dev_mock_used",
        "fallback_used",
    ]:
        assert token in content


def test_docs_describe_external_submitted_image_scope() -> None:
    content = _read(DOC)
    assert "external submitted image" in content
    assert "person_gallery_embeddings" in content
    assert "not" in content
    assert "video observation enrollment" in content
    assert "F4.3" in content
    assert "F4.2b" in content or "real embedding" in content.lower()
    assert "face_observations" in content
    assert "Savant pipeline changes" in content
    assert "Redis producer changes" in content


def test_f35b_smoke_semantics_not_touched() -> None:
    smoke = ROOT / "scripts" / "smoke" / "check_f3_5b_one_face_recognition_e2e.sh"
    if not smoke.exists():
        smoke = (
            ROOT
            / "scripts"
            / "smoke"
            / "archive"
            / "phase-only"
            / "20260602"
            / "check_f3_5b_one_face_recognition_e2e.sh"
        )
    assert smoke.exists()
    content = _read(smoke)
    assert "f35b0000-0000-4000-8000-000000000001" in content


def test_rejects_video_file_types() -> None:
    content = _read(SERVICE)
    assert "IMAGE_FILE_TYPE_UNSUPPORTED" in content
    assert ".mp4" in content
    assert ".mov" in content
    assert ".avi" in content


def test_cli_supports_model_path_args() -> None:
    content = _read(CLI)
    assert "--face-detector-onnx" in content
    assert "--adaface-onnx" in content
    assert "--onnx-provider" in content


def test_offline_embedder_adapter_exists() -> None:
    content = _read(SERVICE)
    assert "OfflineFaceEmbedderAdapter" in content


def test_default_embedder_resolves_offline() -> None:
    content = _read(SERVICE)
    assert "_resolve_default_embedder" in content


def test_provider_info_in_result() -> None:
    content = _read(SERVICE)
    assert "detector_providers" in content
    assert "embedder_providers" in content


def test_offline_adapter_forwards_allow_multiple_faces_and_quality_threshold() -> None:
    content = _read(SERVICE)
    assert "allow_multiple_faces=allow_multiple_faces" in content
    assert "quality_threshold=quality_threshold" in content
    assert "allow_multiple_faces=request.allow_multiple_faces" in content
    assert "quality_threshold=request.quality_threshold" in content
