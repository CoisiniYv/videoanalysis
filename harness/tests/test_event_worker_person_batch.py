"""Event-worker person trajectory batching and ACK safety."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVENT_WORKER_DIR = str(ROOT / "services" / "event-worker")


def _worker_module():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if EVENT_WORKER_DIR in sys.path:
        sys.path.remove(EVENT_WORKER_DIR)
    sys.path.insert(0, EVENT_WORKER_DIR)
    from app import worker

    return worker


def _fields(source_observation_id: str) -> dict[bytes, bytes]:
    return {
        b"data": json.dumps(
            {
                "source_observation_id": source_observation_id,
                "source_id": "source-1",
                "camera_id": "camera-1",
                "track_id": "track-1",
                "timestamp_ms": 1_780_000_000_000,
                "person_bbox": [1, 2, 101, 202],
                "person_confidence": 0.9,
                "gate_status": "accepted",
                "payload": {},
            }
        ).encode()
    }


class _Consumer:
    def __init__(self) -> None:
        self.ack_batches: list[list[str]] = []

    def ack_many(self, msg_ids: list[str]) -> int:
        self.ack_batches.append(list(msg_ids))
        return len(msg_ids)


class _Repository:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.batches: list[list[dict]] = []

    def insert_person_bbox_observations(
        self,
        observations: list[dict],
    ) -> list[str | None]:
        self.batches.append(observations)
        if self.fail:
            raise RuntimeError("database unavailable")
        return ["row-1", None]


def test_person_observations_use_one_database_batch_and_one_committed_ack() -> None:
    worker = _worker_module()
    repo = _Repository()
    consumer = _Consumer()

    result = worker._process_person_observation_batch(
        [
            ("1-0", _fields("person-1")),
            ("1-1", _fields("person-1")),
            ("1-2", {b"data": b"not-json"}),
        ],
        repo,
        consumer,
    )

    assert result == (1, 1, 1, 0)
    assert len(repo.batches) == 1
    assert len(repo.batches[0]) == 2
    assert consumer.ack_batches == [["1-2"], ["1-0", "1-1"]]


def test_person_database_failure_leaves_valid_messages_pending() -> None:
    worker = _worker_module()
    repo = _Repository(fail=True)
    consumer = _Consumer()

    result = worker._process_person_observation_batch(
        [
            ("2-0", _fields("person-2")),
            ("2-1", _fields("person-3")),
        ],
        repo,
        consumer,
    )

    assert result == (0, 0, 0, 2)
    assert consumer.ack_batches == []
