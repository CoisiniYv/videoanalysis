from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CLIP_WORKER_DIR = ROOT / "services" / "clip-worker"


def _activate() -> None:
    path = str(CLIP_WORKER_DIR)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _clip_config(**overrides: Any):
    _activate()
    from app.config import Config
    from app.replay_shards import load_replay_shard_map

    values = {
        "redis_url": "redis://redis:6379/0",
        "record_request_stream": "security.record_requests",
        "replay_api_url": "http://replay-service:8080",
        "replay_job_sink_url": "dealer+connect:tcp://video-file-sink:6666",
        "replay_shards": load_replay_shard_map(
            default_replay_api_url="http://replay-service:8080",
            default_in_stream_endpoint="dealer+connect:tcp://replay-service:5555",
            default_replay_job_sink_url="dealer+connect:tcp://video-file-sink:6666",
            env={},
        ),
        "database_url": "postgresql://video:video@postgres:5432/video_analytics",
        "consumer_group": "clip-workers-test",
        "consumer_name": "clip-worker-test-1",
        "poll_timeout_ms": 1,
        "default_pre_seconds": 5,
        "default_post_seconds": 5,
        "keyframe_lookup_window_s": 10,
        "max_jobs_per_run": 100,
        "run_once": True,
        "max_concurrent_jobs": 1,
        "pending_claim_min_idle_ms": 0,
        "pending_claim_count": 10,
        "pending_claim_interval_s": 0.0,
        "deferred_retry_max_attempts": 5,
        "per_camera_cooldown_seconds": 0,
        "replay_stop_condition_mode": "ts_delta_sec",
        "replay_fps": 30,
        "replay_duration_extra_slack_s": 0.0,
        "replay_anchor_strategy": "request_keyframe",
        "allow_unbounded_keyframe_fallback": False,
        "keyframe_lookup_retries": 0,
        "keyframe_lookup_retry_sleep_s": 0.0,
        "post_savant_frame_proof_attempts": 1,
        "post_savant_frame_proof_retry_sleep_s": 0.0,
        "post_savant_frame_proof_wait_budget_s": 0.0,
        "post_savant_frame_proof_poll_interval_s": 0.0,
        "post_savant_allow_cross_session_post_window_proof": True,
        "post_savant_allow_truncated_pre_window_proof": True,
        "frame_annotation_stream": "security.frame_annotations",
        "frame_annotation_anchor_lookback_count": 100,
        "frame_annotation_anchor_wall_clock_slack_s": 1.0,
        "frame_annotation_anchor_pts_tolerance_s": 1.0,
        "evidence_materialization_policy": "priority",
        "evidence_high_priority_event_types": ("watchlist_hit", "live_search_hit"),
        "evidence_defer_low_priority": False,
        "evidence_replay_ttl_seconds": 300,
        "evidence_frame_annotation_ttl_seconds": 120,
        "evidence_unknown_source_fail_closed": True,
        "evidence_materialization_max_concurrency": 1,
        "evidence_materialization_max_concurrency_per_shard": 0,
        "evidence_materialization_max_concurrency_per_source": 0,
        "evidence_materialization_event_type_quotas": {},
        "evidence_materialization_pressure_level": "normal",
    }
    values.update(overrides)
    if "evidence_materialization_max_concurrency" not in overrides:
        values["evidence_materialization_max_concurrency"] = values["max_concurrent_jobs"]
    return Config(**values)


