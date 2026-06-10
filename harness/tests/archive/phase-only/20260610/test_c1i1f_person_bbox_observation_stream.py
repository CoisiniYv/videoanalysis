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

    event_context = _event_context("watchlist_hit")
    event_context["created_at"] = "2026-06-03T09:14:11+00:00"

    lines, summary = build_continuous_annotations(
        _Conn(),
        event_context,
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
        "replay_duration_extra_slack_s": 0.0,
        "replay_anchor_strategy": "request_keyframe",
        "allow_unbounded_keyframe_fallback": False,
        "keyframe_lookup_retries": 0,
        "keyframe_lookup_retry_sleep_s": 0.0,
        "frame_annotation_stream": "security.frame_annotations",
        "frame_annotation_anchor_lookback_count": 100,
        "frame_annotation_anchor_wall_clock_slack_s": 1.0,
        "frame_annotation_anchor_pts_tolerance_s": 1.0,
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


def test_clip_worker_priority_event_bypasses_poc_concurrency_and_cooldown() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _clip_gate_decision

    decision = _clip_gate_decision(
        _clip_config(
            run_once=False,
            max_jobs_per_run=100,
            max_concurrent_jobs=1,
            per_camera_cooldown_seconds=30,
        ),
        jobs_created=1000,
        active_job_count=1,
        camera_id="c2_replay_first_rtsp",
        cooldown_gate_ts_ms=1_780_000_010_000,
        last_job_by_camera={"c2_replay_first_rtsp": 1_780_000_000_000},
        event_type="watchlist_hit",
    )

    assert decision.allowed is True


def test_clip_worker_derives_priority_event_type_from_legacy_source_event_id() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _record_request_event_type

    assert (
        _record_request_event_type(
            {"source_event_id": "watchlist_hit:face:c2_replay_first_rtsp:49:374708:6"}
        )
        == "watchlist_hit"
    )


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
    def __init__(
        self,
        request: dict[str, Any] | list[dict[str, Any]],
        frame_annotations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.requests = request if isinstance(request, list) else [request]
        self.frame_annotations = frame_annotations or []
        self.acked: list[str] = []
        self.reads = 0

    def xgroup_create(self, *_args, **_kwargs):
        return None

    def xreadgroup(self, *_args, **_kwargs):
        self.reads += 1
        if self.reads > 1:
            return []
        entries = []
        for index, request in enumerate(self.requests, start=1):
            fields = {b"data": json.dumps(request).encode("utf-8")}
            entries.append((f"{index}-0".encode("utf-8"), fields))
        return [(b"security.record_requests", entries)]

    def xack(self, _stream, _group, msg_id):
        self.acked.append(msg_id.decode("utf-8") if isinstance(msg_id, bytes) else msg_id)
        return 1

    def xrevrange(self, _stream, max="+", min="-", count=100):
        _ = (max, min)
        entries = []
        for index, message in enumerate(reversed(self.frame_annotations[:count])):
            stream_id = message.get("_stream_id", f"{index + 1}-0")
            entries.append(
                (
                    str(stream_id).encode("utf-8"),
                    {b"data": json.dumps(message).encode("utf-8")},
                )
            )
        return entries


class _ClipFakeReplay:
    instances: list["_ClipFakeReplay"] = []
    allow_find_keyframe = False
    keyframe_uuid = "lookup-kf-1"

    def __init__(self, _url: str) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.find_keyframe_calls: list[dict[str, Any]] = []
        self.last_job_request: dict[str, Any] = {}
        self.last_job_payload: dict[str, Any] = {}
        _ClipFakeReplay.instances.append(self)

    def find_keyframe(self, *args, **_kwargs):
        if not self.allow_find_keyframe:
            raise AssertionError("keyframe lookup should be bypassed")
        self.find_keyframe_calls.append({"args": args, "kwargs": dict(_kwargs)})
        return self.keyframe_uuid

    def create_job(self, **kwargs):
        from app.replay_client import build_job_payload

        self.jobs.append(kwargs)
        self.last_job_request = dict(kwargs)
        self.last_job_payload = build_job_payload(**kwargs)
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


def test_clip_worker_watchlist_request_not_skipped_by_intrusion_active_job(
    monkeypatch,
) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    intrusion_request = {
        "request_id": "req-intrusion",
        "event_id": "00000000-0000-0000-0000-000000000101",
        "source_event_id": "intrusion:c2_replay_first_rtsp:track-1",
        "event_type": "intrusion",
        "source_id": "c2_replay_first_rtsp",
        "camera_id": "c2_replay_first_rtsp",
        "event_ts_ms": 1_780_000_000_000,
        "strategy": "savant_replay",
        "keyframe_uuid": "kf-intrusion",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    watchlist_request = {
        "request_id": "req-watchlist",
        "event_id": "00000000-0000-0000-0000-000000000102",
        "source_event_id": "watchlist_hit:face:c2_replay_first_rtsp:49:374708:6",
        "event_type": "watchlist_hit",
        "source_id": "c2_replay_first_rtsp",
        "camera_id": "c2_replay_first_rtsp",
        "event_ts_ms": 1_780_000_010_000,
        "strategy": "savant_replay",
        "keyframe_uuid": "kf-watchlist",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis([intrusion_request, watchlist_request])
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
            max_concurrent_jobs=1,
            per_camera_cooldown_seconds=30,
        ),
        redis_client,
        object(),
    )

    replay = _ClipFakeReplay.instances[-1]
    assert [job["keyframe_uuid"] for job in replay.jobs] == [
        "kf-intrusion",
        "kf-watchlist",
    ]
    assert redis_client.acked == ["1-0", "2-0"]
    assert [update["status"] for update in updates] == [
        "replay_job_created",
        "replay_job_created",
    ]


def test_event_start_anchor_strategy_does_not_clear_provided_keyframe(monkeypatch) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    request = {
        "request_id": "req-2",
        "event_id": "00000000-0000-0000-0000-000000000002",
        "source_event_id": "evt-2",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 1_780_000_010_000,
        "frame_uuid": "019ea722-e76e-74a3-b448-be6876fa4ee7",
        "strategy": "savant_replay",
        "keyframe_uuid": "event-kf-direct",
        "previous_keyframe_uuid": "prev-kf-direct",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(request)
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    _ClipFakeReplay.allow_find_keyframe = True
    _ClipFakeReplay.keyframe_uuid = "019ea722-e9b6-79f1-b21c-f910aede49ad"
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    try:
        worker.run_worker(
            _clip_config(
                run_once=True,
                max_jobs_per_run=100,
                max_concurrent_jobs=0,
                per_camera_cooldown_seconds=0,
                replay_anchor_strategy="event_start_keyframe",
            ),
            redis_client,
            object(),
        )
    finally:
        _ClipFakeReplay.allow_find_keyframe = False
        _ClipFakeReplay.keyframe_uuid = "lookup-kf-1"

    replay = _ClipFakeReplay.instances[-1]
    assert replay.find_keyframe_calls == []
    assert replay.jobs[0]["keyframe_uuid"] == "prev-kf-direct"
    assert replay.jobs[0]["offset_seconds_override"] is None
    assert replay.jobs[0]["duration_seconds_override"] is None
    assert redis_client.acked == ["1-0"]
    assert updates[-1]["status"] == "replay_job_created"


def test_post_savant_uuid_anchor_uses_frame_domain_proofs_without_lookup(monkeypatch) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    request = {
        "request_id": "req-pts",
        "event_id": "00000000-0000-0000-0000-000000000003",
        "source_event_id": "evt-pts",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 1_780_000_015_000,
        "frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "frame_pts": 10_000_000_000,
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "anchor_keyframe_uuid": "019ea76b-2800-7000-8000-000000000000",
        "strategy": "savant_replay",
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant_replay",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(
        request,
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 4_000_000_000,
                "_stream_id": "1780925272000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-2800-7000-8000-000000000000",
                "keyframe_uuid": "019ea76b-2800-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-2800-7000-8000-000000000000",
                "keyframe_pts": 6_000_000_000,
                "frame_pts": 6_000_000_000,
                "_stream_id": "1780925279000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "anchor-before-target",
                "keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 14_900_000_000,
                "_stream_id": "1780000015000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "stale-loop-anchor",
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780000000000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "future-loop-anchor",
                "frame_pts": 73_000_000_000,
                "_stream_id": "1780000015000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea769-aae8-72c1-9dd4-84fc94555a7e",
                "keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "keyframe_pts": 12_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25",
                "keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "keyframe_pts": 12_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "other",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "wrong-source",
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780000015000-0",
            },
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    _ClipFakeReplay.allow_find_keyframe = False
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            run_once=True,
            max_jobs_per_run=100,
            max_concurrent_jobs=0,
            per_camera_cooldown_seconds=0,
            replay_anchor_strategy="event_keyframe",
        ),
        redis_client,
        object(),
    )

    replay = _ClipFakeReplay.instances[-1]
    assert replay.find_keyframe_calls == []
    assert replay.jobs[0]["keyframe_uuid"] == "019ea76b-2800-7000-8000-000000000000"
    assert replay.last_job_request["keyframe_uuid"] == request["anchor_keyframe_uuid"]
    assert replay.last_job_payload["anchor_keyframe"] == request["anchor_keyframe_uuid"]
    assert replay.jobs[0]["keyframe_uuid"] != "019ea76b-1000-7000-8000-000000000000"
    assert replay.jobs[0]["keyframe_uuid"] != "019ea76b-3000-7000-8000-000000000000"
    assert replay.jobs[0]["offset_seconds_override"] == 1.0
    assert replay.jobs[0]["duration_seconds_override"] == 11.0
    labels = replay.jobs[0]["labels"]
    assert labels["event_frame_uuid"] == "019ea76b-26ab-75e0-a8da-9285e28adc69"
    assert labels["anchor_keyframe_uuid"] == "019ea76b-2800-7000-8000-000000000000"
    assert labels["anchor_keyframe_pts"] == "6000000000"
    assert labels["anchor_keyframe_source"] == "anchor_keyframe_uuid"
    assert labels["evidence_anchor_strategy"] == "uuid_first_pts_verified"
    assert labels["start_window_frame_uuid"] == "019ea76b-1000-7000-8000-000000000000"
    assert labels["start_window_frame_pts"] == "4000000000"
    assert labels["start_window_coverage_used"] == "true"
    assert labels["post_window_frame_pts"] == "15000000000"
    assert labels["post_window_frame_uuid"] == "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25"
    assert labels["post_window_proof_used"] == "true"
    assert labels["post_window_frame_uuid"] != labels["anchor_keyframe_uuid"]
    assert labels["start_window_frame_uuid"] != labels["anchor_keyframe_uuid"]
    assert labels["frame_domain_proof_method"] == (
        "frame_cache_start_window_keyframe_and_post_window_pts"
    )
    assert labels["requested_start_pts"] == "5000000000"
    assert labels["requested_end_pts"] == "15000000000"
    assert labels["replay_offset_seconds"] == "1.000000"
    assert labels["replay_duration_seconds"] == "11.000000"
    assert updates[-1]["status"] == "replay_job_created"


def test_post_savant_replay_duration_slack_extends_raw_sink_window(
    monkeypatch,
) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    request = {
        "request_id": "req-pts-slack",
        "event_id": "00000000-0000-0000-0000-000000000103",
        "source_event_id": "evt-pts-slack",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 1_780_000_015_000,
        "frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "frame_pts": 10_000_000_000,
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "anchor_keyframe_uuid": "019ea76b-2800-7000-8000-000000000000",
        "strategy": "savant_replay",
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant_replay",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(
        request,
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 4_000_000_000,
                "_stream_id": "1780925272000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-2800-7000-8000-000000000000",
                "keyframe_uuid": "019ea76b-2800-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-2800-7000-8000-000000000000",
                "keyframe_pts": 6_000_000_000,
                "frame_pts": 6_000_000_000,
                "_stream_id": "1780925279000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25",
                "keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "keyframe_pts": 12_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    _ClipFakeReplay.allow_find_keyframe = False
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            run_once=True,
            max_jobs_per_run=100,
            max_concurrent_jobs=0,
            per_camera_cooldown_seconds=0,
            replay_anchor_strategy="event_keyframe",
            replay_duration_extra_slack_s=15.0,
        ),
        redis_client,
        object(),
    )

    replay = _ClipFakeReplay.instances[-1]
    assert replay.jobs[0]["offset_seconds_override"] == 1.0
    assert replay.jobs[0]["duration_seconds_override"] == 26.0
    assert replay.last_job_payload["stop_condition"] == {
        "ts_delta_sec": {"max_delta_sec": 26.0}
    }
    labels = replay.jobs[0]["labels"]
    assert labels["replay_duration_extra_slack_s"] == "15.0"
    assert labels["replay_duration_seconds"] == "26.000000"
    assert labels["requested_start_pts"] == "5000000000"
    assert labels["requested_end_pts"] == "15000000000"
    assert updates[-1]["status"] == "replay_job_created"


