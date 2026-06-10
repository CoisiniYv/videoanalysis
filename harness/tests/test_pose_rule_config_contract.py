"""Pose behavior rule config contract tests.

These tests guard the pure runtime adapter used by Savant. They do not use
video, Redis, PostgreSQL, Docker, or Savant imports.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
MODULES_ROOT = str(Path(__file__).resolve().parents[2] / "modules")


def _isolate_savant_security_modules() -> None:
    sys.path[:] = [
        p for p in sys.path
        if not (p.startswith(MODULES_ROOT) and p != MODULE_DIR)
    ]
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    for name in [m for m in list(sys.modules) if m == "custom" or m.startswith("custom.")]:
        sys.modules.pop(name, None)


@pytest.fixture()
def runtime_mod():
    _isolate_savant_security_modules()
    return importlib.import_module("custom.services.rule_runtime")


@pytest.fixture()
def loader_mod():
    _isolate_savant_security_modules()
    return importlib.import_module("custom.services.camera_config")


def _write_yaml(tmp_path: Path, body: str) -> str:
    path = tmp_path / "cameras.midterm.yml"
    path.write_text(textwrap.dedent(body).strip() + "\n")
    return str(path)


def test_pose_rule_config_and_algorithm_id_survive_runtime_mapping(
    loader_mod,
    runtime_mod,
    tmp_path,
) -> None:
    cfg_path = _write_yaml(
        tmp_path,
        """
        cameras:
          cam_001:
            enabled: true
            source_id: src_001
            name: Lobby
            rtsp_url: rtsp://example.local/lobby
            zones:
              lobby:
                type: polygon
                points: [[0,0],[800,0],[800,600],[0,600]]
            rules:
              fall_lobby:
                rule_id: fall_lobby
                algorithm_id: behavior.fall
                enabled: true
                config:
                  zone_id: lobby
                  min_down_ms: 1500
                  require_transition: true
                  min_visible_keypoints: 3
              crowd_lobby:
                rule_id: crowd_lobby
                algorithm_id: behavior.crowd_gathering
                enabled: true
                config:
                  zone_id: lobby
                  min_person_count: 5
                  exit_person_count: 3
                  eps_px: 180.0
              chasing_lobby:
                rule_id: chasing_lobby
                algorithm_id: behavior.chasing
                enabled: true
                config:
                  zone_id: lobby
                  min_pair_duration_s: 1.5
                  max_distance_px: 220.0
                  min_speed_px_s: 120.0
        """,
    )
    bundle = loader_mod.load_camera_config(cfg_path)
    legacy = runtime_mod.camera_entry_to_legacy_config(bundle.get_camera("cam_001"))

    fall = legacy.rules["fall_lobby"]
    assert fall.rule_type == "fall"
    assert fall.algorithm_id == "behavior.fall"
    assert fall.config["min_down_ms"] == 1500
    assert fall.config["require_transition"] is True
    assert fall.config["min_visible_keypoints"] == 3

    crowd = legacy.rules["crowd_lobby"]
    assert crowd.rule_type == "crowd_gathering"
    assert crowd.algorithm_id == "behavior.crowd_gathering"
    assert crowd.config["min_person_count"] == 5
    assert crowd.config["exit_person_count"] == 3
    assert crowd.config["eps_px"] == 180.0

    chasing = legacy.rules["chasing_lobby"]
    assert chasing.rule_type == "chasing"
    assert chasing.algorithm_id == "behavior.chasing"
    assert chasing.config["min_pair_duration_s"] == 1.5
    assert chasing.config["max_distance_px"] == 220.0
    assert chasing.config["min_speed_px_s"] == 120.0


def test_disabled_and_face_rules_do_not_enter_behavior_runtime(
    loader_mod,
    runtime_mod,
    tmp_path,
) -> None:
    cfg_path = _write_yaml(
        tmp_path,
        """
        cameras:
          cam_001:
            enabled: true
            source_id: src_001
            name: Lobby
            rtsp_url: rtsp://example.local/lobby
            zones:
              lobby:
                type: polygon
                points: [[0,0],[800,0],[800,600],[0,600]]
            rules:
              fall_disabled:
                rule_id: fall_disabled
                algorithm_id: behavior.fall
                enabled: false
                config:
                  zone_id: lobby
                  min_down_ms: 1500
              watchlist_enabled:
                rule_id: watchlist_enabled
                algorithm_id: face.watchlist
                enabled: true
                config:
                  threshold: 0.75
        """,
    )
    bundle = loader_mod.load_camera_config(cfg_path)
    legacy = runtime_mod.camera_entry_to_legacy_config(bundle.get_camera("cam_001"))
    assert legacy.rules == {}
