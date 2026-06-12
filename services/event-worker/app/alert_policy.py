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
    cooldown_scope: str = "algorithm"
    cooldown_key: str = ""
    last_alert_event_type: str | None = None
    last_alert_algorithm_type: str | None = None

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
        event_type: str = "",
        algorithm_type: str = "",
        cooldown_scope: str = "algorithm",
    ) -> int | None:
        ...

    def get_last_unsuppressed_alert(
        self,
        camera_id: str,
        *,
        exclude_source_event_id: str,
        current_event_ts_ms: int,
        event_type: str = "",
        algorithm_type: str = "",
        cooldown_scope: str = "algorithm",
    ) -> dict[str, Any] | None:
        ...

    def mark_event_suppressed(
        self,
        event_id: str,
        *,
        reason: str,
        policy: dict[str, Any],
        last_alert_ts_ms: int | None,
        cooldown_scope: str = "algorithm",
        cooldown_key: str = "",
        last_alert_event_type: str | None = None,
        last_alert_algorithm_type: str | None = None,
    ) -> bool:
        ...


class AlertPolicyService:
    def __init__(self, repo: AlertPolicyRepository) -> None:
        self._repo = repo

    def decide(self, event: dict[str, Any]) -> AlertPolicyDecision:
        camera_id = str(event.get("camera_id") or "")
        policy = self._repo.get_camera_alert_policy(camera_id) if camera_id else {}
        cooldown_s = int(policy.get("global_alert_cooldown_s") or 0)
        cooldown_scope = _cooldown_scope(policy)
        event_type = str(event.get("event_type") or "")
        algorithm_type = str(event.get("algorithm_type") or "")
        cooldown_key = _cooldown_key(
            event_type=event_type,
            algorithm_type=algorithm_type,
            cooldown_scope=cooldown_scope,
        )
        if cooldown_s <= 0:
            return AlertPolicyDecision(
                "emit",
                cooldown_s=0,
                cooldown_scope=cooldown_scope,
                cooldown_key=cooldown_key,
            )

        if policy.get("critical_bypass") and event.get("severity") == "critical":
            return AlertPolicyDecision(
                "emit",
                cooldown_s=cooldown_s,
                cooldown_scope=cooldown_scope,
                cooldown_key=cooldown_key,
            )

        event_ts_ms = int(event.get("event_ts_ms") or event.get("start_ts_ms") or 0)
        last_alert_ts_ms = None
        last_alert = None
        if hasattr(self._repo, "get_last_unsuppressed_alert"):
            last_alert = self._repo.get_last_unsuppressed_alert(
                camera_id,
                exclude_source_event_id=str(event.get("source_event_id") or ""),
                current_event_ts_ms=event_ts_ms,
                event_type=event_type,
                algorithm_type=algorithm_type,
                cooldown_scope=cooldown_scope,
            )
            if last_alert and last_alert.get("event_ts_ms") is not None:
                last_alert_ts_ms = int(last_alert["event_ts_ms"])
        else:
            last_alert_ts_ms = self._repo.get_last_unsuppressed_alert_ts_ms(
                camera_id,
                exclude_source_event_id=str(event.get("source_event_id") or ""),
                current_event_ts_ms=event_ts_ms,
                event_type=event_type,
                algorithm_type=algorithm_type,
                cooldown_scope=cooldown_scope,
            )
        last_event_type = (
            str(last_alert.get("event_type") or "") if isinstance(last_alert, dict) else ""
        )
        last_algorithm_type = (
            str(last_alert.get("algorithm_type") or "")
            if isinstance(last_alert, dict)
            else ""
        )
        if (
            last_alert_ts_ms is not None
            and event_ts_ms > 0
            and event_ts_ms - last_alert_ts_ms < cooldown_s * 1000
        ):
            if (
                cooldown_scope == "global"
                and isinstance(last_alert, dict)
                and _is_identity_event(event_type, algorithm_type)
                and not _is_identity_event(last_event_type, last_algorithm_type)
            ):
                return AlertPolicyDecision(
                    "emit",
                    cooldown_s=cooldown_s,
                    last_alert_ts_ms=last_alert_ts_ms,
                    cooldown_scope=cooldown_scope,
                    cooldown_key=cooldown_key,
                    last_alert_event_type=last_event_type or None,
                    last_alert_algorithm_type=last_algorithm_type or None,
                )
            return AlertPolicyDecision(
                "suppress",
                reason=f"camera_{cooldown_scope}_cooldown",
                cooldown_s=cooldown_s,
                last_alert_ts_ms=last_alert_ts_ms,
                cooldown_scope=cooldown_scope,
                cooldown_key=cooldown_key,
                last_alert_event_type=last_event_type or None,
                last_alert_algorithm_type=last_algorithm_type or None,
            )
        return AlertPolicyDecision(
            "emit",
            cooldown_s=cooldown_s,
            last_alert_ts_ms=last_alert_ts_ms,
            cooldown_scope=cooldown_scope,
            cooldown_key=cooldown_key,
            last_alert_event_type=last_event_type or None,
            last_alert_algorithm_type=last_algorithm_type or None,
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
                cooldown_scope=decision.cooldown_scope,
                cooldown_key=decision.cooldown_key,
                last_alert_event_type=decision.last_alert_event_type,
                last_alert_algorithm_type=decision.last_alert_algorithm_type,
            )
            payload = event.setdefault("payload", {})
            if isinstance(payload, dict):
                payload["alert_policy"] = {
                    "decision": "suppressed",
                    "reason": decision.reason,
                    "cooldown_s": decision.cooldown_s,
                    "cooldown_scope": decision.cooldown_scope,
                    "cooldown_key": decision.cooldown_key,
                    "last_alert_ts_ms": decision.last_alert_ts_ms,
                    "last_alert_event_type": decision.last_alert_event_type,
                    "last_alert_algorithm_type": decision.last_alert_algorithm_type,
                }
        return decision


def _cooldown_scope(policy: dict[str, Any]) -> str:
    scope = str(policy.get("cooldown_scope") or "algorithm").strip().lower()
    if scope in {"global", "camera", "camera_global"}:
        return "global"
    if scope in {"event", "event_type"}:
        return "event_type"
    if scope in {"algorithm", "algorithm_type"}:
        return "algorithm"
    return "algorithm"


def _cooldown_key(
    *,
    event_type: str,
    algorithm_type: str,
    cooldown_scope: str,
) -> str:
    if cooldown_scope == "global":
        return "global"
    if cooldown_scope == "event_type":
        return event_type
    return algorithm_type or event_type


def _is_identity_event(event_type: str, algorithm_type: str) -> bool:
    return event_type in {"watchlist_hit", "live_search_hit"} or algorithm_type in {
        "face_intelligence",
        "face.watchlist",
        "face.live_search",
    }
