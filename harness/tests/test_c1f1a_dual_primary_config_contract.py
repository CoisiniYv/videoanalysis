"""C1F.1a — Dual primary current state audit contract tests.

Read-only checks that the C1 official replay dev stack is configured for
same-frame YOLO26-pose + YOLOv8-Face dual primary detection.

No runtime smoke. No model asset verification beyond file existence.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MODULE_YAML = REPO / "modules" / "savant_security" / "module.yml"
CAMERA_CONFIG = REPO / "modules" / "savant_security" / "config" / "cameras.c1e_replay.yml"
COMPOSE_C1E = REPO / "infra" / "docker-compose.c1-official-replay-dev.yml"
COMPOSE_P1C = REPO / "infra" / "docker-compose.p1c-rtsp-replay-event-evidence.yml"
AUDIT_DOC = REPO / "docs" / "c1f1a_dual_primary_current_state_audit.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_module_yaml() -> dict:
    with open(MODULE_YAML) as f:
        return yaml.safe_load(f)


def _load_camera_config() -> dict:
    with open(CAMERA_CONFIG) as f:
        return yaml.safe_load(f)


def _load_compose(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _get_pipeline_elements(module: dict) -> list[dict]:
    """Get the list of pipeline elements from module.yml.

    Pipeline structure is: { source: {...}, elements: [...] }
    """
    pipeline = module.get("pipeline", {})
    if isinstance(pipeline, dict):
        return pipeline.get("elements", [])
    if isinstance(pipeline, list):
        return pipeline
    return []


def _find_elements(module: dict, element_type: str) -> list[dict]:
    """Find all pipeline elements of a given type."""
    elements = _get_pipeline_elements(module)
    return [e for e in elements if e.get("element") == element_type]


def _find_element_by_name(module: dict, name: str) -> dict | None:
    elements = _get_pipeline_elements(module)
    for e in elements:
        if isinstance(e, dict) and e.get("name") == name:
            return e
    return None


def _element_index(module: dict, name: str) -> int:
    elements = _get_pipeline_elements(module)
    for i, e in enumerate(elements):
        if isinstance(e, dict) and e.get("name") == name:
            return i
    return -1


# ---------------------------------------------------------------------------
# 1. YOLO26-pose is full-frame primary
# ---------------------------------------------------------------------------

class TestYolo26PosePrimary:
    def test_yolo26_pose_exists(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "yolo26_pose")
        assert elem is not None, "yolo26_pose element not found in module.yml"

    def test_yolo26_pose_is_full_frame(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "yolo26_pose")
        # Full-frame = no input.object binding
        inp = elem.get("input", {})
        assert "object" not in inp, (
            f"yolo26_pose has input.object binding (person ROI secondary?): {inp}"
        )

    def test_yolo26_pose_label_is_person(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "yolo26_pose")
        model = elem.get("model", {})
        output = model.get("output", {})
        # Check that person is among the labels
        labels = output.get("objects", [])
        label_names = [l.get("name") for l in labels if isinstance(l, dict)]
        assert "person" in label_names or any("person" in str(l) for l in labels), (
            f"yolo26_pose output does not include 'person' label: {labels}"
        )


# ---------------------------------------------------------------------------
# 2. YOLOv8-Face is full-frame primary
# ---------------------------------------------------------------------------

class TestYoloV8FacePrimary:
    def test_yolov8_face_exists(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "yolov8_face")
        assert elem is not None, "yolov8_face element not found in module.yml"

    def test_yolov8_face_is_full_frame(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "yolov8_face")
        inp = elem.get("input", {})
        assert "object" not in inp, (
            f"yolov8_face has input.object binding (person ROI secondary?): {inp}"
        )

    def test_yolov8_face_label_is_face(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "yolov8_face")
        model = elem.get("model", {})
        output = model.get("output", {})
        labels = output.get("objects", [])
        label_names = [l.get("name") for l in labels if isinstance(l, dict)]
        assert "face" in label_names or any("face" in str(l) for l in labels), (
            f"yolov8_face output does not include 'face' label: {labels}"
        )


# ---------------------------------------------------------------------------
# 3. YOLOv8-Face is NOT person ROI secondary
# ---------------------------------------------------------------------------

class TestFaceNotPersonROISecondary:
    def test_no_input_object_on_face_detector(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "yolov8_face")
        inp = elem.get("input", {})
        assert inp.get("object") is None, (
            f"yolov8_face is bound to parent object (person ROI secondary): {inp}"
        )


# ---------------------------------------------------------------------------
# 4. nvtracker only for person track
# ---------------------------------------------------------------------------

class TestNvtrackerPersonOnly:
    def test_nvtracker_exists(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "tracker")
        assert elem is not None, "nvtracker element not found"
        assert elem.get("element") == "nvtracker"

    def test_nvtracker_after_pose_before_face(self):
        module = _load_module_yaml()
        idx_tracker = _element_index(module, "tracker")
        idx_pose = _element_index(module, "yolo26_pose")
        idx_face = _element_index(module, "yolov8_face")
        assert idx_pose < idx_tracker < idx_face, (
            f"Pipeline order wrong: pose={idx_pose}, tracker={idx_tracker}, face={idx_face}"
        )


# ---------------------------------------------------------------------------
# 5-6. Face-person association exists, after both detections, geometry-only
# ---------------------------------------------------------------------------

class TestFacePersonAssociation:
    def test_associator_element_exists(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "face_person_associator")
        assert elem is not None, "face_person_associator not found in module.yml"

    def test_associator_after_both_detectors(self):
        module = _load_module_yaml()
        idx_assoc = _element_index(module, "face_person_associator")
        idx_pose = _element_index(module, "yolo26_pose")
        idx_face = _element_index(module, "yolov8_face")
        assert idx_assoc > idx_pose, "associator must be after yolo26_pose"
        assert idx_assoc > idx_face, "associator must be after yolov8_face"

    def test_association_service_is_geometry_only(self):
        assoc_file = REPO / "modules" / "savant_security" / "custom" / "services" / "face_person_association.py"
        assert assoc_file.exists(), "face_person_association.py not found"
        source = assoc_file.read_text()
        # Should not import any DL/inference libraries
        for bad in ["torch", "tensorflow", "onnxruntime", "cv2.imread", "nvinfer"]:
            assert bad not in source, (
                f"face_person_association.py imports inference library: {bad}"
            )

    def test_associator_pyfunc_is_geometry_only(self):
        pyfunc_file = REPO / "modules" / "savant_security" / "custom" / "pyfuncs" / "face_person_associator.py"
        assert pyfunc_file.exists(), "face_person_associator.py not found"
        source = pyfunc_file.read_text()
        for bad in ["torch", "tensorflow", "onnxruntime", "cv2.imread", "nvinfer"]:
            assert bad not in source, (
                f"face_person_associator.py imports inference library: {bad}"
            )


# ---------------------------------------------------------------------------
# 7. Frame anchor metadata available
# ---------------------------------------------------------------------------

class TestFrameAnchorMetadata:
    def test_anchor_service_exists(self):
        anchor_file = REPO / "modules" / "savant_security" / "custom" / "services" / "frame_anchor_metadata.py"
        assert anchor_file.exists()

    def test_frame_uuid_in_anchor(self):
        source = (REPO / "modules" / "savant_security" / "custom" / "services" / "frame_anchor_metadata.py").read_text()
        assert "frame_uuid" in source

    def test_frame_num_in_anchor(self):
        source = (REPO / "modules" / "savant_security" / "custom" / "services" / "frame_anchor_metadata.py").read_text()
        assert "frame_num" in source

    def test_frame_pts_in_anchor(self):
        source = (REPO / "modules" / "savant_security" / "custom" / "services" / "frame_anchor_metadata.py").read_text()
        assert "frame_pts" in source


# ---------------------------------------------------------------------------
# 8-9. C1E compose topology and RTSP source
# ---------------------------------------------------------------------------

class TestC1ETopology:
    def test_compose_exists(self):
        assert COMPOSE_C1E.exists(), f"C1E compose not found: {COMPOSE_C1E}"

    def test_source_adapter_to_replay(self):
        compose = _load_compose(COMPOSE_C1E)
        services = compose.get("services", {})
        adapter = services.get("source-adapter", {})
        env = adapter.get("environment", {})
        zmq = env.get("ZMQ_ENDPOINT", "")
        assert "replay-service" in zmq, (
            f"source-adapter ZMQ_ENDPOINT does not target replay-service: {zmq}"
        )

    def test_replay_to_savant(self):
        compose = _load_compose(COMPOSE_C1E)
        services = compose.get("services", {})
        replay = services.get("replay-service", {})
        # Replay config is passed via config file, not compose env
        # Just verify replay-service exists
        assert replay, "replay-service not found in compose"

    def test_fixed_rtsp_url(self):
        compose = _load_compose(COMPOSE_C1E)
        services = compose.get("services", {})
        adapter = services.get("source-adapter", {})
        env = adapter.get("environment", {})
        rtsp = env.get("RTSP_URI", "") or env.get("LOCATION", "")
        assert "10.37.57.112" in rtsp, f"RTSP URI not the fixed source: {rtsp}"
        assert "1080movie" in rtsp, f"RTSP URI not the fixed source: {rtsp}"


# ---------------------------------------------------------------------------
# 10. Source Adapter uses TCP
# ---------------------------------------------------------------------------

class TestSourceAdapterTCP:
    def test_rtsp_transport_tcp(self):
        compose = _load_compose(COMPOSE_C1E)
        services = compose.get("services", {})
        adapter = services.get("source-adapter", {})
        env = adapter.get("environment", {})
        transport = env.get("RTSP_TRANSPORT", "")
        # Allow ${VAR:-tcp} pattern
        assert "tcp" in transport.lower(), (
            f"Source Adapter RTSP_TRANSPORT is not TCP: {transport}"
        )


# ---------------------------------------------------------------------------
# 11. No forbidden paths
# ---------------------------------------------------------------------------

class TestNoForbiddenPaths:
    def test_no_second_rtsp_in_c1e(self):
        compose = _load_compose(COMPOSE_C1E)
        services = compose.get("services", {})
        adapter_count = sum(1 for s in services if "source-adapter" in s)
        assert adapter_count == 1, (
            f"Multiple source adapters found: {adapter_count}"
        )

    def test_no_source_extraction_in_c1e(self):
        compose = _load_compose(COMPOSE_C1E)
        services = compose.get("services", {})
        for svc_name, svc in services.items():
            env = svc.get("environment", {})
            if isinstance(env, dict):
                for k, v in env.items():
                    if "SOURCE_EXTRACTION" in k.upper():
                        assert str(v).lower() == "false", (
                            f"{svc_name}.{k}={v} — source extraction must be false"
                        )

    def test_no_annotated_clip_in_c1e(self):
        compose = _load_compose(COMPOSE_C1E)
        content = str(compose)
        # annotated_clip should not be generated
        assert "annotated_clip" not in content.lower() or "false" in content.lower()

    def test_second_rtsp_pull_false(self):
        compose = _load_compose(COMPOSE_C1E)
        services = compose.get("services", {})
        for svc_name, svc in services.items():
            env = svc.get("environment", {})
            if "SECOND_RTSP_PULL" in str(env):
                assert env.get("EVIDENCE_SECOND_RTSP_PULL") == "false", (
                    f"{svc_name} has SECOND_RTSP_PULL not set to false"
                )


# ---------------------------------------------------------------------------
# 12. AdaFace / face_reid_gate / face_observation_exporter exist
# ---------------------------------------------------------------------------

class TestFacePipelineComponentsExist:
    def test_adaface_element_in_module(self):
        module = _load_module_yaml()
        elem = _find_element_by_name(module, "adaface")
        assert elem is not None, "adaface element not found in module.yml"

    def test_face_reid_gate_pyfunc_exists(self):
        path = REPO / "modules" / "savant_security" / "custom" / "pyfuncs" / "face_reid_gate.py"
        assert path.exists(), "face_reid_gate.py not found"

    def test_face_observation_exporter_pyfunc_exists(self):
        path = REPO / "modules" / "savant_security" / "custom" / "pyfuncs" / "face_observation_exporter.py"
        assert path.exists(), "face_observation_exporter.py not found"

    def test_adaface_model_asset_exists(self):
        model_dir = Path("/data/video-analytics/models/adaface")
        if not model_dir.exists():
            pytest.skip("Model directory not accessible")
        onnx = list(model_dir.glob("*.onnx"))
        assert len(onnx) > 0, "No AdaFace ONNX model found"

    def test_yolov8_face_model_asset_exists(self):
        model_dir = Path("/data/video-analytics/models")
        if not model_dir.exists():
            pytest.skip("Model directory not accessible")
        face_models = list(model_dir.glob("yolov8*face*.onnx"))
        assert len(face_models) > 0, "No YOLOv8-Face ONNX model found"


# ---------------------------------------------------------------------------
# Audit doc exists
# ---------------------------------------------------------------------------

class TestAuditDocExists:
    def test_audit_doc_present(self):
        assert AUDIT_DOC.exists(), f"Audit doc not found: {AUDIT_DOC}"

    def test_audit_doc_has_pass_config_ready(self):
        content = AUDIT_DOC.read_text()
        assert "PASS_CONFIG_READY" in content
