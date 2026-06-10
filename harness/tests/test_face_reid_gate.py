"""Unit tests for face_reid_gate service (pure Python, no GPU / Savant)."""

import math
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from modules.savant_security.custom.services.face_reid_gate import (
    ReIDGateInput,
    ReIDGateResult,
    ReIDThrottleMap,
    evaluate_reid_gate,
)


def _make_input(**kwargs) -> ReIDGateInput:
    """Create a ReIDGateInput with sensible defaults, overridden by kwargs."""
    defaults = dict(
        face_confidence=0.8,
        face_width=60.0,
        face_height=60.0,
        landmarks=[100.0, 200.0, 150.0, 200.0, 125.0, 230.0, 110.0, 240.0, 140.0, 240.0],
        person_track_id=42,
        has_track_id=True,
        feature=[0.01] * 512,
        feature_dim=512,
        embedding_norm=1.0,
        camera_id="cam1",
        source_id="primary_rtsp",
        timestamp_ms=1000,
    )
    defaults.update(kwargs)

    # Recompute derived fields if not explicitly overridden
    if "feature" in kwargs and "feature_dim" not in kwargs:
        defaults["feature_dim"] = len(kwargs["feature"])
    if "feature" in kwargs and "embedding_norm" not in kwargs:
        feat = kwargs["feature"]
        defaults["embedding_norm"] = math.sqrt(sum(x * x for x in feat)) if feat else 0.0

    return ReIDGateInput(**defaults)


class TestValidFaceAllowed:
    def test_valid_face_allowed(self):
        inp = _make_input()
        result = evaluate_reid_gate(inp)
        assert result.allowed is True
        assert result.skip_reason is None

    def test_quality_score_in_range(self):
        inp = _make_input()
        result = evaluate_reid_gate(inp)
        assert 0.0 <= result.quality_score <= 1.0

    def test_throttle_key_format(self):
        inp = _make_input(
            camera_id="cam_001", source_id="primary_rtsp", person_track_id=391,
        )
        result = evaluate_reid_gate(inp)
        assert result.throttle_key == "cam_001:primary_rtsp:391"


class TestMissingTrackId:
    def test_no_track_id_rejected(self):
        inp = _make_input(has_track_id=False, person_track_id=0)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert result.skip_reason == "no_track_id"

    def test_zero_track_id_rejected(self):
        inp = _make_input(person_track_id=0, has_track_id=True)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert result.skip_reason == "no_track_id"


class TestLowConfidence:
    def test_low_confidence_rejected(self):
        inp = _make_input(face_confidence=0.3)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert result.skip_reason == "low_confidence"

    def test_custom_threshold(self):
        inp = _make_input(face_confidence=0.7)
        result = evaluate_reid_gate(inp, {"face_reid_min_confidence": 0.8})
        assert result.allowed is False
        assert result.skip_reason == "low_confidence"

    def test_at_threshold_allowed(self):
        inp = _make_input(face_confidence=0.6)
        result = evaluate_reid_gate(inp)
        assert result.allowed is True


class TestFaceTooSmall:
    def test_too_narrow_rejected(self):
        inp = _make_input(face_width=30.0, face_height=60.0)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert result.skip_reason == "face_too_small"

    def test_too_short_rejected(self):
        inp = _make_input(face_width=60.0, face_height=30.0)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert result.skip_reason == "face_too_small"

    def test_at_min_size_allowed(self):
        inp = _make_input(face_width=40.0, face_height=40.0)
        result = evaluate_reid_gate(inp)
        assert result.allowed is True


class TestLandmarks:
    def test_no_landmarks_rejected(self):
        inp = _make_input(landmarks=None)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert result.skip_reason == "no_landmarks"

    def test_bad_landmarks_count_rejected(self):
        inp = _make_input(landmarks=[1.0, 2.0, 3.0])
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert result.skip_reason == "bad_landmarks"

    def test_10_floats_allowed(self):
        inp = _make_input(landmarks=[1.0] * 10)
        result = evaluate_reid_gate(inp)
        assert result.allowed is True

    def test_5_pairs_allowed(self):
        inp = _make_input(landmarks=[[1.0, 2.0]] * 5)
        result = evaluate_reid_gate(inp)
        assert result.allowed is True


