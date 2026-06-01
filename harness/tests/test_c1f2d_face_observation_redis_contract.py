"""C1F.2d — Face observation Redis export contract tests."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MODULE_YAML = (
    REPO / "modules" / "savant_security" / "module.c1f2d_face_observation_redis.yml"
)
SMOKE_SCRIPT = (
    REPO / "scripts" / "smoke" / "check_c1f2d_face_observation_redis_smoke.sh"
)
DOC = REPO / "docs" / "c1f2d_face_observation_redis_smoke.md"
COMPOSE_C1E = REPO / "infra" / "docker-compose.c1-official-replay-dev.yml"
GATE_PYFUNC = (
    REPO / "modules" / "savant_security" / "custom" / "pyfuncs" / "face_reid_gate.py"
)
EXPORTER_PYFUNC = (
    REPO
    / "modules"
    / "savant_security"
    / "custom"
    / "pyfuncs"
    / "face_observation_exporter.py"
)
GATE_SERVICE = (
    REPO
    / "modules"
    / "savant_security"
    / "custom"
    / "services"
    / "face_reid_gate.py"
)
EXPORTER_SERVICE = (
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


def _load_module() -> dict:
    with open(MODULE_YAML) as f:
        return yaml.safe_load(f)


def _get_elements(module: dict) -> list[dict]:
    pipeline = module.get("pipeline", {})
    if isinstance(pipeline, dict):
        return pipeline.get("elements", [])
    return []


def _find_by_name(module: dict, name: str) -> dict | None:
    for e in _get_elements(module):
        if isinstance(e, dict) and e.get("name") == name:
            return e
    return None


def _code_clean(path: Path) -> str:
    source = path.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                getattr(node.body[0], "value", None), (ast.Constant, ast.Str)
            ):
                node.body = node.body[1:]
    return ast.unparse(tree).lower()


def _smoke_text() -> str:
    return SMOKE_SCRIPT.read_text().lower()


# ---------------------------------------------------------------------------
# 1. Module exists
# ---------------------------------------------------------------------------


class TestModuleExists:
    def test_file_exists(self):
        assert MODULE_YAML.exists()

    def test_valid_yaml(self):
        _load_module()


# ---------------------------------------------------------------------------
# 2. YOLO26-pose full-frame primary
# ---------------------------------------------------------------------------


class TestPoseDetector:
    def test_yolo26_pose_exists(self):
        assert _find_by_name(_load_module(), "yolo26_pose") is not None

    def test_element_type(self):
        elem = _find_by_name(_load_module(), "yolo26_pose")
        assert elem.get("element") == "nvinfer@complex_model"

    def test_label_person(self):
        elem = _find_by_name(_load_module(), "yolo26_pose")
        objects = elem.get("model", {}).get("output", {}).get("objects", [])
        labels = [o.get("label") for o in objects if isinstance(o, dict)]
        assert "person" in labels


# ---------------------------------------------------------------------------
# 3. YOLOv8-Face full-frame primary
# ---------------------------------------------------------------------------


class TestFaceDetector:
    def test_yolov8_face_exists(self):
        assert _find_by_name(_load_module(), "yolov8_face") is not None

    def test_full_frame_primary(self):
        elem = _find_by_name(_load_module(), "yolov8_face")
        model = elem.get("model", {})
        inp = model.get("input", {})
        assert "object" not in inp

    def test_label_face(self):
        elem = _find_by_name(_load_module(), "yolov8_face")
        objects = elem.get("model", {}).get("output", {}).get("objects", [])
        labels = [o.get("label") for o in objects if isinstance(o, dict)]
        assert "face" in labels

    def test_not_person_roi_secondary(self):
        """YOLOv8-Face must NOT be configured as person ROI secondary."""
        elem = _find_by_name(_load_module(), "yolov8_face")
        inp = elem.get("model", {}).get("input", {})
        assert inp.get("object") is None


# ---------------------------------------------------------------------------
# 4. AdaFace element
# ---------------------------------------------------------------------------


class TestAdaFaceElement:
    def test_exists(self):
        assert _find_by_name(_load_module(), "adaface") is not None

    def test_type(self):
        elem = _find_by_name(_load_module(), "adaface")
        assert elem.get("element") == "nvinfer@attribute_model"

    def test_input_object(self):
        elem = _find_by_name(_load_module(), "adaface")
        inp = elem.get("model", {}).get("input", {})
        assert inp.get("object") == "yolov8_face.face"

    def test_after_face_person_associator(self):
        module = _load_module()
        elements = _get_elements(module)
        names = [e.get("name") for e in elements if isinstance(e, dict)]
        assert names.index("adaface") > names.index("face_person_associator")


# ---------------------------------------------------------------------------
# 5. face_reid_gate element
# ---------------------------------------------------------------------------


class TestFaceReidGate:
    def test_exists(self):
        assert _find_by_name(_load_module(), "face_reid_gate") is not None

    def test_type(self):
        elem = _find_by_name(_load_module(), "face_reid_gate")
        assert elem.get("element") == "pyfunc"

    def test_module(self):
        elem = _find_by_name(_load_module(), "face_reid_gate")
        assert elem.get("module") == "custom.pyfuncs.face_reid_gate"

    def test_class(self):
        elem = _find_by_name(_load_module(), "face_reid_gate")
        assert elem.get("class_name") == "FaceReidGatePyFunc"

    def test_after_adaface(self):
        module = _load_module()
        elements = _get_elements(module)
        names = [e.get("name") for e in elements if isinstance(e, dict)]
        assert names.index("face_reid_gate") > names.index("adaface")


# ---------------------------------------------------------------------------
# 6. face_observation_exporter element
# ---------------------------------------------------------------------------


class TestFaceObservationExporter:
    def test_exists(self):
        assert _find_by_name(_load_module(), "face_observation_exporter") is not None

    def test_type(self):
        elem = _find_by_name(_load_module(), "face_observation_exporter")
        assert elem.get("element") == "pyfunc"

    def test_module(self):
        elem = _find_by_name(_load_module(), "face_observation_exporter")
        assert elem.get("module") == "custom.pyfuncs.face_observation_exporter"

    def test_class(self):
        elem = _find_by_name(_load_module(), "face_observation_exporter")
        assert elem.get("class_name") == "FaceObservationExporterPyFunc"

    def test_after_face_reid_gate(self):
        module = _load_module()
        elements = _get_elements(module)
        names = [e.get("name") for e in elements if isinstance(e, dict)]
        assert names.index("face_observation_exporter") > names.index(
            "face_reid_gate"
        )


# ---------------------------------------------------------------------------
# 7. Excluded elements
# ---------------------------------------------------------------------------


class TestExcludedElements:
    def test_no_behavior_rules(self):
        assert _find_by_name(_load_module(), "behavior_rules") is None

    def test_no_same_frame_detection_debug(self):
        assert (
            _find_by_name(_load_module(), "same_frame_detection_debug") is None
        )

    def test_no_adaface_runtime_debug(self):
        assert (
            _find_by_name(_load_module(), "adaface_runtime_debug") is None
        )

    def test_no_face_embedding_debug(self):
        assert (
            _find_by_name(_load_module(), "face_embedding_debug") is None
        )

    def test_no_face_debug(self):
        assert _find_by_name(_load_module(), "face_debug") is None


# ---------------------------------------------------------------------------
# 8. Pipeline order
# ---------------------------------------------------------------------------


class TestPipelineOrder:
    def test_element_order(self):
        module = _load_module()
        elements = _get_elements(module)
        names = [e.get("name") for e in elements if isinstance(e, dict)]
        expected_order = [
            "yolo26_pose",
            "tracker",
            "yolov8_face",
            "face_person_associator",
            "adaface",
            "face_reid_gate",
            "face_observation_exporter",
        ]
        for name in expected_order:
            assert name in names, f"missing element: {name}"
        # Verify order
        indices = [names.index(n) for n in expected_order]
        assert indices == sorted(indices), f"wrong order: {list(zip(expected_order, indices))}"


# ---------------------------------------------------------------------------
# 9. Smoke script contract
# ---------------------------------------------------------------------------


class TestSmokeScript:
    def test_exists(self):
        assert SMOKE_SCRIPT.exists()

    def test_uses_correct_module(self):
        text = _smoke_text()
        assert "module.c1f2d_face_observation_redis.yml" in text

    def test_enables_face_observation_export(self):
        text = _smoke_text()
        assert "face_observation_export_enabled" in text
        assert "true" in text

    def test_reads_redis_stream(self):
        text = _smoke_text()
        assert "security.face_observations" in text

    def test_checks_embedding_dim_512(self):
        text = _smoke_text()
        assert "512" in text

    def test_checks_embedding_norm(self):
        text = _smoke_text()
        assert "embedding_norm" in text

    def test_forbids_image_bytes(self):
        text = _smoke_text()
        for kw in ("jpg", "jpeg", "png", "base64"):
            assert kw in text

    def test_no_second_rtsp(self):
        text = _smoke_text()
        assert "second_rtsp" not in text
        assert "dual_rtsp" not in text

    def test_no_source_extraction(self):
        text = _smoke_text()
        assert "source_extraction" not in text
        assert "ffmpeg.*clip" not in text

    def test_staged_startup(self):
        text = _smoke_text()
        assert "source-adapter" in text
        assert "savant-security" in text

    def test_fixed_rtsp(self):
        text = _smoke_text()
        assert "10.37.57.112" in text


# ---------------------------------------------------------------------------
# 10. Documentation
# ---------------------------------------------------------------------------


class TestDocumentation:
    def test_exists(self):
        assert DOC.exists()

    def test_mentions_c1f2c(self):
        text = DOC.read_text().lower()
        assert "c1f.2c" in text or "c1f2c" in text

    def test_mentions_redis(self):
        text = DOC.read_text().lower()
        assert "redis" in text

    def test_mentions_gate(self):
        text = DOC.read_text().lower()
        assert "gate" in text

    def test_mentions_no_image_bytes(self):
        text = DOC.read_text().lower()
        assert "no image bytes" in text or "image bytes" in text


# ---------------------------------------------------------------------------
# 11. Existing gate/exporter code
# ---------------------------------------------------------------------------


class TestExistingGateCode:
    def test_gate_pyfunc_exists(self):
        assert GATE_PYFUNC.exists()

    def test_gate_service_exists(self):
        assert GATE_SERVICE.exists()

    def test_gate_reads_adaface(self):
        code = _code_clean(GATE_PYFUNC)
        assert "adaface" in code

    def test_gate_reads_reid_namespace(self):
        code = _code_clean(GATE_PYFUNC)
        assert "reid" in code

    def test_gate_checks_track_id(self):
        code = _code_clean(GATE_SERVICE)
        assert "person_track_id" in code or "track_id" in code

    def test_gate_checks_confidence(self):
        code = _code_clean(GATE_SERVICE)
        assert "confidence" in code

    def test_gate_checks_face_size(self):
        code = _code_clean(GATE_SERVICE)
        assert "face_width" in code or "min_size" in code

    def test_gate_checks_landmarks(self):
        code = _code_clean(GATE_SERVICE)
        assert "landmarks" in code

    def test_gate_checks_feature_dim(self):
        code = _code_clean(GATE_SERVICE)
        assert "512" in code

    def test_gate_checks_norm(self):
        code = _code_clean(GATE_SERVICE)
        assert "norm" in code

    def test_gate_checks_nan(self):
        code = _code_clean(GATE_SERVICE)
        assert "nan" in code

    def test_gate_throttle(self):
        code = _code_clean(GATE_SERVICE)
        assert "throttle" in code


class TestExistingExporterCode:
    def test_exporter_pyfunc_exists(self):
        assert EXPORTER_PYFUNC.exists()

    def test_exporter_service_exists(self):
        assert EXPORTER_SERVICE.exists()

    def test_exporter_reads_gate_verdict(self):
        code = _code_clean(EXPORTER_PYFUNC)
        assert "reid_allowed" in code

    def test_exporter_writes_redis_stream(self):
        code = _code_clean(EXPORTER_SERVICE)
        assert "xadd" in code

    def test_exporter_stream_name(self):
        code = _code_clean(EXPORTER_SERVICE)
        assert "security.face_observations" in code

    def test_exporter_no_image_bytes(self):
        code = _code_clean(EXPORTER_PYFUNC)
        assert "image_bytes" not in code
        assert "crop_bytes" not in code
        assert "base64" not in code

    def test_exporter_env_gate(self):
        code = _code_clean(EXPORTER_SERVICE)
        assert "face_observation_export_enabled" in code


class TestFaceEventsModel:
    def test_model_exists(self):
        assert FACE_EVENTS_MODEL.exists()

    def test_no_image_fields(self):
        code = _code_clean(FACE_EVENTS_MODEL)
        assert "image_bytes" not in code
        assert "crop_bytes" not in code

    def test_has_embedding(self):
        code = _code_clean(FACE_EVENTS_MODEL)
        assert "embedding" in code

    def test_has_embedding_dim(self):
        code = _code_clean(FACE_EVENTS_MODEL)
        assert "embedding_dim" in code

    def test_has_embedding_norm(self):
        code = _code_clean(FACE_EVENTS_MODEL)
        assert "embedding_norm" in code

    def test_has_detector_model(self):
        code = _code_clean(FACE_EVENTS_MODEL)
        assert "detector_model" in code

    def test_has_embedding_model(self):
        code = _code_clean(FACE_EVENTS_MODEL)
        assert "embedding_model" in code
