"""Static config tests for F1.2 YOLOv8-Face module.yml (no GPU)."""

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_YML = REPO_ROOT / "modules" / "savant_security" / "module.yml"
COMPOSE_FILE = REPO_ROOT / "infra" / "docker-compose.c1-official-adapter.yml"


def _load_module_yml():
    with open(MODULE_YML) as f:
        return yaml.safe_load(f)


def _load_compose():
    with open(COMPOSE_FILE) as f:
        return yaml.safe_load(f)


def _find_element(config, name):
    for el in config.get("pipeline", {}).get("elements", []):
        if el.get("name") == name:
            return el
    return None


class TestYOLOV8FaceBlockExists:
    def test_yolov8_face_block_present(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "yolov8_face")
        assert el is not None, "yolov8_face element not found in module.yml"

    def test_yolov8_face_is_complex_model(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "yolov8_face")
        assert el is not None
        assert el["element"] == "nvinfer@complex_model"

    def test_yolov8_face_is_full_frame_no_input_object(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "yolov8_face")
        assert el is not None
        model_cfg = el.get("model", {})
        # "model.input" (shape/format) is fine — every nvinfer has it.
        # "model.input.object" is the secondary-model pattern that makes
        # inference depend on another model's output objects.  YOLOv8-Face
        # is full-frame, so this must be absent.
        input_block = model_cfg.get("input", {})
        assert "object" not in input_block, (
            "YOLOv8-Face must be full-frame (model.input.object found)"
        )

    def test_yolov8_face_output_layer_is_output0(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "yolov8_face")
        assert el is not None
        layer_names = el["model"]["output"]["layer_names"]
        assert "output0" in layer_names

    def test_yolov8_face_uses_official_converter(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "yolov8_face")
        assert el is not None
        converter = el["model"]["output"]["converter"]
        assert converter["module"] == "savant.converter.yolo_v8face"
        assert converter["class_name"] == "YoloV8faceConverter"

    def test_yolov8_face_has_landmarks_attribute(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "yolov8_face")
        assert el is not None
        attrs = el["model"]["output"]["attributes"]
        names = [a["name"] for a in attrs]
        assert "landmarks" in names

    def test_yolov8_face_object_label_is_face(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "yolov8_face")
        assert el is not None
        objs = el["model"]["output"]["objects"]
        assert len(objs) == 1
        assert objs[0]["label"] == "face"


class TestFaceBatchPolicy:
    def test_face_detector_batch_default_is_1(self):
        cfg = _load_module_yml()
        params = cfg.get("parameters", {})
        assert "face_detector_batch_size" in params
        batch_param = params["face_detector_batch_size"]
        default = _extract_default(batch_param)
        assert int(default) == 1, f"Default batch must be 1, got {default}"

    def test_compose_has_face_detector_batch_size_1(self):
        cfg = _load_compose()
        savant_cfg = cfg.get("services", {}).get("savant-security", {})
        env = savant_cfg.get("environment", {})
        face_batch = None
        if isinstance(env, dict):
            face_batch = env.get("FACE_DETECTOR_BATCH_SIZE")
        else:
            for item in env:
                if isinstance(item, str) and "FACE_DETECTOR_BATCH_SIZE" in item:
                    parts = item.split("=", 1)
                    if len(parts) == 2:
                        face_batch = parts[1].strip('"')
                        break
        assert face_batch == "1", f"FACE_DETECTOR_BATCH_SIZE must be '1', got {face_batch}"


class TestFaceDebugPyFunc:
    def test_face_debug_pyfunc_present(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "face_debug")
        assert el is not None, "face_debug pyfunc not found"

    def test_face_debug_placed_after_yolov8_face(self):
        cfg = _load_module_yml()
        elements = cfg.get("pipeline", {}).get("elements", [])
        names = [e["name"] for e in elements]
        yolo_idx = names.index("yolov8_face")
        debug_idx = names.index("face_debug")
        assert debug_idx > yolo_idx, "face_debug must be after yolov8_face"

    def test_face_debug_is_pyfunc(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "face_debug")
        assert el is not None
        assert el["element"] == "pyfunc"

    def test_face_debug_module_exists(self):
        pyfunc_path = (
            REPO_ROOT
            / "modules"
            / "savant_security"
            / "custom"
            / "pyfuncs"
            / "face_debug.py"
        )
        assert pyfunc_path.exists(), f"Missing {pyfunc_path}"


class TestExistingC12NotBroken:
    def test_yolo26_pose_still_present(self):
        cfg = _load_module_yml()
        assert _find_element(cfg, "yolo26_pose") is not None

    def test_tracker_still_present(self):
        cfg = _load_module_yml()
        assert _find_element(cfg, "tracker") is not None

    def test_behavior_rules_still_present(self):
        cfg = _load_module_yml()
        assert _find_element(cfg, "behavior_rules") is not None


def _extract_default(param_str):
    """Extract default value from Savant env param like ${oc.env:NAME, 1}."""
    if hasattr(param_str, "get"):
        return param_str.get("default", 1)
    s = str(param_str)
    if ", " in s:
        return s.rsplit(", ", 1)[1].rstrip("}")
    return s
