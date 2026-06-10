"""Event-worker recording policy contracts for midterm multi-camera output."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVENT_WORKER_DIR = str(ROOT / "services" / "event-worker")
if EVENT_WORKER_DIR not in sys.path:
    sys.path.insert(0, EVENT_WORKER_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.record_request import build_record_request
from app.worker import RecordingPolicyState, _apply_recording_window, _handle_event


class _Repo:
    def __init__(self) -> None:
        self.clip_status = ""
        self.task_status = "pending"
        self.inserted_events: list[dict[str, Any]] = []

    def insert_event(self, event: dict[str, Any]) -> str:
        self.inserted_events.append(event)
        return "event-1"

    def create_evidence_task(self, event: dict[str, Any], event_id: str) -> None:
        self.task_status = "pending"

    def get_evidence_task_status(self, event_id: str) -> str:
        return self.task_status

    def get_media_clip_status(self, source_event_id: str) -> str:
        return self.clip_status

    def set_clip_status(self, event_id: str, status: str) -> None:
        self.clip_status = status


class _Consumer:
    def __init__(self) -> None:
        self.acked: list[str] = []

    def ack(self, msg_id: str) -> bool:
        self.acked.append(msg_id)
        return True


class _Publisher:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def has_request(self, source_event_id: str, recording_strategy: str) -> bool:
        return False

    def publish(self, event: dict[str, Any], event_id: str) -> str:
        record = build_record_request(
            event,
            event_id,
            default_replay_source_id="primary_rtsp",
            default_pre_seconds=5,
            default_post_seconds=5,
        )
        assert record is not None
        self.records.append(record)
        return "1-0"


def test_recording_window_preserves_rule_policy_over_env_defaults() -> None:
    event = {
        "event_type": "intrusion",
        "source_event_id": "lab:1",
        "camera_id": "cam_lab",
        "source_id": "source_lab",
        "event_ts_ms": 1_765_000_000_000,
        "clip_required": True,
        "evidence_policy": {
            "snapshot_required": True,
            "clip_required": True,
            "pre_seconds": 3,
            "post_seconds": 7,
        },
        "payload": {
            "media": {
                "clip_required": True,
                "source_id": "source_lab",
                "pre_seconds": 3,
                "post_seconds": 7,
            }
        },
    }

    _apply_recording_window(event, pre_seconds=5, post_seconds=5)

    assert event["evidence_policy"]["pre_seconds"] == 3
    assert event["evidence_policy"]["post_seconds"] == 7
    assert event["payload"]["media"]["pre_seconds"] == 3
    assert event["payload"]["media"]["post_seconds"] == 7


def test_blank_recording_source_id_allows_8090_added_camera_source() -> None:
    event = {
        "event_type": "intrusion",
        "source_event_id": "lab:intrusion:1",
        "camera_id": "cam_lab",
        "source_id": "source_lab",
        "event_ts_ms": 1_765_000_000_000,
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": {
            "snapshot_required": True,
            "clip_required": True,
            "pre_seconds": 4,
            "post_seconds": 9,
        },
        "payload": {
            "media": {
                "snapshot_required": True,
                "clip_required": True,
                "source_id": "source_lab",
                "pre_seconds": 4,
                "post_seconds": 9,
            }
        },
    }
    repo = _Repo()
    consumer = _Consumer()
    publisher = _Publisher()
    state = RecordingPolicyState()

    inserted, event_id = _handle_event(
        event,
        "1-0",
        repo,
        consumer,
        record_publisher=publisher,
        recording_state=state,
        recording_event_types=("intrusion",),
        recording_source_id="",
        recording_cooldown_seconds=0,
        recording_pre_seconds=5,
        recording_post_seconds=5,
    )

    assert inserted is True
    assert event_id == "event-1"
    assert consumer.acked == ["1-0"]
    assert state.published_requests == 1
    assert repo.clip_status == "pending"
    assert publisher.records[0]["source_id"] == "source_lab"
    assert publisher.records[0]["pre_seconds"] == 4
    assert publisher.records[0]["post_seconds"] == 9
