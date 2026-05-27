"""Face ReID gate — pure Python quality gate + throttle for ReID eligibility.

Evaluates whether a face detection with embedding is good enough for
downstream Redis storage / vector search.  No GPU, no Savant, no DB.

Reuses quality scoring from face_quality.py.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ReIDGateInput:
    """Inputs for ReID gate evaluation."""

    face_confidence: float = 0.0
    face_width: float = 0.0
    face_height: float = 0.0
    landmarks: Optional[List[float]] = None
    person_track_id: int = 0
    has_track_id: bool = False
    feature: Optional[List[float]] = None
    feature_dim: int = 0
    embedding_norm: float = 0.0
    camera_id: str = ""
    source_id: str = ""
    timestamp_ms: int = 0
    association_method: str = ""


@dataclass
class ReIDGateResult:
    """Result of ReID gate evaluation."""

    allowed: bool = False
    quality_score: float = 0.0
    skip_reason: Optional[str] = None
    throttle_key: str = ""
    next_allowed_at_ms: Optional[int] = None


_DEFAULT_MIN_CONFIDENCE = 0.6
_DEFAULT_MIN_FACE_SIZE = 40.0
_DEFAULT_MIN_INTERVAL_MS = 1000
_DEFAULT_NORM_TOLERANCE = 0.10
_EXPECTED_FEATURE_DIM = 512


def evaluate_reid_gate(
    inp: ReIDGateInput,
    config: Optional[Dict[str, Any]] = None,
) -> ReIDGateResult:
    """Evaluate whether a face is eligible for ReID / Redis.

    Gate rules (evaluated in order, first failure wins):

    1. Require associated person_track_id > 0.
    2. Require face confidence >= threshold.
    3. Require face bbox min width/height >= threshold.
    4. Require landmarks = 5 points (10 floats).
    5. Require feature dim = 512.
    6. Require embedding norm in [1.0 - tol, 1.0 + tol].
    7. Reject NaN feature values.

    Quality score is a composite in [0, 1] based on confidence and size.

    Args:
        inp: Face detection + embedding inputs.
        config: Optional overrides.

    Returns:
        ReIDGateResult with allowed/skip verdict.
    """
    cfg = config or {}
    min_conf = cfg.get("face_reid_min_confidence", _DEFAULT_MIN_CONFIDENCE)
    min_size = cfg.get("face_reid_min_face_size", _DEFAULT_MIN_FACE_SIZE)
    norm_tol = cfg.get("face_reid_norm_tolerance", _DEFAULT_NORM_TOLERANCE)

    throttle_key = f"{inp.camera_id}:{inp.source_id}:{inp.person_track_id}"

    # Rule 1: person_track_id required
    if not inp.has_track_id or inp.person_track_id <= 0:
        return ReIDGateResult(
            allowed=False,
            quality_score=0.0,
            skip_reason="no_track_id",
            throttle_key=throttle_key,
        )

    # Rule 2: confidence
    if inp.face_confidence < min_conf:
        return ReIDGateResult(
            allowed=False,
            quality_score=_quality_score(inp, min_conf, min_size),
            skip_reason="low_confidence",
            throttle_key=throttle_key,
        )

    # Rule 3: face size
    if inp.face_width < min_size or inp.face_height < min_size:
        return ReIDGateResult(
            allowed=False,
            quality_score=_quality_score(inp, min_conf, min_size),
            skip_reason="face_too_small",
            throttle_key=throttle_key,
        )

    # Rule 4: landmarks (expect 5 points = 10 floats, or list of 5 pairs)
    lm = inp.landmarks
    if lm is None:
        return ReIDGateResult(
            allowed=False,
            quality_score=_quality_score(inp, min_conf, min_size),
            skip_reason="no_landmarks",
            throttle_key=throttle_key,
        )
    lm_count = len(lm)
    # Accept 10 floats (5 points x 2 coords) or 5 items (list of pairs)
    if lm_count not in (5, 10, 15):
        return ReIDGateResult(
            allowed=False,
            quality_score=_quality_score(inp, min_conf, min_size),
            skip_reason="bad_landmarks",
            throttle_key=throttle_key,
        )

    # Rule 5: feature dim
    feat = inp.feature
    if feat is None or len(feat) == 0:
        return ReIDGateResult(
            allowed=False,
            quality_score=_quality_score(inp, min_conf, min_size),
            skip_reason="no_feature",
            throttle_key=throttle_key,
        )
    if len(feat) != _EXPECTED_FEATURE_DIM:
        return ReIDGateResult(
            allowed=False,
            quality_score=_quality_score(inp, min_conf, min_size),
            skip_reason=f"wrong_feature_dim_{len(feat)}",
            throttle_key=throttle_key,
        )

    # Rule 6: embedding norm tolerance
    norm = inp.embedding_norm
    if norm < (1.0 - norm_tol) or norm > (1.0 + norm_tol):
        return ReIDGateResult(
            allowed=False,
            quality_score=_quality_score(inp, min_conf, min_size),
            skip_reason=f"bad_norm_{norm:.3f}",
            throttle_key=throttle_key,
        )

    # Rule 7: NaN feature values
    if any(math.isnan(x) for x in feat):
        return ReIDGateResult(
            allowed=False,
            quality_score=_quality_score(inp, min_conf, min_size),
            skip_reason="nan_feature",
            throttle_key=throttle_key,
        )

    return ReIDGateResult(
        allowed=True,
        quality_score=_quality_score(inp, min_conf, min_size),
        skip_reason=None,
        throttle_key=throttle_key,
    )


def _quality_score(
    inp: ReIDGateInput,
    min_conf: float,
    min_size: float,
) -> float:
    """Composite quality score in [0, 1].

    confidence contributes 0.6, size contributes 0.4.
    """
    conf_score = min(inp.face_confidence / max(min_conf, 0.01), 1.0)
    w_score = min(inp.face_width / max(min_size, 1.0), 1.0)
    h_score = min(inp.face_height / max(min_size, 1.0), 1.0)
    size_score = min(w_score, h_score)
    score = conf_score * 0.6 + size_score * 0.4
    return max(0.0, min(score, 1.0))


class ReIDThrottleMap:
    """In-memory per-camera per-track throttle.

    Tracks the last allowed timestamp_ms for each throttle_key.
    """

    def __init__(self, min_interval_ms: int = _DEFAULT_MIN_INTERVAL_MS):
        self._min_interval_ms = max(min_interval_ms, 0)
        self._last_allowed: Dict[str, int] = {}

    def is_allowed(self, throttle_key: str, timestamp_ms: int) -> bool:
        """Check if this throttle_key is allowed at timestamp_ms."""
        last = self._last_allowed.get(throttle_key)
        if last is None:
            return True
        return (timestamp_ms - last) >= self._min_interval_ms

    def record(self, throttle_key: str, timestamp_ms: int) -> None:
        """Record that this throttle_key was allowed at timestamp_ms."""
        self._last_allowed[throttle_key] = timestamp_ms

    def next_allowed_at(self, throttle_key: str) -> Optional[int]:
        """Return the earliest next allowed timestamp, or None."""
        last = self._last_allowed.get(throttle_key)
        if last is None:
            return None
        return last + self._min_interval_ms

    def clear(self) -> None:
        """Reset all throttle state."""
        self._last_allowed.clear()
