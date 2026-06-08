"""C2.15 replay-first watchlist recording priority tests."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVENT_WORKER_ROOT = ROOT / "services" / "event-worker"


class FakeConsumer:
    def __init__(self) -> None:
        self.acked: list[str] = []

    def ack(self, msg_id: str) -> bool:
        self.acked.append(msg_id)
        return True


class FakeRecordPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def has_request(self, _source_event_id: str, _recording_strategy: str) -> bool:
        return False

    def publish(self, event: dict[str, Any], event_id: str) -> str:
        self.events.append({"event": event, "event_id": event_id})
        return "record-msg-1"


class FakeRepo:
    def __init__(self) -> None:
        self.clip_status_updates: list[tuple[str, str]] = []
        self.evidence_tasks = 0

    def insert_event(self, _event: dict[str, Any]) -> str:
        return str(uuid.uuid4())

    def create_evidence_task(self, _event: dict[str, Any], _event_id: str) -> str:
        self.evidence_tasks += 1
        return "task-1"

    def get_media_clip_status(self, _source_event_id: str) -> str:
        return "not_implemented"

    def set_clip_status(self, event_id: str, status: str) -> bool:
        self.clip_status_updates.append((event_id, status))
        return True


def test_watchlist_recording_bypasses_intrusion_cooldown() -> None:
    worker = _load_event_worker()
    try:
        state = worker.RecordingPolicyState(
            published_requests=1,
            last_recorded_at_ms={
                "c2_replay_first_rtsp": int(worker.time.time() * 1000)
            },
            last_recorded_event_type={"c2_replay_first_rtsp": "intrusion"},
        )
        repo = FakeRepo()
        records = FakeRecordPublisher()

        worker._handle_event(
            _watchlist_event("watchlist_hit:face:c2_replay_first_rtsp:764:16412:6"),
            "1-0",
            repo,
            FakeConsumer(),
            record_publisher=records,
            recording_state=state,
            recording_event_types=("watchlist_hit", "intrusion"),
            recording_source_id="c2_replay_first_rtsp",
            recording_cooldown_seconds=30,
        )

        assert len(records.events) == 1
        assert repo.evidence_tasks == 1
        assert state.published_requests == 2
        assert state.last_recorded_event_type["c2_replay_first_rtsp"] == "watchlist_hit"
    finally:
        _clear_event_worker_app_imports()


def test_watchlist_recording_still_respects_watchlist_cooldown() -> None:
    worker = _load_event_worker()
    try:
        state = worker.RecordingPolicyState(
            published_requests=1,
            last_recorded_at_ms={
                "c2_replay_first_rtsp": int(worker.time.time() * 1000)
            },
            last_recorded_event_type={"c2_replay_first_rtsp": "watchlist_hit"},
        )
        repo = FakeRepo()
        records = FakeRecordPublisher()

        worker._handle_event(
            _watchlist_event("watchlist_hit:face:c2_replay_first_rtsp:764:17413:6"),
            "1-0",
            repo,
            FakeConsumer(),
            record_publisher=records,
            recording_state=state,
            recording_event_types=("watchlist_hit", "intrusion"),
            recording_source_id="c2_replay_first_rtsp",
            recording_cooldown_seconds=30,
        )

        assert records.events == []
        assert repo.evidence_tasks == 1
        assert state.published_requests == 1
        assert state.last_recorded_event_type["c2_replay_first_rtsp"] == "watchlist_hit"
    finally:
        _clear_event_worker_app_imports()


def _watchlist_event(source_event_id: str) -> dict[str, Any]:
    event_ts_ms = 16_412
    return {
        "source_event_id": source_event_id,
        "event_type": "watchlist_hit",
        "camera_id": "c2_replay_first_rtsp",
        "source_id": "c2_replay_first_rtsp",
        "track_id": "764",
        "person_id": 6,
        "algorithm_type": "face_intelligence",
        "event_ts_ms": event_ts_ms,
        "start_ts_ms": event_ts_ms,
        "end_ts_ms": event_ts_ms,
        "confidence": 0.703346610389494,
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": {
            "snapshot_required": True,
            "clip_required": True,
            "pre_seconds": 5,
            "post_seconds": 5,
        },
        "payload": {
            "matched_person": {
                "person_id": 6,
                "external_person_id": "demo:f4_3:finch",
                "name": "Finch",
            },
            "match": {
                "similarity": 0.703346610389494,
                "threshold": 0.65,
                "gallery_embedding_id": 5,
                "source_observation_id": "face:c2_replay_first_rtsp:764:16412",
            },
            "media": {
                "source_id": "c2_replay_first_rtsp",
                "clip_required": True,
                "pre_seconds": 5,
                "post_seconds": 5,
                "frame_pts": 16412588888,
                "frame_uuid": "019ea6d0-0aa9-73a2-b5b5-a48a2e1a536d",
                "keyframe_uuid": "019ea6d0-04a2-7c30-9180-5457ce5d0f1d",
            },
        },
    }


def _load_event_worker() -> Any:
    if str(EVENT_WORKER_ROOT) not in sys.path:
        sys.path.insert(0, str(EVENT_WORKER_ROOT))
    from app import worker

    return worker


def _clear_event_worker_app_imports() -> None:
    if str(EVENT_WORKER_ROOT) in sys.path:
        sys.path.remove(str(EVENT_WORKER_ROOT))
    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            sys.modules.pop(module_name, None)