def test_post_savant_missing_event_anchor_uses_keyframes_find_after_pts_proof(
    monkeypatch,
) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    request = {
        "request_id": "req-pts-post-kf",
        "event_id": "00000000-0000-0000-0000-00000000000a",
        "source_event_id": "evt-pts-post-kf",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 1_780_000_015_000,
        "frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "frame_pts": 10_000_000_000,
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "strategy": "savant_replay",
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant_replay",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(
        request,
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-2400-7000-8000-000000000000",
                "keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 4_900_000_000,
                "_stream_id": "1780925272000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "lookup-kf-1",
                "keyframe_uuid": "lookup-kf-1",
                "previous_keyframe_uuid": "lookup-kf-1",
                "keyframe_pts": 6_000_000_000,
                "frame_pts": 6_000_000_000,
                "_stream_id": "1780925279000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25",
                "keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "keyframe_pts": 12_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    _ClipFakeReplay.allow_find_keyframe = True
    _ClipFakeReplay.keyframe_uuid = "lookup-kf-1"
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    try:
        worker.run_worker(
            _clip_config(
                run_once=True,
                max_jobs_per_run=100,
                max_concurrent_jobs=0,
                per_camera_cooldown_seconds=0,
                replay_anchor_strategy="event_keyframe",
            ),
            redis_client,
            object(),
        )
    finally:
        _ClipFakeReplay.allow_find_keyframe = False
        _ClipFakeReplay.keyframe_uuid = "lookup-kf-1"

    replay = _ClipFakeReplay.instances[-1]
    assert replay.find_keyframe_calls
    assert replay.jobs[0]["keyframe_uuid"] == "lookup-kf-1"
    assert replay.last_job_payload["anchor_keyframe"] == "lookup-kf-1"
    assert replay.jobs[0]["keyframe_uuid"] != "019ea76b-1000-7000-8000-000000000000"
    assert replay.jobs[0]["keyframe_uuid"] != "019ea76b-3000-7000-8000-000000000000"
    assert replay.jobs[0]["offset_seconds_override"] == 1.0
    assert replay.jobs[0]["duration_seconds_override"] == 11.0
    labels = replay.jobs[0]["labels"]
    assert labels["anchor_keyframe_uuid"] == "lookup-kf-1"
    assert labels["anchor_keyframe_pts"] == "6000000000"
    assert labels["anchor_keyframe_source"] == "keyframes_find_pts_verified"
    assert labels["evidence_anchor_strategy"] == "uuid_first_pts_verified"
    assert labels["start_window_frame_uuid"] == "019ea76b-2400-7000-8000-000000000000"
    assert labels["post_window_frame_uuid"] == "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25"
    assert labels["start_window_coverage_used"] == "true"
    assert labels["post_window_proof_used"] == "true"
    assert labels["post_window_frame_uuid"] != labels["anchor_keyframe_uuid"]
    assert labels["start_window_frame_uuid"] != labels["anchor_keyframe_uuid"]
    assert labels["post_window_frame_pts"] == "15000000000"
    assert labels["replay_offset_seconds"] == "1.000000"
    assert labels["replay_duration_seconds"] == "11.000000"
    assert updates[-1]["status"] == "replay_job_created"


def test_post_savant_keyframes_find_must_not_return_post_window_keyframe(
    monkeypatch,
) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    request = {
        "request_id": "req-pts-post-window-kf",
        "event_id": "00000000-0000-0000-0000-00000000000b",
        "source_event_id": "evt-pts-post-window-kf",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 1_780_000_015_000,
        "frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "event_frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "frame_pts": 10_000_000_000,
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "strategy": "savant_replay",
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant_replay",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(
        request,
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-2400-7000-8000-000000000000",
                "keyframe_uuid": "start-window-kf",
                "previous_keyframe_uuid": "start-window-kf",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 4_900_000_000,
                "_stream_id": "1780925272000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25",
                "keyframe_uuid": "post-window-kf",
                "previous_keyframe_uuid": "post-window-kf",
                "keyframe_pts": 12_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    _ClipFakeReplay.allow_find_keyframe = True
    _ClipFakeReplay.keyframe_uuid = "post-window-kf"
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    try:
        worker.run_worker(
            _clip_config(
                run_once=True,
                max_jobs_per_run=100,
                max_concurrent_jobs=0,
                per_camera_cooldown_seconds=0,
                replay_anchor_strategy="event_keyframe",
            ),
            redis_client,
            object(),
        )
    finally:
        _ClipFakeReplay.allow_find_keyframe = False
        _ClipFakeReplay.keyframe_uuid = "lookup-kf-1"

    replay = _ClipFakeReplay.instances[-1]
    assert replay.find_keyframe_calls
    assert replay.jobs == []
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error_message"].startswith(
        "keyframes_find_returned_proof_keyframe"
    )
    assert "proof=post_window_frame" in updates[-1]["error_message"]


def test_post_savant_keyframes_find_must_not_return_start_window_keyframe(
    monkeypatch,
) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    request = {
        "request_id": "req-pts-start-window-kf",
        "event_id": "00000000-0000-0000-0000-00000000000c",
        "source_event_id": "evt-pts-start-window-kf",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 1_780_000_015_000,
        "frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "event_frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "frame_pts": 10_000_000_000,
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "strategy": "savant_replay",
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant_replay",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(
        request,
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-2400-7000-8000-000000000000",
                "keyframe_uuid": "start-window-kf",
                "previous_keyframe_uuid": "start-window-kf",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 4_900_000_000,
                "_stream_id": "1780925272000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25",
                "keyframe_uuid": "post-window-kf",
                "previous_keyframe_uuid": "post-window-kf",
                "keyframe_pts": 12_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    _ClipFakeReplay.allow_find_keyframe = True
    _ClipFakeReplay.keyframe_uuid = "start-window-kf"
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    try:
        worker.run_worker(
            _clip_config(
                run_once=True,
                max_jobs_per_run=100,
                max_concurrent_jobs=0,
                per_camera_cooldown_seconds=0,
                replay_anchor_strategy="event_keyframe",
            ),
            redis_client,
            object(),
        )
    finally:
        _ClipFakeReplay.allow_find_keyframe = False
        _ClipFakeReplay.keyframe_uuid = "lookup-kf-1"

    replay = _ClipFakeReplay.instances[-1]
    assert replay.find_keyframe_calls
    assert replay.jobs == []
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error_message"].startswith(
        "keyframes_find_returned_proof_keyframe"
    )
    assert "proof=start_window_frame" in updates[-1]["error_message"]


def test_post_savant_anchor_does_not_require_event_ts_ms(monkeypatch) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    request = {
        "request_id": "req-pts-no-event-ts",
        "event_id": "00000000-0000-0000-0000-000000000004",
        "source_event_id": "evt-pts-no-event-ts",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 0,
        "frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "frame_pts": 10_000_000_000,
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "anchor_keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
        "strategy": "savant_replay",
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant_replay",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(
        request,
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 4_000_000_000,
                "_stream_id": "1780925272000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25",
                "keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    _ClipFakeReplay.allow_find_keyframe = False
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            run_once=True,
            max_jobs_per_run=100,
            max_concurrent_jobs=0,
            per_camera_cooldown_seconds=0,
            replay_anchor_strategy="event_keyframe",
        ),
        redis_client,
        object(),
    )

    replay = _ClipFakeReplay.instances[-1]
    assert replay.jobs[0]["keyframe_uuid"] == "019ea76b-1000-7000-8000-000000000000"
    assert replay.jobs[0]["offset_seconds_override"] == 0.0
    assert replay.jobs[0]["duration_seconds_override"] == 22.0
    assert replay.last_job_payload["anchor_keyframe"] == request["anchor_keyframe_uuid"]
    labels = replay.jobs[0]["labels"]
    assert labels["anchor_keyframe_uuid"] == request["anchor_keyframe_uuid"]
    assert labels["replay_offset_seconds"] == "0.000000"
    assert labels["replay_duration_seconds"] == "22.000000"
    assert replay.find_keyframe_calls == []
    assert updates[-1]["status"] == "replay_job_created"


