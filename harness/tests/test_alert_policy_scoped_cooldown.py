"""Scoped alert-policy cooldown contracts."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVENT_WORKER_DIR = str(ROOT / "services" / "event-worker")
if EVENT_WORKER_DIR not in sys.path:
    sys.path.insert(0, EVENT_WORKER_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.alert_policy import AlertPolicyService
from app.record_request import build_record_request
from app.repository import EventRepository
from app.worker import _handle_event


class _Repo:
    def __init__(self) -> None:
        self.policy: dict[str, Any] = {
            "global_alert_cooldown_s": 30,
            "store_suppressed_events": True,
            "suppress_record_request": True,
            "critical_bypass": False,
        }
        self.events_by_id: dict[str, dict[str, Any]] = {}
        self.events_by_source_id: dict[str, str] = {}
        self.evidence_tasks: list[tuple[dict[str, Any], str]] = []
        self.clip_status_by_event_id: dict[str, str] = {}

    def insert_event(self, event: dict[str, Any]) -> str | None:
        source_event_id = event["source_event_id"]
        if source_event_id in self.events_by_source_id:
            return None
        event_id = str(uuid.uuid4())
        self.events_by_source_id[source_event_id] = event_id
        self.events_by_id[event_id] = {
            **event,
            "id": event_id,
            "status": "new",
            "payload": dict(event.get("payload") or {}),
        }
        return event_id

    def get_camera_alert_policy(self, _camera_id: str) -> dict[str, Any]:
        return dict(self.policy)

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
        row = self.get_last_unsuppressed_alert(
            camera_id,
            exclude_source_event_id=exclude_source_event_id,
            current_event_ts_ms=current_event_ts_ms,
            event_type=event_type,
            algorithm_type=algorithm_type,
            cooldown_scope=cooldown_scope,
        )
        return int(row["event_ts_ms"]) if row else None

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
        def matches_scope(event: dict[str, Any]) -> bool:
            if cooldown_scope == "global":
                return True
            if cooldown_scope == "event_type":
                return event.get("event_type") == event_type
            return (
                event.get("algorithm_type")
                or event.get("event_type")
            ) == (algorithm_type or event_type)

        candidates = [
            event
            for event in self.events_by_id.values()
            if event["camera_id"] == camera_id
            and event["source_event_id"] != exclude_source_event_id
            and event.get("status") != "suppressed"
            and int(event["event_ts_ms"]) <= current_event_ts_ms
            and matches_scope(event)
        ]
        if not candidates:
            return None
        event = max(candidates, key=lambda candidate: int(candidate["event_ts_ms"]))
        return {
            "event_ts_ms": int(event["event_ts_ms"]),
            "event_type": event.get("event_type", ""),
            "algorithm_type": event.get("algorithm_type", ""),
        }

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
        event = self.events_by_id[event_id]
        event["status"] = "suppressed"
        event.setdefault("payload", {})["alert_policy"] = {
            "decision": "suppressed",
            "reason": reason,
            "policy": policy,
            "last_alert_ts_ms": last_alert_ts_ms,
            "cooldown_scope": cooldown_scope,
            "cooldown_key": cooldown_key,
            "last_alert_event_type": last_alert_event_type,
            "last_alert_algorithm_type": last_alert_algorithm_type,
        }
        return True

    def create_evidence_task(self, event: dict[str, Any], event_id: str) -> str:
        self.evidence_tasks.append((event, event_id))
        return f"task-{event_id}"

    def get_evidence_task_status(self, _event_id: str) -> str:
        return "pending"

    def get_media_clip_status(self, source_event_id: str) -> str:
        event_id = self.events_by_source_id.get(source_event_id, "")
        return self.clip_status_by_event_id.get(event_id, "not_implemented")

    def set_clip_status(self, event_id: str, status: str) -> bool:
        self.clip_status_by_event_id[event_id] = status
        return True


class _Consumer:
    def __init__(self) -> None:
        self.acked: list[str] = []

    def ack(self, msg_id: str) -> bool:
        self.acked.append(msg_id)
        return True


class _RecordPublisher:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def has_request(self, _source_event_id: str, _recording_strategy: str) -> bool:
        return False

    def publish(self, event: dict[str, Any], event_id: str) -> str:
        record = build_record_request(event, event_id)
        assert record is not None
        self.records.append(record)
        return "1-0"


class _Cursor:
    def __init__(self, row: tuple[int, str, str] | None) -> None:
        self.row = row
        self.sql = ""
        self.params: dict[str, Any] = {}

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.sql = sql
        self.params = params

    def fetchone(self) -> tuple[int, str, str] | None:
        return self.row


class _Conn:
    def __init__(self, row: tuple[int, str, str] | None) -> None:
        self.cursor_obj = _Cursor(row)

    def cursor(self) -> _Cursor:
        return self.cursor_obj


def _event(
    source_event_id: str,
    event_type: str,
    algorithm_type: str,
    event_ts_ms: int,
) -> dict[str, Any]:
    return {
        "source_event_id": source_event_id,
        "event_type": event_type,
        "algorithm_type": algorithm_type,
        "camera_id": "cam1",
        "source_id": "primary_rtsp",
        "track_id": "1",
        "event_ts_ms": event_ts_ms,
        "start_ts_ms": event_ts_ms,
        "end_ts_ms": event_ts_ms,
        "severity": "medium",
        "confidence": 0.8,
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": {
            "snapshot_required": True,
            "clip_required": True,
            "pre_seconds": 5,
            "post_seconds": 5,
        },
        "payload": {
            "media": {
                "source_id": "primary_rtsp",
                "clip_required": True,
                "pre_seconds": 5,
                "post_seconds": 5,
            }
        },
    }


def test_repository_algorithm_scope_filters_by_algorithm_key() -> None:
    conn = _Conn((100_000, "watchlist_hit", "face_intelligence"))
    repo = EventRepository(conn)

    row = repo.get_last_unsuppressed_alert(
        "cam1",
        exclude_source_event_id="watchlist:current",
        current_event_ts_ms=120_000,
        event_type="watchlist_hit",
        algorithm_type="face_intelligence",
        cooldown_scope="algorithm",
    )

    assert row == {
        "event_ts_ms": 100_000,
        "event_type": "watchlist_hit",
        "algorithm_type": "face_intelligence",
    }
    assert "COALESCE(NULLIF(algorithm_type, ''), event_type)" in conn.cursor_obj.sql
    assert "event_type = %(event_type)s" not in conn.cursor_obj.sql
    assert conn.cursor_obj.params["camera_id"] == "cam1"
    assert conn.cursor_obj.params["algorithm_key"] == "face_intelligence"
    assert conn.cursor_obj.params["exclude_source_event_id"] == "watchlist:current"


def test_repository_event_type_scope_filters_by_event_type() -> None:
    conn = _Conn((100_000, "watchlist_hit", "face_intelligence"))
    repo = EventRepository(conn)

    row = repo.get_last_unsuppressed_alert(
        "cam1",
        exclude_source_event_id="watchlist:current",
        current_event_ts_ms=120_000,
        event_type="watchlist_hit",
        algorithm_type="face_intelligence",
        cooldown_scope="event_type",
    )

    assert row is not None
    assert "event_type = %(event_type)s" in conn.cursor_obj.sql
    assert "algorithm_key" not in conn.cursor_obj.params
    assert conn.cursor_obj.params["event_type"] == "watchlist_hit"


def test_algorithm_scoped_cooldown_suppresses_same_algorithm_only() -> None:
    repo = _Repo()
    service = AlertPolicyService(repo)

    first = _event("intrusion:first", "intrusion", "behavior.intrusion", 100_000)
    first_id = repo.insert_event(first)
    assert first_id is not None

    same_algorithm = _event(
        "intrusion:second",
        "intrusion",
        "behavior.intrusion",
        110_000,
    )
    identity = _event(
        "watchlist:first",
        "watchlist_hit",
        "face_intelligence",
        110_000,
    )

    same_decision = service.decide(same_algorithm)
    identity_decision = service.decide(identity)

    assert same_decision.suppressed is True
    assert same_decision.reason == "camera_algorithm_cooldown"
    assert identity_decision.suppressed is False


def test_event_worker_creates_watchlist_recording_during_intrusion_cooldown() -> None:
    repo = _Repo()
    service = AlertPolicyService(repo)
    consumer = _Consumer()
    publisher = _RecordPublisher()

    intrusion_new, intrusion_id = _handle_event(
        _event("intrusion:first", "intrusion", "behavior.intrusion", 100_000),
        "1-0",
        repo,
        consumer,
        record_publisher=publisher,
        alert_policy_service=service,
        recording_event_types=("intrusion", "watchlist_hit"),
    )
    watchlist_new, watchlist_id = _handle_event(
        _event("watchlist:first", "watchlist_hit", "face_intelligence", 110_000),
        "2-0",
        repo,
        consumer,
        record_publisher=publisher,
        alert_policy_service=service,
        recording_event_types=("intrusion", "watchlist_hit"),
    )

    assert intrusion_new is True
    assert intrusion_id is not None
    assert watchlist_new is True
    assert watchlist_id is not None
    assert repo.events_by_id[watchlist_id]["status"] == "new"
    assert len(repo.evidence_tasks) == 2
    assert [record["event_type"] for record in publisher.records] == [
        "intrusion",
        "watchlist_hit",
    ]
    assert consumer.acked == ["1-0", "2-0"]


def test_event_worker_suppresses_repeated_watchlist_within_scope() -> None:
    repo = _Repo()
    service = AlertPolicyService(repo)
    consumer = _Consumer()
    publisher = _RecordPublisher()

    first_new, first_id = _handle_event(
        _event("watchlist:first", "watchlist_hit", "face_intelligence", 100_000),
        "1-0",
        repo,
        consumer,
        record_publisher=publisher,
        alert_policy_service=service,
        recording_event_types=("watchlist_hit",),
    )
    second_new, second_id = _handle_event(
        _event("watchlist:second", "watchlist_hit", "face_intelligence", 110_000),
        "2-0",
        repo,
        consumer,
        record_publisher=publisher,
        alert_policy_service=service,
        recording_event_types=("watchlist_hit",),
    )

    assert first_new is True
    assert first_id is not None
    assert second_new is True
    assert second_id is not None
    assert repo.events_by_id[second_id]["status"] == "suppressed"
    assert (
        repo.events_by_id[second_id]["payload"]["alert_policy"]["reason"]
        == "camera_algorithm_cooldown"
    )
    assert (
        repo.events_by_id[second_id]["payload"]["alert_policy"]["cooldown_scope"]
        == "algorithm"
    )
    assert (
        repo.events_by_id[second_id]["payload"]["alert_policy"]["cooldown_key"]
        == "face_intelligence"
    )
    assert (
        repo.events_by_id[second_id]["payload"]["alert_policy"][
            "last_alert_event_type"
        ]
        == "watchlist_hit"
    )
    assert (
        repo.events_by_id[second_id]["payload"]["alert_policy"][
            "last_alert_algorithm_type"
        ]
        == "face_intelligence"
    )
    assert len(repo.evidence_tasks) == 1
    assert len(publisher.records) == 1


def test_global_scope_still_bypasses_intrusion_for_identity_event() -> None:
    repo = _Repo()
    repo.policy["cooldown_scope"] = "global"
    service = AlertPolicyService(repo)

    first = _event("intrusion:first", "intrusion", "behavior.intrusion", 100_000)
    first_id = repo.insert_event(first)
    assert first_id is not None

    identity = _event(
        "watchlist:first",
        "watchlist_hit",
        "face_intelligence",
        110_000,
    )
    repeated_identity = _event(
        "watchlist:second",
        "watchlist_hit",
        "face_intelligence",
        120_000,
    )

    identity_decision = service.decide(identity)
    identity_id = repo.insert_event(identity)
    assert identity_id is not None
    repeated_identity_decision = service.decide(repeated_identity)

    assert identity_decision.suppressed is False
    assert repeated_identity_decision.suppressed is True
    assert repeated_identity_decision.reason == "camera_global_cooldown"
