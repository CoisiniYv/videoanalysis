"""C1F.2c — AdaFace runtime validation contract tests."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MODULE_YAML = REPO / "modules" / "savant_security" / "module.c1f2c_adaface_runtime.yml"
DEBUG_PYFUNC = (
    REPO / "modules" / "savant_security" / "custom" / "pyfuncs" / "adaface_runtime_debug.py"
)
SMOKE_SCRIPT = REPO / "scripts" / "smoke" / "check_c1f2c_adaface_runtime_smoke.sh"
DOC = REPO / "docs" / "c1f2c_adaface_runtime_enablement.md"
COMPOSE_C1E = REPO / "infra" / "docker-compose.c1-official-replay-dev.yml"


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
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), (ast.Constant, ast.Str)):
                node.body = node.body[1:]
    return ast.unparse(tree).lower()


# ---------------------------------------------------------------------------
# 1. Module exists
# ---------------------------------------------------------------------------

class TestModuleExists:
    def test_file_exists(self):
        assert MODULE_YAML.exists()

    def test_valid_yaml(self):
        _load_module()  # will raise if invalid


# ---------------------------------------------------------------------------
# 2. YOLOv8-Face full-frame primary
# ---------------------------------------------------------------------------

class TestFaceDetector:
    def test_yolov8_face_exists(self):
        assert _find_by_name(_load_module(), "yolov8_face") is not None

    def test_full_frame_primary(self):
        elem = _find_by_name(_load_module(), "yolov8_face")
        assert "object" not in elem.get("input", {})

    def test_label_face(self):
        elem = _find_by_name(_load_module(), "yolov8_face")
        objects = elem.get("model", {}).get("output", {}).get("objects", [])
        labels = [o.get("label") for o in objects if isinstance(o, dict)]
        assert "face" in labels


# ---------------------------------------------------------------------------
# 3. AdaFace element
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
# 4. Module exclusions
# ---------------------------------------------------------------------------

class TestModuleExclusions:
    def test_no_behavior_rules(self):
        assert _find_by_name(_load_module(), "behavior_rules") is None

    def test_no_same_frame_debug(self):
        assert _find_by_name(_load_module(), "same_frame_detection_debug") is None

    def test_no_face_reid_gate(self):
        assert _find_by_name(_load_module(), "face_reid_gate") is None

    def test_no_face_observation_exporter(self):
        assert _find_by_name(_load_module(), "face_observation_exporter") is None


# ---------------------------------------------------------------------------
# 5. Debug pyfunc
# ---------------------------------------------------------------------------

class TestDebugPyfunc:
    def test_file_exists(self):
        assert DEBUG_PYFUNC.exists()

    def test_no_on_start_file_io(self):
        code = DEBUG_PYFUNC.read_text()
        lines = code.split("\n")
        in_on_start = False
        for line in lines:
            if "def on_start" in line:
                in_on_start = True
            elif in_on_start and line.strip().startswith("def "):
                in_on_start = False
            if in_on_start:
                assert "mkdir" not in line
                assert ".open(" not in line

    def test_lazy_init_pattern(self):
        code = DEBUG_PYFUNC.read_text()
        assert "_ensure_file" in code or "_file_initialized" in code

    def test_writes_to_c1f2c(self):
        code = DEBUG_PYFUNC.read_text()
        assert "c1f2c" in code

    def test_no_image_output(self):
        code = _code_clean(DEBUG_PYFUNC)
        for token in ["cv2.imwrite", ".save(", "base64", "jpeg", "png"]:
            assert token not in code

    def test_no_redis(self):
        code = _code_clean(DEBUG_PYFUNC)
        assert "xadd" not in code
        assert "redis" not in code

    def test_reads_embedding(self):
        code = DEBUG_PYFUNC.read_text()
        assert "feature" in code or "embedding" in code

    def test_checks_dim_512(self):
        code = DEBUG_PYFUNC.read_text()
        assert "512" in code

    def test_checks_norm(self):
        code = DEBUG_PYFUNC.read_text()
        assert "norm" in code

    def test_class_name(self):
        code = DEBUG_PYFUNC.read_text()
        assert "AdaFaceRuntimeDebugPyFunc" in code


# ---------------------------------------------------------------------------
# 6. Smoke script
# ---------------------------------------------------------------------------

class TestSmokeScript:
    def test_exists(self):
        assert SMOKE_SCRIPT.exists()

    def test_uses_correct_module(self):
        content = SMOKE_SCRIPT.read_text()
        assert "module.c1f2c_adaface_runtime.yml" in content

    def test_fixed_rtsp(self):
        content = SMOKE_SCRIPT.read_text()
        assert "10.37.57.112" in content
        assert "1080movie" in content

    def test_no_source_extraction(self):
        content = SMOKE_SCRIPT.read_text().lower()
        assert "source_extraction" not in content

    def test_no_second_rtsp(self):
        content = SMOKE_SCRIPT.read_text()
        # Only one RTSP URI reference
        assert content.count("rtsp://") <= 2  # the variable + the check


# ---------------------------------------------------------------------------
# 7. Doc
# ---------------------------------------------------------------------------

class TestDoc:
    def test_exists(self):
        assert DOC.exists()

    def test_has_pass_status(self):
        content = DOC.read_text()
        assert "PASS" in content

    def test_mentions_c1f2b(self):
        content = DOC.read_text()
        assert "C1F.2b" in content
