"""Tests for face pipeline configuration — pure Python, no DB, no network."""

import sys
from pathlib import Path

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import pytest

from custom.services.face_config import FacePipelineConfig, DEFAULT_FACE_PIPELINE_CONFIG


class TestDefaults:
    def test_default_enabled_is_false(self):
        cfg = FacePipelineConfig()
        assert cfg.enabled is False

    def test_default_min_person_height(self):
        assert FacePipelineConfig().min_person_height == 80.0

    def test_default_face_confidence_threshold(self):
        assert FacePipelineConfig().face_confidence_threshold == 0.6

    def test_default_quality_threshold(self):
        assert FacePipelineConfig().face_quality_threshold == 0.65

    def test_default_cooldown(self):
        assert FacePipelineConfig().same_track_cooldown_s == 10.0

    def test_default_emit(self):
        assert FacePipelineConfig().emit_face_observations is True

    def test_default_include_crop_path(self):
        assert FacePipelineConfig().include_crop_path is False


class TestOverride:
    def test_field_overrides(self):
        cfg = FacePipelineConfig(
            enabled=True,
            min_person_height=120.0,
            face_confidence_threshold=0.75,
            face_quality_threshold=0.8,
            same_track_cooldown_s=30.0,
        )
        assert cfg.enabled is True
        assert cfg.min_person_height == 120.0
        assert cfg.face_confidence_threshold == 0.75
        assert cfg.face_quality_threshold == 0.8
        assert cfg.same_track_cooldown_s == 30.0

    def test_enabled_false_semantics(self):
        """When enabled=False, the pipeline should skip — this is a config
        layer truth that callers check."""
        cfg = FacePipelineConfig(enabled=False)
        assert cfg.enabled is False


class TestValidation:
    def test_valid_default_passes(self):
        DEFAULT_FACE_PIPELINE_CONFIG.validate()

    def test_negative_min_person_height_raises(self):
        cfg = FacePipelineConfig(min_person_height=-1)
        with pytest.raises(ValueError, match="min_person_height"):
            cfg.validate()

    def test_negative_face_interval_raises(self):
        cfg = FacePipelineConfig(face_attempt_interval_ms=-100)
        with pytest.raises(ValueError, match="face_attempt_interval_ms"):
            cfg.validate()

    def test_confidence_threshold_out_of_range_raises(self):
        cfg = FacePipelineConfig(face_confidence_threshold=1.5)
        with pytest.raises(ValueError, match="face_confidence_threshold"):
            cfg.validate()

    def test_quality_threshold_out_of_range_raises(self):
        cfg = FacePipelineConfig(face_quality_threshold=-0.1)
        with pytest.raises(ValueError, match="face_quality_threshold"):
            cfg.validate()

    def test_negative_cooldown_raises(self):
        cfg = FacePipelineConfig(same_track_cooldown_s=-5)
        with pytest.raises(ValueError, match="same_track_cooldown_s"):
            cfg.validate()

    def test_negative_min_face_width_raises(self):
        cfg = FacePipelineConfig(min_face_width=-1)
        with pytest.raises(ValueError, match="min_face_width"):
            cfg.validate()

    def test_negative_min_face_height_raises(self):
        cfg = FacePipelineConfig(min_face_height=-1)
        with pytest.raises(ValueError, match="min_face_height"):
            cfg.validate()


class TestZeroThresholdIsValid:
    def test_zero_confidence_threshold_passes(self):
        cfg = FacePipelineConfig(face_confidence_threshold=0.0)
        cfg.validate()

    def test_zero_quality_passes(self):
        cfg = FacePipelineConfig(face_quality_threshold=0.0)
        cfg.validate()
