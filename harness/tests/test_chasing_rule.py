"""Pure unit tests for the chasing frame-level rule."""

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
        "CHASE_MIN_SPEED_PX_S",
        "CHASE_MAX_DISTANCE_PX",
        "CHASE_MAX_PAIR_DISTANCE_PX",
        "CHASE_MIN_COS_ALIGNMENT",
        "CHASE_SPEED_RATIO_TOLERANCE",
        "CHASE_BEHIND_COS_MIN",
        "CHASE_VELOCITY_WINDOW_MS",
        "CHASE_MIN_PAIR_DURATION_MS",
        "CHASE_MIN_DURATION_MS",
        "CHASE_COOLDOWN_S",
        "CHASE_MAX_TRACK_AGE_MS",
        "CHASE_PAIR_GAP_TOLERANCE_MS",
        "CHASE_MIN_NORMALIZED_SPEED",
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
            "min_speed_px_s": 100.0,
            "max_distance_px": 140.0,
            "min_cos_alignment": 0.8,
            "speed_ratio_tolerance": 0.6,
            "behind_cos_min": 0.5,
            "velocity_window_ms": 700,
            "min_pair_duration_s": 1.5,
            "cooldown_s": 30,
        },
        **(config or {}),
    )
    camera = camera_config.CameraConfig(
        camera_id="cam_001",
        zones={
            "lobby": camera_config.ZoneConfig(
                name="lobby",
                polygon=[(0, 0), (1000, 0), (1000, 1000), (0, 1000)],
            )
        },
        rules={
            "chasing_lobby": camera_config.RuleConfig(
                name="chasing_lobby",
                rule_type="chasing",
                algorithm_id="behavior.chasing",
                zone="lobby",
                cooldown_s=int(cfg.get("cooldown_s", 30)),
                severity="medium",
                config=cfg,
            )
        },
    )
    return rules.build_rules(camera, cooldown or modules["cooldown"].CooldownTracker())[0]


def _track(
    modules,
    track_id: int,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    at_ms: int = 700,
    span_ms: int = 700,
    height: float = 60.0,
    camera_id: str = "cam_001",
):
    """Track that moved from *start* to *end* over the *span_ms* ending at *at_ms*.

    Rules only treat a track as present when its last observation is fresh
    relative to the evaluated frame, so tests build tracks per frame.
    """
    pose = modules["pose"]
    tracks = modules["tracks"]
    track = tracks.TrackState(track_id=track_id)
    for ts_ms, (foot_x, foot_y) in ((at_ms - span_ms, start), (at_ms, end)):
        track.add_observation(
            pose.PersonPoseObservation(
                source_id=f"src_{camera_id}",
                camera_id=camera_id,
                frame_id=ts_ms // 100,
                timestamp_ms=ts_ms,
                bbox=pose.BBox(
                    x=foot_x - height / 6.0,
                    y=foot_y - height,
                    width=height / 3.0,
                    height=height,
                ),
                confidence=0.9,
                track_id=track_id,
            ),
            window_s=10.0,
        )
    return track


def _chase_tracks(modules, at_ms: int = 700, *, camera_id: str = "cam_001"):
    leader = _track(
        modules,
        11,
        (100.0, 100.0),
        (240.0, 100.0),
        at_ms=at_ms,
        camera_id=camera_id,
    )
    follower = _track(
        modules,
        12,
        (40.0, 100.0),
        (180.0, 100.0),
        at_ms=at_ms,
        camera_id=camera_id,
    )
    return [leader, follower]


def _swapped_chase_tracks(modules, at_ms: int):
    """Same pair a moment later, with track 12 now just ahead of track 11."""
    return [
        _track(modules, 11, (40.0, 100.0), (180.0, 100.0), at_ms=at_ms),
        _track(modules, 12, (100.0, 100.0), (240.0, 100.0), at_ms=at_ms),
    ]


def _pair(modules, a_start, a_end, b_start, b_end, at_ms, **kwargs):
    return [
        _track(modules, 11, a_start, a_end, at_ms=at_ms, **kwargs),
        _track(modules, 12, b_start, b_end, at_ms=at_ms, **kwargs),
    ]


def test_sustained_fast_close_aligned_following_alerts(modules) -> None:
    rule = _rule(modules)
    assert rule.evaluate_frame(_chase_tracks(modules, 700), 700) == []
    events = rule.evaluate_frame(_chase_tracks(modules, 2300), 2300)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "chasing"
    assert event.track_id == 12
    assert event.payload["algorithm_id"] == "behavior.chasing"
    assert event.payload["rule_id"] == "chasing_lobby"
    assert event.payload["camera_id"] == "cam_001"
    assert event.payload["zone_id"] == "lobby"
    assert event.payload["leader_track_id"] == 11
    assert event.payload["follower_track_id"] == 12
    assert event.payload["member_track_ids"] == [11, 12]
    assert event.payload["distance_px"] == 60.0
    assert event.payload["alignment"] == 1.0
    assert event.payload["leader_speed_px_s"] == pytest.approx(200.0)
    assert event.payload["follower_speed_px_s"] == pytest.approx(200.0)
    assert event.payload["duration_ms"] == 1600


def test_side_by_side_fast_walk_does_not_alert(modules) -> None:
    rule = _rule(modules)
    for at_ms in (700, 2300):
        tracks = _pair(modules, (100, 100), (240, 100), (100, 150), (240, 150), at_ms)
        assert rule.evaluate_frame(tracks, at_ms) == []


