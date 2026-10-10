"""Per-track face identity refresh policy — decides when AdaFace runs again.

A person track is recognized once when its face first passes the minimum
ReID gate. After that the track is only recognized again when:

- the last recognition used a clear face: the refresh interval has passed and
  the current face is clear as well;
- the last recognition used an unclear face: a clear face appears (after the
  minimum per-track interval), or the refresh interval has passed.

This keeps watchlist latency for the first good face unchanged while cutting
AdaFace crops, face observations and face-worker vector searches from about
one per person per second to a few per person.

"Clear" is judged from detector output only (face size, detector confidence,
and head yaw estimated from the five landmarks), so the decision is made
before any crop or embedding is produced.

Pure Python: no Savant, GPU, Redis or numpy imports.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

DEFAULT_MIN_INTERVAL_MS = 1000
DEFAULT_REFRESH_MS = 5000
DEFAULT_STATE_TTL_MS = 60000
DEFAULT_CLEAR_MIN_FACE_SIZE = 64.0
DEFAULT_CLEAR_MIN_CONFIDENCE = 0.60
DEFAULT_CLEAR_MAX_YAW_RATIO = 0.35
# Eye distance below this fraction of the face's short side means the eyes
# have collapsed onto each other: a profile view, never frontal.
MIN_EYE_DISTANCE_RATIO = 0.20


@dataclass(frozen=True)
class FaceClarityConfig:
    min_face_size: float = DEFAULT_CLEAR_MIN_FACE_SIZE
    min_confidence: float = DEFAULT_CLEAR_MIN_CONFIDENCE
    max_yaw_ratio: float = DEFAULT_CLEAR_MAX_YAW_RATIO


@dataclass(frozen=True)
class FaceClarity:
    clear: bool
    score: float
    min_side: float
    yaw_ratio: Optional[float]
    reasons: List[str] = field(default_factory=list)


def _flatten_floats(values: Any) -> List[float]:
    out: List[float] = []
    stack = [values]
    while stack:
        item = stack.pop()
        if isinstance(item, (str, bytes)):
            raise ValueError("landmarks must be numeric")
        if hasattr(item, "__iter__"):
            stack.extend(reversed(list(item)))
        else:
            out.append(float(item))
    return out


def landmark_points(landmarks: Any) -> Optional[List[tuple[float, float]]]:
    """Return the five (x, y) landmarks, or None if the layout is unknown.

    Accepts 10 floats (x, y per point), 15 floats (x, y, score per point) or
    nested per-point sequences of either shape.
    """
    if landmarks is None:
        return None
    try:
        flat = _flatten_floats(landmarks)
    except (TypeError, ValueError):
        return None
    if len(flat) == 10:
        stride = 2
    elif len(flat) == 15:
        stride = 3
    else:
        return None
    points = [(flat[i], flat[i + 1]) for i in range(0, len(flat), stride)]
    if any(not (math.isfinite(x) and math.isfinite(y)) for x, y in points):
        return None
    return points


def yaw_ratio_from_landmarks(
    points: List[tuple[float, float]],
    min_side: float,
) -> float:
    """Nose offset from the eye midpoint along the eye axis / eye distance.

    0 for a frontal face; grows as the head turns. Measured along the eye
    axis so in-plane roll (which face alignment removes) does not count.
    Returns ``inf`` for collapsed eyes (profile view).
    """
    (lx, ly), (rx, ry), (nx, ny) = points[0], points[1], points[2]
    eye_dx, eye_dy = rx - lx, ry - ly
    eye_distance = math.hypot(eye_dx, eye_dy)
    if eye_distance <= 1e-6 or (
        min_side > 0 and eye_distance < MIN_EYE_DISTANCE_RATIO * min_side
    ):
        return math.inf
    mid_x, mid_y = (lx + rx) / 2.0, (ly + ry) / 2.0
    along = ((nx - mid_x) * eye_dx + (ny - mid_y) * eye_dy) / eye_distance
    return abs(along) / eye_distance


def assess_face_clarity(
    *,
    face_width: float,
    face_height: float,
    face_confidence: float,
    landmarks: Any,
    config: FaceClarityConfig = FaceClarityConfig(),
) -> FaceClarity:
    """Judge whether a detected face is clear enough to be worth recognizing."""
    min_side = max(0.0, min(float(face_width or 0.0), float(face_height or 0.0)))
    confidence = float(face_confidence or 0.0)
    reasons: List[str] = []
    if min_side < config.min_face_size:
        reasons.append("small_face")
    if confidence < config.min_confidence:
        reasons.append("low_confidence")

    points = landmark_points(landmarks)
    yaw: Optional[float] = None
    if points is None:
        reasons.append("no_landmarks")
    else:
        yaw = yaw_ratio_from_landmarks(points, min_side)
        if yaw > config.max_yaw_ratio:
            reasons.append("not_frontal")

    size_score = min(min_side / 112.0, 1.0)
    frontal_score = 0.0 if yaw is None or math.isinf(yaw) else max(0.0, 1.0 - yaw)
    score = max(0.0, min(1.0, size_score * 0.4 + confidence * 0.3 + frontal_score * 0.3))
    return FaceClarity(
        clear=not reasons,
        score=score,
        min_side=min_side,
        yaw_ratio=yaw,
        reasons=reasons,
    )


def _int_from(value: Any, default: int) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float_from(value: Any, default: float) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class IdentityRefreshConfig:
    # Never recognize the same track more often than this (FACE_REID_MIN_INTERVAL_MS).
    min_interval_ms: int = DEFAULT_MIN_INTERVAL_MS
    # Re-recognize an identified track after this long (FACE_IDENTITY_REFRESH_MS).
    # 0 disables the policy: plain min_interval_ms cadence, as before.
    refresh_ms: int = DEFAULT_REFRESH_MS
    # Forget a track this long after its last recognition (bounded memory).
    state_ttl_ms: int = DEFAULT_STATE_TTL_MS
    clarity: FaceClarityConfig = FaceClarityConfig()

    @property
    def refresh_enabled(self) -> bool:
        return self.refresh_ms > 0

    @property
    def effective_refresh_ms(self) -> int:
        if not self.refresh_enabled:
            return max(self.min_interval_ms, 0)
        return max(self.refresh_ms, self.min_interval_ms)

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        min_interval_ms: Optional[int] = None,
        refresh_ms: Optional[int] = None,
        clear_min_face_size: Optional[float] = None,
        clear_min_confidence: Optional[float] = None,
        clear_max_yaw_ratio: Optional[float] = None,
        state_ttl_ms: Optional[int] = None,
    ) -> "IdentityRefreshConfig":
        """Build from explicit values, falling back to env, then defaults."""
        source = os.environ if env is None else env

        def pick_int(explicit, name, default):
            if explicit is not None and str(explicit).strip() != "":
                return max(_int_from(explicit, default), 0)
            return max(_int_from(source.get(name), default), 0)

        def pick_float(explicit, name, default):
            if explicit is not None and str(explicit).strip() != "":
                return max(_float_from(explicit, default), 0.0)
            return max(_float_from(source.get(name), default), 0.0)

        min_interval = pick_int(min_interval_ms, "FACE_REID_MIN_INTERVAL_MS", DEFAULT_MIN_INTERVAL_MS)
        refresh = pick_int(refresh_ms, "FACE_IDENTITY_REFRESH_MS", DEFAULT_REFRESH_MS)
        ttl = pick_int(state_ttl_ms, "FACE_IDENTITY_STATE_TTL_MS", DEFAULT_STATE_TTL_MS)
        ttl = max(ttl, 2 * max(refresh, min_interval), 1)
        return cls(
            min_interval_ms=min_interval,
            refresh_ms=refresh,
            state_ttl_ms=ttl,
            clarity=FaceClarityConfig(
                min_face_size=pick_float(
                    clear_min_face_size, "FACE_CLEAR_MIN_SIZE_PX", DEFAULT_CLEAR_MIN_FACE_SIZE
                ),
                min_confidence=pick_float(
                    clear_min_confidence, "FACE_CLEAR_MIN_CONFIDENCE", DEFAULT_CLEAR_MIN_CONFIDENCE
                ),
                max_yaw_ratio=pick_float(
                    clear_max_yaw_ratio, "FACE_CLEAR_MAX_YAW_RATIO", DEFAULT_CLEAR_MAX_YAW_RATIO
                ),
            ),
        )


@dataclass(frozen=True)
class RefreshDecision:
    allowed: bool
    reason: str
    next_allowed_at_ms: Optional[int] = None


@dataclass
class _TrackIdentityState:
    last_recognized_ms: int
    last_clear: bool


def _scope_of(key: str) -> str:
    """Throttle keys are ``camera:source:track``; the scope is ``camera:source``."""
    head, sep, _tail = str(key).rpartition(":")
    return head if sep else ""


class IdentityRefreshPolicy:
    """Per-track recognition cadence; state is pruned per source time domain."""

    # Reasons a face is recognized, and reasons it is skipped.
    ALLOW_REASONS = (
        "first_sighting",
        "time_reset",
        "refresh",
        "clear_upgrade",
        "unclear_refresh",
        "min_interval_elapsed",
    )
    SKIP_REASONS = ("min_interval", "refresh_wait", "refresh_not_clear", "await_clear")

    def __init__(self, config: Optional[IdentityRefreshConfig] = None) -> None:
        self.config = config or IdentityRefreshConfig()
        self._tracks: Dict[str, Dict[str, _TrackIdentityState]] = {}
        self._last_prune_ms: Dict[str, int] = {}

    @property
    def tracked_count(self) -> int:
        return sum(len(tracks) for tracks in self._tracks.values())

    def assess(self, **face: Any) -> FaceClarity:
        return assess_face_clarity(config=self.config.clarity, **face)

    def decide(self, key: str, timestamp_ms: int, clear: bool) -> RefreshDecision:
        """Return whether this face should be recognized. Does not record."""
        ts = int(timestamp_ms)
        scope = _scope_of(key)
        self._maybe_prune(scope, ts)
        state = self._tracks.get(scope, {}).get(key)
        if state is None:
            return RefreshDecision(True, "first_sighting")
        if ts < state.last_recognized_ms:
            return RefreshDecision(True, "time_reset")

        cfg = self.config
        elapsed = ts - state.last_recognized_ms
        if elapsed < cfg.min_interval_ms:
            return RefreshDecision(
                False, "min_interval", state.last_recognized_ms + cfg.min_interval_ms
            )
        if not cfg.refresh_enabled:
            return RefreshDecision(True, "min_interval_elapsed")

        refresh_due = elapsed >= cfg.effective_refresh_ms
        refresh_at = state.last_recognized_ms + cfg.effective_refresh_ms
        if state.last_clear:
            if not refresh_due:
                return RefreshDecision(False, "refresh_wait", refresh_at)
            if not clear:
                return RefreshDecision(False, "refresh_not_clear")
            return RefreshDecision(True, "refresh")
        if clear:
            return RefreshDecision(True, "clear_upgrade")
        if refresh_due:
            return RefreshDecision(True, "unclear_refresh")
        return RefreshDecision(False, "await_clear", refresh_at)

    def record(self, key: str, timestamp_ms: int, clear: bool) -> None:
        """Record that this track's face was sent for recognition."""
        ts = int(timestamp_ms)
        scope = _scope_of(key)
        self._maybe_prune(scope, ts)
        self._tracks.setdefault(scope, {})[key] = _TrackIdentityState(
            last_recognized_ms=ts,
            last_clear=bool(clear),
        )

    def clear(self) -> None:
        self._tracks.clear()
        self._last_prune_ms.clear()

    def _maybe_prune(self, scope: str, ts: int) -> None:
        ttl = self.config.state_ttl_ms
        last = self._last_prune_ms.get(scope)
        if last is None:
            self._last_prune_ms[scope] = ts
            return
        if abs(ts - last) < ttl:
            return
        self._last_prune_ms[scope] = ts
        tracks = self._tracks.get(scope)
        if not tracks:
            return
        stale = [
            key
            for key, state in tracks.items()
            if abs(ts - state.last_recognized_ms) > ttl
        ]
        for key in stale:
            del tracks[key]
        if not tracks:
            del self._tracks[scope]


def count_decision(counters: Dict[str, int], decision: RefreshDecision) -> None:
    """Increment ``identity_<reason>`` in a pyfunc counter dict."""
    name = f"identity_{decision.reason}"
    counters[name] = counters.get(name, 0) + 1


def identity_counter_names() -> Iterable[str]:
    return tuple(
        f"identity_{reason}"
        for reason in IdentityRefreshPolicy.ALLOW_REASONS + IdentityRefreshPolicy.SKIP_REASONS
    )
