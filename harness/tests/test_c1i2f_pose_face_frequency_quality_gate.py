"""C1I.2f pose/face frequency and face quality gate calibration."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = ROOT / "modules" / "savant_security"
MODULE_YML = MODULE_DIR / "module.yml"
ENV_FILE = ROOT / "infra" / "env" / "c1-official-replay-dev.env"
COMPOSE = ROOT / "infra" / "docker-compose.c1-official-replay-dev.yml"
BEHAVIOR_RULES = MODULE_DIR / "custom" / "pyfuncs" / "behavior_rules.py"
FACE_REID_GATE = MODULE_DIR / "custom" / "pyfuncs" / "face_reid_gate.py"
FACE_OBS_EXPORTER = MODULE_DIR / "custom" / "pyfuncs" / "face_observation_exporter.py"
SMOKE = ROOT / "scripts" / "smoke" / "current" / "check_c1i2f_pose_face_frequency_quality_gate.sh"


def _activate_module() -> None:
    for name in list(sys.modules):
        if name == "custom" or name.startswith("custom."):
            del sys.modules[name]
    module_path = str(MODULE_DIR)
    if module_path in sys.path:
        sys.path.remove(module_path)
    sys.path.insert(0, module_path)


def _load_module() -> dict[str, Any]:
    return yaml.safe_load(MODULE_YML.read_text(encoding="utf-8"))


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def _find_element(name: str) -> dict[str, Any]:
    for element in _load_module().get("pipeline", {}).get("elements", []):
        if element.get("name") == name:
            return element
    raise AssertionError(f"missing module element: {name}")


def _default_value(expr: Any) -> str:
    text = str(expr)
    if ", " in text:
        return text.rsplit(", ", 1)[1].rstrip("}")
    return text


def _gate_input(**overrides: Any):
    _activate_module()
    from custom.services.face_reid_gate import ReIDGateInput

    data = {
        "face_confidence": 0.80,
        "face_width": 64.0,
        "face_height": 64.0,
        "landmarks": [1.0] * 10,
        "person_track_id": 7,
        "has_track_id": True,
        "feature": [1.0 / math.sqrt(512)] * 512,
        "feature_dim": 512,
        "embedding_norm": 1.0,
        "camera_id": "cam_c1e_rtsp_replay",
        "source_id": "c1e_rtsp_replay",
        "timestamp_ms": 1_000,
    }
    data.update(overrides)
    return ReIDGateInput(**data)


def test_person_bbox_observation_interval_is_333ms() -> None:
    env = _load_env()
    assert env["PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS"] == "333"
    assert env["PERSON_OBSERVATION_MIN_INTERVAL_MS"] == "333"
    assert env["PERSON_OBSERVATION_BATCH_SIZE"] == "100"

    code = BEHAVIOR_RULES.read_text(encoding="utf-8")
    assert "PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS" in code
    assert "_person_bbox_observation_min_interval_ms" in code


def test_face_reid_min_interval_remains_1000ms() -> None:
    env = _load_env()
    module_text = MODULE_YML.read_text(encoding="utf-8")
    assert env["FACE_REID_MIN_INTERVAL_MS"] == "1000"
    assert "FACE_REID_MIN_INTERVAL_MS, 1000" in module_text


def test_face_detector_threshold_uses_actual_face_confidence_env_at_025() -> None:
    env = _load_env()
    module = _load_module()
    yolo = _find_element("yolov8_face")
    converter_kwargs = yolo["model"]["output"]["converter"]["kwargs"]

    assert env["FACE_CONFIDENCE_THRESHOLD"] == "0.25"
    assert "FACE_DETECT_CONFIDENCE_THRESHOLD" not in env
    assert float(_default_value(module["parameters"]["face_confidence_threshold"])) == 0.25
    assert converter_kwargs["confidence_threshold"] == "${parameters.face_confidence_threshold}"


def test_face_reid_min_confidence_is_separate_and_not_below_035() -> None:
    env = _load_env()
    gate_kwargs = _find_element("face_reid_gate")["kwargs"]
    min_conf = float(env["FACE_REID_MIN_CONFIDENCE"])
    assert min_conf == 0.45
    assert min_conf >= 0.35
    assert float(_default_value(gate_kwargs["face_reid_min_confidence"])) == 0.45
    assert gate_kwargs["face_detector_confidence_threshold"] == "${parameters.face_confidence_threshold}"


def test_low_confidence_face_is_rejected_before_embedding_export() -> None:
    _activate_module()
    from custom.services.face_reid_gate import evaluate_reid_gate

    result = evaluate_reid_gate(
        _gate_input(face_confidence=0.30, feature=None, feature_dim=0, embedding_norm=0.0),
        {"face_reid_min_confidence": 0.45},
    )
    assert result.allowed is False
    assert result.skip_reason == "low_confidence"


def test_too_small_face_is_rejected_before_embedding_export() -> None:
    _activate_module()
    from custom.services.face_reid_gate import evaluate_reid_gate

    result = evaluate_reid_gate(
        _gate_input(face_width=32.0, face_height=64.0, feature=None, feature_dim=0),
        {"face_reid_min_face_size": 40.0},
    )
    assert result.allowed is False
    assert result.skip_reason == "face_too_small"


def test_missing_landmarks_face_is_rejected_before_embedding_export() -> None:
    _activate_module()
    from custom.services.face_reid_gate import evaluate_reid_gate

    result = evaluate_reid_gate(
        _gate_input(landmarks=None, feature=None, feature_dim=0, embedding_norm=0.0),
        {"face_reid_min_confidence": 0.45, "face_reid_min_face_size": 40.0},
    )
    assert result.allowed is False
    assert result.skip_reason == "no_landmarks"


def test_person_bbox_observation_payload_has_no_keypoints_embedding_or_image_bytes() -> None:
    _activate_module()
    from custom.models.person_events import PersonBBoxObservationEventDraft

    draft = PersonBBoxObservationEventDraft(
        source_observation_id="person:c1e:7:1000:0",
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        track_id="7",
        timestamp_ms=1000,
        frame_num=30,
        person_bbox=[10.0, 20.0, 110.0, 220.0],
        person_confidence=0.82,
        gate_status="accepted",
        payload={"media": {"frame_uuid": "frame-1"}},
    )
    payload_text = json.dumps(draft.to_dict(), sort_keys=True).lower()
    assert "keypoint" not in payload_text
    assert "embedding" not in payload_text
    assert "image" not in payload_text
    assert "jpeg" not in payload_text
    assert "png" not in payload_text


def test_face_observation_contains_512_dim_embedding_and_norm() -> None:
    _activate_module()
    from custom.models.face_events import FaceObservationEventDraft

    embedding = [1.0 / math.sqrt(512)] * 512
    draft = FaceObservationEventDraft(
        source_observation_id="face:c1e:7:1000",
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        track_id=7,
        timestamp_ms=1000,
        face_bbox=[20.0, 30.0, 60.0, 60.0],
        landmarks=[1.0] * 10,
        face_confidence=0.80,
        embedding=embedding,
        embedding_dim=512,
        embedding_norm=math.sqrt(sum(value * value for value in embedding)),
        reid_allowed=True,
    )
    data = draft.to_dict()
    assert data["embedding_dim"] == 512
    assert abs(float(data["embedding_norm"]) - 1.0) < 1e-6


def test_person_bbox_observation_rate_is_configured_above_face_observation_rate() -> None:
    env = _load_env()
    person_interval = int(env["PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS"])
    face_interval = int(env["FACE_REID_MIN_INTERVAL_MS"])
    person_rate = 1000.0 / person_interval
    face_rate = 1000.0 / face_interval
    assert person_interval == 333
    assert face_interval == 1000
    assert person_rate > face_rate


def test_all_faces_are_iterated_not_first_face_only() -> None:
    gate_code = FACE_REID_GATE.read_text(encoding="utf-8")
    exporter_code = FACE_OBS_EXPORTER.read_text(encoding="utf-8")
    assert "for i, obj in enumerate(face_objects):" in gate_code
    assert "for i, obj in enumerate(face_objects):" in exporter_code
    assert "face_objects[:1]" not in gate_code
    assert "face_objects[:1]" not in exporter_code


def test_face_reid_gate_exposes_lightweight_quality_counters() -> None:
    code = FACE_REID_GATE.read_text(encoding="utf-8")
    for token in (
        "raw_face_detections",
        "detector_confidence_passed",
        "detector_confidence_rejected",
        "reid_rejected_by_confidence",
        "reid_rejected_by_size",
        "reid_rejected_by_landmarks",
        "reid_rejected_by_embedding_norm",
        "face_reid_gate_summary",
    ):
        assert token in code


def test_compose_passes_only_actual_calibration_env_names() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    env = compose["services"]["savant-security"]["environment"]
    event_env = compose["services"]["event-worker"]["environment"]
    assert env["PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS"] == "${PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS:-333}"
    assert event_env["PERSON_OBSERVATION_BATCH_SIZE"] == "${PERSON_OBSERVATION_BATCH_SIZE:-100}"
    assert env["FACE_CONFIDENCE_THRESHOLD"] == "${FACE_CONFIDENCE_THRESHOLD:-0.25}"
    assert env["FACE_REID_MIN_CONFIDENCE"] == "${FACE_REID_MIN_CONFIDENCE:-0.45}"
    assert env["FACE_REID_MIN_INTERVAL_MS"] == "${FACE_REID_MIN_INTERVAL_MS:-1000}"
    assert "FACE_DETECT_CONFIDENCE_THRESHOLD" not in env


def test_smoke_contract_exists_and_writes_c1i2f_summary() -> None:
    text = SMOKE.read_text(encoding="utf-8")
    assert "PASS_C1I2F_POSE_FACE_FREQUENCY_QUALITY_GATE" in text
    assert "pose_face_frequency_quality_gate_summary.json" in text
    for token in (
        "person_bbox_observations_per_second",
        "face_observations_per_second",
        "watchlist_hit_count",
        "low_quality_face_reject_counts",
        "raw_clip_duration",
    ):
        assert token in text