def test_opposite_direction_running_does_not_alert(modules) -> None:
    rule = _rule(modules)
    for at_ms in (700, 2300):
        tracks = _pair(modules, (100, 100), (240, 100), (260, 100), (120, 100), at_ms)
        assert rule.evaluate_frame(tracks, at_ms) == []


def test_speed_difference_too_large_does_not_alert(modules) -> None:
    rule = _rule(modules, config={"speed_ratio_tolerance": 0.2})
    for at_ms in (700, 2300):
        tracks = _pair(modules, (100, 100), (300, 100), (70, 100), (170, 100), at_ms)
        assert rule.evaluate_frame(tracks, at_ms) == []


def test_distance_too_far_does_not_alert(modules) -> None:
    rule = _rule(modules, config={"max_distance_px": 80.0})
    for at_ms in (700, 2300):
        tracks = _pair(modules, (100, 100), (240, 100), (-80, 100), (60, 100), at_ms)
        assert rule.evaluate_frame(tracks, at_ms) == []


def test_duration_below_threshold_does_not_alert(modules) -> None:
    rule = _rule(modules)
    assert rule.evaluate_frame(_chase_tracks(modules, 700), 700) == []
    assert rule.evaluate_frame(_chase_tracks(modules, 2000), 2000) == []


def test_pair_interruption_resets_timer(modules) -> None:
    rule = _rule(modules)
    assert rule.evaluate_frame(_chase_tracks(modules, 700), 700) == []
    broken = _pair(modules, (100, 100), (240, 100), (500, 100), (640, 100), 1400)
    assert rule.evaluate_frame(broken, 1400) == []
    assert rule.evaluate_frame(_chase_tracks(modules, 2300), 2300) == []


def test_cooldown_key_is_camera_zone_pair_scoped(modules) -> None:
    cooldown = modules["cooldown"].CooldownTracker()
    rule_a = _rule(modules, cooldown=cooldown)
    rule_b = _rule(modules, cooldown=cooldown)

    rule_a.evaluate_frame(_chase_tracks(modules, 700, camera_id="cam_a"), 700)
    rule_b.evaluate_frame(_chase_tracks(modules, 700, camera_id="cam_b"), 700)
    assert rule_a.evaluate_frame(_chase_tracks(modules, 2300, camera_id="cam_a"), 2300)
    assert rule_a.evaluate_frame(_chase_tracks(modules, 2400, camera_id="cam_a"), 2400) == []
    assert rule_b.evaluate_frame(_chase_tracks(modules, 2300, camera_id="cam_b"), 2300)


def test_departed_pair_does_not_keep_chasing(modules) -> None:
    rule = _rule(modules)
    assert rule.evaluate_frame(_chase_tracks(modules, 700), 700) == []
    # Both people left the frame at 700 ms. The track store still holds them,
    # with their last velocities, but a pair that is no longer seen must not
    # accumulate chase duration.
    stale = _chase_tracks(modules, 700)
    assert rule.evaluate_frame(stale, 2300) == []


def test_max_track_age_zero_keeps_legacy_pairing(modules) -> None:
    rule = _rule(modules, config={"max_track_age_ms": 0})
    assert rule.evaluate_frame(_chase_tracks(modules, 700), 700) == []
    assert len(rule.evaluate_frame(_chase_tracks(modules, 700), 2300)) == 1


def test_leader_follower_flip_keeps_pair_timer(modules) -> None:
    rule = _rule(modules)
    assert rule.evaluate_frame(_chase_tracks(modules, 700), 700) == []
    assert rule.evaluate_frame(_swapped_chase_tracks(modules, 1500), 1500) == []
    events = rule.evaluate_frame(_chase_tracks(modules, 2300), 2300)
    assert len(events) == 1
    assert events[0].payload["duration_ms"] == 1600


def test_leader_follower_flip_does_not_bypass_cooldown(modules) -> None:
    rule = _rule(modules)
    rule.evaluate_frame(_chase_tracks(modules, 700), 700)
    assert len(rule.evaluate_frame(_chase_tracks(modules, 2300), 2300)) == 1
    assert rule.evaluate_frame(_swapped_chase_tracks(modules, 2500), 2500) == []
    assert rule.evaluate_frame(_swapped_chase_tracks(modules, 4200), 4200) == []


def test_brief_pair_gap_does_not_reset_timer(modules) -> None:
    rule = _rule(modules)
    assert rule.evaluate_frame(_chase_tracks(modules, 700), 700) == []
    broken = _pair(modules, (100, 100), (240, 100), (500, 100), (640, 100), 1000)
    assert rule.evaluate_frame(broken, 1000) == []
    assert rule.evaluate_frame(_chase_tracks(modules, 1200), 1200) == []
    assert rule.evaluate_frame(_chase_tracks(modules, 1700), 1700) == []
    events = rule.evaluate_frame(_chase_tracks(modules, 2300), 2300)
    assert len(events) == 1
    assert events[0].payload["duration_ms"] == 1600


def test_normalized_speed_rejects_large_people_walking(modules) -> None:
    def walking_pair(at_ms):
        # 300 px tall people moving 200 px/s: about 0.67 body heights per second.
        return _pair(
            modules,
            (300, 600),
            (440, 600),
            (180, 600),
            (320, 600),
            at_ms,
            height=300.0,
        )

    pixel_rule = _rule(modules)
    pixel_rule.evaluate_frame(walking_pair(700), 700)
    assert len(pixel_rule.evaluate_frame(walking_pair(2300), 2300)) == 1

    normalized_rule = _rule(modules, config={"min_normalized_speed": 1.0})
    normalized_rule.evaluate_frame(walking_pair(700), 700)
    assert normalized_rule.evaluate_frame(walking_pair(2300), 2300) == []
