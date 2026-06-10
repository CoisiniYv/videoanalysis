"""Static config tests for F2.1 AdaFace attribute_model in module.yml (no GPU)."""

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


def _extract_default(param_str):
    """Extract default value from Savant env param like ${oc.env:NAME, 1}."""
    if hasattr(param_str, "get"):
        return param_str.get("default", 1)
    s = str(param_str)
    if ", " in s:
        return s.rsplit(", ", 1)[1].rstrip("}")
    return s


class TestAdaFaceBlockExists:
    def test_adaface_block_present(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None, "adaface element not found in module.yml"

    def test_adaface_is_attribute_model(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        assert el["element"] == "nvinfer@attribute_model"


class TestAdaFaceModelConfig:
    def test_model_file(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        model_file = el["model"]["model_file"]
        assert model_file == "/models/adaface/adaface_ir50_webface4m.onnx"

    def test_format_onnx(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        assert el["model"]["format"] == "onnx"

    def test_input_object_is_yolov8_face(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        input_block = el["model"]["input"]
        assert input_block["object"] == "yolov8_face.face"

    def test_input_shape(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        shape = el["model"]["input"]["shape"]
        assert shape == [3, 112, 112]

    def test_offsets(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        offsets = el["model"]["input"]["offsets"]
        assert offsets == [127.5, 127.5, 127.5]

    def test_scale_factor(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        scale = el["model"]["input"]["scale_factor"]
        assert abs(scale - 0.007843137254902) < 1e-10

    def test_color_format_bgr(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        assert el["model"]["input"]["color_format"] == "bgr"

    def test_preprocess_object_image(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        preproc = el["model"]["input"]["preprocess_object_image"]
        assert preproc["module"] == "savant.input_preproc.align_face"
        assert preproc["class_name"] == "AlignFacePreprocessingObjectImageGPU"


class TestAdaFaceOutputConfig:
    def test_output_layer_names(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        layer_names = el["model"]["output"]["layer_names"]
        assert layer_names == ["feature"]

    def test_converter(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        converter = el["model"]["output"]["converter"]
        assert converter["module"] == "savant.converter"
        assert converter["class_name"] == "TensorToVectorConverter"

    def test_output_attributes(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        attrs = el["model"]["output"]["attributes"]
        names = [a["name"] for a in attrs]
        assert "feature" in names

    def test_only_feature_layer(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "adaface")
        assert el is not None
        layer_names = el["model"]["output"]["layer_names"]
        assert "norm" not in layer_names, "norm layer should not be configured yet"


class TestAdaFaceBatchPolicy:
    def test_embedding_batch_default_is_16(self):
        cfg = _load_module_yml()
        params = cfg.get("parameters", {})
        assert "face_embedding_batch_size" in params
        batch_param = params["face_embedding_batch_size"]
        default = _extract_default(batch_param)
        assert int(default) == 16, f"Default embedding batch must be 16, got {default}"

    def test_compose_has_face_embedding_batch_size(self):
        cfg = _load_compose()
        savant_cfg = cfg.get("services", {}).get("savant-security", {})
        env = savant_cfg.get("environment", {})
        assert env.get("FACE_EMBEDDING_BATCH_SIZE") == "16"

    def test_detector_batch_still_1(self):
        cfg = _load_compose()
        savant_cfg = cfg.get("services", {}).get("savant-security", {})
        env = savant_cfg.get("environment", {})
        assert env.get("FACE_DETECTOR_BATCH_SIZE") == "1"


class TestPipelineOrder:
    def test_adaface_after_face_person_associator(self):
        cfg = _load_module_yml()
        elements = cfg.get("pipeline", {}).get("elements", [])
        names = [e["name"] for e in elements]
        assoc_idx = names.index("face_person_associator")
        adaface_idx = names.index("adaface")
        assert adaface_idx > assoc_idx, "adaface must be after face_person_associator"

    def test_face_embedding_debug_after_adaface(self):
        cfg = _load_module_yml()
        elements = cfg.get("pipeline", {}).get("elements", [])
        names = [e["name"] for e in elements]
        adaface_idx = names.index("adaface")
        debug_idx = names.index("face_embedding_debug")
        assert debug_idx > adaface_idx, "face_embedding_debug must be after adaface"


class TestFaceEmbeddingDebugPyFunc:
    def test_pyfunc_present(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "face_embedding_debug")
        assert el is not None, "face_embedding_debug pyfunc not found"

    def test_pyfunc_module(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "face_embedding_debug")
        assert el is not None
        assert el["element"] == "pyfunc"
        assert el["module"] == "custom.pyfuncs.face_embedding_debug"
        assert el["class_name"] == "FaceEmbeddingDebugPyFunc"

    def test_pyfunc_file_exists(self):
        pyfunc_path = (
            REPO_ROOT
            / "modules"
            / "savant_security"
            / "custom"
            / "pyfuncs"
            / "face_embedding_debug.py"
        )
        assert pyfunc_path.exists(), f"Missing {pyfunc_path}"


class TestNoForbiddenElements:
    def test_no_hnswlib(self):
        cfg = _load_module_yml()
        cfg_str = str(cfg)
        assert "hnswlib" not in cfg_str.lower()

    def test_no_recognition_pyfunc(self):
        cfg = _load_module_yml()
        el = _find_element(cfg, "recognition")
        assert el is None, "Recognition pyfunc must not be present"

    def test_no_qdrant(self):
        cfg = _load_module_yml()
        cfg_str = str(cfg)
        assert "qdrant" not in cfg_str.lower()

    def test_no_redis_face_observations(self):
        cfg = _load_module_yml()
        cfg_str = str(cfg)
        assert "face_observations" not in cfg_str


class TestExistingElementsNotBroken:
    def test_yolo26_pose_still_present(self):
        cfg = _load_module_yml()
        assert _find_element(cfg, "yolo26_pose") is not None

    def test_tracker_still_present(self):
        cfg = _load_module_yml()
        assert _find_element(cfg, "tracker") is not None

    def test_behavior_rules_still_present(self):
        cfg = _load_module_yml()
        assert _find_element(cfg, "behavior_rules") is not None

    def test_yolov8_face_still_present(self):
        cfg = _load_module_yml()
        assert _find_element(cfg, "yolov8_face") is not None

    def test_face_person_associator_still_present(self):
        cfg = _load_module_yml()
        assert _find_element(cfg, "face_person_associator") is not None

    def test_face_debug_still_present(self):
        cfg = _load_module_yml()
        assert _find_element(cfg, "face_debug") is not None
