"""C1I.1f person bbox observation stream contract."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = str(ROOT / "modules" / "savant_security")
EVENT_WORKER_DIR = str(ROOT / "services" / "event-worker")
MEDIA_WORKER_DIR = str(ROOT / "services" / "media-worker")
CLIP_WORKER_DIR = str(ROOT / "services" / "clip-worker")
COMPOSE = ROOT / "infra" / "docker-compose.c1-official-replay-dev.yml"
ENV_FILE = ROOT / "infra" / "env" / "c1-official-replay-dev.env"
MIGRATION = ROOT / "db" / "migrations" / "011_c1i1f_person_bbox_observations.sql"
SMOKE = ROOT / "scripts" / "smoke" / "current" / "check_c1i1f_person_bbox_observation_stream.sh"
DOCTOR = ROOT / "scripts" / "runtime" / "doctor_c1_official.sh"


def _activate(path: str) -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
        if name == "custom" or name.startswith("custom."):
            del sys.modules[name]
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)


def _pose_obs(*, confidence: float = 0.82, track_id: int = 7):
    _activate(MODULE_DIR)
    from custom.models.pose import BBox, PersonPoseObservation

    return PersonPoseObservation(
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        frame_id=42,
        timestamp_ms=1_780_000_010_000,
        bbox=BBox(x=100, y=200, width=160, height=400),
        confidence=confidence,
        track_id=track_id,
        keypoints=[],
    )


def _event_context(event_type: str = "intrusion") -> dict[str, Any]:
    payload: dict[str, Any] = {"media": {"frame_num": 42}}
    if event_type == "intrusion":
        payload["person_bbox"] = {
            "format": "xyxy",
            "xyxy": [100, 200, 260, 600],
        }
    else:
        payload["match"] = {
            "source_observation_id": "face:obs:1",
            "similarity": 0.9,
            "threshold": 0.5,
        }
        payload["matched_person"] = {"person_id": 1, "name": "Reese"}
    return {
        "event_id": "ev-c1i1f",
        "event_type": event_type,
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "track_id": "7",
        "event_ts_ms": 1_780_000_010_000,
        "confidence": 0.82,
        "severity": "high",
        "payload": payload,
    }


class _FakeRedisClient:
    instances: list["_FakeRedisClient"] = []

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, str], int | None, bool | None]] = []
        _FakeRedisClient.instances.append(self)

    def xadd(self, stream: str, fields: dict[str, str], maxlen=None, approximate=None):
        self.messages.append((stream, fields, maxlen, approximate))
        return b"1-0"


def _install_fake_redis() -> None:
    fake_module = types.SimpleNamespace(
        Redis=types.SimpleNamespace(from_url=lambda *_a, **_kw: _FakeRedisClient())
    )
    sys.modules["redis"] = fake_module
    _FakeRedisClient.instances.clear()


def _export_observations(observations: list[Any]) -> list[dict[str, Any]]:
    _activate(MODULE_DIR)
    _install_fake_redis()
    from custom.models.person_events import (
        PersonBBoxObservationEventDraft,
        build_person_source_observation_id,
    )
    from custom.services.person_observation_exporter import (
        RedisStreamPersonObservationExporter,
    )

    exporter = RedisStreamPersonObservationExporter(
        redis_url="redis://example/0",
        stream="security.person_observations",
        maxlen=100,
    )
    for index, obs in enumerate(observations):
        exporter.export(
            PersonBBoxObservationEventDraft(
                source_observation_id=build_person_source_observation_id(
                    obs.source_id,
                    obs.track_id,
                    obs.timestamp_ms,
                    index,
                ),
                source_id=obs.source_id,
                camera_id=obs.camera_id,
                track_id=str(obs.track_id),
                timestamp_ms=obs.timestamp_ms,
                frame_pts=123456789,
                frame_num=obs.frame_id,
                person_bbox=[float(v) for v in obs.bbox.xyxy],
                person_confidence=obs.confidence,
                gate_status="accepted",
                payload={"media": {"frame_pts": 123456789}},
            )
        )
    client = _FakeRedisClient.instances[-1]
    return [json.loads(fields["data"]) for _stream, fields, _maxlen, _approx in client.messages]


def test_accepted_person_bbox_generates_redis_message() -> None:
    messages = _export_observations([_pose_obs()])

    assert len(messages) == 1
    msg = messages[0]
    assert msg["message_type"] == "person_bbox_observation"
    assert msg["source_observation_id"].startswith("person:c1e_rtsp_replay:7:")
    assert msg["person_bbox"] == [100.0, 200.0, 260.0, 600.0]
    assert msg["gate_status"] == "accepted"


def test_low_confidence_person_bbox_does_not_generate_message() -> None:
    _activate(MODULE_DIR)
    from custom.models.pose import PersonQualityGateConfig, filter_person_pose_observations

    result = filter_person_pose_observations(
        [_pose_obs(confidence=0.05)],
        PersonQualityGateConfig(min_confidence=0.25),
        frame_width=1920,
        frame_height=1080,
    )
    assert result.accepted_observations == []
    assert _export_observations(result.accepted_observations) == []


def test_multiple_people_same_frame_all_generate_messages() -> None:
    messages = _export_observations([
        _pose_obs(track_id=7),
        _pose_obs(track_id=8),
    ])

    assert len(messages) == 2
    assert {msg["track_id"] for msg in messages} == {"7", "8"}


def test_person_message_has_no_keypoints_image_bytes_or_embedding() -> None:
    message_text = json.dumps(_export_observations([_pose_obs()])[0]).lower()

    assert "keypoint" not in message_text
    assert "embedding" not in message_text
    assert "image" not in message_text
    assert "jpeg" not in message_text
    assert "png" not in message_text


class _FakeConsumer:
    def __init__(self) -> None:
        self.acked: list[str] = []

    def ack(self, msg_id: str) -> bool:
        self.acked.append(msg_id)
        return True


class _FakeRepo:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    def insert_person_bbox_observation(self, observation: dict[str, Any]) -> str | None:
        obs_id = observation["source_observation_id"]
        if obs_id in self.seen:
            return None
        self.seen.add(obs_id)
        return "row-1"


def _person_stream_fields(source_observation_id: str = "person:c1e:7:1000:0"):
    data = {
        "source_observation_id": source_observation_id,
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "track_id": "7",
        "timestamp_ms": 1_780_000_010_000,
        "person_bbox": [100, 200, 260, 600],
        "person_confidence": 0.82,
        "gate_status": "accepted",
        "payload": {},
    }
    return {b"data": json.dumps(data).encode("utf-8")}


def test_event_worker_writes_person_bbox_observations_idempotently() -> None:
    _activate(EVENT_WORKER_DIR)
    from app.worker import _process_person_observation_batch

    consumer = _FakeConsumer()
    inserted, duplicates, skipped, failed = _process_person_observation_batch(
        [
            ("1-0", _person_stream_fields("person:c1e:7:1000:0")),
            ("1-1", _person_stream_fields("person:c1e:7:1000:0")),
        ],
        _FakeRepo(),
        consumer,
    )

    assert (inserted, duplicates, skipped, failed) == (1, 1, 0, 0)
    assert consumer.acked == ["1-0", "1-1"]


def test_media_worker_generates_person_context_from_person_bbox_observations() -> None:
    _activate(MEDIA_WORKER_DIR)
    from app.continuous_annotation import (
        extract_person_bbox_timeline_from_observations,
    )

    lines, summary = extract_person_bbox_timeline_from_observations(
        observations=[
            {
                "source_observation_id": "person:c1e:7:1000:0",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "track_id": "7",
                "timestamp_ms": 1_780_000_010_000,
                "frame_pts": 123456789,
                "frame_num": 42,
                "person_bbox": [100, 200, 260, 600],
                "person_confidence": 0.82,
                "gate_status": "accepted",
                "payload": {},
            }
        ],
        event_context=_event_context(),
        start_ts_ms=1_780_000_005_000,
    )

    assert len(lines) == 1
    assert lines[0]["annotation_role"] == "person_context"
    assert lines[0]["bbox"]["source"] == "person_bbox_observations.person_bbox"
    assert lines[0]["bbox"]["xyxy"] == [100.0, 200.0, 260.0, 600.0]
    assert lines[0]["time_offset_ms"] == 5000
    assert summary["person_observation_count"] == 1


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn
        self.rows: list[dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql: str, params: dict[str, Any] | tuple[Any, ...] | None = None):
        self.conn.calls.append((sql, params))
        if "FROM person_bbox_observations" in sql:
            self.rows = [
                {
                    "id": "p1",
                    "source_observation_id": "person:c1e:7:1000:0",
                    "source_id": "c1e_rtsp_replay",
                    "camera_id": "cam_c1e_rtsp_replay",
                    "track_id": "7",
                    "timestamp_ms": 1_780_000_010_000,
                    "frame_pts": 123456789,
                    "frame_num": 42,
                    "person_bbox": [100, 200, 260, 600],
                    "person_confidence": 0.82,
                    "gate_status": "accepted",
                    "payload": {},
                }
            ]
        elif "FROM face_observations" in sql:
            self.rows = [
                {
                    "id": "f1",
                    "source_observation_id": "face:obs:1",
                    "camera_id": "cam_c1e_rtsp_replay",
                    "source_id": "c1e_rtsp_replay",
                    "track_id": "7",
                    "timestamp_ms": 1_780_000_010_000,
                    "frame_num": 42,
                    "face_bbox": [180, 220, 80, 80],
                    "landmarks": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                    "face_confidence": 0.88,
                    "quality": 0.91,
                    "detector_model": "yolov8_face",
                    "embedding_model": "adaface",
                    "embedding_dim": 512,
                    "embedding_norm": 1.0,
                    "payload": {"media": {"frame_pts": 123456789}},
                }
            ]
        else:
            self.rows = []

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def cursor(self, *_, **__):
        return _Cursor(self)


def test_person_query_window_uses_source_camera_and_timestamp_ms() -> None:
    _activate(MEDIA_WORKER_DIR)
    from app.continuous_annotation import _load_person_bbox_observations

    conn = _Conn()
    rows = _load_person_bbox_observations(
        conn,
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        start_ts_ms=1,
        end_ts_ms=2,
    )

    assert rows
    sql, params = conn.calls[0]
    assert "source_id = %(source_id)s" in sql
    assert "camera_id = %(camera_id)s" in sql
    assert "timestamp_ms BETWEEN %(start_ts_ms)s AND %(end_ts_ms)s" in sql
    assert params == {
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "start_ts_ms": 1,
        "end_ts_ms": 2,
    }


class _CreatedAtFallbackCursor:
    def __init__(self, conn: "_CreatedAtFallbackConn") -> None:
        self.conn = conn
        self.rows: list[dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql: str, params: dict[str, Any] | None = None):
        self.conn.calls.append((sql, params))
        if "timestamp_ms BETWEEN" in sql:
            self.rows = []
        elif "created_at BETWEEN" in sql:
            self.rows = [
                {
                    "id": "p-created",
                    "source_observation_id": "person:c1e:7:old-ts:0",
                    "source_id": "c1e_rtsp_replay",
                    "camera_id": "cam_c1e_rtsp_replay",
                    "track_id": "7",
                    "timestamp_ms": 1_780_472_819_976,
                    "frame_pts": 123456789,
                    "frame_num": 42,
                    "person_bbox": [100, 200, 260, 600],
                    "person_confidence": 0.82,
                    "gate_status": "accepted",
                    "payload": {},
                    "created_at": "2026-06-03T09:14:11+00:00",
                    "time_offset_ms": 5000,
                    "time_basis": "created_at",
                }
            ]
        else:
            self.rows = []

    def fetchall(self):
        return self.rows


class _CreatedAtFallbackConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def cursor(self, *_, **__):
        return _CreatedAtFallbackCursor(self)


def test_media_worker_created_at_fallback_handles_misaligned_person_timestamp() -> None:
    _activate(MEDIA_WORKER_DIR)
    from app.continuous_annotation import extract_person_bbox_timeline_from_db

    context = _event_context()
    context["created_at"] = "2026-06-03T09:14:11+00:00"
    lines, summary = extract_person_bbox_timeline_from_db(
        _CreatedAtFallbackConn(),
        event_context=context,
        start_ts_ms=1_780_478_046_020,
        end_ts_ms=1_780_478_056_020,
    )

    assert len(lines) == 1
    assert lines[0]["annotation_role"] == "person_context"
    assert lines[0]["time_offset_ms"] == 5000
    assert lines[0]["bbox"]["source"] == "person_bbox_observations.person_bbox"
    assert summary["person_observation_lookup_mode"] == "created_at_fallback"


def _person_context(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        line
        for line in lines
        if line.get("record_type") == "object_annotation"
        and line.get("annotation_role") == "person_context"
    ]


def test_intrusion_behavior_event_is_not_overwritten_by_person_context() -> None:
    _activate(MEDIA_WORKER_DIR)
    from app.continuous_annotation import build_continuous_annotations

    lines, summary = build_continuous_annotations(_Conn(), _event_context())
    behavior = [
        line
        for line in lines
        if line.get("record_type") == "object_annotation"
        and line.get("annotation_role") == "behavior_event"
    ]

    assert len(_person_context(lines)) == 1
    assert len(behavior) == 1
    assert behavior[0]["label"]["text"] == "Intrusion"
    assert behavior[0]["style"]["bbox_color"] == "#FF6D00"
    assert summary["person_context_count"] == 1


def test_watchlist_evidence_can_contain_face_annotation_and_person_context() -> None:
    _activate(MEDIA_WORKER_DIR)
    from app.continuous_annotation import build_continuous_annotations

    lines, summary = build_continuous_annotations(
        _Conn(),
        _event_context("watchlist_hit"),
    )
    face_lines = [
        line for line in lines
        if any(obj.get("object_type") == "face" for obj in line.get("objects", []))
    ]

    assert face_lines
    assert _person_context(lines)
    assert summary["face_objects"] >= 1
    assert summary["person_context_count"] == 1


def test_person_context_count_positive_is_required_for_pass_marker() -> None:
    assert SMOKE.exists()
    text = SMOKE.read_text(encoding="utf-8")

    assert "security.person_observations" in text
    assert "person_bbox_observations" in text
    assert "person_context_count" in text
    assert "PASS_C1I1F_PERSON_BBOX_OBSERVATION_STREAM" in text
    assert "FAIL_C1I1F_CLIP_WORKER_MAX_JOBS_EXHAUSTED" in text
    assert "PARTIAL_C1I1F_CLIP_OR_MEDIA_PENDING" in text
    assert "FAIL_C1I1F_NO_PERSON_CONTEXT_IN_EVIDENCE" in text
    assert "replay_job_created" in text
    assert "max_jobs_reached" in text
    assert "person_context_count > 0" in text or "person_context_count'] > 0" in text


def _clip_config(**overrides: Any):
    _activate(CLIP_WORKER_DIR)
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
        "run_once": False,
        "max_concurrent_jobs": 1,
        "per_camera_cooldown_seconds": 30,
        "replay_stop_condition_mode": "ts_delta_sec",
        "replay_fps": 30,
        "replay_anchor_strategy": "request_keyframe",
        "allow_unbounded_keyframe_fallback": False,
    }
    values.update(overrides)
    return Config(**values)


def test_clip_worker_max_jobs_per_run_does_not_block_long_running_mode() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _max_jobs_limit_reached

    cfg = _clip_config(max_jobs_per_run=100, run_once=False)

    assert _max_jobs_limit_reached(cfg, jobs_created=100) is False
    assert _max_jobs_limit_reached(cfg, jobs_created=1000) is False


def test_clip_worker_max_concurrent_still_blocks_when_active_job_limit_reached() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _clip_gate_decision

    decision = _clip_gate_decision(
        _clip_config(
            run_once=False,
            max_jobs_per_run=100,
            max_concurrent_jobs=1,
            per_camera_cooldown_seconds=0,
        ),
        jobs_created=1000,
        active_job_count=1,
        camera_id="cam_c1e_rtsp_replay",
        cooldown_gate_ts_ms=1_780_000_010_000,
        last_job_by_camera={},
    )

    assert decision.allowed is False
    assert decision.reason == "max_concurrent_reached"
    assert decision.error_message == "CLIP_WORKER_MAX_CONCURRENT_JOBS reached"


def test_clip_worker_per_camera_cooldown_still_blocks_same_camera() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _clip_gate_decision

    decision = _clip_gate_decision(
        _clip_config(
            run_once=False,
            max_jobs_per_run=100,
            max_concurrent_jobs=0,
            per_camera_cooldown_seconds=30,
        ),
        jobs_created=1000,
        active_job_count=0,
        camera_id="cam_c1e_rtsp_replay",
        cooldown_gate_ts_ms=1_780_000_010_000,
        last_job_by_camera={"cam_c1e_rtsp_replay": 1_780_000_000_000},
    )

    assert decision.allowed is False
    assert decision.reason == "cooldown"
    assert decision.error_message == "CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS reached"


def test_clip_worker_run_once_mode_can_enforce_max_jobs_cap() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _clip_gate_decision

    decision = _clip_gate_decision(
        _clip_config(
            run_once=True,
            max_jobs_per_run=2,
            max_concurrent_jobs=0,
            per_camera_cooldown_seconds=0,
        ),
        jobs_created=2,
        active_job_count=0,
        camera_id="cam_c1e_rtsp_replay",
        cooldown_gate_ts_ms=1_780_000_010_000,
        last_job_by_camera={},
    )

    assert decision.allowed is False
    assert decision.reason == "max_jobs_reached"
    assert decision.error_message == "CLIP_WORKER_MAX_JOBS_PER_RUN reached"


class _ClipFakeRedis:
    def __init__(self, request: dict[str, Any]) -> None:
        self.request = request
        self.acked: list[str] = []
        self.reads = 0

    def xgroup_create(self, *_args, **_kwargs):
        return None

    def xreadgroup(self, *_args, **_kwargs):
        self.reads += 1
        if self.reads > 1:
            return []
        fields = {b"data": json.dumps(self.request).encode("utf-8")}
        return [(b"security.record_requests", [(b"1-0", fields)])]

    def xack(self, _stream, _group, msg_id):
        self.acked.append(msg_id.decode("utf-8") if isinstance(msg_id, bytes) else msg_id)
        return 1


class _ClipFakeReplay:
    instances: list["_ClipFakeReplay"] = []

    def __init__(self, _url: str) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.last_job_request: dict[str, Any] = {}
        _ClipFakeReplay.instances.append(self)

    def find_keyframe(self, *_args, **_kwargs):
        raise AssertionError("keyframe lookup should be bypassed")

    def create_job(self, **kwargs):
        self.jobs.append(kwargs)
        self.last_job_request = dict(kwargs)
        return "replay-job-1"


def test_clip_worker_submits_replay_job_when_record_request_allowed(monkeypatch) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    request = {
        "request_id": "req-1",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "source_event_id": "evt-1",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 1_780_000_010_000,
        "strategy": "savant_replay",
        "keyframe_uuid": "kf-1",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(request)
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            run_once=True,
            max_jobs_per_run=100,
            max_concurrent_jobs=0,
            per_camera_cooldown_seconds=0,
        ),
        redis_client,
        object(),
    )

    replay = _ClipFakeReplay.instances[-1]
    assert len(replay.jobs) == 1
    assert replay.jobs[0]["source_id"] == "c1e_rtsp_replay"
    assert replay.jobs[0]["keyframe_uuid"] == "kf-1"
    assert redis_client.acked == ["1-0"]
    assert updates[-1]["status"] == "replay_job_created"


def test_c1i1f_config_and_migration_contract() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    env = ENV_FILE.read_text(encoding="utf-8")
    migration = MIGRATION.read_text(encoding="utf-8")
    doctor = DOCTOR.read_text(encoding="utf-8")

    assert "PERSON_OBSERVATION_EXPORT_ENABLED=true" in env
    assert "PERSON_OBSERVATION_MIN_INTERVAL_MS=1000" in env
    assert "PERSON_OBSERVATION_STREAM=security.person_observations" in env
    assert "CLIP_WORKER_RUN_ONCE=false" in env
    assert "PERSON_OBSERVATION_CONSUMER_ENABLED" in compose
    assert "security.person_observations" in compose
    assert "CLIP_WORKER_RUN_ONCE" in compose
    assert "CLIP_WORKER_RUN_ONCE" in doctor
    assert "max_jobs_exhausted_suspected" in doctor
    assert "CREATE TABLE IF NOT EXISTS person_bbox_observations" in migration
    assert "source_observation_id   TEXT NOT NULL UNIQUE" in migration
    assert "person_bbox             JSONB NOT NULL" in migration
    assert "idx_person_bbox_obs_source_ts" in migration
    assert "idx_person_bbox_obs_camera_ts" in migration
