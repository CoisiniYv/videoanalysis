"""Face pipeline configuration dataclass with defaults.

F0 only defines the config shape.  F1 will read from cameras.generated.yml
or equivalent runtime config.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FacePipelineConfig:
    """Per-camera face pipeline configuration.

    All thresholds are conservative defaults suitable for initial
    deployment.  Values must be calibrated against field data.
    """

    enabled: bool = False
    """Master switch.  When ``False`` the face pipeline is skipped entirely."""

    min_person_height: float = 80.0
    """Minimum person bbox height (px) to attempt head ROI extraction."""

    face_attempt_interval_ms: int = 1000
    """Minimum interval between face detection attempts per camera (ms)."""

    min_face_width: float = 24.0
    """Minimum face width (px) for quality evaluation."""

    min_face_height: float = 24.0
    """Minimum face height (px) for quality evaluation."""

    face_confidence_threshold: float = 0.6
    """Minimum face detection confidence to consider a detection valid."""

    face_quality_threshold: float = 0.65
    """Minimum composite quality score to pass the quality filter."""

    same_track_cooldown_s: float = 10.0
    """Cooldown (seconds) before re-emitting a face observation for the
    same track."""

    emit_face_observations: bool = True
    """When ``True``, emit FaceObservation events to Redis Stream."""

    include_crop_path: bool = False
    """When ``True``, include crop_path in emitted events (requires media
    worker integration)."""

    def validate(self) -> None:
        """Raise ``ValueError`` if any field is invalid."""
        if self.min_person_height < 0:
            raise ValueError("min_person_height must be >= 0")
        if self.face_attempt_interval_ms < 0:
            raise ValueError("face_attempt_interval_ms must be >= 0")
        if self.min_face_width < 0:
            raise ValueError("min_face_width must be >= 0")
        if self.min_face_height < 0:
            raise ValueError("min_face_height must be >= 0")
        if not (0.0 <= self.face_confidence_threshold <= 1.0):
            raise ValueError("face_confidence_threshold must be in [0.0, 1.0]")
        if not (0.0 <= self.face_quality_threshold <= 1.0):
            raise ValueError("face_quality_threshold must be in [0.0, 1.0]")
        if self.same_track_cooldown_s < 0:
            raise ValueError("same_track_cooldown_s must be >= 0")


DEFAULT_FACE_PIPELINE_CONFIG = FacePipelineConfig()
