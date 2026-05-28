"""Contract tests for F4.2b OfflineFaceEmbedder.

These tests verify the offline face embedder implementation without
requiring actual model files or inference. They check:
- Module structure and imports
- Required classes and methods
- Environment variable support
- CLI flag support
- Image type rejection
- Integration with image_face_registration
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "services" / "face-worker" / "app" / "image_face_registration.py"
PREPROCESS = ROOT / "services" / "face-worker" / "app" / "face_image_preprocess.py"
OFFLINE_EMBEDDER = ROOT / "services" / "face-worker" / "app" / "offline_face_embedder.py"
ONNX_UTILS = ROOT / "services" / "face-worker" / "app" / "onnx_runtime_utils.py"
CLI = ROOT / "services" / "face-worker" / "register_face_image.py"
DOC = ROOT / "docs" / "phase_f4_2_external_face_registration_mvp.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# === File existence ===

def test_offline_face_embedder_exists() -> None:
    assert OFFLINE_EMBEDDER.exists(), "offline_face_embedder.py must exist"


def test_onnx_runtime_utils_exists() -> None:
    assert ONNX_UTILS.exists(), "onnx_runtime_utils.py must exist"


# === OfflineFaceEmbedder class ===

def test_offline_embedder_class_exists() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "class OfflineFaceEmbedder" in content


def test_offline_embedder_has_extract_method() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "def extract(" in content


def test_offline_embedder_returns_face_image_embedding_result() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "FaceImageEmbeddingResult" in content


# === ONNX Runtime integration ===

def test_offline_embedder_uses_onnxruntime() -> None:
    content = _read(OFFLINE_EMBEDDER) + _read(ONNX_UTILS)
    assert "onnxruntime" in content or "import onnx" in content.lower()


def test_onnx_utils_loads_session() -> None:
    content = _read(ONNX_UTILS)
    assert "def load_session(" in content


def test_onnx_utils_resolves_providers() -> None:
    content = _read(ONNX_UTILS)
    assert "def resolve_providers(" in content


# === Environment variable support ===

def test_supports_yolov8_face_onnx_env() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "YOLOV8_FACE_ONNX" in content


def test_supports_adaface_onnx_env() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "ADAFACE_ONNX" in content


# === CLI flag support ===

def test_cli_supports_face_detector_onnx() -> None:
    content = _read(CLI)
    assert "--face-detector-onnx" in content


def test_cli_supports_adaface_onnx() -> None:
    content = _read(CLI)
    assert "--adaface-onnx" in content


def test_cli_supports_onnx_provider() -> None:
    content = _read(CLI)
    assert "--onnx-provider" in content


# === RegistrationRequest model paths ===

def test_registration_request_has_model_path_fields() -> None:
    content = _read(SERVICE)
    assert "face_detector_onnx" in content
    assert "adaface_onnx" in content
    assert "onnx_provider" in content


# === Image type rejection ===

def test_rejects_video_file_types() -> None:
    content = _read(SERVICE)
    assert "IMAGE_FILE_TYPE_UNSUPPORTED" in content
    assert ".mp4" in content
    assert ".mov" in content
    assert ".avi" in content


def test_supported_image_extensions_include_jpg_png_webp() -> None:
    content = _read(SERVICE)
    assert ".jpg" in content
    assert ".jpeg" in content
    assert ".png" in content
    assert ".webp" in content


# === No fallback to face_observations ===

def test_no_fallback_observation_id() -> None:
    content = _read(CLI) + _read(SERVICE)
    assert "--fallback-observation-id" not in content


def test_no_face_observations_in_service() -> None:
    content = _read(SERVICE)
    assert "face_observations" not in content


# === Embedding validation ===

def test_embedding_dim_512_validated() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "512" in content
    assert "EMBEDDING_DIM_INVALID" in content


def test_embedding_norm_validated() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "0.90" in content
    assert "1.10" in content
    assert "EMBEDDING_NORM_INVALID" in content


def test_l2_normalization_applied() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "linalg.norm" in content or "L2" in content


# === YOLOv8-Face decode ===

def test_decodes_yolov8_output_format() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "output0" in content or "cxcywh" in content or "cx, cy, w, h" in content


def test_implements_nms() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "nms" in content.lower() or "_nms" in content


def test_letterbox_resize() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "letterbox" in content.lower() or "_letterbox" in content


# === AdaFace alignment ===

def test_face_alignment_with_landmarks() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "align" in content.lower() or "warpAffine" in content


def test_adaface_preprocessing() -> None:
    content = _read(OFFLINE_EMBEDDER)
    assert "127.5" in content  # AdaFace normalization


# === Provider info output ===

def test_registration_result_has_provider_fields() -> None:
    content = _read(SERVICE)
    assert "detector_providers" in content
    assert "embedder_providers" in content


def test_cli_prints_provider_info() -> None:
    content = _read(CLI)
    assert "detector_providers" in content or "embedder_providers" in content


# === OfflineFaceEmbedderAdapter ===

def test_adapter_class_exists() -> None:
    content = _read(SERVICE)
    assert "OfflineFaceEmbedderAdapter" in content


# === Doc updates ===

def test_doc_describes_f42b_goal() -> None:
    content = _read(DOC)
    assert "F4.2b" in content or "real" in content.lower()


def test_doc_describes_real_embedding_requirement() -> None:
    content = _read(DOC)
    assert "real_embedding_used" in content


def test_doc_describes_video_rejection() -> None:
    content = _read(DOC)
    assert "mp4" in content.lower() or "video" in content.lower()
