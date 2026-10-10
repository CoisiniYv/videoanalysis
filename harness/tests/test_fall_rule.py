"""Pure unit tests for the fall behavior rule."""

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
        "FALL_MIN_DOWN_MS",
        "FALL_MIN_ON_GROUND_MS",
        "FALL_REQUIRE_TRANSITION",
        "FALL_TRANSITION_WINDOW_MS",
        "FALL_MIN_VISIBLE_KEYPOINTS",
        "FALL_COOLDOWN_S",
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
            "min_down_ms": 1200,
            "transition_window_ms": 1500,
            "cooldown_s": 60,
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
            "fall_lobby": camera_config.RuleConfig(
                name="fall_lobby",
                rule_type="fall",
                algorithm_id="behavior.fall",
                zone="lobby",
                cooldown_s=int(cfg.get("cooldown_s", 60)),
                severity="high",
                config=cfg,
            )
        },
    )
    return rules.build_rules(camera, cooldown or modules["cooldown"].CooldownTracker())[0]


def _keypoints(modules, posture: str):
    pose = modules["pose"]
    if posture == "upright":
        points = {
            "nose": (130, 110),
            "left_shoulder": (115, 180),
            "right_shoulder": (145, 180),
            "left_hip": (118, 300),
            "right_hip": (142, 300),
        }
    elif posture == "lying":
        points = {
            "nose": (125, 330),
            "left_shoulder": (210, 330),
            "right_shoulder": (230, 330),
            "left_hip": (390, 335),
            "right_hip": (410, 335),
        }
    elif posture == "bent":
        points = {
            "nose": (130, 140),
            "left_shoulder": (115, 210),
            "right_shoulder": (145, 210),
            "left_hip": (125, 300),
            "right_hip": (155, 300),
        }
    else:
        points = {}
    return [
        pose.Keypoint(x=x, y=y, confidence=0.9, name=name)
        for name, (x, y) in points.items()
    ]


def _obs(modules, track_id: int, ts_ms: int, posture: str, *, keypoints=True):
    pose = modules["pose"]
    if posture == "upright":
        bbox = pose.BBox(x=100, y=100, width=60, height=220)
    elif posture == "lying":
        bbox = pose.BBox(x=100, y=280, width=360, height=90)
    elif posture == "crouch":
        bbox = pose.BBox(x=100, y=230, width=90, height=110)
    elif posture == "pushup":
        bbox = pose.BBox(x=100, y=300, width=360, height=90)
    else:
        bbox = pose.BBox(x=100, y=100, width=60, height=220)
    kp = [] if keypoints is False else _keypoints(
        modules,
        "bent" if posture in ("crouch", "pushup") else posture,
    )
    return pose.PersonPoseObservation(
        source_id="src_001",
        camera_id="cam_001",
        frame_id=ts_ms // 100,
        timestamp_ms=ts_ms,
        bbox=bbox,
        confidence=0.92,
        track_id=track_id,
        keypoints=kp,
    )


def _track(modules, observations):
    tracks = modules["tracks"]
    track = tracks.TrackState(track_id=observations[0].track_id)
    for obs in observations:
        track.add_observation(obs, window_s=10.0)
    return track


def test_upright_to_lying_sustained_produces_fall(modules) -> None:
    rule = _rule(modules)
    track = _track(
        modules,
        [
            _obs(modules, 7, 0, "upright"),
            _obs(modules, 7, 500, "lying"),
            _obs(modules, 7, 1300, "lying"),
            _obs(modules, 7, 1800, "lying"),
        ],
    )
    event = rule.evaluate(track)
    assert event is not None
    assert event.event_type == "fall"
    assert event.payload["algorithm_id"] == "behavior.fall"
    assert event.payload["rule_id"] == "fall_lobby"
    assert event.payload["camera_id"] == "cam_001"
    assert event.payload["zone_id"] == "lobby"
    assert event.payload["track_id"] == 7
    assert event.payload["on_ground_ms"] == 1300
    assert event.payload["require_transition"] is True
    assert event.payload["transition_window_ms"] == 1500
    assert event.payload["posture"]["is_lying"] is True
    assert event.payload["posture"]["is_upright_before"] is True


def test_lying_shorter_than_threshold_does_not_alert(modules) -> None:
    rule = _rule(modules)
    track = _track(
        modules,
        [
            _obs(modules, 7, 0, "upright"),
            _obs(modules, 7, 500, "lying"),
            _obs(modules, 7, 1200, "lying"),
        ],
    )
    assert rule.evaluate(track) is None


def test_already_lying_requires_transition_by_default(modules) -> None:
    rule = _rule(modules)
    track = _track(
        modules,
        [
            _obs(modules, 7, 0, "lying"),
            _obs(modules, 7, 700, "lying"),
            _obs(modules, 7, 1400, "lying"),
        ],
    )
    assert rule.evaluate(track) is None