class TestFeatureDim:
    def test_wrong_dim_rejected(self):
        inp = _make_input(feature=[0.01] * 256)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert "wrong_feature_dim" in result.skip_reason

    def test_no_feature_rejected(self):
        inp = _make_input(feature=None, feature_dim=0, embedding_norm=0.0)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert result.skip_reason == "no_feature"

    def test_empty_feature_rejected(self):
        inp = _make_input(feature=[], feature_dim=0, embedding_norm=0.0)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert result.skip_reason == "no_feature"


class TestEmbeddingNorm:
    def test_norm_too_low_rejected(self):
        feat = [0.001] * 512
        norm = math.sqrt(sum(x * x for x in feat))
        inp = _make_input(feature=feat, embedding_norm=norm)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert "bad_norm" in result.skip_reason

    def test_norm_too_high_rejected(self):
        feat = [10.0] * 512
        norm = math.sqrt(sum(x * x for x in feat))
        inp = _make_input(feature=feat, embedding_norm=norm)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert "bad_norm" in result.skip_reason

    def test_norm_1_0_allowed(self):
        feat = [1.0 / math.sqrt(512)] * 512
        norm = math.sqrt(sum(x * x for x in feat))
        inp = _make_input(feature=feat, embedding_norm=norm)
        result = evaluate_reid_gate(inp)
        assert result.allowed is True

    def test_custom_tolerance(self):
        # norm = 0.85, default tolerance 0.10 -> rejected (below 0.90)
        inp = _make_input(embedding_norm=0.85)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        # wider tolerance 0.20 -> allowed (above 0.80)
        result2 = evaluate_reid_gate(inp, {"face_reid_norm_tolerance": 0.20})
        assert result2.allowed is True


class TestNanFeature:
    def test_nan_feature_rejected(self):
        feat = [0.01] * 512
        feat[100] = float("nan")
        inp = _make_input(feature=feat)
        result = evaluate_reid_gate(inp)
        assert result.allowed is False
        assert result.skip_reason == "nan_feature"


class TestQualityScore:
    def test_score_range(self):
        inp = _make_input()
        result = evaluate_reid_gate(inp)
        assert 0.0 <= result.quality_score <= 1.0

    def test_higher_confidence_higher_score(self):
        r1 = evaluate_reid_gate(_make_input(face_confidence=0.6))
        r2 = evaluate_reid_gate(_make_input(face_confidence=0.9))
        assert r2.quality_score >= r1.quality_score

    def test_larger_face_higher_score(self):
        r1 = evaluate_reid_gate(_make_input(face_width=40, face_height=40))
        r2 = evaluate_reid_gate(_make_input(face_width=100, face_height=100))
        assert r2.quality_score >= r1.quality_score


class TestThrottleMap:
    def test_first_observation_allowed(self):
        throttle = ReIDThrottleMap(min_interval_ms=1000)
        assert throttle.is_allowed("cam1:42", 1000) is True

    def test_repeated_observation_blocked(self):
        throttle = ReIDThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        assert throttle.is_allowed("cam1:42", 1500) is False

    def test_after_interval_allowed(self):
        throttle = ReIDThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        assert throttle.is_allowed("cam1:42", 2001) is True

    def test_separate_track_not_blocked(self):
        throttle = ReIDThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        assert throttle.is_allowed("cam1:99", 1000) is True

    def test_separate_camera_not_blocked(self):
        throttle = ReIDThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        assert throttle.is_allowed("cam2:42", 1000) is True

    def test_next_allowed_at(self):
        throttle = ReIDThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        assert throttle.next_allowed_at("cam1:42") == 2000
        assert throttle.next_allowed_at("cam1:99") is None

    def test_clear_resets(self):
        throttle = ReIDThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        throttle.clear()
        assert throttle.is_allowed("cam1:42", 1001) is True
