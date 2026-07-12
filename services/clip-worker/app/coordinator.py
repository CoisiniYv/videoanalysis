"""Side-effect ordering boundary for Clip Coordinator V2."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import logging
import os
from typing import Protocol

from app.contracts import (
    AckDisposition,
    CrashPoint,
    DeliveryEnvelope,
    ProcessingCode,
    ProcessingOutcome,
)
from app.request_consumer import RequestConsumer


logger = logging.getLogger(__name__)


class DeliveryProcessor(Protocol):
    def __call__(self, delivery: DeliveryEnvelope) -> ProcessingOutcome: ...


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


class InjectedCoordinatorCrash(RuntimeError):
    """Test-only crash signal that must escape the coordinator boundary."""


@dataclass(frozen=True)
class RuntimeOnceCrashInjector:
    """Opt-in process crash used only by audited restart/reclaim canaries."""

    point: CrashPoint | None = None
    marker_path: str = ""
    exit_code: int = 91

    @classmethod
    def from_env(cls) -> "RuntimeOnceCrashInjector":
        raw_point = os.getenv("CLIP_WORKER_CRASH_INJECT_POINT", "").strip()
        if not raw_point:
            return cls()
        try:
            point = CrashPoint(raw_point)
        except ValueError as exc:
            raise RuntimeError(
                f"invalid CLIP_WORKER_CRASH_INJECT_POINT={raw_point!r}"
            ) from exc
        marker = os.getenv("CLIP_WORKER_CRASH_INJECT_MARKER", "").strip()
        if not marker:
            marker = f"/tmp/clip-worker-crash-{point.value}.once"
        try:
            exit_code = int(
                os.getenv("CLIP_WORKER_CRASH_INJECT_EXIT_CODE", "91") or 91
            )
        except ValueError as exc:
            raise RuntimeError(
                "CLIP_WORKER_CRASH_INJECT_EXIT_CODE must be an integer"
            ) from exc
        return cls(point=point, marker_path=marker, exit_code=exit_code)

    def hit(
        self,
        point: CrashPoint,
        *,
        delivery: DeliveryEnvelope,
        outcome: ProcessingOutcome | None = None,
    ) -> None:
        if self.point is None or point is not self.point:
            return
        try:
            marker_fd = os.open(
                self.marker_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            return
        marker = (
            f"point={point.value} message_id={delivery.message_id} "
            f"event_id={(outcome.event_id if outcome else '')} "
            f"job_id={(outcome.replay_job_id if outcome else '')}\n"
        ).encode("utf-8", errors="replace")
        try:
            os.write(marker_fd, marker)
        finally:
            os.close(marker_fd)
        os.write(2, b"clip_worker_runtime_crash_injected " + marker)
        os._exit(max(1, min(int(self.exit_code), 255)))


class AckPolicy:
    """The only Coordinator V2 decision for acknowledging a delivery."""

    @staticmethod
    def should_ack(outcome: ProcessingOutcome) -> bool:
        return (
            outcome.ack_disposition is AckDisposition.ACK
            and outcome.durable
        )


@dataclass
class ClipCoordinator:
    consumer: RequestConsumer
    processor: DeliveryProcessor
    crash_injector: CrashInjector = field(default_factory=NoopCrashInjector)

    def process_one(self, delivery: DeliveryEnvelope) -> ProcessingOutcome:
        try:
            outcome = self.processor(delivery)
        except InjectedCoordinatorCrash:
            raise
        except Exception as exc:
            logger.exception(
                "clip_coordinator_process_failed message_id=%s",
                delivery.message_id,
            )
            return ProcessingOutcome(
                code=ProcessingCode.UNEXPECTED_FAILURE,
                ack_disposition=AckDisposition.HOLD,
                durable=False,
                reason=f"{type(exc).__name__}:{exc}",
            )
        if (
            outcome.code is ProcessingCode.MALFORMED
            and outcome.ack_disposition is AckDisposition.ACK
            and not outcome.durable
        ):
            outcome = replace(
                outcome,
                durable=self.consumer.quarantine(
                    delivery,
                    reason=outcome.reason or ProcessingCode.MALFORMED.value,
                ),
            )
        self.crash_injector.hit(
            CrashPoint.BEFORE_ACK,
            delivery=delivery,
            outcome=outcome,
        )
        acked = False
        if AckPolicy.should_ack(outcome):
            acked = self.consumer.ack(delivery)
        self.crash_injector.hit(
            CrashPoint.AFTER_ACK,
            delivery=delivery,
            outcome=outcome,
        )
        return outcome.with_ack_performed(acked)

    def crash(
        self,
        point: CrashPoint,
        *,
        delivery: DeliveryEnvelope,
        outcome: ProcessingOutcome | None = None,
    ) -> None:
        self.crash_injector.hit(
            point,
            delivery=delivery,
            outcome=outcome,
        )

    def tick(self, *, reclaim: bool = False) -> list[ProcessingOutcome]:
        deliveries = self.consumer.reclaim() if reclaim else []
        if not deliveries:
            deliveries = self.consumer.read_new()
        return [self.process_one(delivery) for delivery in deliveries]
