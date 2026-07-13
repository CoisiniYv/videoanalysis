"""Event-worker recording policy contracts for midterm multi-camera output."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVENT_WORKER_DIR = str(ROOT / "services" / "event-worker")
REPOSITORY_SOURCE = (
    ROOT / "services" / "event-worker" / "app" / "repository.py"
).read_text(encoding="utf-8")
if EVENT_WORKER_DIR in sys.path:
    sys.path.remove(EVENT_WORKER_DIR)
sys.path.insert(0, EVENT_WORKER_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.record_request import build_record_request
from app.worker import (
    RecordingPolicyState,
    _apply_default_evidence_policy,
    _apply_recording_window,
    _effective_evidence_task_gate,
    _handle_event,
    _requires_evidence,
)


class _Repo:
    def __init__(self) -> None:
        self.clip_status = ""
        self.task_status = "pending"
        self.inserted_events: list[dict[str, Any]] = []
        self.event_ids_by_source_event_id: dict[str, str] = {}
        self.source_event_ids_by_event_id: dict[str, str] = {}
        self.clip_status_by_source_event_id: dict[str, str] = {}
        self.skipped_materializations: list[dict[str, str]] = []
        self.evidence_task_creations = 0

    def insert_event(self, event: dict[str, Any]) -> str:
        event_id = f"event-{len(self.inserted_events) + 1}"
        self.inserted_events.append(event)
        source_event_id = str(event.get("source_event_id") or "")
        self.event_ids_by_source_event_id[source_event_id] = event_id
        self.source_event_ids_by_event_id[event_id] = source_event_id
        return event_id

    def create_evidence_task(self, event: dict[str, Any], event_id: str) -> None:
        self.evidence_task_creations += 1
        self.task_status = "pending"

    def get_evidence_task_status(self, event_id: str) -> str:
        return self.task_status

    def get_media_clip_status(self, source_event_id: str) -> str:
        return self.clip_status_by_source_event_id.get(source_event_id, "")

    def set_clip_status(self, event_id: str, status: str) -> None:
        self.clip_status = status
        source_event_id = self.source_event_ids_by_event_id.get(event_id, "")
        if source_event_id:
            self.clip_status_by_source_event_id[source_event_id] = status

    def mark_evidence_materialization_skipped(
        self,
        event_id: str,
        *,
        reason: str,
    ) -> bool:
        self.task_status = "materialization_skipped"
        self.skipped_materializations.append({"event_id": event_id, "reason": reason})
        return True


class _Consumer:
    def __init__(self) -> None:
        self.acked: list[str] = []

    def ack(self, msg_id: str) -> bool:
        self.acked.append(msg_id)
        return True


class _Publisher:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.existing_requests: set[tuple[str, str]] = set()
        self.has_request_calls: list[tuple[str, str]] = []

    def has_request(self, source_event_id: str, recording_strategy: str) -> bool:
        self.has_request_calls.append((source_event_id, recording_strategy))
        return (source_event_id, recording_strategy) in self.existing_requests

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
        self.existing_requests.add(
            (str(record["source_event_id"]), str(record["strategy"]))
        )
        return "1-0"


class _GateRedis:
    def __init__(self, payload: bytes | None) -> None:
        self.payload = payload
        self.keys: list[str] = []

    def get(self, key: str) -> bytes | None:
        self.keys.append(key)
        return self.payload


def test_event_worker_runtime_gate_overrides_static_prefill_state() -> None:
    redis = _GateRedis(
        b'{"enabled":true,"event_not_before_ts_ms":1765000010123}'
    )

    enabled, cutoff, upper_cutoff = _effective_evidence_task_gate(
        redis,  # type: ignore[arg-type]
        configured_enabled=False,
        configured_not_before_ts_ms=0,
        configured_not_after_ts_ms=0,
        redis_key="pressure:run-1:evidence-task-gate",
    )

    assert enabled is True
    assert cutoff == 1_765_000_010_123
    assert upper_cutoff == 0
    assert redis.keys == ["pressure:run-1:evidence-task-gate"]


def test_event_worker_runtime_gate_falls_back_when_document_is_absent() -> None:
    enabled, cutoff, upper_cutoff = _effective_evidence_task_gate(
        _GateRedis(None),  # type: ignore[arg-type]
        configured_enabled=False,
        configured_not_before_ts_ms=123,
        configured_not_after_ts_ms=456,
        redis_key="pressure:run-1:evidence-task-gate",
    )

    assert enabled is False
    assert cutoff == 123
    assert upper_cutoff == 456


def test_explicit_false_evidence_policy_disables_legacy_intrusion_default() -> None:
    event = {
        "event_type": "intrusion",
        "snapshot_required": False,
        "clip_required": False,
        "evidence_policy": {
            "snapshot_required": False,
            "clip_required": False,
            "pre_seconds": 0,
            "post_seconds": 0,
        },
        "payload": {
            "media": {
                "snapshot_required": False,
                "clip_required": False,
            }
        },
    }

    _apply_default_evidence_policy(event)

    assert _requires_evidence(event) is False
    assert event["snapshot_required"] is False
    assert event["clip_required"] is False


def test_missing_intrusion_evidence_policy_still_uses_legacy_default() -> None:
    event = {"event_type": "intrusion", "payload": {}}

    _apply_default_evidence_policy(event)

    assert _requires_evidence(event) is True
    assert event["snapshot_required"] is True
    assert event["clip_required"] is True


def test_event_worker_can_persist_event_without_creating_evidence_task() -> None:
    event = {
        "event_type": "intrusion",
        "source_event_id": "pressure:intrusion:1",
        "camera_id": "pressure-camera",
        "source_id": "pressure-source",
        "event_ts_ms": 1_765_000_000_000,
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": {"snapshot_required": True, "clip_required": True},
    }
    repo = _Repo()
    consumer = _Consumer()

    inserted, _ = _handle_event(
        event,
        "1-0",
        repo,
        consumer,
        evidence_task_creation_enabled=False,
    )

    assert inserted is True
    assert repo.evidence_task_creations == 0
    assert consumer.acked == ["1-0"]


def test_event_worker_task_cutoff_uses_event_timestamp_not_consumer_time() -> None:
    cutoff_ms = 1_765_000_010_000
    repo = _Repo()
    consumer = _Consumer()

    old_event = {
        "event_type": "intrusion",
        "source_event_id": "pressure:intrusion:before-cutoff",
        "camera_id": "pressure-camera",
        "source_id": "pressure-source",
        "event_ts_ms": cutoff_ms - 1,
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": {"snapshot_required": True, "clip_required": True},
    }
    current_event = {
        **old_event,
        "source_event_id": "pressure:intrusion:at-cutoff",
        "event_ts_ms": cutoff_ms,
    }

    first_inserted, _ = _handle_event(
        old_event,
        "1-0",
        repo,
        consumer,
        evidence_task_event_not_before_ts_ms=cutoff_ms,
    )
    second_inserted, _ = _handle_event(
        current_event,
        "2-0",
        repo,
        consumer,
        evidence_task_event_not_before_ts_ms=cutoff_ms,
    )

    assert first_inserted is True
    assert second_inserted is True
    assert len(repo.inserted_events) == 2
    assert repo.evidence_task_creations == 1
    assert consumer.acked == ["1-0", "2-0"]


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


def test_record_request_preserves_replay_shard_manifest_fields() -> None:
    record = build_record_request(
        {
            "event_type": "intrusion",
            "source_event_id": "lab:intrusion:1",
            "camera_id": "cam_lab",
            "source_id": "source_lab",
            "event_ts_ms": 1_765_000_000_000,
            "clip_required": True,
            "payload": {
                "media": {
                    "source_id": "source_lab",
                    "record_request_shard_id": "replay-a",
                    "shard_mapping_version": "phase2-v1",
                },
            },
        },
        "00000000-0000-4000-8000-000000000001",
    )

    assert record is not None
    assert record["record_request_shard_id"] == "replay-a"
    assert record["shard_mapping_version"] == "phase2-v1"


def test_event_repository_persists_runtime_epoch_on_evidence_tasks() -> None:
    assert "_runtime_epoch_id_for_task" in REPOSITORY_SOURCE
    assert "runtime_epoch_id" in REPOSITORY_SOURCE
    assert "NULLIF(%(runtime_epoch_id)s::text, '')" in REPOSITORY_SOURCE


def test_event_repository_persists_materialization_ready_at() -> None:
    assert "_materialization_ready_at" in REPOSITORY_SOURCE
    assert "materialization_ready_at" in REPOSITORY_SOURCE
    assert "%(materialization_ready_at)s::timestamptz" in REPOSITORY_SOURCE


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


def test_duplicate_record_request_marks_new_retry_task_skipped_without_publish() -> None:
    event = {
        "event_type": "intrusion",
        "source_event_id": "lab:intrusion:duplicate",
        "camera_id": "cam_lab",
        "source_id": "source_lab",
        "event_ts_ms": 1_765_000_000_000,
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
                "clip_required": True,
                "source_id": "source_lab",
            }
        },
    }
    repo = _Repo()
    consumer = _Consumer()
    publisher = _Publisher()
    publisher.existing_requests.add(("lab:intrusion:duplicate", "savant_replay"))

    inserted, event_id = _handle_event(
        event,
        "1-0",
        repo,
        consumer,
        record_publisher=publisher,
        recording_state=RecordingPolicyState(),
        recording_event_types=("intrusion",),
        recording_source_id="",
        recording_cooldown_seconds=0,
    )

    assert inserted is True
    assert event_id == "event-1"
    assert publisher.has_request_calls == [
        ("lab:intrusion:duplicate", "savant_replay")
    ]
    assert publisher.records == []
    assert repo.skipped_materializations == [
        {
            "event_id": "event-1",
            "reason": "recording_policy_skipped:duplicate_record_request",
        }
    ]


def test_rolling_cache_suppression_keeps_task_pending_without_replay_request() -> None:
    event = {
        "event_type": "intrusion",
        "source_event_id": "lab:intrusion:rolling",
        "camera_id": "cam_lab",
        "source_id": "source_lab",
        "event_ts_ms": 1_765_000_000_000,
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": {
            "snapshot_required": True,
            "clip_required": True,
            "pre_seconds": 5,
            "post_seconds": 10,
        },
        "payload": {
            "media": {
                "clip_required": True,
                "source_id": "source_lab",
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
        rolling_cache_suppress_record_requests=True,
    )

    assert inserted is True
    assert event_id == "event-1"
    assert consumer.acked == ["1-0"]
    assert publisher.records == []
    assert state.published_requests == 0
    assert repo.clip_status == ""
    assert repo.task_status == "pending"
    assert repo.skipped_materializations == []


def test_current_runtime_epoch_overrides_stale_event_epoch() -> None:
    event = {
        "event_type": "intrusion",
        "source_event_id": "lab:intrusion:epoch",
        "camera_id": "cam_lab",
        "source_id": "source_lab",
        "event_ts_ms": 1_765_000_000_000,
        "snapshot_required": True,
        "clip_required": True,
        "runtime_epoch_id": "midterm-old",
        "evidence_policy": {
            "snapshot_required": True,
            "clip_required": True,
            "pre_seconds": 5,
            "post_seconds": 5,
        },
        "payload": {
            "runtime_epoch_id": "midterm-old",
            "media": {
                "runtime_epoch_id": "midterm-old",
                "clip_required": True,
                "source_id": "source_lab",
            }
        },
    }
    repo = _Repo()
    consumer = _Consumer()
    publisher = _Publisher()

    inserted, event_id = _handle_event(
        event,
        "1-0",
        repo,
        consumer,
        record_publisher=publisher,
        recording_state=RecordingPolicyState(),
        recording_event_types=("intrusion",),
        recording_source_id="",
        recording_cooldown_seconds=0,
        runtime_epoch_id="midterm-current",
    )

    assert inserted is True
    assert event_id == "event-1"
    assert repo.inserted_events[0]["runtime_epoch_id"] == "midterm-current"
    assert repo.inserted_events[0]["payload"]["runtime_epoch_id"] == "midterm-current"
    assert repo.inserted_events[0]["payload"]["media"]["runtime_epoch_id"] == (
        "midterm-current"
    )
    assert publisher.records[0]["runtime_epoch_id"] == "midterm-current"


def test_recording_cooldown_marks_evidence_task_skipped_not_pending() -> None:
    repo = _Repo()
    consumer = _Consumer()
    publisher = _Publisher()
    state = RecordingPolicyState()
    first_intrusion = {
        "event_type": "intrusion",
        "source_event_id": "intrusion:primary:1",
        "camera_id": "cam_primary",
        "source_id": "primary_rtsp",
        "event_ts_ms": 1_765_000_000_000,
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
                "clip_required": True,
                "source_id": "primary_rtsp",
            }
        },
    }
    second_intrusion = {
        "event_type": "intrusion",
        "source_event_id": "intrusion:primary:2",
        "camera_id": "cam_primary",
        "source_id": "primary_rtsp",
        "event_ts_ms": 1_765_000_010_000,
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
                "clip_required": True,
                "source_id": "primary_rtsp",
            }
        },
    }

    _handle_event(
        first_intrusion,
        "1-0",
        repo,
        consumer,
        record_publisher=publisher,
        recording_state=state,
        recording_event_types=("watchlist_hit", "intrusion"),
        recording_source_id="",
        recording_cooldown_seconds=30,
    )
    inserted, event_id = _handle_event(
        second_intrusion,
        "2-0",
        repo,
        consumer,
        record_publisher=publisher,
        recording_state=state,
        recording_event_types=("watchlist_hit", "intrusion"),
        recording_source_id="",
        recording_cooldown_seconds=30,
    )

    assert inserted is True
    assert event_id == "event-2"
    assert len(publisher.records) == 1
    assert state.published_requests == 1
    assert repo.skipped_materializations == [
        {
            "event_id": "event-2",
            "reason": "recording_policy_skipped:cooldown",
        }
    ]
    assert repo.task_status == "materialization_skipped"


def test_recording_cooldown_does_not_cross_event_types() -> None:
    repo = _Repo()
    consumer = _Consumer()
    publisher = _Publisher()
    state = RecordingPolicyState()
    watchlist = {
        "event_type": "watchlist_hit",
        "source_event_id": "watchlist:primary:1",
        "camera_id": "cam_primary",
        "source_id": "primary_rtsp",
        "event_ts_ms": 1_765_000_000_000,
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
                "clip_required": True,
                "source_id": "primary_rtsp",
            }
        },
    }
    intrusion = {
        "event_type": "intrusion",
        "source_event_id": "intrusion:primary:1",
        "camera_id": "cam_primary",
        "source_id": "primary_rtsp",
        "event_ts_ms": 1_765_000_010_000,
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
                "clip_required": True,
                "source_id": "primary_rtsp",
            }
        },
    }

    _handle_event(
        watchlist,
        "1-0",
        repo,
        consumer,
        record_publisher=publisher,
        recording_state=state,
        recording_event_types=("watchlist_hit", "intrusion"),
        recording_source_id="",
        recording_cooldown_seconds=30,
    )
    inserted, event_id = _handle_event(
        intrusion,
        "2-0",
        repo,
        consumer,
        record_publisher=publisher,
        recording_state=state,
        recording_event_types=("watchlist_hit", "intrusion"),
        recording_source_id="",
        recording_cooldown_seconds=30,
    )

    assert inserted is True
    assert event_id == "event-2"
    assert len(publisher.records) == 2
    assert state.published_requests == 2
    assert repo.skipped_materializations == []
    assert repo.task_status == "pending"


def test_recording_source_cooldown_suppresses_cross_event_types() -> None:
    repo = _Repo()
    consumer = _Consumer()
    publisher = _Publisher()
    state = RecordingPolicyState()
    watchlist = {
        "event_type": "watchlist_hit",
        "source_event_id": "watchlist:primary:1",
        "camera_id": "cam_primary",
        "source_id": "primary_rtsp",
        "event_ts_ms": 1_765_000_000_000,
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": {
            "snapshot_required": True,
            "clip_required": True,
            "pre_seconds": 5,
            "post_seconds": 5,
        },
        "payload": {"media": {"clip_required": True, "source_id": "primary_rtsp"}},
    }
    intrusion = {
        **watchlist,
        "event_type": "intrusion",
        "source_event_id": "intrusion:primary:1",
        "event_ts_ms": 1_765_000_010_000,
    }

    _handle_event(
        watchlist,
        "1-0",
        repo,
        consumer,
        record_publisher=publisher,
        recording_state=state,
        recording_event_types=("watchlist_hit", "intrusion"),
        recording_cooldown_seconds=30,
        recording_cooldown_scope="source",
    )
    inserted, event_id = _handle_event(
        intrusion,
        "2-0",
        repo,
        consumer,
        record_publisher=publisher,
        recording_state=state,
        recording_event_types=("watchlist_hit", "intrusion"),
        recording_cooldown_seconds=30,
        recording_cooldown_scope="source",
    )

    assert inserted is True
    assert event_id == "event-2"
    assert len(publisher.records) == 1
    assert repo.skipped_materializations[-1]["reason"] == (
        "recording_policy_skipped:cooldown"
    )


def test_recording_cooldown_grace_allows_near_boundary_event() -> None:
    repo = _Repo()
    consumer = _Consumer()
    publisher = _Publisher()
    state = RecordingPolicyState()
    first = {
        "event_type": "intrusion",
        "source_event_id": "intrusion:primary:1",
        "camera_id": "cam_primary",
        "source_id": "primary_rtsp",
        "event_ts_ms": 1_765_000_000_000,
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
                "clip_required": True,
                "source_id": "primary_rtsp",
            }
        },
    }
    second = {
        **first,
        "source_event_id": "intrusion:primary:2",
        "event_ts_ms": 1_765_000_029_000,
        "payload": {
            "media": {
                "clip_required": True,
                "source_id": "primary_rtsp",
            }
        },
    }

    _handle_event(
        first,
        "1-0",
        repo,
        consumer,
        record_publisher=publisher,
        recording_state=state,
        recording_event_types=("watchlist_hit", "intrusion"),
        recording_source_id="",
        recording_cooldown_seconds=30,
        recording_cooldown_grace_ms=1000,
    )
    inserted, event_id = _handle_event(
        second,
        "2-0",
        repo,
        consumer,
        record_publisher=publisher,
        recording_state=state,
        recording_event_types=("watchlist_hit", "intrusion"),
        recording_source_id="",
        recording_cooldown_seconds=30,
        recording_cooldown_grace_ms=1000,
    )

    assert inserted is True
    assert event_id == "event-2"
    assert len(publisher.records) == 2
    assert state.published_requests == 2
    assert repo.skipped_materializations == []
