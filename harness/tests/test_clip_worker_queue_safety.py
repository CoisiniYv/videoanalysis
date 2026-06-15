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

    values = {
        "redis_url": "redis://redis:6379/0",
        "record_request_stream": "security.record_requests",
        "replay_api_url": "http://replay-service:8080",
        "replay_job_sink_url": "dealer+connect:tcp://video-file-sink:6666",
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
        "frame_annotation_stream": "security.frame_annotations",
        "frame_annotation_anchor_lookback_count": 100,
        "frame_annotation_anchor_wall_clock_slack_s": 1.0,
        "frame_annotation_anchor_pts_tolerance_s": 1.0,
    }
    values.update(overrides)
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

    def __init__(self, _url: str) -> None:
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


def test_concurrency_pressure_defers_without_permanent_skip(monkeypatch) -> None:
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
    assert all(update["status"] != "skipped_by_poc_limit" for update in updates)


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
    assert event_update["evidence_state"] == "replaying"
    assert event_update["evidence_reason"] == "replay_job_created"
    assert event_update["request_id"] == "req-1"
    assert event_update["attempt_count"] == 2
    assert task_update["evidence_state"] == "replaying"
    assert task_update["attempt_count"] == 2


def test_post_savant_missing_frame_proof_is_deferred(monkeypatch) -> None:
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
        _clip_config(max_concurrent_jobs=0, pending_claim_count=0),
        redis_client,
        object(),
    )

    assert redis_client.acked == []
    assert updates[-1]["status"] == "pending"
    assert "post_savant_frame_proof" in updates[-1]["error_message"]