def test_post_savant_request_without_pts_window_fails_closed(monkeypatch) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    request = {
        "request_id": "req-post-savant-no-pts",
        "event_id": "00000000-0000-0000-0000-000000000005",
        "source_event_id": "evt-post-savant-no-pts",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 1_780_000_015_000,
        "frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "strategy": "savant_replay",
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant_replay",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(request)
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    _ClipFakeReplay.allow_find_keyframe = False
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            run_once=True,
            max_jobs_per_run=100,
            max_concurrent_jobs=0,
            per_camera_cooldown_seconds=0,
            replay_anchor_strategy="event_keyframe",
        ),
        redis_client,
        object(),
    )

    replay = _ClipFakeReplay.instances[-1]
    assert replay.find_keyframe_calls == []
    assert replay.jobs == []
    assert redis_client.acked == ["1-0"]
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error_message"].startswith(
        "missing_post_savant_frame_pts_window"
    )


def test_post_savant_anchor_without_anchor_keyframe_pts_fails_closed(monkeypatch) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    request = {
        "request_id": "req-post-savant-missing-anchor-pts",
        "event_id": "00000000-0000-0000-0000-000000000006",
        "source_event_id": "evt-post-savant-missing-anchor-pts",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 1_780_000_015_000,
        "frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "frame_pts": 10_000_000_000,
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "anchor_keyframe_uuid": "019ea76b-2800-7000-8000-000000000000",
        "strategy": "savant_replay",
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant_replay",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(
        request,
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-2400-7000-8000-000000000000",
                "keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 4_900_000_000,
                "_stream_id": "1780925272000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25",
                "keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "keyframe_pts": 12_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    _ClipFakeReplay.allow_find_keyframe = False
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            run_once=True,
            max_jobs_per_run=100,
            max_concurrent_jobs=0,
            per_camera_cooldown_seconds=0,
            replay_anchor_strategy="event_keyframe",
        ),
        redis_client,
        object(),
    )

    replay = _ClipFakeReplay.instances[-1]
    assert replay.find_keyframe_calls == []
    assert replay.jobs == []
    assert redis_client.acked == ["1-0"]
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error_message"].startswith("missing_anchor_keyframe_pts")
    assert "019ea76b-2800-7000-8000-000000000000" in updates[-1]["error_message"]