def test_require_transition_false_allows_sustained_lying(modules) -> None:
    rule = _rule(modules, config={"require_transition": False})
    track = _track(
        modules,
        [
            _obs(modules, 7, 0, "lying"),
            _obs(modules, 7, 700, "lying"),
            _obs(modules, 7, 1400, "lying"),
        ],
    )
    event = rule.evaluate(track)
    assert event is not None
    assert event.payload["require_transition"] is False
    assert event.payload["posture"]["is_upright_before"] is False


def test_short_fall_recovery_does_not_alert(modules) -> None:
    rule = _rule(modules)
    track = _track(
        modules,
        [
            _obs(modules, 7, 0, "upright"),
            _obs(modules, 7, 400, "lying"),
            _obs(modules, 7, 900, "upright"),
        ],
    )
    assert rule.evaluate(track) is None


@pytest.mark.parametrize("posture", ["crouch", "pushup"])
def test_crouch_bend_and_pushup_counterexamples_do_not_alert(modules, posture) -> None:
    rule = _rule(modules)
    track = _track(
        modules,
        [
            _obs(modules, 7, 0, "upright"),
            _obs(modules, 7, 500, posture),
            _obs(modules, 7, 1300, posture),
            _obs(modules, 7, 1800, posture),
        ],
    )
    assert rule.evaluate(track) is None


def test_bbox_only_fallback_is_allowed_and_marked(modules) -> None:
    rule = _rule(modules, config={"require_transition": False})
    track = _track(
        modules,
        [
            _obs(modules, 7, 0, "lying", keypoints=False),
            _obs(modules, 7, 700, "lying", keypoints=False),
            _obs(modules, 7, 1400, "lying", keypoints=False),
        ],
    )
    event = rule.evaluate(track)
    assert event is not None
    assert event.payload["pose_quality_mode"] == "bbox_only"


def test_min_visible_keypoints_blocks_low_quality_bbox_only(modules) -> None:
    rule = _rule(
        modules,
        config={"require_transition": False, "min_visible_keypoints": 1},
    )
    track = _track(
        modules,
        [
            _obs(modules, 7, 0, "lying", keypoints=False),
            _obs(modules, 7, 700, "lying", keypoints=False),
            _obs(modules, 7, 1400, "lying", keypoints=False),
        ],
    )
    assert rule.evaluate(track) is None


def test_cooldown_suppresses_duplicate_fall(modules) -> None:
    cooldown = modules["cooldown"].CooldownTracker()
    rule = _rule(modules, config={"require_transition": False}, cooldown=cooldown)
    track = _track(
        modules,
        [
            _obs(modules, 7, 0, "lying"),
            _obs(modules, 7, 700, "lying"),
            _obs(modules, 7, 1400, "lying"),
        ],
    )
    assert rule.evaluate(track) is not None
    assert rule.evaluate(track) is None


# --- full-skeleton cases: joint-order votes ------------------------------
#
# The five-point fixtures above have no knees or ankles, so they exercise
# only the original shape votes. The cases below use full COCO-17 skeletons
# (image y grows downward) to pin the joint-order cues: shoulders at or below
# ankle level, shoulders below a knee, and the keypoint-box aspect.

FULL_POSTURES = {
    # Standing, legs stacked under the hips.
    "standing": {
        "bbox": (100, 100, 60, 400),
        "points": {
            "nose": (130, 110),
            "left_shoulder": (115, 180), "right_shoulder": (145, 180),
            "left_hip": (118, 300), "right_hip": (142, 300),
            "left_knee": (120, 400), "right_knee": (140, 400),
            "left_ankle": (120, 495), "right_ankle": (140, 495),
        },
    },
    # Same standing skeleton seen from a high camera: the detector box is
    # wide (aspect 0.75) although the joints are still stacked upright.
    "standing_high_angle": {
        "bbox": (40, 100, 300, 400),
        "points": {
            "nose": (130, 110),
            "left_shoulder": (115, 180), "right_shoulder": (145, 180),
            "left_hip": (118, 300), "right_hip": (142, 300),
            "left_knee": (120, 400), "right_knee": (140, 400),
            "left_ankle": (120, 495), "right_ankle": (140, 495),
        },
    },
    # Lying with the head toward the camera and the feet away from it: the
    # body is foreshortened, so the box is tall and the torso looks vertical,
    # but the shoulders are below the ankles in the image.
    "lying_head_toward_camera": {
        "bbox": (100, 290, 60, 190),
        "points": {
            "nose": (130, 470),
            "left_shoulder": (115, 430), "right_shoulder": (145, 430),
            "left_hip": (118, 380), "right_hip": (142, 380),
            "left_knee": (120, 340), "right_knee": (140, 340),
            "left_ankle": (120, 300), "right_ankle": (140, 300),
        },
    },
    # Sitting side-on: thighs horizontal, shins vertical. The old C++ rule's
    # thigh-angle condition fires on this; it must never count as lying.
    "sitting": {
        "bbox": (190, 100, 160, 310),
        "points": {
            "nose": (240, 120),
            "left_shoulder": (215, 180), "right_shoulder": (225, 180),
            "left_hip": (210, 300), "right_hip": (220, 300),
            "left_knee": (320, 300), "right_knee": (330, 305),
            "left_ankle": (330, 400), "right_ankle": (335, 400),
        },
    },
    # Squatting and leaning forward: the box is nearly square and the torso
    # is past 45 degrees, which is two of the original three lying votes,
    # but the shoulders stay above the knees and the ankles.
    "leaning_squat": {
        "bbox": (120, 320, 120, 140),
        "points": {
            "nose": (230, 345),
            "left_shoulder": (190, 330), "right_shoulder": (205, 332),
            "left_hip": (130, 380), "right_hip": (140, 382),
            "left_knee": (190, 360), "right_knee": (200, 362),
            "left_ankle": (150, 450), "right_ankle": (160, 450),
        },
    },
}


