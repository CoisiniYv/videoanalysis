"""Fenced Replay side-effect orchestration for Clip Coordinator V2."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping, Protocol

from app.contracts import (
    AckDisposition,
    CrashPoint,
    DeliveryEnvelope,
    ProcessingCode,
    ProcessingOutcome,
    ReplayPlan,
    ReplaySlotTiming,
    ReplaySubmission,
    ReplaySubmissionCode,
)
from app.replay_admission_repository import ReplaySlotReservation


class ReplayAdmissionPort(Protocol):
    def load(self, event_id: str) -> ReplaySlotReservation | None: ...

    def acquire_fenced(self, **kwargs: object) -> ReplaySlotReservation | None: ...

    def takeover(
        self,
        reservation: ReplaySlotReservation,
        *,
        owner: str,
        delivery_id: str,
    ) -> ReplaySlotReservation | None: ...

    def mark_submitting(
        self,
        reservation: ReplaySlotReservation,
        *,
        replay_job_request: dict | None = None,
    ) -> bool: ...

    def mark_uncertain(
        self,
        reservation: ReplaySlotReservation,
        *,
        reason: str,
    ) -> bool: ...

    def abort_permanent(
        self,
        reservation: ReplaySlotReservation,
        *,
        reason: str,
        diagnostics: dict | None = None,
    ) -> bool: ...

    def commit_handoff(
        self,
        reservation: ReplaySlotReservation,
        *,
        replay_job_id: str,
        resulting_stream_id: str,
        replay_job_request: dict | None,
        diagnostics: dict | None,
        replay_shard: dict | None,
    ) -> bool: ...


class ReplayTransport(Protocol):
    def submit_plan(self, plan: ReplayPlan) -> ReplaySubmission: ...

    def recover_submission(
        self,
        *,
        slot_token: str,
        resulting_stream_id: str,
    ) -> ReplaySubmission | None: ...


class CrashInjector(Protocol):
    def hit(
        self,
        point: CrashPoint,
        *,
        delivery: DeliveryEnvelope,
        outcome: ProcessingOutcome | None = None,
    ) -> None: ...


class NoopCrashInjector:
    def hit(
        self,
        point: CrashPoint,
        *,
        delivery: DeliveryEnvelope,
        outcome: ProcessingOutcome | None = None,
    ) -> None:
        del point, delivery, outcome


@dataclass(frozen=True)
class FencedReplayRequest:
    delivery: DeliveryEnvelope
    event_id: str
    request_id: str
    owner: str
    slot_token: str
    source_id: str
    camera_id: str
    replay_shard: Mapping[str, object]
    sink_instance: str
    plan: ReplayPlan
    timing: ReplaySlotTiming
    max_global: int
    max_per_shard: int
    max_per_source: int
    diagnostics: Mapping[str, object] = field(default_factory=dict)

    @property
    def resulting_stream_id(self) -> str:
        configuration = self.plan.payload().get("configuration")
        if not isinstance(configuration, dict):
            return ""
        return str(configuration.get("resulting_stream_id") or "")


@dataclass
class FencedReplayExecutor:
    admission: ReplayAdmissionPort
    transport: ReplayTransport
    crash_injector: CrashInjector = field(default_factory=NoopCrashInjector)

    def execute(self, request: FencedReplayRequest) -> ProcessingOutcome:
        reservation = self.admission.load(request.event_id)
        if reservation is not None and reservation.slot_status == "active":
            existing = self._existing_active_outcome(request, reservation)
            if isinstance(existing, ProcessingOutcome):
                return existing
            reservation = existing
        elif reservation is not None and reservation.slot_status in {
            "released",
            "timeout",
        }:
            return ProcessingOutcome(
                code=ProcessingCode.RETRY_PENDING,
                ack_disposition=AckDisposition.HOLD,
                durable=True,
                event_id=request.event_id,
                request_id=request.request_id,
                reason=f"replay_slot_{reservation.slot_status}",
                slot_token=reservation.token,
            )
        else:
            reservation = self.admission.acquire_fenced(
                event_id=request.event_id,
                owner=request.owner,
                slot_token=request.slot_token,
                request_id=request.request_id,
                delivery_id=request.delivery.message_id,
                plan_hash=request.plan.plan_hash,
                source_id=request.source_id,
                camera_id=request.camera_id,
                replay_shard=dict(request.replay_shard),
                sink_instance=request.sink_instance,
                replay_duration_seconds_effective=(
                    request.timing.replay_duration_seconds_effective
                ),
                replay_duration_effective_reason=(
                    request.timing.replay_duration_effective_reason
                ),
                timeout_budget_s=request.timing.timeout_budget_s,
                max_global=request.max_global,
                max_per_shard=request.max_per_shard,
                max_per_source=request.max_per_source,
            )
            if reservation is None:
                return self._hold(
                    request,
                    code=ProcessingCode.TRANSIENT_FAILURE,
                    reason="fenced_replay_admission_unavailable",
                )
            if not reservation.acquired:
                code = (
                    ProcessingCode.DUPLICATE_TERMINAL
                    if reservation.reason == "replay_slot_terminal_state"
                    else ProcessingCode.CAPACITY_PENDING
                )
                return ProcessingOutcome(
                    code=code,
                    ack_disposition=(
                        AckDisposition.ACK
                        if code is ProcessingCode.DUPLICATE_TERMINAL
                        else AckDisposition.HOLD
                    ),
                    durable=code is ProcessingCode.DUPLICATE_TERMINAL,
                    event_id=request.event_id,
                    request_id=request.request_id,
                    reason=reservation.reason or "fenced_replay_admission_denied",
                    slot_token=reservation.token,
                    diagnostics=tuple(reservation.quota_decision),
                )

        self.crash_injector.hit(
            CrashPoint.BEFORE_REPLAY_CREATE,
            delivery=request.delivery,
        )
        if not self.admission.mark_submitting(
            reservation,
            replay_job_request=request.plan.payload(),
        ):
            return self._hold(
                request,
                reason="stale_replay_slot_before_create",
                slot_token=reservation.token,
            )
        reservation = replace(reservation, create_state="submitting")

        submission = self.transport.submit_plan(request.plan)
        provisional = ProcessingOutcome(
            code=(
                ProcessingCode.REPLAY_CREATED
                if submission.code is ReplaySubmissionCode.CREATED
                else ProcessingCode.TRANSIENT_FAILURE
            ),
            ack_disposition=AckDisposition.HOLD,
            durable=False,
            event_id=request.event_id,
            request_id=request.request_id,
            reason=submission.reason,
            replay_job_id=submission.job_id,
            slot_token=reservation.token,
        )
        self.crash_injector.hit(
            CrashPoint.AFTER_REPLAY_RESPONSE,
            delivery=request.delivery,
            outcome=provisional,
        )
        if submission.code is ReplaySubmissionCode.PERMANENT_REJECTED:
            reason = submission.reason or submission.code.value
            diagnostics = {
                **dict(request.diagnostics),
                "replay_plan_hash": request.plan.plan_hash,
                "replay_slot_owner": reservation.owner,
                "replay_slot_token": reservation.token,
                "replay_slot_generation": reservation.generation,
                "replay_submission_code": submission.code.value,
                "replay_submission_reason": reason,
            }
            persisted = self.admission.abort_permanent(
                reservation,
                reason=reason,
                diagnostics=diagnostics,
            )
            return ProcessingOutcome(
                code=ProcessingCode.PERMANENT_FAILURE,
                ack_disposition=(
                    AckDisposition.ACK if persisted else AckDisposition.HOLD
                ),
                durable=persisted,
                event_id=request.event_id,
                request_id=request.request_id,
                reason=reason,
                slot_token=reservation.token,
                diagnostics=tuple(sorted(diagnostics.items())),
            )
        if submission.code is not ReplaySubmissionCode.CREATED:
            persisted = self.admission.mark_uncertain(
                reservation,
                reason=submission.reason or submission.code.value,
            )
            return ProcessingOutcome(
                code=ProcessingCode.TRANSIENT_FAILURE,
                ack_disposition=AckDisposition.HOLD,
                durable=persisted,
                event_id=request.event_id,
                request_id=request.request_id,
                reason=submission.reason or submission.code.value,
                slot_token=reservation.token,
            )
        return self._commit_submission(request, reservation, submission)

    def _existing_active_outcome(
        self,
        request: FencedReplayRequest,
        reservation: ReplaySlotReservation,
    ) -> ProcessingOutcome | ReplaySlotReservation:
        if reservation.replay_job_id and reservation.create_state == "committed":
            return ProcessingOutcome(
                code=ProcessingCode.REPLAY_CREATED,
                ack_disposition=AckDisposition.ACK,
                durable=True,
                event_id=request.event_id,
                request_id=request.request_id,
                reason="replay_handoff_already_committed",
                replay_job_id=reservation.replay_job_id,
                slot_token=reservation.token,
            )
        if reservation.token and reservation.token != request.slot_token:
            return self._hold(
                request,
                reason="replay_slot_token_mismatch",
                slot_token=reservation.token,
            )
        if reservation.plan_hash and reservation.plan_hash != request.plan.plan_hash:
            return self._hold(
                request,
                reason="replay_plan_hash_mismatch",
                slot_token=reservation.token,
            )
        if not request.delivery.reclaimed:
            return self._hold(
                request,
                code=ProcessingCode.CAPACITY_PENDING,
                reason="replay_slot_owned_by_active_delivery",
                slot_token=reservation.token,
                durable=True,
            )
        winner = self.admission.takeover(
            reservation,
            owner=request.owner,
            delivery_id=request.delivery.message_id,
        )
        if winner is None:
            return self._hold(
                request,
                reason="replay_slot_takeover_lost",
                slot_token=reservation.token,
            )
        if winner.create_state in {"submitting", "uncertain"}:
            recovered = self.transport.recover_submission(
                slot_token=winner.token,
                resulting_stream_id=request.resulting_stream_id,
            )
            if recovered is None:
                persisted = self.admission.mark_uncertain(
                    winner,
                    reason="replay_response_unresolved",
                )
                return self._hold(
                    request,
                    reason="replay_response_unresolved",
                    slot_token=winner.token,
                    durable=persisted,
                )
            return self._commit_submission(request, winner, recovered)
        return winner

    def _commit_submission(
        self,
        request: FencedReplayRequest,
        reservation: ReplaySlotReservation,
        submission: ReplaySubmission,
    ) -> ProcessingOutcome:
        self.crash_injector.hit(
            CrashPoint.BEFORE_DURABLE_COMMIT,
            delivery=request.delivery,
            outcome=ProcessingOutcome(
                code=ProcessingCode.REPLAY_CREATED,
                ack_disposition=AckDisposition.HOLD,
                durable=False,
                event_id=request.event_id,
                request_id=request.request_id,
                replay_job_id=submission.job_id,
                slot_token=reservation.token,
            ),
        )
        request_payload = request.plan.payload()
        diagnostics = {
            **dict(request.diagnostics),
            "replay_plan_hash": request.plan.plan_hash,
            "replay_slot_owner": reservation.owner,
            "replay_slot_token": reservation.token,
            "replay_slot_generation": reservation.generation,
            "replay_submission_recovered": (
                submission.reason == "recovered_active_replay_job"
            ),
        }
        committed = self.admission.commit_handoff(
            reservation,
            replay_job_id=submission.job_id,
            resulting_stream_id=(
                submission.resulting_stream_id or request.resulting_stream_id
            ),
            replay_job_request=request_payload,
            diagnostics=diagnostics,
            replay_shard=dict(request.replay_shard),
        )
        outcome = ProcessingOutcome(
            code=ProcessingCode.REPLAY_CREATED,
            ack_disposition=(
                AckDisposition.ACK if committed else AckDisposition.HOLD
            ),
            durable=committed,
            event_id=request.event_id,
            request_id=request.request_id,
            reason=("" if committed else "stale_replay_slot_durable_commit"),
            replay_job_id=submission.job_id,
            slot_token=reservation.token,
            diagnostics=tuple(sorted(diagnostics.items())),
        )
        self.crash_injector.hit(
            CrashPoint.AFTER_DURABLE_COMMIT,
            delivery=request.delivery,
            outcome=outcome,
        )
        return outcome

    @staticmethod
    def _hold(
        request: FencedReplayRequest,
        *,
        reason: str,
        code: ProcessingCode = ProcessingCode.TRANSIENT_FAILURE,
        slot_token: str = "",
        durable: bool = False,
    ) -> ProcessingOutcome:
        return ProcessingOutcome(
            code=code,
            ack_disposition=AckDisposition.HOLD,
            durable=durable,
            event_id=request.event_id,
            request_id=request.request_id,
            reason=reason,
            slot_token=slot_token,
        )