def test_post_savant_keyframes_find_does_not_reuse_request_anchor_pts(
    monkeypatch,
) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    request = {
        "request_id": "req-lookup-stale-anchor-pts",
        "event_id": "00000000-0000-0000-0000-00000000000d",
        "source_event_id": "evt-lookup-stale-anchor-pts",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 1_780_000_015_000,
        "frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "event_frame_uuid": "019ea76b-26ab-75e0-a8da-9285e28adc69",
        "frame_pts": 10_000_000_000,
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "anchor_keyframe_pts": 6_000_000_000,
        "strategy": "savant_replay",
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant_replay",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(
        request,
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-2400-7000-8000-000000000000",
                "keyframe_uuid": "start-window-kf",
                "previous_keyframe_uuid": "start-window-kf",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 4_900_000_000,
                "_stream_id": "1780925272000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25",
                "keyframe_uuid": "post-window-kf",
                "previous_keyframe_uuid": "post-window-kf",
                "keyframe_pts": 12_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    _ClipFakeReplay.allow_find_keyframe = True
    _ClipFakeReplay.keyframe_uuid = "lookup-kf-without-annotation"
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    try:
        worker.run_worker(
            _clip_config(
                run_once=True,
                max_jobs_per_run=100,
                max_concurrent_jobs=0,
                per_camera_cooldown_seconds=0,
                replay_anchor_strategy="event_keyframe",
            ),
            redis_client,
            object(),
        )
    finally:
        _ClipFakeReplay.allow_find_keyframe = False
        _ClipFakeReplay.keyframe_uuid = "lookup-kf-1"

    replay = _ClipFakeReplay.instances[-1]
    assert replay.find_keyframe_calls
    assert replay.jobs == []
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error_message"].startswith("missing_anchor_keyframe_pts")
    assert "lookup-kf-without-annotation" in updates[-1]["error_message"]


def test_post_savant_event_frame_uuid_is_not_anchor_unless_keyframe(monkeypatch) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    event_frame_uuid = "019ea76b-26ab-75e0-a8da-9285e28adc69"
    request = {
        "request_id": "req-post-savant-event-frame-anchor",
        "event_id": "00000000-0000-0000-0000-000000000007",
        "source_event_id": "evt-post-savant-event-frame-anchor",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "event_ts_ms": 1_780_000_015_000,
        "frame_uuid": event_frame_uuid,
        "event_frame_uuid": event_frame_uuid,
        "frame_pts": 10_000_000_000,
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "anchor_keyframe_uuid": event_frame_uuid,
        "anchor_keyframe_pts": 10_000_000_000,
        "strategy": "savant_replay",
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant_replay",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "pre_seconds": 5,
        "post_seconds": 5,
    }
    redis_client = _ClipFakeRedis(
        request,
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-2400-7000-8000-000000000000",
                "keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 4_900_000_000,
                "_stream_id": "1780925272000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": event_frame_uuid,
                "keyframe_uuid": "019ea76b-2000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-2000-7000-8000-000000000000",
                "keyframe_pts": 8_000_000_000,
                "frame_pts": 10_000_000_000,
                "_stream_id": "1780925280000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25",
                "keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-3000-7000-8000-000000000000",
                "keyframe_pts": 12_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )
    updates: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    _ClipFakeReplay.instances.clear()
    _ClipFakeReplay.allow_find_keyframe = False
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _ClipFakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)

    worker.run_worker(
        _clip_config(
            run_once=True,
            max_jobs_per_run=100,
            max_concurrent_jobs=0,
            per_camera_cooldown_seconds=0,
            replay_anchor_strategy="event_keyframe",
        ),
        redis_client,
        object(),
    )

    replay = _ClipFakeReplay.instances[-1]
    assert replay.find_keyframe_calls == []
    assert replay.jobs == []
    assert redis_client.acked == ["1-0"]
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error_message"].startswith("event_frame_anchor_not_keyframe")


