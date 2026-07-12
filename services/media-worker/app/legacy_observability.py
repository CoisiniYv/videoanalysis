"""Behavior-preserving observability helpers for the legacy media worker."""

from __future__ import annotations

from typing import Any, Mapping


CORRELATION_SCHEMA_VERSION = "clip-media-correlation-v1"
GUARD_ATTRIBUTION_SCHEMA_VERSION = "media-guard-attribution-v1"


def materialization_correlation(
    event_context: Mapping[str, Any],
    *,
    phase_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Carry clip correlation into one media finalization job.

    Legacy materialization has no fenced lease yet.  Recording that explicitly
    avoids inventing an owner/token while still making the missing contract
    visible in Phase 0 artifacts.
    """

    payload = event_context.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    media = payload.get("media")
    media = media if isinstance(media, dict) else {}
    diagnostics = media.get("evidence_diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    inherited = diagnostics.get("correlation")
    inherited = inherited if isinstance(inherited, dict) else {}
    phase = dict(phase_diagnostics or {})

    event_id = str(event_context.get("event_id") or inherited.get("event_id") or "")
    runtime_epoch_id = str(
        event_context.get("runtime_epoch_id")
        or payload.get("runtime_epoch_id")
        or media.get("runtime_epoch_id")
        or inherited.get("runtime_epoch_id")
        or ""
    )
    replay_job_id = str(
        media.get("replay_job_id")
        or inherited.get("replay_job_id")
        or ""
    )
    attempt_marker = str(
        phase.get("attempt_id")
        or phase.get("finalizer_submitted_at")
        or phase.get("sink_metadata_first_seen_at")
        or "legacy"
    )
    return {
        "schema_version": CORRELATION_SCHEMA_VERSION,
        "event_id": event_id,
        "request_id": str(
            inherited.get("request_id")
            or media.get("request_id")
            or media.get("legacy_record_request_id")
            or ""
        ),
        "attempt_id": f"media:{event_id}:{attempt_marker}",
        "attempt_number": inherited.get("attempt_number"),
        "lease_token": None,
        "lease_state": "not_applicable_legacy",
        "replay_job_id": replay_job_id or None,
        "runtime_epoch_id": runtime_epoch_id,
        "stream_session_id": str(
            media.get("stream_session_id")
            or inherited.get("stream_session_id")
            or ""
        ),
        "source_id": str(
            event_context.get("source_id")
            or inherited.get("source_id")
            or ""
        ),
    }


def guard_failure_attribution(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Identify the actual guard behind the legacy aggregate clip status."""

    time_window = summary.get("time_window")
    time_window = time_window if isinstance(time_window, dict) else {}
    checks = (
        (
            "duration",
            bool(summary.get("duration_guard_failed")),
            summary.get("duration_guard_reason"),
        ),
        (
            "runtime_epoch",
            bool(summary.get("epoch_guard_failed")),
            summary.get("epoch_guard_reason"),
        ),
        (
            "sink_window",
            bool(summary.get("sink_window_guard_failed")),
            summary.get("sink_window_guard_reason"),
        ),
        (
            "time_domain_crop",
            bool(
                summary.get("time_domain_crop_failed")
                or time_window.get("time_domain_crop_failed")
            ),
            "time_domain_crop_failed",
        ),
    )
    failures = [
        {"category": category, "reason": str(reason or f"{category}_guard_failed")}
        for category, failed, reason in checks
        if failed
    ]
    primary = failures[0] if failures else {}
    return {
        "schema_version": GUARD_ATTRIBUTION_SCHEMA_VERSION,
        "failed": bool(failures),
        "primary_category": primary.get("category"),
        "primary_reason": primary.get("reason"),
        "failures": failures,
    }