def _full_obs(modules, track_id: int, ts_ms: int, posture: str, *, extra=None):
    pose = modules["pose"]
    spec = FULL_POSTURES[posture]
    x, y, w, h = spec["bbox"]
    keypoints = [
        pose.Keypoint(x=px, y=py, confidence=0.9, name=name)
        for name, (px, py) in spec["points"].items()
    ]
    keypoints.extend(extra or [])
    return pose.PersonPoseObservation(
        source_id="src_001",
        camera_id="cam_001",
        frame_id=ts_ms // 100,
        timestamp_ms=ts_ms,
        bbox=pose.BBox(x=x, y=y, width=w, height=h),
        confidence=0.92,
        track_id=track_id,
        keypoints=keypoints,
    )


def _sequence(modules, before: str, after: str):
    return _track(
        modules,
        [
            _full_obs(modules, 9, 0, before),
            _full_obs(modules, 9, 500, after),
            _full_obs(modules, 9, 1300, after),
            _full_obs(modules, 9, 1800, after),
        ],
    )


def test_lying_head_toward_camera_is_detected_by_joint_order(modules) -> None:
    rule = _rule(modules)
    event = rule.evaluate(_sequence(modules, "standing", "lying_head_toward_camera"))
    assert event is not None
    posture = event.payload["posture"]
    assert posture["shoulders_below_ankles"] is True
    assert posture["shoulders_below_knees"] is True
    assert posture["is_lying"] is True


def test_high_angle_standing_still_counts_as_upright_before_a_fall(modules) -> None:
    rule = _rule(modules)
    event = rule.evaluate(
        _sequence(modules, "standing_high_angle", "lying_head_toward_camera")
    )
    assert event is not None
    assert event.payload["posture"]["is_upright_before"] is True


@pytest.mark.parametrize("posture", ["sitting", "leaning_squat"])
def test_sitting_and_leaning_squat_do_not_alert(modules, posture) -> None:
    rule = _rule(modules)
    assert rule.evaluate(_sequence(modules, "standing", posture)) is None


def test_leaning_squat_is_not_lying_although_two_shape_votes_say_so(modules) -> None:
    fall = importlib.import_module("custom.rules.fall")
    signals = fall.posture_signals(
        _full_obs(modules, 9, 0, "leaning_squat"),
        fall.FallConfig(),
    )
    assert signals.aspect_ratio >= fall.FallConfig().lying_aspect_ratio
    assert signals.torso_angle_deg >= fall.FallConfig().torso_horizontal_deg
    assert signals.shoulders_below_ankles is False
    assert signals.is_lying is False


def test_low_confidence_keypoints_are_ignored_by_joint_order(modules) -> None:
    pose = modules["pose"]
    fall = importlib.import_module("custom.rules.fall")
    # Occluded ankles come back with a guessed position near the top of the
    # frame and a near-zero score; they must not make a standing person
    # look like their shoulders are below their feet.
    standing = _full_obs(modules, 9, 0, "standing")
    standing.keypoints = [
        kp for kp in standing.keypoints if kp.name not in ("left_ankle", "right_ankle")
    ] + [
        pose.Keypoint(x=120, y=40, confidence=0.05, name="left_ankle"),
        pose.Keypoint(x=140, y=40, confidence=0.05, name="right_ankle"),
    ]
    signals = fall.posture_signals(standing, fall.FallConfig())
    assert signals.shoulders_below_ankles is None
    assert signals.is_lying is False
