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
    owner: str = ""
    token: str = ""
    generation: int = 0
    create_state: str = ""
    replay_job_id: str = ""
    resulting_stream_id: str = ""
    plan_hash: str = ""

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
        return self._reservation_from_result(kwargs, result)

    def acquire_fenced(self, **kwargs: Any) -> ReplaySlotReservation | None:
        result = repository.try_acquire_fenced_replay_slot(
            self.connection,
            **kwargs,
        )
        if result is None:
            return None
        return self._reservation_from_result(kwargs, result)

    def takeover(
        self,
        reservation: ReplaySlotReservation,
        *,
        owner: str,
        delivery_id: str,
    ) -> ReplaySlotReservation | None:
        result = repository.takeover_fenced_replay_slot(
            self.connection,
            event_id=reservation.event_id,
            owner=owner,
            expected_token=reservation.token,
            expected_generation=reservation.generation,
            delivery_id=delivery_id,
        )
        if result is None or not bool(result.get("claimed")):
            return None
        values = {
            **result,
            "acquired": True,
            "reason": "replay_slot_taken_over",
            "counts": reservation.counts_dict(),
            "quota_decision": reservation.quota_dict(),
        }
        return self._reservation_from_result(
            {"event_id": reservation.event_id, "owner": owner},
            values,
        )

    def mark_submitting(self, reservation: ReplaySlotReservation) -> bool:
        if not reservation.acquired:
            return False
        return repository.mark_replay_create_started(
            self.connection,
            event_id=reservation.event_id,
            owner=reservation.owner,
            slot_token=reservation.token,
            slot_generation=reservation.generation,
            plan_hash=reservation.plan_hash,
        )

    def commit_handoff(
        self,
        reservation: ReplaySlotReservation,
        *,
        replay_job_id: str,
        resulting_stream_id: str,
        replay_job_request: dict | None,
        diagnostics: dict | None,
        replay_shard: dict | None,
    ) -> bool:
        if not reservation.acquired:
            return False
        return repository.commit_replay_job_handoff(
            self.connection,
            event_id=reservation.event_id,
            owner=reservation.owner,
            slot_token=reservation.token,
            slot_generation=reservation.generation,
            plan_hash=reservation.plan_hash,
            replay_job_id=replay_job_id,
            resulting_stream_id=resulting_stream_id,
            replay_job_request=replay_job_request,
            diagnostics=diagnostics,
            replay_shard=replay_shard,
        )

    def load(self, event_id: str) -> ReplaySlotReservation | None:
        result = repository.get_replay_slot_state(
            self.connection,
            event_id=event_id,
        )
        if result is None:
            return None
        values = {
            "acquired": str(result.get("replay_slot_status") or "") == "active",
            "reason": "replay_slot_loaded",
            "counts": {},
            "quota_decision": {},
            "slot_owner": result.get("replay_slot_owner"),
            "slot_token": result.get("replay_slot_token"),
            "slot_generation": result.get("replay_slot_generation"),
            "create_state": result.get("replay_create_state"),
            "replay_job_id": result.get("replay_job_id"),
            "resulting_stream_id": result.get("replay_resulting_stream_id"),
            "plan_hash": result.get("replay_plan_hash"),
        }
        return self._reservation_from_result({"event_id": event_id}, values)

    @staticmethod
    def _reservation_from_result(
        kwargs: dict[str, Any],
        result: dict[str, object],
    ) -> ReplaySlotReservation:
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
            owner=str(result.get("slot_owner") or kwargs.get("owner") or ""),
            token=str(result.get("slot_token") or kwargs.get("slot_token") or ""),
            generation=int(result.get("slot_generation") or 0),
            create_state=str(result.get("create_state") or ""),
            replay_job_id=str(result.get("replay_job_id") or ""),
            resulting_stream_id=str(result.get("resulting_stream_id") or ""),
            plan_hash=str(result.get("plan_hash") or kwargs.get("plan_hash") or ""),
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