def test_anchor_keyframe_pts_recovery_rejects_mismatched_keyframe_reference() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _find_anchor_keyframe_pts

    event_frame_uuid = "019ea76b-26ab-75e0-a8da-9285e28adc69"
    redis_client = _ClipFakeRedis(
        {},
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": event_frame_uuid,
                "keyframe_uuid": "019ea76b-2000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-2000-7000-8000-000000000000",
                "keyframe_pts": 8_000_000_000,
                "frame_pts": 10_000_000_000,
                "_stream_id": "1780925280000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "post-window-frame",
                "keyframe_uuid": "post-window-keyframe",
                "previous_keyframe_uuid": "post-window-keyframe",
                "keyframe_pts": 12_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )

    assert (
        _find_anchor_keyframe_pts(
            redis_client,
            stream_name="security.frame_annotations",
            source_id="c1e_rtsp_replay",
            camera_id="cam_c1e_rtsp_replay",
            anchor_keyframe_uuid=event_frame_uuid,
            count=100,
        )
        is None
    )


def test_anchor_keyframe_pts_from_request_does_not_reuse_mismatched_keyframe_pts() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _anchor_keyframe_pts_from_request

    req = {
        "anchor_keyframe_uuid": "previous-kf",
        "previous_keyframe_uuid": "previous-kf",
        "keyframe_uuid": "event-kf",
        "keyframe_pts": 8_000_000_000,
    }

    assert (
        _anchor_keyframe_pts_from_request(
            req,
            anchor_keyframe_uuid="previous-kf",
        )
        is None
    )


