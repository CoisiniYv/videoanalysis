"""C2 Replay->Savant frame dump probe contract tests."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[2]
MODULE_YAML = REPO / "modules" / "savant_security" / "module.yml"
COMPOSE = REPO / "infra" / "docker-compose.c2-replay-first-dev.yml"
PYFUNC = (
    REPO
    / "modules"
    / "savant_security"
    / "custom"
    / "pyfuncs"
    / "replay_savant_frame_dump.py"
)


def _module() -> dict:
    return yaml.safe_load(MODULE_YAML.read_text(encoding="utf-8"))


def _elements() -> list[dict]:
    return _module()["pipeline"]["elements"]


def _element_index(name: str) -> int:
    for index, element in enumerate(_elements()):
        if element.get("name") == name:
            return index
    return -1


def test_probe_file_exists_and_compiles() -> None:
    source = PYFUNC.read_text(encoding="utf-8")
    ast.parse(source)


def test_probe_is_registered_before_models() -> None:
    replay_dump = _element_index("replay_savant_frame_dump")
    pose = _element_index("yolo26_pose")
    face = _element_index("yolov8_face")

    assert replay_dump >= 0
    assert replay_dump < pose
    assert replay_dump < face

    element = _elements()[replay_dump]
    assert element["element"] == "pyfunc"
    assert element["module"] == "custom.pyfuncs.replay_savant_frame_dump"
    assert element["class_name"] == "ReplaySavantFrameDumpPyFunc"


def test_probe_is_default_disabled_and_target_gated() -> None:
    source = PYFUNC.read_text(encoding="utf-8")

    assert "C2_REPLAY_SAVANT_FRAME_DUMP_ENABLED" in source
    assert 'default: bool = False' in source
    assert "C2_REPLAY_SAVANT_FRAME_DUMP_TARGET_UUIDS" in source
    assert "C2_REPLAY_SAVANT_FRAME_DUMP_TARGET_PTS" in source
    assert "target_by_uuid" in source
    assert "target_by_pts" in source
    assert "has_targets" in source


def test_probe_writes_runtime_frame_sidecar_contract() -> None:
    source = PYFUNC.read_text(encoding="utf-8")

    assert "pyds.get_nvds_buf_surface" in source
    assert "cv2.imwrite" in source
    assert '"dump_source": "savant_runtime_frame"' in source
    assert '"created_by": "debug_only_runtime_frame_dump"' in source
    assert '"frame_uuid"' in source
    assert '"frame_pts"' in source
    assert '"keyframe_pts"' in source
    assert '"image_sha256"' in source


def test_compose_defaults_probe_off_and_mounts_debug_media_root() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["savant-security"]
    env = service["environment"]

    assert env["C2_REPLAY_SAVANT_FRAME_DUMP_ENABLED"].endswith(":-false}")
    assert "C2_REPLAY_SAVANT_FRAME_DUMP_TARGET_UUIDS" in env
    assert "C2_REPLAY_SAVANT_FRAME_DUMP_TARGET_PTS" in env
    assert "C2_REPLAY_SAVANT_FRAME_DUMP_ROOT" in env
    assert "/data/video-analytics/media:/data/video-analytics/media:rw" in service[
        "volumes"
    ]
