"""Pure unit tests for the running behavior rule."""

from __future__ import annotations

import importlib
import sys
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
def modules(monkeypatch):
    _isolate_savant_security_modules()
    for name in (
        "RUNNING_MIN_SPEED_PX_S",
        "RUNNING_MIN_NORMALIZED_SPEED",
        "RUNNING_MIN_DURATION_MS",
        "RUNNING_VELOCITY_WINDOW_MS",
        "RUNNING_COOLDOWN_S",
    ):
        monkeypatch.delenv(name, raising=False)
    return {
        "rules": importlib.import_module("custom.rules"),
        "camera_config": importlib.import_module("custom.models.camera_config"),
        "cooldown": importlib.import_module("custom.services.cooldown"),
        "pose": importlib.import_module("custom.models.pose"),
        "tracks": importlib.import_module("custom.models.tracks"),
    }


def _rule(modules, *, config=None, cooldown=None):
    camera_config = modules["camera_config"]
    rules = modules["rules"]
    cfg = {
        "zone_id": "lobby",
        "min_speed_px_s": 200.0,
        "min_duration_ms": 500,
        "velocity_window_ms": 700,
        "cooldown_s": 20,
        **(config or {}),
    }
    camera = camera_config.CameraConfig(
        camera_id="cam_001",
        zones={
            "lobby": camera_config.ZoneConfig(
                name="lobby",
                polygon=[(0, 0), (1000, 0), (1000, 1000), (0, 1000)],
            )
        },
        rules={
            "running_lobby": camera_config.RuleConfig(
                name="running_lobby",
                rule_type="running",
                algorithm_id="behavior.running",
                zone="lobby",
                cooldown_s=int(cfg.get("cooldown_s", 20)),
                severity="medium",
                config=cfg,
            )
        },
    )
    return rules.build_rules(camera, cooldown or modules["cooldown"].CooldownTracker())[0]


def _track(modules, points, *, track_id: int = 8, camera_id: str = "cam_001", height: float = 80.0):
    pose = modules["pose"]
    tracks = modules["tracks"]
    track = tracks.TrackState(track_id=track_id)
    for ts_ms, (foot_x, foot_y) in points:
        track.add_observation(
            pose.PersonPoseObservation(
                source_id=f"src_{camera_id}",
                camera_id=camera_id,
                frame_id=ts_ms // 100,
                timestamp_ms=ts_ms,
                bbox=pose.BBox(
                    x=foot_x - 10.0,
                    y=foot_y - height,
                    width=20.0,
                    height=height,
                ),
                confidence=0.88,
                track_id=track_id,
            ),
            window_s=10.0,
        )
    return track


def test_sustained_high_speed_alerts_after_duration(modules) -> None:
    rule = _rule(modules)

    assert rule.evaluate(_track(modules, [(0, (100, 100)), (700, (310, 100))])) is None
    event = rule.evaluate(_track(modules, [(0, (100, 100)), (700, (310, 100)), (1400, (520, 100))]))

    assert event is not None
    assert event.event_type == "running"
    assert event.track_id == 8
    assert event.payload["algorithm_id"] == "behavior.running"
    assert event.payload["rule_id"] == "running_lobby"
    assert event.payload["camera_id"] == "cam_001"
    assert event.payload["zone_id"] == "lobby"
    assert event.payload["duration_ms"] == 700
    assert event.payload["speed_px_s"] == pytest.approx(300.0)
    assert event.payload["normalized_speed"] == pytest.approx(3.75)


def test_slow_motion_does_not_alert(modules) -> None:
    rule = _rule(modules)

    assert rule.evaluate(_track(modules, [(0, (100, 100)), (700, (170, 100))])) is None
    assert rule.evaluate(_track(modules, [(0, (100, 100)), (700, (170, 100)), (1400, (240, 100))])) is None


def test_normalized_speed_threshold_can_suppress_near_camera_motion(modules) -> None:
    rule = _rule(modules, config={"min_normalized_speed": 5.0})

    rule.evaluate(_track(modules, [(0, (100, 100)), (700, (310, 100))], height=120.0))
    event = rule.evaluate(
        _track(modules, [(0, (100, 100)), (700, (310, 100)), (1400, (520, 100))], height=120.0)
    )
    assert event is None


def test_outside_roi_resets_running_timer(modules) -> None:
    rule = _rule(modules)

    rule.evaluate(_track(modules, [(0, (100, 100)), (700, (310, 100))]))
    assert rule.evaluate(_track(modules, [(0, (100, 100)), (700, (310, 100)), (1400, (1200, 1200))])) is None
    assert rule.evaluate(_track(modules, [(1400, (100, 100)), (2100, (310, 100))])) is None


def test_cooldown_suppresses_repeated_running_event(modules) -> None:
    rule = _rule(modules)

    rule.evaluate(_track(modules, [(0, (100, 100)), (700, (310, 100))]))
    track = _track(modules, [(0, (100, 100)), (700, (310, 100)), (1400, (520, 100))])
    assert rule.evaluate(track) is not None
    assert rule.evaluate(track) is None