def test_anchor_keyframe_pts_recovery_ignores_previous_reference_without_exact_keyframe() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _find_anchor_keyframe_pts

    redis_client = _ClipFakeRedis(
        {},
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "event-frame",
                "keyframe_uuid": "event-kf",
                "previous_keyframe_uuid": "previous-kf",
                "keyframe_pts": 8_000_000_000,
                "frame_pts": 10_000_000_000,
                "_stream_id": "1780925280000-0",
            },
        ],
    )

    assert (
        _find_anchor_keyframe_pts(
            redis_client,
            stream_name="security.frame_annotations",
            source_id="c1e_rtsp_replay",
            camera_id="cam_c1e_rtsp_replay",
            anchor_keyframe_uuid="previous-kf",
            count=100,
        )
        is None
    )


def test_frame_annotation_anchor_rejects_previous_loop_high_pts() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _find_frame_annotation_anchor

    redis_client = _ClipFakeRedis(
        {},
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea769-aae8-72c1-9dd4-84fc94555a7e",
                "frame_pts": 21_198_900_000,
                "_stream_id": "1780000015000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-37f0-78a3-b4e2-2d3c6744c7ba",
                "frame_pts": 9_500_000_000,
                "_stream_id": "1780000016000-0",
            },
        ],
    )

    anchor = _find_frame_annotation_anchor(
        redis_client,
        stream_name="security.frame_annotations",
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        target_pts=9_407_544_444,
        count=100,
        min_stream_ms=1780000014000,
        max_pts_delta_ns=1_000_000_000,
        min_frame_uuid_ms=1780925277867,
    )

    assert anchor is not None
    assert anchor.frame_uuid == "019ea76b-37f0-78a3-b4e2-2d3c6744c7ba"
    assert anchor.frame_pts == 9_500_000_000


