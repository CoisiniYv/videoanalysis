"""Pure unit tests for the loitering behavior rule."""

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
        "LOITERING_MIN_DURATION_MS",
        "LOITERING_MAX_AVG_SPEED_PX_S",
        "LOITERING_MAX_DISPLACEMENT_PX",
        "LOITERING_MIN_OBSERVATION_COUNT",
        "LOITERING_COOLDOWN_S",
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
        "min_duration_ms": 1000,
        "max_avg_speed_px_s": 15.0,
        "max_displacement_px": 0.0,
        "cooldown_s": 60,
        **(config or {}),
    }
    camera = camera_config.CameraConfig(
        camera_id="cam_001",
        zones={
            "lobby": camera_config.ZoneConfig(
                name="lobby",
                polygon=[(0, 0), (500, 0), (500, 500), (0, 500)],
            )
        },
        rules={
            "loiter_lobby": camera_config.RuleConfig(
                name="loiter_lobby",
                rule_type="loitering",
                algorithm_id="behavior.loitering",
                zone="lobby",
                cooldown_s=int(cfg.get("cooldown_s", 60)),
                severity="medium",
                config=cfg,
            )
        },
    )
    return rules.build_rules(camera, cooldown or modules["cooldown"].CooldownTracker())[0]


def _track(modules, points, *, track_id: int = 7, camera_id: str = "cam_001"):
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
                    y=foot_y - 80.0,
                    width=20.0,
                    height=80.0,
                ),
                confidence=0.91,
                track_id=track_id,
            ),
            window_s=10.0,
        )
    return track


def test_stationary_track_in_roi_past_duration_alerts(modules) -> None:
    rule = _rule(modules)
    event = rule.evaluate(
        _track(modules, [(0, (100, 100)), (500, (102, 100)), (1200, (103, 100))])
    )

    assert event is not None
    assert event.event_type == "loitering"
    assert event.track_id == 7
    assert event.payload["algorithm_id"] == "behavior.loitering"
    assert event.payload["rule_id"] == "loiter_lobby"
    assert event.payload["camera_id"] == "cam_001"
    assert event.payload["zone_id"] == "lobby"
    assert event.payload["duration_ms"] == 1200
    assert event.payload["avg_speed_px_s"] == pytest.approx(2.5)
    assert event.payload["displacement_px"] == pytest.approx(3.0)


def test_duration_below_threshold_does_not_alert(modules) -> None:
    rule = _rule(modules)
    event = rule.evaluate(_track(modules, [(0, (100, 100)), (800, (101, 100))]))
    assert event is None


def test_fast_motion_inside_roi_does_not_alert(modules) -> None:
    rule = _rule(modules)
    event = rule.evaluate(
        _track(modules, [(0, (100, 100)), (500, (180, 100)), (1200, (260, 100))])
    )
    assert event is None


def test_displacement_cap_suppresses_slow_wandering(modules) -> None:
    rule = _rule(modules, config={"max_avg_speed_px_s": 100.0, "max_displacement_px": 20.0})
    event = rule.evaluate(
        _track(modules, [(0, (100, 100)), (600, (115, 100)), (1300, (135, 100))])
    )
    assert event is None


def test_currently_outside_roi_does_not_alert(modules) -> None:
    rule = _rule(modules)
    event = rule.evaluate(
        _track(modules, [(0, (100, 100)), (700, (105, 100)), (1400, (800, 800))])
    )
    assert event is None


def test_cooldown_suppresses_repeated_loitering_event(modules) -> None:
    rule = _rule(modules)
    track = _track(modules, [(0, (100, 100)), (600, (101, 100)), (1300, (102, 100))])

    assert rule.evaluate(track) is not None
    assert rule.evaluate(track) is None
