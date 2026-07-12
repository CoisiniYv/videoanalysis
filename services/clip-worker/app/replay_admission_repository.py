"""Replay admission port for Clip Coordinator V2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app import repository


@dataclass(frozen=True)
class ReplaySlotReservation:
    event_id: str
    acquired: bool
    reason: str
    counts: tuple[tuple[str, int], ...]
    quota_decision: tuple[tuple[str, object], ...]
    error_message: str = ""

    def counts_dict(self) -> dict[str, int]:
        return dict(self.counts)

    def quota_dict(self) -> dict[str, object]:
        return dict(self.quota_decision)


class ReplayAdmissionRepository:
    """Named adapter around the schema-compatible Replay slot repository."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def active_counts(self, *, shard_id: str, source_id: str) -> dict[str, int] | None:
        return repository.active_replay_slot_counts(
            self.connection,
            shard_id=shard_id,
            source_id=source_id,
        )

    def acquire(self, **kwargs: Any) -> ReplaySlotReservation | None:
        result = repository.try_acquire_replay_slot(self.connection, **kwargs)
        if result is None:
            return None
        counts = result.get("counts")
        counts = counts if isinstance(counts, dict) else {}
        quota = result.get("quota_decision")
        quota = quota if isinstance(quota, dict) else {}
        return ReplaySlotReservation(
            event_id=str(kwargs.get("event_id") or ""),
            acquired=bool(result.get("acquired")),
            reason=str(result.get("reason") or ""),
            counts=tuple(
                sorted((str(key), int(value or 0)) for key, value in counts.items())
            ),
            quota_decision=tuple(sorted((str(key), value) for key, value in quota.items())),
            error_message=str(result.get("error_message") or ""),
        )

    def record_job(
        self,
        reservation: ReplaySlotReservation,
        *,
        replay_job_id: str,
        resulting_stream_id: str,
    ) -> bool:
        if not reservation.acquired:
            return False
        return repository.record_replay_job_for_slot(
            self.connection,
            event_id=reservation.event_id,
            replay_job_id=replay_job_id,
            resulting_stream_id=resulting_stream_id,
        )

    def release(
        self,
        reservation: ReplaySlotReservation,
        *,
        reason: str,
    ) -> bool:
        if not reservation.acquired:
            return False
        return repository.release_replay_slot(
            self.connection,
            event_id=reservation.event_id,
            release_reason=reason,
        )