def test_replay_frame_domain_proofs_use_start_keyframe_and_post_window_frame() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _find_replay_frame_domain_proofs

    redis_client = _ClipFakeRedis(
        {},
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "not-a-keyframe",
                "keyframe_uuid": "older-keyframe",
                "frame_pts": 4_900_000_000,
                "_stream_id": "1780925277000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 4_000_000_000,
                "_stream_id": "1780925272000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25",
                "keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "previous_keyframe_uuid": "019ea76b-1000-7000-8000-000000000000",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )

    proofs = _find_replay_frame_domain_proofs(
        redis_client,
        stream_name="security.frame_annotations",
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        requested_start_pts=5_000_000_000,
        requested_end_pts=15_000_000_000,
        count=100,
        min_start_stream_ms=1780925270000,
        min_post_stream_ms=1780925277000,
        max_start_pts_delta_ns=5_000_000_000,
        max_post_pts_delta_ns=1_000_000_000,
        min_start_frame_uuid_ms=1780925200000,
        min_post_frame_uuid_ms=1780925277867,
    )

    assert proofs is not None
    assert proofs.start_window_frame.frame_uuid == "019ea76b-1000-7000-8000-000000000000"
    assert proofs.post_window_frame.frame_uuid == "019ea76b-3bdb-7ac3-b60c-959c2a7dcf25"


def test_replay_frame_domain_proofs_use_start_window_keyframe_reference() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _find_replay_frame_domain_proofs

    redis_client = _ClipFakeRedis(
        {},
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "start-window-frame",
                "keyframe_uuid": "replay-keyframe-uuid",
                "previous_keyframe_uuid": "replay-keyframe-uuid",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 4_900_000_000,
                "_stream_id": "1780925277000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "post-window-frame",
                "keyframe_uuid": "replay-keyframe-uuid",
                "previous_keyframe_uuid": "replay-keyframe-uuid",
                "keyframe_pts": 4_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )

    proofs = _find_replay_frame_domain_proofs(
        redis_client,
        stream_name="security.frame_annotations",
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        requested_start_pts=5_000_000_000,
        requested_end_pts=15_000_000_000,
        count=100,
        min_start_stream_ms=1780925270000,
        min_post_stream_ms=1780925277000,
        max_start_pts_delta_ns=5_000_000_000,
        max_post_pts_delta_ns=1_000_000_000,
    )

    assert proofs is not None
    assert proofs.start_window_frame.frame_uuid == "start-window-frame"
    assert proofs.start_window_frame.frame_pts == 4_900_000_000
    assert proofs.start_window_frame.keyframe_uuid == "replay-keyframe-uuid"
    assert proofs.start_window_frame.keyframe_pts == 4_000_000_000
    assert proofs.start_window_frame.anchor_method == (
        "frame_annotation_start_window_keyframe_reference"
    )
    assert proofs.post_window_frame.frame_uuid == "post-window-frame"


def test_replay_frame_domain_proofs_allow_gop_keyframe_before_start_window() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _find_replay_frame_domain_proofs

    redis_client = _ClipFakeRedis(
        {},
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "start-window-frame",
                "keyframe_uuid": "replay-keyframe-uuid",
                "previous_keyframe_uuid": "replay-keyframe-uuid",
                "keyframe_pts": 3_500_000_000,
                "frame_pts": 4_950_000_000,
                "_stream_id": "1780925277000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "post-window-frame",
                "keyframe_uuid": "later-keyframe-uuid",
                "previous_keyframe_uuid": "later-keyframe-uuid",
                "keyframe_pts": 12_000_000_000,
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )

    proofs = _find_replay_frame_domain_proofs(
        redis_client,
        stream_name="security.frame_annotations",
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        requested_start_pts=10_000_000_000,
        requested_end_pts=15_000_000_000,
        count=100,
        min_start_stream_ms=1780925270000,
        min_post_stream_ms=1780925277000,
        max_start_pts_delta_ns=6_000_000_000,
        max_post_pts_delta_ns=1_000_000_000,
        max_keyframe_pts_delta_ns=15_000_000_000,
    )

    assert proofs is not None
    assert proofs.start_window_frame.frame_uuid == "start-window-frame"
    assert proofs.start_window_frame.frame_pts == 4_950_000_000
    assert proofs.start_window_frame.keyframe_uuid == "replay-keyframe-uuid"
    assert proofs.start_window_frame.keyframe_pts == 3_500_000_000
    assert proofs.post_window_frame.frame_uuid == "post-window-frame"


def test_replay_frame_domain_proofs_reject_keyframe_reference_without_pts() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _find_replay_frame_domain_proofs

    redis_client = _ClipFakeRedis(
        {},
        frame_annotations=[
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "start-window-frame",
                "keyframe_uuid": "replay-keyframe-uuid",
                "previous_keyframe_uuid": "replay-keyframe-uuid",
                "frame_pts": 4_900_000_000,
                "_stream_id": "1780925277000-0",
            },
            {
                "message_type": "frame_annotation",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "frame_uuid": "post-window-frame",
                "keyframe_uuid": "replay-keyframe-uuid",
                "previous_keyframe_uuid": "replay-keyframe-uuid",
                "frame_pts": 15_000_000_000,
                "_stream_id": "1780925283000-0",
            },
        ],
    )

    proofs = _find_replay_frame_domain_proofs(
        redis_client,
        stream_name="security.frame_annotations",
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        requested_start_pts=5_000_000_000,
        requested_end_pts=15_000_000_000,
        count=100,
        min_start_stream_ms=1780925270000,
        min_post_stream_ms=1780925277000,
        max_start_pts_delta_ns=5_000_000_000,
        max_post_pts_delta_ns=1_000_000_000,
    )

    assert proofs is None


