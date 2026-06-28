"""production production sidecar policy helpers.

The policy is deliberately small and env-driven. The default is disabled and
the only write mode accepted in production is ``sidecar_only``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


DEFAULT_SIDECAR_CONFIG = {
    "enabled": False,
    "event_types": {"watchlist_hit"},
    "require_trigger_face": True,
    "write_mode": "sidecar_only",
    "fail_open": True,
    "lookback_count": 10000,
    "range_count": 2000,
    "max_scan": 20000,
    "stream_session_filter_mode": "strict",
    "max_events_per_run": 5,
    "annotations_filename": "annotations.frame_cache.identity.jsonl",
    "summary_filename": "summary.frame_cache.identity.json",
    "stream_name": "security.frame_annotations",
    "redis_url": "redis://redis:6379/0",
    "freshness_guard_mode": "wall_clock",
    "require_event_centered": True,
    "canonical_min_duration_seconds": 8.0,
    "canonical_max_duration_seconds": 12.5,
    "canonical_expected_event_t_s": 5.0,
    "canonical_event_center_tolerance_seconds": 0.75,
    "max_row_age_before_event_seconds": None,
    "max_row_age_after_event_seconds": None,
    "write_dropped_debug_sidecar": False,
}


@dataclass
class FrameCacheSidecarRunState:
    """In-memory per-process sidecar counters for bounded runtime behavior."""

    sidecar_attempted: int = 0
    sidecar_written: int = 0
    sidecar_skipped_disabled: int = 0
    sidecar_skipped_event_type: int = 0
    sidecar_skipped_max_events: int = 0
    sidecar_missing_frame_metadata: int = 0
    sidecar_missing_trigger_face: int = 0
    sidecar_errors: int = 0
    _events_seen: set[str] = field(default_factory=set)

    def can_attempt(self, config: dict[str, Any], *, event_id: str | None) -> tuple[bool, str | None]:
        max_events = int(config.get("max_events_per_run") or 0)
        if max_events <= 0:
            return False, "max_events_per_run_zero"
        if event_id and event_id in self._events_seen:
            return False, "event_already_attempted"
        if self.sidecar_attempted >= max_events:
            self.sidecar_skipped_max_events += 1
            return False, "max_events_per_run_reached"
        if event_id:
            self._events_seen.add(event_id)
        self.sidecar_attempted += 1
        return True, None

    def as_dict(self) -> dict[str, int]:
        return {
            "sidecar_attempted": self.sidecar_attempted,
            "sidecar_written": self.sidecar_written,
            "sidecar_skipped_disabled": self.sidecar_skipped_disabled,
            "sidecar_skipped_event_type": self.sidecar_skipped_event_type,
            "sidecar_skipped_max_events": self.sidecar_skipped_max_events,
            "sidecar_missing_frame_metadata": self.sidecar_missing_frame_metadata,
            "sidecar_missing_trigger_face": self.sidecar_missing_trigger_face,
            "sidecar_errors": self.sidecar_errors,
        }


def load_frame_cache_sidecar_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Load production sidecar config from env with safe defaults."""

    source = env if env is not None else os.environ
    config = dict(DEFAULT_SIDECAR_CONFIG)
    config.update(
        {
            "enabled": _boolish(source.get("FRAME_CACHE_SIDECAR_ENABLED"), False),
            "event_types": _csv_set(
                source.get("FRAME_CACHE_SIDECAR_EVENT_TYPES"),
                default={"watchlist_hit"},
            ),
            "require_trigger_face": _boolish(
                source.get("FRAME_CACHE_SIDECAR_REQUIRE_TRIGGER_FACE"),
                True,
            ),
            "write_mode": str(
                source.get("FRAME_CACHE_SIDECAR_WRITE_MODE")
                or config["write_mode"]
            ),
            "fail_open": _boolish(source.get("FRAME_CACHE_SIDECAR_FAIL_OPEN"), True),
            "lookback_count": _positive_int(
                source.get("FRAME_CACHE_SIDECAR_LOOKBACK_COUNT"),
                int(config["lookback_count"]),
            ),
            "range_count": _positive_int(
                source.get("FRAME_CACHE_SIDECAR_RANGE_COUNT"),
                int(config["range_count"]),
            ),
            "max_scan": _positive_int(
                source.get("FRAME_CACHE_SIDECAR_MAX_SCAN"),
                int(config["max_scan"]),
            ),
            "stream_session_filter_mode": str(
                source.get("FRAME_CACHE_SIDECAR_STREAM_SESSION_FILTER_MODE")
                or config["stream_session_filter_mode"]
            ).strip().lower(),
            "max_events_per_run": _positive_int(
                source.get("FRAME_CACHE_SIDECAR_MAX_EVENTS_PER_RUN"),
                int(config["max_events_per_run"]),
            ),
            "annotations_filename": str(
                source.get("FRAME_CACHE_SIDECAR_OUTPUT_ANNOTATIONS")
                or config["annotations_filename"]
            ),
            "summary_filename": str(
                source.get("FRAME_CACHE_SIDECAR_OUTPUT_SUMMARY")
                or config["summary_filename"]
            ),
            "stream_name": str(
                source.get("FRAME_CACHE_SIDECAR_STREAM")
                or source.get("FRAME_ANNOTATION_STREAM")
                or config["stream_name"]
            ),
            "redis_url": str(source.get("REDIS_URL") or config["redis_url"]),
            "freshness_guard_mode": str(
                source.get("FRAME_CACHE_FRESHNESS_GUARD_MODE")
                or config["freshness_guard_mode"]
            ),
            "require_event_centered": _boolish(
                source.get("FRAME_CACHE_REQUIRE_EVENT_CENTERED"),
                bool(config["require_event_centered"]),
            ),
            "canonical_min_duration_seconds": _positive_float(
                source.get("FRAME_CACHE_CANONICAL_MIN_DURATION_SECONDS"),
                float(config["canonical_min_duration_seconds"]),
            ),
            "canonical_max_duration_seconds": _positive_float(
                source.get("FRAME_CACHE_CANONICAL_MAX_DURATION_SECONDS"),
                float(config["canonical_max_duration_seconds"]),
            ),
            "canonical_expected_event_t_s": _positive_float(
                source.get("FRAME_CACHE_CANONICAL_EXPECTED_EVENT_T_S"),
                float(config["canonical_expected_event_t_s"]),
            ),
            "canonical_event_center_tolerance_seconds": _positive_float(
                source.get("FRAME_CACHE_CANONICAL_EVENT_CENTER_TOLERANCE_SECONDS"),
                float(config["canonical_event_center_tolerance_seconds"]),
            ),
            "max_row_age_before_event_seconds": _optional_positive_float(
                source.get("FRAME_CACHE_MAX_ROW_AGE_BEFORE_EVENT_SECONDS"),
                config["max_row_age_before_event_seconds"],
            ),
            "max_row_age_after_event_seconds": _optional_positive_float(
                source.get("FRAME_CACHE_MAX_ROW_AGE_AFTER_EVENT_SECONDS"),
                config["max_row_age_after_event_seconds"],
            ),
            "write_dropped_debug_sidecar": _boolish(
                source.get("FRAME_CACHE_WRITE_DROPPED_DEBUG_SIDECAR"),
                False,
            ),
        }
    )
    config["production_replacement"] = config["write_mode"] != "sidecar_only"
    return config


