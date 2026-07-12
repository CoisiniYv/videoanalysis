"""Behavior-preserving observability helpers for the legacy clip coordinator.

Phase 0 records enough identity to compare the legacy and V2 coordinators
without moving Redis, PostgreSQL, or Replay side effects yet.
"""

from __future__ import annotations

from typing import Any, Mapping


CORRELATION_SCHEMA_VERSION = "clip-media-correlation-v1"


def request_correlation(
    request: Mapping[str, Any],
    *,
    event_id: str,
    request_id: str,
    delivery_id: str,
    retry_count: int,
    consumer: str,
) -> dict[str, Any]:
    """Return stable correlation fields without changing request semantics."""

    attempt_number = max(0, int(retry_count)) + 1
    return {
        "schema_version": CORRELATION_SCHEMA_VERSION,
        "event_id": str(event_id or ""),
        "request_id": str(request_id or ""),
        "attempt_id": (
            f"redis:{consumer}:{delivery_id}:delivery-{attempt_number}"
        ),
        "attempt_number": attempt_number,
        "delivery_id": str(delivery_id or ""),
        "lease_token": None,
        "lease_state": "not_applicable_legacy",
        "replay_job_id": None,
        "runtime_epoch_id": str(request.get("runtime_epoch_id") or ""),
        "stream_session_id": str(request.get("stream_session_id") or ""),
        "source_id": str(request.get("source_id") or ""),
    }


def with_replay_job(
    correlation: Mapping[str, Any],
    *,
    replay_job_id: str,
) -> dict[str, Any]:
    """Return a copy carrying the Replay job created by this delivery."""

    return {**dict(correlation), "replay_job_id": str(replay_job_id or "") or None}
