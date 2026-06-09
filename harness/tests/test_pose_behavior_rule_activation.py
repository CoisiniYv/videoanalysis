"""Algorithm activation contract for pose behavior rules."""

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
def modules():
    _isolate_savant_security_modules()
    return {
        "rules": importlib.import_module("custom.rules"),
        "loader": importlib.import_module("custom.services.camera_config"),
        "runtime": importlib.import_module("custom.services.rule_runtime"),
        "cooldown": importlib.import_module("custom.services.cooldown"),
        "pose": importlib.import_module("custom.models.pose"),
    }


def _write_yaml(tmp_path: Path, body: str) -> str:
    path = tmp_path / "cameras.generated.yml"
    path.write_text(textwrap.dedent(body).strip() + "\n")
    return str(path)


@pytest.mark.parametrize(
    ("algorithm_id", "rule_id", "expected_rule_type", "extra_config"),
    [
        ("behavior.fall", "fall_lobby", "fall", {"min_down_ms": 1200}),
        (
            "behavior.crowd_gathering",
            "crowd_lobby",
            "crowd_gathering",
            {"min_person_count": 5, "eps_px": 180.0},
        ),
        (
            "behavior.chasing",
            "chasing_lobby",
            "chasing",
            {"min_pair_duration_s": 1.5, "max_distance_px": 220.0},
        ),
    ],
)
def test_behavior_algorithm_activates_expected_rule(
    modules,
    tmp_path,
    algorithm_id,
    rule_id,
    expected_rule_type,
    extra_config,
) -> None:
    config_yaml = "\n".join(
        f"                  {key}: {value}".lower()
        if isinstance(value, bool)
        else f"                  {key}: {value}"
        for key, value in extra_config.items()
    )
    cfg_path = _write_yaml(
        tmp_path,
        f"""
        cameras:
          cam_a:
            enabled: true
            source_id: src_a
            name: A
            rtsp_url: rtsp://a
            zones:
              lobby:
                type: polygon
                points: [[0,0],[1000,0],[1000,1000],[0,1000]]
            rules:
              {rule_id}:
                rule_id: {rule_id}
                algorithm_id: {algorithm_id}
                enabled: true
                config:
                  zone_id: lobby
                  cooldown_s: 60
{config_yaml}
        """,
    )
    bundle = modules["loader"].load_camera_config(cfg_path)
    runtimes = modules["runtime"].build_per_source_runtime(
        bundle,
        modules["cooldown"].CooldownTracker(),
    )
    assert [rule.rule_type for rule in runtimes["src_a"].rules] == [expected_rule_type]
    assert runtimes["src_a"].rules[0].config.algorithm_id == algorithm_id


def test_pose_rule_registry_contains_stage3_rules(modules) -> None:
    known = modules["rules"].REGISTRY.known_rule_types()
    assert "fall" in known
    assert "crowd_gathering" in known
    assert "chasing" in known


def test_unknown_enabled_behavior_rule_is_skipped_with_log(
    modules,
    tmp_path,
    capsys,
) -> None:
    cfg_path = _write_yaml(
        tmp_path,
        """
        cameras:
          cam_a:
            enabled: true
            source_id: src_a
            name: A
            rtsp_url: rtsp://a
            zones:
              lobby:
                type: polygon
                points: [[0,0],[1000,0],[1000,1000],[0,1000]]
            rules:
              loiter_lobby:
                rule_id: loiter_lobby
                algorithm_id: behavior.loitering
                enabled: true
                config:
                  zone_id: lobby
                  min_duration_s: 60
        """,
    )
    bundle = modules["loader"].load_camera_config(cfg_path)
    runtimes = modules["runtime"].build_per_source_runtime(
        bundle,
        modules["cooldown"].CooldownTracker(),
    )
    assert runtimes == {}
    assert "stage=savant_security_rule_registry_unknown" in capsys.readouterr().out


def test_camera_a_fall_does_not_activate_camera_b_fall(modules, tmp_path) -> None:
    cfg_path = _write_yaml(
        tmp_path,
        """
        cameras:
          cam_a:
            enabled: true
            source_id: src_a
            name: A
            rtsp_url: rtsp://a
            zones:
              lobby:
                type: polygon
                points: [[0,0],[1000,0],[1000,1000],[0,1000]]
            rules:
              fall_lobby:
                rule_id: fall_lobby
                algorithm_id: behavior.fall
                enabled: true
                config:
                  zone_id: lobby
                  min_down_ms: 1200
          cam_b:
            enabled: true
            source_id: src_b
            name: B
            rtsp_url: rtsp://b
            zones:
              lobby:
                type: polygon
                points: [[0,0],[1000,0],[1000,1000],[0,1000]]
            rules:
              watchlist:
                rule_id: watchlist
                algorithm_id: face.watchlist
                enabled: true
                config:
                  threshold: 0.75
        """,
    )
    bundle = modules["loader"].load_camera_config(cfg_path)
    runtimes = modules["runtime"].build_per_source_runtime(
        bundle,
        modules["cooldown"].CooldownTracker(),
    )
    assert set(runtimes) == {"src_a"}
