"""Pure unit tests for the crowd_gathering frame-level rule."""

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
        "CROWD_MIN_PERSON_COUNT",
        "CROWD_EXIT_PERSON_COUNT",
        "CROWD_MIN_DURATION_MS",
        "CROWD_EPS_PX",
        "CROWD_REQUIRE_IN_ZONE",
        "CROWD_COOLDOWN_S",
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
    cfg = dict(
        {
            "zone_id": "lobby",
            "min_person_count": 5,
            "exit_person_count": 3,
            "min_duration_s": 2,
            "eps_px": 100.0,
            "require_in_zone": True,
            "cooldown_s": 60,
        },
        **(config or {}),
    )
    camera = camera_config.CameraConfig(
        camera_id="cam_001",
        zones={
            "lobby": camera_config.ZoneConfig(
                name="lobby",
                polygon=[(0, 0), (500, 0), (500, 500), (0, 500)],
            )
        },
        rules={
            "crowd_lobby": camera_config.RuleConfig(
                name="crowd_lobby",
                rule_type="crowd_gathering",
                algorithm_id="behavior.crowd_gathering",
                zone="lobby",
                cooldown_s=int(cfg.get("cooldown_s", 60)),
                severity="medium",
                config=cfg,
            )
        },
    )
    return rules.build_rules(camera, cooldown or modules["cooldown"].CooldownTracker())[0]


def _track(
    modules,
    track_id: int,
    foot_x: float,
    foot_y: float,
    ts_ms: int = 0,
    *,
    camera_id: str = "cam_001",
):
    pose = modules["pose"]
    tracks = modules["tracks"]
    track = tracks.TrackState(track_id=track_id)
    track.add_observation(
        pose.PersonPoseObservation(
            source_id=f"src_{camera_id}",
            camera_id=camera_id,
            frame_id=ts_ms // 100,
            timestamp_ms=ts_ms,
            bbox=pose.BBox(x=foot_x - 10.0, y=foot_y - 60.0, width=20.0, height=60.0),
            confidence=0.9,
            track_id=track_id,
        ),
        window_s=10.0,
    )
    return track


def _cluster(
    modules,
    ts_ms: int,
    ids=(1, 2, 3, 4, 5),
    *,
    x0=100.0,
    y0=100.0,
    camera_id: str = "cam_001",
):
    return [
        _track(
            modules,
            track_id,
            x0 + idx * 15.0,
            y0 + (idx % 2) * 10.0,
            ts_ms,
            camera_id=camera_id,
        )
        for idx, track_id in enumerate(ids)
    ]


def test_roi_inside_five_person_cluster_sustained_alerts(modules) -> None:
    rule = _rule(modules)
    assert rule.evaluate_frame(_cluster(modules, 1000), 1000) == []
    events = rule.evaluate_frame(_cluster(modules, 3100), 3100)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "crowd_gathering"
    assert event.track_id == 0
    assert event.payload["algorithm_id"] == "behavior.crowd_gathering"
    assert event.payload["rule_id"] == "crowd_lobby"
    assert event.payload["camera_id"] == "cam_001"
    assert event.payload["zone_id"] == "lobby"
    assert event.payload["cluster_id"] == 1
    assert event.payload["member_track_ids"] == [1, 2, 3, 4, 5]
    assert event.payload["person_count"] == 5
    assert event.payload["duration_ms"] == 2100
    assert event.payload["centroid"]["x"] > 100.0
    assert event.payload["eps_px"] == 100.0


def test_threshold_count_without_duration_does_not_alert(modules) -> None:
    rule = _rule(modules)
    assert rule.evaluate_frame(_cluster(modules, 1000), 1000) == []
    assert rule.evaluate_frame(_cluster(modules, 2500), 2500) == []


def test_below_min_person_count_does_not_alert(modules) -> None:
    rule = _rule(modules)
    assert rule.evaluate_frame(_cluster(modules, 1000, ids=(1, 2, 3, 4)), 1000) == []
    assert rule.evaluate_frame(_cluster(modules, 4000, ids=(1, 2, 3, 4)), 4000) == []


def test_hysteresis_keeps_state_between_high_and_exit(modules) -> None:
    rule = _rule(modules)
    rule.evaluate_frame(_cluster(modules, 1000), 1000)
    assert rule.evaluate_frame(_cluster(modules, 2000, ids=(1, 2, 3, 4)), 2000) == []
    events = rule.evaluate_frame(_cluster(modules, 3200, ids=(1, 2, 3, 4)), 3200)
    assert len(events) == 1
    assert events[0].payload["member_track_ids"] == [1, 2, 3, 4]


def test_exit_threshold_clears_crowd_state(modules) -> None:
    rule = _rule(modules, config={"cooldown_s": 0})
    rule.evaluate_frame(_cluster(modules, 1000), 1000)
    assert rule.evaluate_frame(_cluster(modules, 3200), 3200)
    rule.evaluate_frame(_cluster(modules, 3400, ids=(1, 2, 3)), 3400)
    assert rule.evaluate_frame(_cluster(modules, 5400, ids=(1, 2, 3)), 5400) == []


def test_roi_outside_people_do_not_count(modules) -> None:
    rule = _rule(modules)
    inside = _cluster(modules, 1000, ids=(1, 2, 3, 4))
    outside = [_track(modules, 9, 900.0, 900.0, 1000)]
    assert rule.evaluate_frame(inside + outside, 1000) == []
    inside_late = _cluster(modules, 4000, ids=(1, 2, 3, 4))
    outside_late = [_track(modules, 9, 900.0, 900.0, 4000)]
    assert rule.evaluate_frame(inside_late + outside_late, 4000) == []


def test_separated_clusters_do_not_merge(modules) -> None:
    rule = _rule(modules)
    tracks = _cluster(modules, 1000, ids=(1, 2, 3), x0=100) + _cluster(
        modules,
        1000,
        ids=(4, 5),
        x0=350,
    )
    rule.evaluate_frame(tracks, 1000)
    tracks_late = _cluster(modules, 4000, ids=(1, 2, 3), x0=100) + _cluster(
        modules,
        4000,
        ids=(4, 5),
        x0=350,
    )
    assert rule.evaluate_frame(tracks_late, 4000) == []


def test_cluster_identity_survives_small_member_changes(modules) -> None:
    rule = _rule(modules)
    rule.evaluate_frame(_cluster(modules, 1000, ids=(1, 2, 3, 4, 5)), 1000)
    events = rule.evaluate_frame(_cluster(modules, 3200, ids=(1, 2, 3, 4, 6)), 3200)
    assert len(events) == 1
    assert events[0].payload["cluster_id"] == 1
    assert events[0].payload["member_track_ids"] == [1, 2, 3, 4, 6]


def test_cooldown_key_is_camera_zone_cluster_scoped(modules) -> None:
    cooldown = modules["cooldown"].CooldownTracker()
    rule_a = _rule(modules, cooldown=cooldown)
    rule_b = _rule(modules, cooldown=cooldown)
    assert rule_a.evaluate_frame(_cluster(modules, 1000), 1000) == []
    assert rule_b.evaluate_frame(_cluster(modules, 1000), 1000) == []

    assert rule_a.evaluate_frame(_cluster(modules, 3200), 3200)
    assert rule_a.evaluate_frame(_cluster(modules, 3300), 3300) == []
    assert rule_b.evaluate_frame(_cluster(modules, 3200, camera_id="cam_002"), 3200)
