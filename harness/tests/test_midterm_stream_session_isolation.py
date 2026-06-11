"""Midterm stream-session isolation tests."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
EVENT_ID = "11111111-1111-4111-8111-111111111111"
CURRENT_EPOCH = "midterm-20260610T134500Z-a1b2c3d4"
SESSION_A = "stream-primary-a"
SESSION_B = "stream-primary-b"


def _activate(service: str, module_name: str):
    service_root = str(REPO_ROOT / "services" / service)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    service_roots = {str(path) for path in (REPO_ROOT / "services").glob("*")}
    sys.path[:] = [path for path in sys.path if path not in service_roots]
    sys.path.insert(0, service_root)
    return importlib.import_module(module_name)


def test_face_match_event_carries_observation_stream_session() -> None:
    service = _activate("face-worker", "app.face_match_event_service")

    event = service.build_watchlist_hit_event(
        observation={
            "source_observation_id": "face:primary_rtsp:1:1000",
            "camera_id": "primary_rtsp",
            "source_id": "primary_rtsp",
            "track_id": "1",
            "timestamp_ms": 1_780_000_000_000,
            "quality": 0.9,
            "face_confidence": 0.8,
            "payload": {"media": {"stream_session_id": SESSION_A}},
        },
        gallery_match={
            "id": 123,
            "person_id": 456,
            "external_person_id": "p456",
            "person_name": "Ada",
            "similarity": 0.91,
        },
        threshold=0.5,
    )

    assert event["payload"]["media"]["stream_session_id"] == SESSION_A


def test_record_request_and_replay_labels_carry_stream_session() -> None:
    record_request = _activate("event-worker", "app.record_request")
    replay_client = _activate("clip-worker", "app.replay_client")

    event = {
        "source_event_id": "intrusion:primary_rtsp:1",
        "event_type": "intrusion",
        "camera_id": "primary_rtsp",
        "source_id": "primary_rtsp",
        "event_ts_ms": 1_780_000_000_000,
        "frame_uuid": "frame-1",
        "clip_required": True,
        "evidence_policy": {"pre_seconds": 5, "post_seconds": 5},
        "payload": {
            "runtime_epoch_id": CURRENT_EPOCH,
            "media": {
                "runtime_epoch_id": CURRENT_EPOCH,
                "stream_session_id": SESSION_A,
                "frame_pts": 100_000_000_000,
            },
        },
    }

    record = record_request.build_record_request(event, EVENT_ID)
    assert record is not None
    assert record["stream_session_id"] == SESSION_A

    labels = {
        "event_id": EVENT_ID,
        "runtime_epoch_id": CURRENT_EPOCH,
        "stream_session_id": SESSION_A,
    }
    payload = replay_client.build_job_payload(
        source_id="primary_rtsp",
        keyframe_uuid="keyframe-1",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels=labels,
    )
    assert payload["configuration"]["labels"]["stream_session_id"] == SESSION_A


def test_clip_worker_frame_annotation_lookup_filters_stream_session() -> None:
    worker = _activate("clip-worker", "app.worker")

    class FakeRedis:
        def xrevrange(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> list[tuple[str, dict[str, str]]]:
            return [
                (
                    "2-0",
                    {"data": json.dumps(_frame_annotation("wrong-session", SESSION_B))},
                ),
                (
                    "1-0",
                    {"data": json.dumps(_frame_annotation("current-session", SESSION_A))},
                ),
            ]

    anchor = worker._find_frame_annotation_anchor(
        FakeRedis(),
        stream_name="security.frame_annotations",
        source_id="primary_rtsp",
        camera_id="primary_rtsp",
        target_pts=100_000_000_000,
        count=10,
        runtime_epoch_id=CURRENT_EPOCH,
        stream_session_id=SESSION_A,
    )

    assert anchor is not None
    assert anchor.frame_uuid == "current-session"
    assert anchor.stream_session_id == SESSION_A


def test_clip_worker_post_savant_request_requires_stream_session() -> None:
    worker = _activate("clip-worker", "app.worker")
    cfg = SimpleNamespace(
        keyframe_lookup_window_s=10,
        replay_duration_extra_slack_s=5.0,
        replay_anchor_strategy=worker.REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME,
        keyframe_lookup_retries=8,
        keyframe_lookup_retry_sleep_s=1.0,
        post_savant_frame_proof_attempts=1,
        post_savant_frame_proof_retry_sleep_s=0.0,
        frame_annotation_stream="security.frame_annotations",
        frame_annotation_anchor_lookback_count=10,
        frame_annotation_anchor_wall_clock_slack_s=1.0,
        frame_annotation_anchor_pts_tolerance_s=1.0,
    )

    updated, _keyframe_uuid, _keyframe_source, error = worker._prepare_post_savant_replay_request(
        redis_client=object(),
        replay=object(),
        cfg=cfg,
        req={
            "request_id": "request-1",
            "frame_uuid": "frame-1",
            "frame_pts": 100_000_000_000,
            "event_frame_pts": 100_000_000_000,
            "requested_start_pts": 95_000_000_000,
            "requested_end_pts": 105_000_000_000,
            "runtime_epoch_id": CURRENT_EPOCH,
        },
        source_id="primary_rtsp",
        camera_id="primary_rtsp",
        keyframe_uuid="keyframe-1",
        keyframe_source="record_request",
        pre_seconds=5,
        post_seconds=5,
    )

    assert updated is None
    assert error == "missing_stream_session_id source_id=primary_rtsp"


def test_media_worker_frame_cache_sidecar_filters_stream_session() -> None:
    writer = _activate("media-worker", "app.frame_cache_sidecar_writer")

    class FakeRedis:
        def xrevrange(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> list[tuple[str, dict[str, str]]]:
            return [
                (
                    "2-0",
                    {"data": json.dumps(_frame_annotation("wrong-session", SESSION_B))},
                ),
                (
                    "1-0",
                    {"data": json.dumps(_frame_annotation("current-session", SESSION_A))},
                ),
            ]

    messages, summary = writer._read_frame_annotations(
        redis_client=FakeRedis(),
        config={"stream_name": "security.frame_annotations", "lookback_count": 10},
        event={
            "source_id": "primary_rtsp",
            "camera_id": "primary_rtsp",
            "frame_uuid": "current-session",
            "frame_pts": 100_000_000_000,
            "payload": {
                "runtime_epoch_id": CURRENT_EPOCH,
                "media": {"stream_session_id": SESSION_A},
            },
        },
    )

    assert [message["frame_uuid"] for message in messages] == ["current-session"]
    assert summary["expected_stream_session_id"] == SESSION_A
    assert summary["messages_filtered_stream_session"] == 1


def _frame_annotation(frame_uuid: str, stream_session_id: str) -> dict[str, Any]:
    return {
        "message_type": "frame_annotation",
        "source_id": "primary_rtsp",
        "camera_id": "primary_rtsp",
        "frame_pts": 100_000_000_000,
        "frame_uuid": frame_uuid,
        "runtime_epoch_id": CURRENT_EPOCH,
        "stream_session_id": stream_session_id,
    }
