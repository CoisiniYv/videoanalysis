"""C1G.1 alert-policy cooldown contract tests."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any


EW_DIR = str(Path(__file__).resolve().parents[2] / "services" / "event-worker")
if EW_DIR not in sys.path:
    sys.path.insert(0, EW_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.alert_policy import AlertPolicyService
from app.worker import _handle_event


class FakeConsumer:
    def __init__(self) -> None:
        self.acked: list[str] = []

    def ack(self, msg_id: str) -> bool:
        self.acked.append(msg_id)
        return True


class FakeAlertPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, event: dict[str, Any], event_id: str | None = None) -> str:
        self.events.append({"event": event, "event_id": event_id})
        return "alert-msg-1"


class FakeRecordPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def has_request(self, source_event_id: str, recording_strategy: str) -> bool:
        return False

    def publish(self, event: dict[str, Any], event_id: str) -> str:
        self.events.append({"event": event, "event_id": event_id})
        return "record-msg-1"


class FakeEventRepository:
    def __init__(self) -> None:
        self.events_by_id: dict[str, dict[str, Any]] = {}
        self.events_by_source_id: dict[str, str] = {}
        self.evidence_tasks = 0
        self.clip_status_updates = 0
        self.policy = {
            "global_alert_cooldown_s": 30,
            "store_suppressed_events": True,
            "suppress_record_request": True,
            "critical_bypass": False,
        }

    def insert_event(self, event: dict[str, Any]) -> str | None:
        source_event_id = event["source_event_id"]
        if source_event_id in self.events_by_source_id:
            return None
        event_id = str(uuid.uuid4())
        self.events_by_id[event_id] = {
            **event,
            "id": event_id,
            "status": "new",
            "payload": dict(event.get("payload") or {}),
        }
        self.events_by_source_id[source_event_id] = event_id
        return event_id

    def get_camera_alert_policy(self, camera_id: str) -> dict[str, Any]:
        return dict(self.policy)

    def get_last_unsuppressed_alert_ts_ms(
        self,
        camera_id: str,
        *,
        exclude_source_event_id: str,
        current_event_ts_ms: int,
    ) -> int | None:
        candidates = [
            event["event_ts_ms"]
            for event in self.events_by_id.values()
            if event["camera_id"] == camera_id
            and event["source_event_id"] != exclude_source_event_id
            and event.get("status") != "suppressed"
            and event["event_ts_ms"] <= current_event_ts_ms
        ]
        return max(candidates) if candidates else None

    def mark_event_suppressed(
        self,
        event_id: str,
        *,
        reason: str,
        policy: dict[str, Any],
        last_alert_ts_ms: int | None,
    ) -> bool:
        event = self.events_by_id[event_id]
        event["status"] = "suppressed"
        event.setdefault("payload", {})["alert_policy"] = {
            "decision": "suppressed",
            "reason": reason,
            "policy": policy,
            "last_alert_ts_ms": last_alert_ts_ms,
        }
        return True

    def get_media_clip_status(self, source_event_id: str) -> str | None:
        return "not_implemented"

    def create_evidence_task(self, event: dict[str, Any], event_id: str) -> str:
        self.evidence_tasks += 1
        return f"task-{event_id}"

    def set_clip_status(self, event_id: str, status: str) -> bool:
        self.clip_status_updates += 1
        return True


def _event(source_event_id: str, event_ts_ms: int) -> dict[str, Any]:
    return {
        "source_event_id": source_event_id,
        "event_type": "watchlist_hit",
        "camera_id": "cam_c1g1_test",
        "source_id": "c1e_rtsp_replay",
        "track_id": "10",
        "event_ts_ms": event_ts_ms,
        "start_ts_ms": event_ts_ms,
        "end_ts_ms": event_ts_ms,
        "severity": "medium",
        "confidence": 0.8,
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": {"snapshot_required": True, "clip_required": True},
        "payload": {
            "media": {
                "source_id": "c1e_rtsp_replay",
                "clip_required": True,
            }
        },
    }


def test_camera_global_cooldown_suppresses_second_event_and_skips_record_request():
    repo = FakeEventRepository()
    service = AlertPolicyService(repo)
    consumer = FakeConsumer()
    alert_publisher = FakeAlertPublisher()
    record_publisher = FakeRecordPublisher()

    first_new, first_event_id = _handle_event(
        _event("c1g1:first", 100_000),
        "1-0",
        repo,
        consumer,
        alert_publisher,
        record_publisher,
        alert_policy_service=service,
    )
    assert first_new is True
    assert first_event_id is not None
    assert len(alert_publisher.events) == 1
    assert len(record_publisher.events) == 1
    assert repo.evidence_tasks == 1

    second_new, second_event_id = _handle_event(
        _event("c1g1:second", 110_000),
        "2-0",
        repo,
        consumer,
        alert_publisher,
        record_publisher,
        alert_policy_service=service,
    )
    assert second_new is True
    assert second_event_id is not None
    assert len(alert_publisher.events) == 1
    assert len(record_publisher.events) == 1
    assert repo.evidence_tasks == 1
    assert repo.events_by_id[second_event_id]["status"] == "suppressed"
    assert (
        repo.events_by_id[second_event_id]["payload"]["alert_policy"]["reason"]
        == "camera_global_cooldown"
    )
    assert consumer.acked == ["1-0", "2-0"]


def test_zero_cooldown_emits_every_event():
    repo = FakeEventRepository()
    repo.policy["global_alert_cooldown_s"] = 0
    service = AlertPolicyService(repo)
    assert service.decide(_event("c1g1:first", 100_000)).decision == "emit"