class _FakeRedis:
    def __init__(
        self,
        requests: list[dict[str, Any]] | None = None,
        *,
        pending_requests: list[tuple[str, dict[str, Any], int]] | None = None,
        frame_annotations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.requests = requests or []
        self.pending_requests = pending_requests or []
        self.frame_annotations = frame_annotations or []
        self.acked: list[str] = []
        self.reads = 0
        self.claims = 0
        self.proof_reads = 0

    def xgroup_create(self, *_args, **_kwargs):
        return None

    def xpending_range(self, *_args, **_kwargs):
        return [
            {
                "message_id": msg_id,
                "consumer": "old-worker",
                "time_since_delivered": 120000,
                "times_delivered": deliveries,
            }
            for msg_id, _request, deliveries in self.pending_requests
        ]

    def xautoclaim(self, *_args, **_kwargs):
        self.claims += 1
        entries = [
            (msg_id.encode("utf-8"), {b"data": json.dumps(request).encode("utf-8")})
            for msg_id, request, _deliveries in self.pending_requests
        ]
        self.pending_requests = []
        return "0-0", entries, []

    def xreadgroup(self, *_args, **_kwargs):
        self.reads += 1
        if self.reads > 1:
            return []
        entries = []
        for index, request in enumerate(self.requests, start=1):
            fields = {b"data": json.dumps(request).encode("utf-8")}
            entries.append((f"{index}-0".encode("utf-8"), fields))
        return [(b"security.record_requests", entries)] if entries else []

    def xack(self, _stream, _group, msg_id):
        self.acked.append(msg_id.decode("utf-8") if isinstance(msg_id, bytes) else msg_id)
        return 1

    def xrevrange(self, *_args, **_kwargs):
        self.proof_reads += 1
        entries = []
        for index, message in enumerate(reversed(self.frame_annotations)):
            stream_id = str(message.get("_stream_id", f"{index + 1}-0"))
            entries.append(
                (
                    stream_id.encode("utf-8"),
                    {b"data": json.dumps(message).encode("utf-8")},
                )
            )
        return entries

    def xpending(self, *_args, **_kwargs):
        return {"pending": len(self.pending_requests)}

    def xinfo_groups(self, *_args, **_kwargs):
        return [{"name": b"clip-workers-test", "pending": len(self.pending_requests), "lag": 0}]


class _FakeReplay:
    instances: list["_FakeReplay"] = []

    def __init__(self, url: str) -> None:
        self.url = url
        self.jobs: list[dict[str, Any]] = []
        self.last_job_request: dict[str, Any] = {}
        self.last_job_payload: dict[str, Any] = {}
        _FakeReplay.instances.append(self)

    def find_keyframe(self, *_args, **_kwargs):
        return "lookup-kf"

    def create_job(self, **kwargs):
        self.jobs.append(kwargs)
        self.last_job_request = dict(kwargs)
        self.last_job_payload = dict(kwargs)
        return "replay-job-1"


class _DiagnosticsCursor:
    def __init__(self, diagnostics: dict[str, Any]) -> None:
        self.diagnostics = diagnostics

    def __enter__(self) -> "_DiagnosticsCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, *_args, **_kwargs) -> None:
        return None

    def fetchone(self):
        return {"diagnostics": self.diagnostics}


class _DiagnosticsConn:
    def __init__(self, diagnostics: dict[str, Any] | None = None) -> None:
        self.diagnostics = diagnostics or {}

    def cursor(self) -> _DiagnosticsCursor:
        return _DiagnosticsCursor(self.diagnostics)


def _request(event_suffix: str, *, event_type: str = "intrusion") -> dict[str, Any]:
    return {
        "request_id": f"req-{event_suffix}",
        "event_id": f"00000000-0000-4000-8000-000000000{event_suffix}",
        "source_event_id": f"{event_type}:source:track-{event_suffix}",
        "event_type": event_type,
        "source_id": "source-1",
        "camera_id": "camera-1",
        "event_ts_ms": 1_780_000_000_000,
        "strategy": "savant_replay",
        "keyframe_uuid": f"kf-{event_suffix}",
        "pre_seconds": 5,
        "post_seconds": 5,
    }


def _two_replay_shards():
    from app.replay_shards import parse_replay_shard_map

    return parse_replay_shard_map(
        {
            "default_shard_id": "replay-a",
            "shards": [
                {
                    "shard_id": "replay-a",
                    "replay_api_url": "http://replay-a:8080",
                    "in_stream_endpoint": "dealer+connect:tcp://replay-a:5555",
                    "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-a:6666",
                    "source_ids": ["source-a"],
                },
                {
                    "shard_id": "replay-b",
                    "replay_api_url": "http://replay-b:8080",
                    "in_stream_endpoint": "dealer+connect:tcp://replay-b:5555",
                    "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-b:6666",
                    "source_ids": ["source-b"],
                },
            ],
        },
        default_replay_api_url="http://replay-service:8080",
        default_in_stream_endpoint="dealer+connect:tcp://replay-service:5555",
        default_replay_job_sink_url="dealer+connect:tcp://video-file-sink:6666",
    )


def _frame_annotation(
    *,
    frame_uuid: str,
    frame_pts: int,
    stream_id: str,
    source_id: str = "source-1",
    camera_id: str = "camera-1",
    runtime_epoch_id: str = "epoch-1",
    stream_session_id: str = "session-1",
    keyframe_uuid: str | None = None,
    previous_keyframe_uuid: str | None = None,
    keyframe_pts: int | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "_stream_id": stream_id,
        "message_type": "frame_annotation",
        "source_id": source_id,
        "camera_id": camera_id,
        "frame_uuid": frame_uuid,
        "frame_pts": frame_pts,
        "runtime_epoch_id": runtime_epoch_id,
        "stream_session_id": stream_session_id,
    }
    if keyframe_uuid is not None:
        message["keyframe_uuid"] = keyframe_uuid
    if previous_keyframe_uuid is not None:
        message["previous_keyframe_uuid"] = previous_keyframe_uuid
    if keyframe_pts is not None:
        message["keyframe_pts"] = keyframe_pts
    return message


def test_concurrency_pressure_queues_without_permanent_skip(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    redis_client = _FakeRedis([_request("001"), _request("002")])
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(max_concurrent_jobs=1, pending_claim_count=0),
        redis_client,
        object(),
    )

    assert redis_client.acked == ["1-0"]
    assert [update["status"] for update in updates] == ["replay_job_created", "pending"]
    assert updates[-1]["evidence_state"] == "materialization_deferred"
    assert updates[-1]["diagnostics"]["active_job_count"] == 1
    assert updates[-1]["diagnostics"]["max_concurrent_jobs"] == 1
    assert all(update["status"] != "skipped_by_poc_limit" for update in updates)


def test_post_savant_concurrency_queue_does_not_wait_for_proof(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    first = _request("016")
    second = {
        **_request("017"),
        "replay_source_kind": "post_savant",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "stream_session_id": "session-1",
        "runtime_epoch_id": "epoch-1",
    }
    redis_client = _FakeRedis([first, second])
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(max_concurrent_jobs=1, pending_claim_count=0),
        redis_client,
        _DiagnosticsConn(),
    )

    assert redis_client.acked == ["1-0"]
    assert redis_client.proof_reads == 0
    assert [update["status"] for update in updates] == ["replay_job_created", "pending"]
    assert updates[-1]["evidence_state"] == "materialization_deferred"


def test_pending_entry_is_claimed_and_processed(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    redis_client = _FakeRedis(
        pending_requests=[("9-0", _request("003"), 2)],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(max_concurrent_jobs=0),
        redis_client,
        object(),
    )

    assert redis_client.claims == 1
    assert redis_client.acked == ["9-0"]
    assert updates[-1]["status"] == "replay_job_created"


def test_terminal_pending_entry_is_acked_without_replay(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    redis_client = _FakeRedis(
        pending_requests=[("9-0", _request("022"), 8)],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)
    monkeypatch.setattr(
        worker,
        "terminal_evidence_state",
        lambda _pg_conn, _event_id: "materialization_expired",
    )

    worker.run_worker(
        _clip_config(max_concurrent_jobs=0),
        redis_client,
        object(),
    )

    assert redis_client.claims == 1
    assert redis_client.acked == ["9-0"]
    assert updates == []
    assert _FakeReplay.instances == []


def test_stale_pending_entry_without_db_target_is_acked_without_replay(
    monkeypatch,
) -> None:
    _activate()
    import app.worker as worker

    redis_client = _FakeRedis(
        pending_requests=[("9-0", _request("023"), 8)],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)
    monkeypatch.setattr(worker, "terminal_evidence_state", lambda *_args: "")
    monkeypatch.setattr(
        worker,
        "record_request_target_exists",
        lambda *_args, **_kwargs: False,
    )

    worker.run_worker(
        _clip_config(max_concurrent_jobs=0),
        redis_client,
        object(),
    )

    assert redis_client.claims == 1
    assert redis_client.acked == ["9-0"]
    assert updates == []
    assert _FakeReplay.instances == []


def test_replay_job_routes_to_source_shard(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    request = {**_request("020"), "source_id": "source-b"}
    redis_client = _FakeRedis([request])
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(max_concurrent_jobs=0, replay_shards=_two_replay_shards()),
        redis_client,
        object(),
    )

    assert redis_client.acked == ["1-0"]
    assert len(_FakeReplay.instances) == 1
    replay = _FakeReplay.instances[0]
    assert replay.url == "http://replay-b:8080"
    assert replay.jobs[0]["source_id"] == "source-b"
    assert replay.jobs[0]["sink_endpoint"] == "dealer+connect:tcp://video-file-sink-b:6666"
    assert updates[-1]["status"] == "replay_job_created"
    assert updates[-1]["replay_shard"]["shard_id"] == "replay-b"
    assert updates[-1]["replay_shard"]["replay_api_url"] == "http://replay-b:8080"


def test_unknown_source_fails_before_replay_job(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    request = {**_request("021"), "source_id": "source-missing"}
    redis_client = _FakeRedis([request])
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(max_concurrent_jobs=0, replay_shards=_two_replay_shards()),
        redis_client,
        object(),
    )

    assert redis_client.acked == ["1-0"]
    assert _FakeReplay.instances == []
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["evidence_reason"] == "replay_shard_routing_failed"
    assert "source-missing" in updates[-1]["error_message"]


def test_deferred_retry_budget_exhaustion_fails_closed(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    redis_client = _FakeRedis()
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    acked = worker._defer_clip_request(
        redis_client,
        object(),
        stream="security.record_requests",
        group="clip-workers-test",
        msg_id="1-0",
        event_id="00000000-0000-4000-8000-000000000004",
        request_id="req-004",
        reason="max_concurrent_reached",
        error_message="CLIP_WORKER_MAX_CONCURRENT_JOBS reached",
        retry_count=5,
        max_retries=5,
        seen_requests=set(),
    )

    assert acked is True
    assert redis_client.acked == ["1-0"]
    assert updates[-1]["status"] == "failed"
    assert "retry_budget_exhausted" in updates[-1]["error_message"]


def test_concurrency_queue_does_not_fail_on_retry_budget(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    redis_client = _FakeRedis(
        pending_requests=[
            ("9-0", _request("007"), 8),
            ("10-0", _request("008"), 8),
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(max_concurrent_jobs=1, deferred_retry_max_attempts=5),
        redis_client,
        object(),
    )

    assert redis_client.acked == ["9-0"]
    assert [update["status"] for update in updates] == ["replay_job_created", "pending"]
    assert updates[-1]["evidence_state"] == "materialization_deferred"
    assert "retry_budget_exhausted" not in updates[-1].get("error_message", "")
    assert updates[-1]["attempt_count"] == 7


def test_priority_event_bypasses_replay_concurrency(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    redis_client = _FakeRedis(
        [
            _request("009", event_type="intrusion"),
            _request("010", event_type="watchlist_hit"),
        ]
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(max_concurrent_jobs=1, pending_claim_count=0),
        redis_client,
        object(),
    )

    assert redis_client.acked == ["1-0", "2-0"]
    assert [update["status"] for update in updates] == [
        "replay_job_created",
        "replay_job_created",
    ]
    assert len(_FakeReplay.instances[-1].jobs) == 2


class _RepoCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rowcount = 1

    def __enter__(self) -> "_RepoCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.calls.append((sql, params))


class _RepoConn:
    def __init__(self) -> None:
        self.cursor_obj = _RepoCursor()

    def cursor(self) -> _RepoCursor:
        return self.cursor_obj


def test_update_clip_status_writes_operator_evidence_state() -> None:
    _activate()
    from app.repository import update_clip_status

    conn = _RepoConn()

    assert update_clip_status(
        conn,
        "00000000-0000-4000-8000-000000000099",
        "replay_job_created",
        replay_job_id="job-1",
        request_id="req-1",
        attempt_count=2,
    )

    event_update = conn.cursor_obj.calls[0][1]
    task_update = conn.cursor_obj.calls[-1][1]
    assert event_update["evidence_state"] == "materializing"
    assert event_update["materialization_status"] == "materializing"
    assert event_update["evidence_reason"] == "replay_job_created"
    assert event_update["request_id"] == "req-1"
    assert event_update["attempt_count"] == 2
    assert task_update["materialization_status"] == "materializing"
    assert task_update["attempt_count"] == 2


def test_post_savant_missing_frame_proof_defers_with_diagnostics(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    request = {
        **_request("005"),
        "replay_source_kind": "post_savant",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "stream_session_id": "session-1",
        "runtime_epoch_id": "epoch-1",
    }
    redis_client = _FakeRedis(
        [request],
        frame_annotations=[
            _frame_annotation(
                frame_uuid="latest-wrong-session",
                frame_pts=14_000_000_000,
                stream_id="10-0",
                stream_session_id="session-2",
                keyframe_uuid="latest-wrong-session",
                keyframe_pts=14_000_000_000,
            )
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            max_concurrent_jobs=0,
            pending_claim_count=0,
            post_savant_frame_proof_wait_budget_s=0.0,
            post_savant_frame_proof_poll_interval_s=0.0,
        ),
        redis_client,
        object(),
    )

    assert redis_client.acked == []
    assert [update["status"] for update in updates] == ["pending", "pending"]
    assert updates[0]["evidence_state"] == "waiting_proof"
    assert updates[-1]["evidence_state"] == "waiting_proof"
    assert updates[-1]["evidence_reason"].startswith(
        "missing_post_savant_frame_pts_window"
    )
    diagnostics = updates[-1]["diagnostics"]
    assert diagnostics["target_pts"] == 15_000_000_000
    assert diagnostics["requested_start_pts"] == 5_000_000_000
    assert diagnostics["requested_end_pts"] == 15_000_000_000
    assert diagnostics["runtime_epoch_id"] == "epoch-1"
    assert diagnostics["stream_session_id"] == "session-1"
    assert diagnostics["same_source_same_session_seen"] is False
    assert diagnostics["same_source_different_session_candidate"] is True
    assert (
        diagnostics["latest_same_source_frame_annotation"]["stream_session_id"]
        == "session-2"
    )
    assert diagnostics["cross_session_post_window_candidate"] is None
    assert diagnostics["proof_retry_count"] == 1


def test_post_savant_missing_frame_proof_fails_after_proof_retry_budget(
    monkeypatch,
) -> None:
    _activate()
    import app.worker as worker

    request = {
        **_request("018"),
        "replay_source_kind": "post_savant",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "stream_session_id": "session-1",
        "runtime_epoch_id": "epoch-1",
    }
    redis_client = _FakeRedis([request])
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            max_concurrent_jobs=0,
            pending_claim_count=0,
            deferred_retry_max_attempts=2,
            post_savant_frame_proof_wait_budget_s=0.0,
            post_savant_frame_proof_poll_interval_s=0.0,
        ),
        redis_client,
        _DiagnosticsConn({"proof_retry_count": 2}),
    )

    assert redis_client.acked == ["1-0"]
    assert updates[-1]["evidence_state"] == "failed"
    assert updates[-1]["evidence_reason"].startswith(
        "retry_budget_exhausted reason=missing_post_savant_frame_proof"
    )
    assert updates[-1]["diagnostics"]["proof_retry_count"] == 2


def test_post_savant_reclaimed_proof_request_uses_single_shot(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    request = {
        **_request("019"),
        "replay_source_kind": "post_savant",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "stream_session_id": "session-1",
        "runtime_epoch_id": "epoch-1",
    }
    redis_client = _FakeRedis(pending_requests=[("9-0", request, 2)])
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            max_concurrent_jobs=0,
            post_savant_frame_proof_wait_budget_s=1.0,
            post_savant_frame_proof_poll_interval_s=0.1,
        ),
        redis_client,
        _DiagnosticsConn({"proof_retry_count": 1}),
    )

    waiting_updates = [
        update for update in updates if update.get("evidence_state") == "waiting_proof"
    ]
    assert redis_client.acked == []
    assert redis_client.claims == 1
    assert len(waiting_updates) == 2
    assert waiting_updates[0]["diagnostics"]["proof_attempts"] == 1
    assert updates[-1]["diagnostics"]["proof_retry_count"] == 2


def test_post_savant_cross_session_post_window_proof_can_create_job(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    request = {
        **_request("012"),
        "replay_source_kind": "post_savant",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "stream_session_id": "session-1",
        "runtime_epoch_id": "epoch-1",
        "keyframe_uuid": "start-keyframe",
        "keyframe_pts": 5_000_000_000,
    }
    redis_client = _FakeRedis(
        [request],
        frame_annotations=[
            _frame_annotation(
                frame_uuid="start-keyframe",
                frame_pts=5_000_000_000,
                stream_id="1779999995000-0",
                stream_session_id="session-1",
                keyframe_uuid="start-keyframe",
                keyframe_pts=5_000_000_000,
            ),
            _frame_annotation(
                frame_uuid="post-window-frame",
                frame_pts=15_000_000_000,
                stream_id="1780000005000-0",
                stream_session_id="session-2",
                keyframe_uuid="post-session-keyframe",
                keyframe_pts=14_900_000_000,
            ),
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            max_concurrent_jobs=0,
            pending_claim_count=0,
            post_savant_frame_proof_wait_budget_s=0.0,
            post_savant_frame_proof_poll_interval_s=0.0,
            post_savant_allow_cross_session_post_window_proof=True,
        ),
        redis_client,
        object(),
    )

    assert redis_client.acked == ["1-0"]
    assert updates[-1]["status"] == "replay_job_created"
    labels = _FakeReplay.instances[-1].last_job_request["labels"]
    assert labels["stream_session_id"] == "session-1"
    assert labels["start_window_stream_session_id"] == "session-1"
    assert labels["post_window_stream_session_id"] == "session-2"
    assert labels["post_window_cross_session_proof_used"] == "true"
    assert labels["frame_domain_session_policy"] == (
        "post_window_cross_session_pts_verified"
    )
    assert labels["frame_domain_proof_method"] == (
        "frame_cache_start_window_keyframe_and_cross_session_post_window_pts"
    )


def test_post_savant_cross_session_post_window_proof_can_be_disabled(
    monkeypatch,
) -> None:
    _activate()
    import app.worker as worker

    request = {
        **_request("013"),
        "replay_source_kind": "post_savant",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "stream_session_id": "session-1",
        "runtime_epoch_id": "epoch-1",
        "keyframe_uuid": "start-keyframe",
        "keyframe_pts": 5_000_000_000,
    }
    redis_client = _FakeRedis(
        [request],
        frame_annotations=[
            _frame_annotation(
                frame_uuid="start-keyframe",
                frame_pts=5_000_000_000,
                stream_id="1779999995000-0",
                stream_session_id="session-1",
                keyframe_uuid="start-keyframe",
                keyframe_pts=5_000_000_000,
            ),
            _frame_annotation(
                frame_uuid="post-window-frame",
                frame_pts=15_000_000_000,
                stream_id="1780000005000-0",
                stream_session_id="session-2",
                keyframe_uuid="post-session-keyframe",
                keyframe_pts=14_900_000_000,
            ),
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            max_concurrent_jobs=0,
            pending_claim_count=0,
            post_savant_frame_proof_wait_budget_s=0.0,
            post_savant_frame_proof_poll_interval_s=0.0,
            post_savant_allow_cross_session_post_window_proof=False,
        ),
        redis_client,
        object(),
    )

    assert redis_client.acked == []
    assert updates[-1]["evidence_state"] == "waiting_proof"
    assert updates[-1]["evidence_reason"].startswith(
        "missing_post_savant_frame_pts_window"
    )
    assert not _FakeReplay.instances[-1].jobs
    diagnostics = updates[-1]["diagnostics"]
    assert diagnostics["same_source_different_session_candidate"] is True
    assert diagnostics["cross_session_post_window_candidate"]["frame_uuid"] == (
        "post-window-frame"
    )


def test_post_savant_truncated_pre_window_can_create_job(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    request = {
        **_request("014"),
        "replay_source_kind": "post_savant",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "stream_session_id": "session-2",
        "runtime_epoch_id": "epoch-1",
        "keyframe_uuid": "current-session-keyframe",
        "keyframe_pts": 9_250_000_000,
    }
    redis_client = _FakeRedis(
        [request],
        frame_annotations=[
            _frame_annotation(
                frame_uuid="old-session-start",
                frame_pts=5_000_000_000,
                stream_id="1779999995000-0",
                stream_session_id="session-1",
                keyframe_uuid="old-session-start",
                keyframe_pts=5_000_000_000,
            ),
            _frame_annotation(
                frame_uuid="current-session-keyframe",
                frame_pts=9_250_000_000,
                stream_id="1779999999250-0",
                stream_session_id="session-2",
                keyframe_uuid="current-session-keyframe",
                keyframe_pts=9_250_000_000,
            ),
            _frame_annotation(
                frame_uuid="event-frame",
                frame_pts=10_000_000_000,
                stream_id="1780000000000-0",
                stream_session_id="session-2",
                keyframe_uuid="current-session-keyframe",
                keyframe_pts=9_250_000_000,
            ),
            _frame_annotation(
                frame_uuid="post-window-frame",
                frame_pts=15_000_000_000,
                stream_id="1780000005000-0",
                stream_session_id="session-2",
                keyframe_uuid="post-session-keyframe",
                keyframe_pts=14_900_000_000,
            ),
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            max_concurrent_jobs=0,
            pending_claim_count=0,
            post_savant_frame_proof_wait_budget_s=0.0,
            post_savant_frame_proof_poll_interval_s=0.0,
            post_savant_allow_truncated_pre_window_proof=True,
        ),
        redis_client,
        object(),
    )

    assert redis_client.acked == ["1-0"]
    assert updates[-1]["status"] == "replay_job_created"
    job = _FakeReplay.instances[-1].last_job_request
    labels = job["labels"]
    assert labels["original_requested_start_pts"] == "5000000000"
    assert labels["effective_start_pts"] == "9250000000"
    assert labels["requested_start_pts"] == "9250000000"
    assert labels["requested_end_pts"] == "15000000000"
    assert labels["pre_window_truncated"] == "true"
    assert labels["pre_window_policy"] == "truncated_to_current_session"
    assert labels["requested_pre_window_seconds"] == "5.0"
    assert labels["effective_pre_window_seconds"] == "0.75"
    assert labels["pre_window_truncated_seconds"] == "4.25"
    assert labels["frame_domain_session_policy"] == "strict_single_session"
    assert labels["frame_domain_proof_method"] == (
        "frame_cache_truncated_start_window_keyframe_reference_and_post_window_pts"
    )
    assert job["offset_seconds_override"] == 0.0
    assert job["duration_seconds_override"] == 16.75


def test_post_savant_truncated_pre_window_can_be_disabled(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    request = {
        **_request("015"),
        "replay_source_kind": "post_savant",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "stream_session_id": "session-2",
        "runtime_epoch_id": "epoch-1",
        "keyframe_uuid": "current-session-keyframe",
        "keyframe_pts": 9_250_000_000,
    }
    redis_client = _FakeRedis(
        [request],
        frame_annotations=[
            _frame_annotation(
                frame_uuid="current-session-keyframe",
                frame_pts=9_250_000_000,
                stream_id="1779999999250-0",
                stream_session_id="session-2",
                keyframe_uuid="current-session-keyframe",
                keyframe_pts=9_250_000_000,
            ),
            _frame_annotation(
                frame_uuid="post-window-frame",
                frame_pts=15_000_000_000,
                stream_id="1780000005000-0",
                stream_session_id="session-2",
                keyframe_uuid="post-session-keyframe",
                keyframe_pts=14_900_000_000,
            ),
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            max_concurrent_jobs=0,
            pending_claim_count=0,
            post_savant_frame_proof_wait_budget_s=0.0,
            post_savant_frame_proof_poll_interval_s=0.0,
            post_savant_allow_truncated_pre_window_proof=False,
        ),
        redis_client,
        object(),
    )

    assert redis_client.acked == []
    assert updates[-1]["evidence_state"] == "waiting_proof"
    assert updates[-1]["evidence_reason"].startswith(
        "missing_post_savant_frame_pts_window"
    )
    assert not _FakeReplay.instances[-1].jobs
    diagnostics = updates[-1]["diagnostics"]
    assert diagnostics["truncated_pre_window_candidate"]["frame_uuid"] == (
        "current-session-keyframe"
    )
    assert diagnostics["truncated_pre_window_candidate"]["effective_start_pts"] == (
        9_250_000_000
    )


def test_post_savant_frame_proof_wait_respects_deadline(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    request = {
        **_request("011"),
        "replay_source_kind": "post_savant",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "stream_session_id": "session-1",
        "runtime_epoch_id": "epoch-1",
    }

    class SlowRedis(_FakeRedis):
        def xrevrange(self, *_args, **_kwargs):
            worker.time.sleep(0.015)
            return super().xrevrange(*_args, **_kwargs)

    redis_client = SlowRedis([request])
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    started = worker.time.monotonic()
    worker.run_worker(
        _clip_config(
            max_concurrent_jobs=0,
            pending_claim_count=0,
            post_savant_frame_proof_wait_budget_s=0.02,
            post_savant_frame_proof_poll_interval_s=0.01,
        ),
        redis_client,
        object(),
    )
    elapsed_s = worker.time.monotonic() - started

    waiting_updates = [
        update for update in updates if update.get("evidence_state") == "waiting_proof"
    ]
    assert redis_client.acked == []
    assert updates[-1]["evidence_state"] == "waiting_proof"
    assert len(waiting_updates) <= 2
    assert elapsed_s < 0.12


def test_post_savant_frame_proof_succeeds_after_local_poll(monkeypatch) -> None:
    _activate()
    import app.worker as worker

    request = {
        **_request("006"),
        "replay_source_kind": "post_savant",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "stream_session_id": "session-1",
        "runtime_epoch_id": "epoch-1",
        "keyframe_uuid": "start-keyframe",
        "keyframe_pts": 5_000_000_000,
    }

    class DelayedProofRedis(_FakeRedis):
        def __init__(self) -> None:
            super().__init__([request], frame_annotations=[])
            self.delayed_reads = 0

        def xrevrange(self, *_args, **_kwargs):
            self.delayed_reads += 1
            if self.delayed_reads <= 2:
                self.frame_annotations = []
            else:
                self.frame_annotations = [
                    _frame_annotation(
                        frame_uuid="start-keyframe",
                        frame_pts=5_000_000_000,
                        stream_id="1779999995000-0",
                        keyframe_uuid="start-keyframe",
                        keyframe_pts=5_000_000_000,
                    ),
                    _frame_annotation(
                        frame_uuid="post-window-frame",
                        frame_pts=15_000_000_000,
                        stream_id="1780000000000-0",
                        keyframe_uuid="start-keyframe",
                        keyframe_pts=5_000_000_000,
                    ),
                ]
            return super().xrevrange(*_args, **_kwargs)

    redis_client = DelayedProofRedis()
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            max_concurrent_jobs=0,
            pending_claim_count=0,
            post_savant_frame_proof_wait_budget_s=0.03,
            post_savant_frame_proof_poll_interval_s=0.01,
        ),
        redis_client,
        object(),
    )

    assert redis_client.acked == ["1-0"]
    assert [update["status"] for update in updates][:-1]
    assert all(update["status"] == "pending" for update in updates[:-1])
    assert updates[-1]["status"] == "replay_job_created"
    assert updates[0]["evidence_state"] == "waiting_proof"
    assert updates[0]["attempt_count"] == 1
    assert _FakeReplay.instances[-1].jobs
    labels = _FakeReplay.instances[-1].last_job_request["labels"]
    assert labels["post_window_frame_uuid"] == "post-window-frame"
    assert labels["start_window_frame_uuid"] == "start-keyframe"
    assert labels["post_window_cross_session_proof_used"] == "false"
    assert labels["frame_domain_session_policy"] == "strict_single_session"
