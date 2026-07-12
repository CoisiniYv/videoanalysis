"""Side-effect ordering boundary for Clip Coordinator V2."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
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
