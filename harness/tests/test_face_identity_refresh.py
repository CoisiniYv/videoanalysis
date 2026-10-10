"""Per-track face identity refresh policy (pure Python, no GPU / Savant).

A person's face is recognized once when it first appears with usable quality.
After that the same person track is only recognized again when the refresh
interval has passed and the face is clearly visible, so AdaFace and the
face-worker vector search run a few times per person instead of about once per
second.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.savant_security.custom.services.face_identity_refresh import (  # noqa: E402
    FaceClarityConfig,
    IdentityRefreshConfig,
    IdentityRefreshPolicy,
    assess_face_clarity,
)
from modules.savant_security.custom.services.face_reid_gate import (  # noqa: E402
    ReIDThrottleMap,
)


# Five landmarks in image pixels: two eyes, nose, two mouth corners.
FRONTAL = [100.0, 200.0, 140.0, 200.0, 120.0, 222.0, 104.0, 240.0, 136.0, 240.0]
# Head turned: the nose sits almost under one eye.
TURNED = [100.0, 200.0, 140.0, 200.0, 137.0, 222.0, 118.0, 240.0, 140.0, 240.0]
# Profile: both eyes nearly on top of each other.
PROFILE = [118.0, 200.0, 122.0, 201.0, 135.0, 222.0, 120.0, 240.0, 126.0, 240.0]

KEY = "cam1:src1:42"


def _clarity(**overrides):
    values = dict(
        face_width=80.0,
        face_height=96.0,
        face_confidence=0.85,
        landmarks=FRONTAL,
    )
    values.update(overrides)
    return assess_face_clarity(config=FaceClarityConfig(), **values)


def _policy(**overrides) -> IdentityRefreshPolicy:
    values = dict(min_interval_ms=1000, refresh_ms=5000, state_ttl_ms=60000)
    values.update(overrides)
    return IdentityRefreshPolicy(IdentityRefreshConfig(**values))


def _admit(policy: IdentityRefreshPolicy, ts_ms: int, clear: bool, key: str = KEY):
    decision = policy.decide(key, ts_ms, clear)
    if decision.allowed:
        policy.record(key, ts_ms, clear)
    return decision


class TestFaceClarity:
    def test_large_confident_frontal_face_is_clear(self):
        clarity = _clarity()
        assert clarity.clear is True
        assert clarity.reasons == []
        assert clarity.yaw_ratio == pytest.approx(0.0)
        assert 0.0 < clarity.score <= 1.0

    def test_small_face_is_not_clear(self):
        clarity = _clarity(face_width=48.0, face_height=56.0)
        assert clarity.clear is False
        assert "small_face" in clarity.reasons

    def test_low_confidence_face_is_not_clear(self):
        clarity = _clarity(face_confidence=0.5)
        assert clarity.clear is False
        assert "low_confidence" in clarity.reasons

    def test_turned_head_is_not_clear(self):
        clarity = _clarity(landmarks=TURNED)
        assert clarity.clear is False
        assert "not_frontal" in clarity.reasons
        assert clarity.yaw_ratio > 0.35

    def test_profile_with_collapsed_eyes_is_not_clear(self):
        clarity = _clarity(landmarks=PROFILE)
        assert clarity.clear is False
        assert "not_frontal" in clarity.reasons

    def test_missing_landmarks_is_not_clear(self):
        clarity = _clarity(landmarks=None)
        assert clarity.clear is False
        assert "no_landmarks" in clarity.reasons
        assert clarity.yaw_ratio is None

    def test_landmarks_with_point_confidence_are_supported(self):
        with_conf = []
        for i in range(0, 10, 2):
            with_conf.extend([FRONTAL[i], FRONTAL[i + 1], 0.9])
        assert _clarity(landmarks=with_conf).clear is True

    def test_nested_landmark_points_are_supported(self):
        nested = [[FRONTAL[i], FRONTAL[i + 1]] for i in range(0, 10, 2)]
        assert _clarity(landmarks=nested).clear is True

    def test_rolled_frontal_face_is_still_frontal(self):
        # Same frontal face rotated by 90 degrees: alignment removes roll,
        # so roll alone must not make a face "unclear".
        rolled = []
        for i in range(0, 10, 2):
            x, y = FRONTAL[i] - 120.0, FRONTAL[i + 1] - 220.0
            rolled.extend([120.0 - y, 220.0 + x])
        assert _clarity(landmarks=rolled).clear is True

    def test_thresholds_are_configurable(self):
        clarity = assess_face_clarity(
            face_width=48.0,
            face_height=56.0,
            face_confidence=0.85,
            landmarks=FRONTAL,
            config=FaceClarityConfig(min_face_size=40.0),
        )
        assert clarity.clear is True


class TestIdentityRefreshPolicy:
    def test_first_sighting_is_recognized_immediately_even_if_unclear(self):
        decision = _admit(_policy(), 1000, clear=False)
        assert decision.allowed is True
        assert decision.reason == "first_sighting"

    def test_clear_identity_waits_for_refresh_interval(self):
        policy = _policy()
        _admit(policy, 1000, clear=True)
        for ts in (1500, 2000, 4000, 5999):
            decision = _admit(policy, ts, clear=True)
            assert decision.allowed is False
        assert decision.reason == "refresh_wait"
        decision = _admit(policy, 6000, clear=True)
        assert decision.allowed is True
        assert decision.reason == "refresh"

    def test_clear_identity_refreshes_only_with_a_clear_face(self):
        policy = _policy()
        _admit(policy, 1000, clear=True)
        decision = _admit(policy, 7000, clear=False)
        assert decision.allowed is False
        assert decision.reason == "refresh_not_clear"
        assert _admit(policy, 9000, clear=True).reason == "refresh"

    def test_unclear_first_shot_is_upgraded_by_the_next_clear_face(self):
        policy = _policy()
        _admit(policy, 1000, clear=False)
        too_soon = _admit(policy, 1500, clear=True)
        assert too_soon.allowed is False
        assert too_soon.reason == "min_interval"
        upgrade = _admit(policy, 2000, clear=True)
        assert upgrade.allowed is True
        assert upgrade.reason == "clear_upgrade"
        # Once a clear face is recognized, the slow refresh cadence applies.
        assert _admit(policy, 3000, clear=True).reason == "refresh_wait"
        assert _admit(policy, 7000, clear=True).reason == "refresh"

    def test_unclear_identity_does_not_retry_unclear_faces_quickly(self):
        policy = _policy()
        _admit(policy, 1000, clear=False)
        decision = _admit(policy, 3000, clear=False)
        assert decision.allowed is False
        assert decision.reason == "await_clear"

    def test_unclear_identity_still_refreshes_at_the_slow_cadence(self):
        # Low-resolution cameras may never produce a "clear" face; they must
        # not go silent after the first recognition.
        policy = _policy()
        _admit(policy, 1000, clear=False)
        decision = _admit(policy, 6000, clear=False)
        assert decision.allowed is True
        assert decision.reason == "unclear_refresh"

    def test_tracks_and_cameras_are_independent(self):
        policy = _policy()
        _admit(policy, 1000, clear=True, key="cam1:src1:42")
        assert _admit(policy, 1200, clear=True, key="cam1:src1:43").reason == "first_sighting"
        assert _admit(policy, 1200, clear=True, key="cam2:src2:42").reason == "first_sighting"

    def test_time_going_backwards_starts_over(self):
        policy = _policy()
        _admit(policy, 50_000, clear=True)
        decision = _admit(policy, 1_000, clear=True)
        assert decision.allowed is True
        assert decision.reason == "time_reset"

    def test_refresh_disabled_restores_plain_min_interval(self):
        policy = _policy(refresh_ms=0)
        _admit(policy, 1000, clear=True)
        assert _admit(policy, 1500, clear=True).allowed is False
        decision = _admit(policy, 2000, clear=False)
        assert decision.allowed is True
        assert decision.reason == "min_interval_elapsed"

    def test_refresh_never_shorter_than_min_interval(self):
        config = IdentityRefreshConfig(min_interval_ms=3000, refresh_ms=2000)
        assert config.effective_refresh_ms == 3000

    def test_decide_does_not_change_state(self):
        policy = _policy()
        assert policy.decide(KEY, 1000, True).allowed is True
        assert policy.decide(KEY, 1100, True).reason == "first_sighting"

    def test_state_is_pruned_after_ttl(self):
        policy = _policy(state_ttl_ms=10_000)
        for track in range(100):
            _admit(policy, 1000, clear=True, key=f"cam1:src1:{track}")
        assert policy.tracked_count == 100
        _admit(policy, 20_000, clear=True, key="cam1:src1:500")
        assert policy.tracked_count == 1

    def test_pruning_is_per_source_time_domain(self):
        # Sources can have unrelated PTS clocks; one source's time must not
        # evict another source's tracks.
        policy = _policy(state_ttl_ms=10_000)
        _admit(policy, 1_000, clear=True, key="cam1:src1:1")
        _admit(policy, 9_000_000, clear=True, key="cam2:src2:1")
        assert policy.tracked_count == 2
        assert _admit(policy, 2_000, clear=True, key="cam1:src1:1").reason == "refresh_wait"

    def test_pruned_track_is_recognized_again(self):
        policy = _policy(state_ttl_ms=10_000)
        _admit(policy, 1000, clear=True)
        decision = _admit(policy, 40_000, clear=False)
        assert decision.allowed is True


class TestIdentityRefreshConfig:
    def test_defaults(self):
        config = IdentityRefreshConfig.from_env({})
        assert config.min_interval_ms == 1000
        assert config.refresh_ms == 5000
        assert config.clarity.min_face_size == 64.0
        assert config.clarity.min_confidence == 0.6
        assert config.clarity.max_yaw_ratio == 0.35
        assert config.state_ttl_ms >= config.effective_refresh_ms

    def test_env_overrides(self):
        config = IdentityRefreshConfig.from_env(
            {
                "FACE_REID_MIN_INTERVAL_MS": "500",
                "FACE_IDENTITY_REFRESH_MS": "8000",
                "FACE_CLEAR_MIN_SIZE_PX": "48",
                "FACE_CLEAR_MIN_CONFIDENCE": "0.7",
                "FACE_CLEAR_MAX_YAW_RATIO": "0.25",
            }
        )
        assert config.min_interval_ms == 500
        assert config.refresh_ms == 8000
        assert config.clarity.min_face_size == 48.0
        assert config.clarity.min_confidence == 0.7
        assert config.clarity.max_yaw_ratio == 0.25

    def test_explicit_values_win_over_env(self):
        config = IdentityRefreshConfig.from_env(
            {"FACE_IDENTITY_REFRESH_MS": "8000"},
            min_interval_ms=2000,
            refresh_ms=3000,
        )
        assert config.min_interval_ms == 2000
        assert config.refresh_ms == 3000

    def test_bad_values_fall_back_to_defaults(self):
        config = IdentityRefreshConfig.from_env({"FACE_IDENTITY_REFRESH_MS": "soon"})
        assert config.refresh_ms == 5000

    def test_zero_disables_refresh_policy(self):
        config = IdentityRefreshConfig.from_env({"FACE_IDENTITY_REFRESH_MS": "0"})
        assert config.refresh_ms == 0
        assert config.refresh_enabled is False


class TestReIDThrottleMapIsBounded:
    def test_old_track_entries_are_pruned(self):
        throttle = ReIDThrottleMap(min_interval_ms=1000, state_ttl_ms=10_000)
        for track in range(200):
            throttle.record(f"cam1:src1:{track}", 1000)
        throttle.record("cam1:src1:999", 30_000)
        assert throttle.tracked_count == 1

    def test_default_behavior_is_unchanged(self):
        throttle = ReIDThrottleMap(min_interval_ms=1000)
        throttle.record(KEY, 1000)
        assert throttle.is_allowed(KEY, 1500) is False
        assert throttle.is_allowed(KEY, 2000) is True
