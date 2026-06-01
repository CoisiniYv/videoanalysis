"""Camera alert-policy decisions for event-worker.

The policy is intentionally applied after the SecurityEvent is persisted and
before alert/record-request publication. Suppressed events remain diagnosable
in PostgreSQL but do not create alerts or Replay evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AlertPolicyDecision:
    decision: str
    reason: str | None = None
    cooldown_s: int = 0
    last_alert_ts_ms: int | None = None

    @property
    def suppressed(self) -> bool:
        return self.decision == "suppress"


class AlertPolicyRepository(Protocol):
    def get_camera_alert_policy(self, camera_id: str) -> dict[str, Any]:
        ...

    def get_last_unsuppressed_alert_ts_ms(
        self,
        camera_id: str,
        *,
        exclude_source_event_id: str,
        current_event_ts_ms: int,
    ) -> int | None:
        ...

    def mark_event_suppressed(
        self,
        event_id: str,
        *,
        reason: str,
        policy: dict[str, Any],
        last_alert_ts_ms: int | None,
    ) -> bool:
        ...


class AlertPolicyService:
    def __init__(self, repo: AlertPolicyRepository) -> None:
        self._repo = repo

    def decide(self, event: dict[str, Any]) -> AlertPolicyDecision:
        camera_id = str(event.get("camera_id") or "")
        policy = self._repo.get_camera_alert_policy(camera_id) if camera_id else {}
        cooldown_s = int(policy.get("global_alert_cooldown_s") or 0)
        if cooldown_s <= 0:
            return AlertPolicyDecision("emit", cooldown_s=0)

        if policy.get("critical_bypass") and event.get("severity") == "critical":
            return AlertPolicyDecision("emit", cooldown_s=cooldown_s)

        event_ts_ms = int(event.get("event_ts_ms") or event.get("start_ts_ms") or 0)
        last_alert_ts_ms = self._repo.get_last_unsuppressed_alert_ts_ms(
            camera_id,
            exclude_source_event_id=str(event.get("source_event_id") or ""),
            current_event_ts_ms=event_ts_ms,
        )
        if (
            last_alert_ts_ms is not None
            and event_ts_ms > 0
            and event_ts_ms - last_alert_ts_ms < cooldown_s * 1000
        ):
            return AlertPolicyDecision(
                "suppress",
                reason="camera_global_cooldown",
                cooldown_s=cooldown_s,
                last_alert_ts_ms=last_alert_ts_ms,
            )
        return AlertPolicyDecision(
            "emit", cooldown_s=cooldown_s, last_alert_ts_ms=last_alert_ts_ms
        )

    def apply(self, event: dict[str, Any], event_id: str) -> AlertPolicyDecision:
        decision = self.decide(event)
        if decision.suppressed:
            policy = self._repo.get_camera_alert_policy(str(event.get("camera_id") or ""))
            self._repo.mark_event_suppressed(
                event_id,
                reason=decision.reason or "suppressed",
                policy=policy,
                last_alert_ts_ms=decision.last_alert_ts_ms,
            )
            payload = event.setdefault("payload", {})
            if isinstance(payload, dict):
                payload["alert_policy"] = {
                    "decision": "suppressed",
                    "reason": decision.reason,
                    "cooldown_s": decision.cooldown_s,
                    "last_alert_ts_ms": decision.last_alert_ts_ms,
                }
        return decision