def should_attempt_sidecar(
    event: dict[str, Any],
    config: dict[str, Any],
    state: FrameCacheSidecarRunState | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Return whether a sidecar should be attempted and a decision summary."""

    event_type = str(event.get("event_type") or "")
    event_id = str(event.get("event_id") or event.get("id") or "")
    base = {
        "sidecar_enabled": bool(config.get("enabled")),
        "sidecar_mode": config.get("write_mode"),
        "event_type": event_type,
        "event_id": event_id or None,
        "allowed_event_types": sorted(config.get("event_types") or []),
        "max_events_per_run": int(config.get("max_events_per_run") or 0),
    }
    if not config.get("enabled"):
        if state is not None:
            state.sidecar_skipped_disabled += 1
        return False, {**base, "reason": "disabled"}
    if config.get("write_mode") != "sidecar_only":
        if state is not None:
            state.sidecar_errors += 1
        return False, {**base, "reason": "invalid_write_mode"}
    if event_type not in set(config.get("event_types") or []):
        if state is not None:
            state.sidecar_skipped_event_type += 1
        return False, {**base, "reason": "event_type_not_allowed"}
    if state is not None:
        allowed, reason = state.can_attempt(config, event_id=event_id)
        if not allowed:
            return False, {**base, "reason": reason}
    return True, {**base, "reason": "allowed"}


def _boolish(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _optional_positive_float(value: Any, default: float | None) -> float | None:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if parsed > 0 else float(default)


def _csv_set(value: str | None, *, default: set[str]) -> set[str]:
    if not value:
        return set(default)
    result = {item.strip() for item in value.split(",") if item.strip()}
    return result or set(default)
