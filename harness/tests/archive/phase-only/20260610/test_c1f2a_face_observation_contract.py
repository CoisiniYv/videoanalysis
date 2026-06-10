"""C1F.2a — Face observation pipeline audit contract tests.

Read-only checks for AdaFace, face_reid_gate, face_observation_exporter,
landmark contract, image byte prohibition, and export default state.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MODULE_YAML = REPO / "modules" / "savant_security" / "module.yml"
COMPOSE_C1E = REPO / "infra" / "docker-compose.c1-official-replay-dev.yml"
AUDIT_DOC = REPO / "docs" / "c1f2a_face_observation_pipeline_audit.md"

FACE_REID_GATE_PYFUNC = (
    REPO / "modules" / "savant_security" / "custom" / "pyfuncs" / "face_reid_gate.py"
)
FACE_OBS_EXPORTER_PYFUNC = (
    REPO
    / "modules"
    / "savant_security"
    / "custom"
    / "pyfuncs"
    / "face_observation_exporter.py"
)
FACE_REID_GATE_SERVICE = (
    REPO / "modules" / "savant_security" / "custom" / "services" / "face_reid_gate.py"
)
FACE_OBS_EXPORTER_SERVICE = (
    REPO
    / "modules"
    / "savant_security"
    / "custom"
    / "services"
    / "face_observation_exporter.py"
)
FACE_EVENTS_MODEL = (
    REPO / "modules" / "savant_security" / "custom" / "models" / "face_events.py"
)

MODEL_DIR = Path("/data/video-analytics/models")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_module_yaml() -> dict:
    with open(MODULE_YAML) as f:
        return yaml.safe_load(f)


def _load_compose(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _get_pipeline_elements(module: dict) -> list[dict]:
    pipeline = module.get("pipeline", {})
    if isinstance(pipeline, dict):
        return pipeline.get("elements", [])
    if isinstance(pipeline, list):
        return pipeline
    return []


def _find_element_by_name(module: dict, name: str) -> dict | None:
    for e in _get_pipeline_elements(module):
        if isinstance(e, dict) and e.get("name") == name:
            return e
    return None


def _element_index(module: dict, name: str) -> int:
    for i, e in enumerate(_get_pipeline_elements(module)):
        if isinstance(e, dict) and e.get("name") == name:
            return i
    return -1


def _code_without_docstrings(path: Path) -> str:
    source = path.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(
                    getattr(node.body[0], "value", None), (ast.Constant, ast.Str)
                )
            ):
                node.body = node.body[1:]
    return ast.unparse(tree).lower()


# ---------------------------------------------------------------------------
# 1. AdaFace element in module.yml
# ---------------------------------------------------------------------------

class TestAdaFaceElement:
    def test_exists(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "adaface")
        assert elem is not None

    def test_element_type(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "adaface")
        assert elem.get("element") == "nvinfer@attribute_model"

    def test_input_object_is_face(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "adaface")
        inp = elem.get("model", {}).get("input", {})
        assert inp.get("object") == "yolov8_face.face"

    def test_input_shape_112(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "adaface")
        shape = elem.get("model", {}).get("input", {}).get("shape", [])
        assert shape == [3, 112, 112]

    def test_has_preprocessing(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "adaface")
        preproc = (
            elem.get("model", {}).get("input", {}).get("preprocess_object_image", {})
        )
        assert preproc.get("class_name") == "AlignFacePreprocessingObjectImageGPU"

    def test_output_attribute_feature(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "adaface")
        attrs = elem.get("model", {}).get("output", {}).get("attributes", [])
        names = [a.get("name") for a in attrs if isinstance(a, dict)]
        assert "feature" in names

    def test_after_face_person_associator(self):
        module = _load_module_yaml()
        idx_adaface = _element_index(module, "adaface")
        idx_assoc = _element_index(module, "face_person_associator")
        assert idx_adaface > idx_assoc


# ---------------------------------------------------------------------------
# 2. AdaFace model asset
# ---------------------------------------------------------------------------

class TestAdaFaceModelAsset:
    def test_onnx_exists(self):
        onnx = MODEL_DIR / "adaface" / "adaface_ir50_webface4m.onnx"
        if not MODEL_DIR.exists():
            pytest.skip("Model directory not accessible")
        assert onnx.exists()

    def test_engine_exists(self):
        engine = (
            MODEL_DIR
            / "adaface"
            / "adaface_ir50_webface4m.onnx_b16_gpu0_fp16.engine"
        )
        if not MODEL_DIR.exists():
            pytest.skip("Model directory not accessible")
        assert engine.exists()


# ---------------------------------------------------------------------------
# 3. YOLOv8-Face landmark output
# ---------------------------------------------------------------------------

class TestFaceLandmarkOutput:
    def test_yolov8_face_has_landmarks_attr(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "yolov8_face")
        assert elem is not None
        attrs = elem.get("model", {}).get("output", {}).get("attributes", [])
        names = [a.get("name") for a in attrs if isinstance(a, dict)]
        assert "landmarks" in names


# ---------------------------------------------------------------------------
# 4. face_reid_gate
# ---------------------------------------------------------------------------

class TestFaceReidGate:
    def test_element_exists(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "face_reid_gate")
        assert elem is not None

    def test_pyfunc_file_exists(self):
        assert FACE_REID_GATE_PYFUNC.exists()

    def test_service_file_exists(self):
        assert FACE_REID_GATE_SERVICE.exists()

    def test_checks_person_track_id(self):
        code = FACE_REID_GATE_SERVICE.read_text()
        assert "person_track_id" in code
        assert "has_track_id" in code

    def test_checks_confidence(self):
        code = FACE_REID_GATE_SERVICE.read_text()
        assert "face_confidence" in code
        assert "min_confidence" in code.lower() or "min_conf" in code

    def test_checks_face_size(self):
        code = FACE_REID_GATE_SERVICE.read_text()
        assert "face_width" in code
        assert "face_height" in code
        assert "min_size" in code

    def test_checks_landmarks(self):
        code = FACE_REID_GATE_SERVICE.read_text()
        assert "landmarks" in code
        assert "bad_landmarks" in code or "no_landmarks" in code

    def test_checks_feature_dim_512(self):
        code = FACE_REID_GATE_SERVICE.read_text()
        assert "512" in code
        assert "EXPECTED_FEATURE_DIM" in code or "expected_feature_dim" in code

    def test_checks_embedding_norm(self):
        code = FACE_REID_GATE_SERVICE.read_text()
        assert "embedding_norm" in code or "norm" in code
        assert "norm_tol" in code or "norm_tolerance" in code

    def test_rejects_nan(self):
        code = FACE_REID_GATE_SERVICE.read_text()
        assert "isnan" in code or "nan" in code.lower()

    def test_has_throttle(self):
        code = FACE_REID_GATE_SERVICE.read_text()
        assert "ThrottleMap" in code or "throttle" in code.lower()

    def test_after_adaface(self):
        module = _load_module_yaml()
        idx_gate = _element_index(module, "face_reid_gate")
        idx_adaface = _element_index(module, "adaface")
        assert idx_gate > idx_adaface


# ---------------------------------------------------------------------------
# 5. face_observation_exporter
# ---------------------------------------------------------------------------

class TestFaceObservationExporter:
    def test_element_exists(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "face_observation_exporter")
        assert elem is not None

    def test_pyfunc_file_exists(self):
        assert FACE_OBS_EXPORTER_PYFUNC.exists()

    def test_service_file_exists(self):
        assert FACE_OBS_EXPORTER_SERVICE.exists()

    def test_writes_redis_stream(self):
        code = FACE_OBS_EXPORTER_SERVICE.read_text()
        assert "security.face_observations" in code
        assert "xadd" in code

    def test_requires_reid_allowed(self):
        code = FACE_OBS_EXPORTER_PYFUNC.read_text()
        assert "reid_allowed" in code

    def test_requires_person_track_id(self):
        code = FACE_OBS_EXPORTER_PYFUNC.read_text()
        assert "person_track_id" in code

    def test_requires_embedding(self):
        code = FACE_OBS_EXPORTER_PYFUNC.read_text()
        assert "feature" in code or "embedding" in code

    def test_after_face_reid_gate(self):
        module = _load_module_yaml()
        idx_exporter = _element_index(module, "face_observation_exporter")
        idx_gate = _element_index(module, "face_reid_gate")
        assert idx_exporter > idx_gate


# ---------------------------------------------------------------------------
# 6. Image bytes forbidden
# ---------------------------------------------------------------------------

class TestImageBytesForbidden:
    def test_no_jpeg_png_raw_in_exporter(self):
        code = _code_without_docstrings(FACE_OBS_EXPORTER_PYFUNC)
        for token in ["cv2.imwrite", ".save(", "jpeg", "png", "raw_image"]:
            assert token not in code, f"Forbidden token in exporter: {token}"

    def test_no_base64_in_exporter(self):
        code = _code_without_docstrings(FACE_OBS_EXPORTER_PYFUNC)
        assert "base64" not in code

    def test_no_image_bytes_in_service(self):
        code = _code_without_docstrings(FACE_OBS_EXPORTER_SERVICE)
        for token in ["cv2.imwrite", ".save(", "base64", "jpeg", "png"]:
            assert token not in code, f"Forbidden token in service: {token}"

    def test_embedding_is_json_list(self):
        code = FACE_OBS_EXPORTER_SERVICE.read_text()
        assert "json.dumps" in code


# ---------------------------------------------------------------------------
# 7. Export default disabled in compose
# ---------------------------------------------------------------------------

class TestExportDefaultDisabled:
    def test_compose_disables_export(self):
        compose = _load_compose(COMPOSE_C1E)
        savant = compose.get("services", {}).get("savant-security", {})
        env = savant.get("environment", {})
        assert env.get("FACE_OBSERVATION_EXPORT_ENABLED") == "false"

    def test_code_default_is_true(self):
        code = FACE_OBS_EXPORTER_SERVICE.read_text()
        # Code defaults to "true" but compose overrides to "false"
        assert '"true"' in code


# ---------------------------------------------------------------------------
# 8. Face events model
# ---------------------------------------------------------------------------

class TestFaceEventsModel:
    def test_file_exists(self):
        assert FACE_EVENTS_MODEL.exists()

    def test_has_draft_class(self):
        code = FACE_EVENTS_MODEL.read_text()
        assert "FaceObservationEventDraft" in code

    def test_has_source_observation_id_builder(self):
        code = FACE_EVENTS_MODEL.read_text()
        assert "build_face_source_observation_id" in code


# ---------------------------------------------------------------------------
# 9. Audit doc
# ---------------------------------------------------------------------------

class TestAuditDoc:
    def test_exists(self):
        assert AUDIT_DOC.exists()

    def test_has_pass_status(self):
        content = AUDIT_DOC.read_text()
        assert "PASS_FACE_OBSERVATION_CONFIG_READY" in content


# ---------------------------------------------------------------------------
# 10. Full module startup risk note
# ---------------------------------------------------------------------------

class TestFullModuleStartupRisk:
    def test_full_module_has_many_elements(self):
        module = _load_module_yaml()
        elements = _get_pipeline_elements(module)
        # Full module should have more than 8 elements
        assert len(elements) > 8

    def test_same_frame_debug_hardened(self):
        debug_pyfunc = (
            REPO
            / "modules"
            / "savant_security"
            / "custom"
            / "pyfuncs"
            / "same_frame_detection_debug.py"
        )
        code = debug_pyfunc.read_text()
        # Should use lazy init, not on_start file I/O
        assert "_ensure_file" in code or "process_frame" in code
        # on_start should NOT have file operations
        # (check that on_start doesn't contain mkdir or open)
        lines = code.split("\n")
        in_on_start = False
        for line in lines:
            if "def on_start" in line:
                in_on_start = True
            elif in_on_start and line.strip().startswith("def "):
                in_on_start = False
            if in_on_start:
                assert "mkdir" not in line, "on_start should not do mkdir"
                assert ".open(" not in line, "on_start should not open files"