def test_frame_annotation_anchor_freshness_prefers_frame_uuid_over_publish_time() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _frame_annotation_anchor_min_stream_ms

    min_stream_ms = _frame_annotation_anchor_min_stream_ms(
        {
            "frame_uuid": "019ea773-28a1-71a0-a0b8-925443e181a2",
            "event_ts_ms": 1_780_926_037_901,
        },
        slack_seconds=1.0,
    )

    assert min_stream_ms == 1_780_925_801_657


def test_frame_annotation_anchor_freshness_ignores_watchlist_pts_ms() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.worker import _frame_annotation_anchor_min_stream_ms

    min_stream_ms = _frame_annotation_anchor_min_stream_ms(
        {
            "frame_uuid": "019ea773-28a1-71a0-a0b8-925443e181a2",
            "event_ts_ms": 56_134,
        },
        slack_seconds=1.0,
    )

    assert min_stream_ms == 1_780_925_801_657
    assert _frame_annotation_anchor_min_stream_ms(
        {"event_ts_ms": 56_134},
        slack_seconds=1.0,
    ) is None


def test_frame_annotation_anchor_freshness_uses_epoch_event_time_without_uuid(
    monkeypatch,
) -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    monkeypatch.setattr(worker.time, "time", lambda: 1_780_926_038.0)

    min_stream_ms = worker._frame_annotation_anchor_min_stream_ms(
        {"event_ts_ms": 1_780_926_037_901},
        slack_seconds=1.0,
    )

    assert min_stream_ms == 1_780_926_036_901


def test_c1i1f_config_and_migration_contract() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    env = ENV_FILE.read_text(encoding="utf-8")
    migration = MIGRATION.read_text(encoding="utf-8")
    doctor = DOCTOR.read_text(encoding="utf-8")

    assert "PERSON_OBSERVATION_EXPORT_ENABLED=true" in env
    assert "PERSON_OBSERVATION_MIN_INTERVAL_MS=333" in env
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
