"""Midterm Replay runtime epoch isolation tests."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
EVENT_ID = "11111111-1111-4111-8111-111111111111"
CURRENT_EPOCH = "midterm-20260610T134500Z-a1b2c3d4"
OLD_EPOCH = "midterm-20260610T131500Z-00aa11bb"


def _activate(service: str, module_name: str):
    service_root = str(REPO_ROOT / "services" / service)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if service_root in sys.path:
        sys.path.remove(service_root)
    sys.path.insert(0, service_root)
    return importlib.import_module(module_name)


class _FakeRedisClient:
    values: dict[str, str] = {}
    deleted: list[str] = []

    def set(self, key: str, value: str) -> None:
        self.values[str(key)] = str(value)

    def delete(self, *keys: str) -> int:
        self.deleted.extend(str(key) for key in keys)
        return len(keys)


class _FakeRedis:
    @classmethod
    def from_url(cls, _url: str) -> _FakeRedisClient:
        return _FakeRedisClient()


def _delivery_duration_seconds(payload: dict[str, Any]) -> int:
    return int(payload["configuration"]["max_delivery_duration"]["secs"])


def test_runtime_epoch_id_rejects_unsafe_segments() -> None:
    runtime_apply = _activate("api", "app.services.runtime_apply")

    for value in ("", ".", "..", "bad/epoch", "bad\\epoch", "bad epoch"):
        try:
            runtime_apply.validate_runtime_epoch_id(value)
        except runtime_apply.RuntimeApplyError:
            continue
        raise AssertionError(f"unsafe epoch id was accepted: {value!r}")


def test_runtime_apply_writes_epoch_state_and_redis(monkeypatch, tmp_path: Path) -> None:
    runtime_apply = _activate("api", "app.services.runtime_apply")
    state_path = tmp_path / "replay-sink-output" / "midterm" / ".current_epoch.json"

    monkeypatch.setenv("RUNTIME_EPOCH_ROOT", str(state_path.parent))
    monkeypatch.setenv("RUNTIME_EPOCH_STATE_PATH", str(state_path))
    monkeypatch.setattr(runtime_apply, "Redis", _FakeRedis)
    _FakeRedisClient.values.clear()
    _FakeRedisClient.deleted.clear()

    state = runtime_apply.create_runtime_epoch_state(
        reason="test",
        runtime_epoch_id=CURRENT_EPOCH,
    )

    assert state["runtime_epoch_id"] == CURRENT_EPOCH
    assert json.loads(state_path.read_text(encoding="utf-8"))["runtime_epoch_id"] == CURRENT_EPOCH
    assert (state_path.parent / "epochs" / CURRENT_EPOCH).is_dir()
    assert _FakeRedisClient.values["video_analytics:midterm:runtime_epoch"] == CURRENT_EPOCH
    assert _FakeRedisClient.deleted == ["security.frame_annotations"]
    assert state["redis_frame_cache_streams_reset"] == ["security.frame_annotations"]
    written_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert written_state["redis_frame_cache_streams_reset"] == ["security.frame_annotations"]
    assert written_state["redis_frame_cache_reset_count"] == 1


def test_record_request_and_replay_job_carry_runtime_epoch() -> None:
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
                "frame_pts": 100_000_000_000,
            },
        },
    }

    record = record_request.build_record_request(event, EVENT_ID)
    assert record is not None
    assert record["runtime_epoch_id"] == CURRENT_EPOCH

    labels = {"event_id": EVENT_ID, "runtime_epoch_id": CURRENT_EPOCH}
    payload = replay_client.build_job_payload(
        source_id="primary_rtsp",
        keyframe_uuid="keyframe-1",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels=labels,
    )
    assert payload["configuration"]["labels"]["runtime_epoch_id"] == CURRENT_EPOCH
    assert payload["configuration"]["resulting_stream_id"] == (
        f"replay-{CURRENT_EPOCH}-event-{EVENT_ID}"
    )


def test_replay_job_default_delivery_duration_keeps_minimum() -> None:
    replay_client = _activate("clip-worker", "app.replay_client")

    payload = replay_client.build_job_payload(
        source_id="primary_rtsp",
        keyframe_uuid="keyframe-1",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        stop_condition_mode="ts_delta_sec",
        fps=24,
    )

    assert payload["configuration"]["max_delivery_duration"] == {"secs": 30, "nanos": 0}


def test_replay_job_delivery_duration_follows_duration_override() -> None:
    replay_client = _activate("clip-worker", "app.replay_client")

    payload = replay_client.build_job_payload(
        source_id="primary_rtsp",
        keyframe_uuid="keyframe-1",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        stop_condition_mode="ts_delta_sec",
        fps=24,
        duration_seconds_override=36.0,
    )

    assert payload["stop_condition"] == {"ts_delta_sec": {"max_delta_sec": 36.0}}
    assert _delivery_duration_seconds(payload) == 46
    assert _delivery_duration_seconds(payload) > 36


def test_replay_job_fallback_preserves_dynamic_delivery_duration(monkeypatch) -> None:
    replay_client = _activate("clip-worker", "app.replay_client")
    client = replay_client.ReplayClient("http://replay-service:8080")
    submitted: list[dict[str, Any]] = []

    def fake_submit(payload: dict[str, Any]) -> str:
        submitted.append(payload)
        if len(submitted) == 1:
            request = replay_client.httpx.Request(
                "PUT",
                "http://replay-service:8080/api/v1/job",
            )
            response = replay_client.httpx.Response(
                400,
                request=request,
                text="bad payload",
            )
            raise replay_client.httpx.HTTPStatusError(
                "bad payload",
                request=request,
                response=response,
            )
        return "job-2"

    monkeypatch.setattr(client, "_submit_job_payload", fake_submit)

    job_id = client.create_job(
        source_id="primary_rtsp",
        keyframe_uuid="keyframe-1",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        stop_condition_mode="ts_delta_sec",
        fps=24,
        duration_seconds_override=36.0,
    )

    assert job_id == "job-2"
    assert len(submitted) == 2
    assert [_delivery_duration_seconds(payload) for payload in submitted] == [46, 46]
    assert submitted[1]["fallback_reason"] == "replay_api_rejected_ts_delta_sec"


def test_clip_worker_frame_annotation_lookup_filters_runtime_epoch() -> None:
    worker = _activate("clip-worker", "app.worker")

    class FakeRedis:
        def xrevrange(self, *_args: object, **_kwargs: object) -> list[tuple[str, dict[str, str]]]:
            return [
                ("2-0", {"data": json.dumps(_frame_annotation("old-frame", OLD_EPOCH))}),
                ("1-0", {"data": json.dumps(_frame_annotation("current-frame", CURRENT_EPOCH))}),
            ]

    anchor = worker._find_frame_annotation_anchor(
        FakeRedis(),
        stream_name="security.frame_annotations",
        source_id="primary_rtsp",
        camera_id="primary_rtsp",
        target_pts=100_000_000_000,
        count=10,
        runtime_epoch_id=CURRENT_EPOCH,
    )

    assert anchor is not None
    assert anchor.frame_uuid == "current-frame"


def test_clip_worker_post_savant_frame_proof_wait_has_separate_config(monkeypatch) -> None:
    config = _activate("clip-worker", "app.config")

    monkeypatch.delenv("POST_SAVANT_FRAME_PROOF_ATTEMPTS", raising=False)
    monkeypatch.delenv("POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S", raising=False)
    monkeypatch.setenv("KEYFRAME_LOOKUP_RETRIES", "8")
    monkeypatch.setenv("KEYFRAME_LOOKUP_RETRY_SLEEP_S", "0.25")
    cfg = config.load_config()

    assert cfg.keyframe_lookup_retries == 8
    assert cfg.post_savant_frame_proof_attempts == 1
    assert cfg.post_savant_frame_proof_retry_sleep_s == 0.25
    assert cfg.post_savant_frame_proof_wait_budget_s == 12.0
    assert cfg.post_savant_frame_proof_poll_interval_s == 0.25
    assert cfg.post_savant_allow_cross_session_post_window_proof is True
    assert cfg.post_savant_allow_truncated_pre_window_proof is True

    monkeypatch.setenv("POST_SAVANT_FRAME_PROOF_ATTEMPTS", "45")
    monkeypatch.setenv("POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S", "0.5")
    monkeypatch.setenv("POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S", "9")
    monkeypatch.setenv("POST_SAVANT_FRAME_PROOF_POLL_INTERVAL_S", "0.2")
    monkeypatch.setenv("POST_SAVANT_ALLOW_CROSS_SESSION_POST_WINDOW_PROOF", "false")
    monkeypatch.setenv("POST_SAVANT_ALLOW_TRUNCATED_PRE_WINDOW_PROOF", "false")
    cfg = config.load_config()

    assert cfg.post_savant_frame_proof_attempts == 45
    assert cfg.post_savant_frame_proof_retry_sleep_s == 0.5
    assert cfg.post_savant_frame_proof_wait_budget_s == 9.0
    assert cfg.post_savant_frame_proof_poll_interval_s == 0.2
    assert cfg.post_savant_allow_cross_session_post_window_proof is False
    assert cfg.post_savant_allow_truncated_pre_window_proof is False


def test_media_worker_frame_cache_sidecar_filters_runtime_epoch() -> None:
    writer = _activate("media-worker", "app.frame_cache_sidecar_writer")

    class FakeRedis:
        def xrevrange(self, *_args: object, **_kwargs: object) -> list[tuple[str, dict[str, str]]]:
            return [
                ("3-0", {"data": json.dumps(_frame_annotation("missing-epoch", ""))}),
                ("2-0", {"data": json.dumps(_frame_annotation("old-frame", OLD_EPOCH))}),
                ("1-0", {"data": json.dumps(_frame_annotation("current-frame", CURRENT_EPOCH))}),
            ]

    messages, summary = writer._read_frame_annotations(
        redis_client=FakeRedis(),
        config={"stream_name": "security.frame_annotations", "lookback_count": 10},
        event={
            "source_id": "primary_rtsp",
            "camera_id": "primary_rtsp",
            "frame_uuid": "current-frame",
            "frame_pts": 100_000_000_000,
            "payload": {"runtime_epoch_id": CURRENT_EPOCH},
        },
    )

    assert [message["frame_uuid"] for message in messages] == ["current-frame"]
    assert summary["expected_runtime_epoch_id"] == CURRENT_EPOCH
    assert summary["messages_filtered_runtime_epoch"] == 2


def test_media_worker_uses_active_epoch_root(monkeypatch, tmp_path: Path) -> None:
    worker = _activate("media-worker", "app.worker")
    root = tmp_path / "replay-sink-output" / "midterm"
    state_path = root / ".current_epoch.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"runtime_epoch_id": CURRENT_EPOCH}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RUNTIME_EPOCH_STATE_PATH", str(state_path))

    assert worker._active_epoch_sink_output_dir(str(root)) == str(
        root / "epochs" / CURRENT_EPOCH
    )


def test_media_worker_epoch_mismatch_fails_closed(monkeypatch, tmp_path: Path) -> None:
    worker = _activate("media-worker", "app.worker")
    root = tmp_path / "replay-sink-output" / "midterm"
    state_path = root / ".current_epoch.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"runtime_epoch_id": CURRENT_EPOCH}) + "\n",
        encoding="utf-8",
    )
    sink_dir = root / "epochs" / CURRENT_EPOCH / f"replay-{CURRENT_EPOCH}-event-{EVENT_ID}"
    sink_dir.mkdir(parents=True)
    (sink_dir / "video.mov").write_bytes(b"cropped replay source")
    metadata_file = sink_dir / "metadata.json"
    metadata_file.write_text(
        json.dumps(
            {
                "job_id": "job-1",
                "labels": {
                    "event_id": EVENT_ID,
                    "runtime_epoch_id": OLD_EPOCH,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output_root = tmp_path / "evidence"

    monkeypatch.setenv("RUNTIME_EPOCH_STATE_PATH", str(state_path))
    monkeypatch.setenv("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", "true")
    monkeypatch.setenv("POST_SAVANT_DURATION_GUARD_SLACK_SEC", "1")
    monkeypatch.setenv("EVIDENCE_RUNTIME_EPOCH_STRICT", "true")
    monkeypatch.setattr(worker, "_load_event_context", lambda _conn, _event_id: _event_context())
    monkeypatch.setattr(worker, "load_native_metadata", lambda _path: _metadata_rows_inside_window())
    monkeypatch.setattr(worker, "read_decoded_video_frame_count", lambda _path: 10)
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 10.0)
    monkeypatch.setattr(worker, "_copy_or_crop_video", _write_valid_duration_crop)
    monkeypatch.setattr(worker, "write_frame_cache_identity_sidecar", _sidecar_result)

    bundle = worker._finalize_post_savant_evidence_bundle(
        None,
        event_id=EVENT_ID,
        meta_dir=str(sink_dir),
        metadata_file=str(metadata_file),
        evidence_output_dir=str(output_root),
    )

    raw_clip = output_root / EVENT_ID / "raw_clip.mov"
    summary = json.loads((output_root / EVENT_ID / "summary.json").read_text(encoding="utf-8"))
    metadata = json.loads((output_root / EVENT_ID / "metadata.json").read_text(encoding="utf-8"))

    assert not raw_clip.exists()
    assert bundle["raw_clip"] == ""
    assert bundle["clip_status"] == "duration_guard_failed"
    assert summary["epoch_guard_failed"] is True
    assert summary["epoch_guard_reason"] == "missing_or_mismatched_runtime_epoch"
    assert summary["duration_guard_failed"] is True
    assert metadata["media"]["raw_clip_path"] == ""
    assert metadata["media"]["epoch_guard_failed"] is True


def test_media_worker_allows_missing_sink_metadata_epoch(monkeypatch, tmp_path: Path) -> None:
    worker = _activate("media-worker", "app.worker")
    root = tmp_path / "replay-sink-output" / "midterm"
    state_path = root / ".current_epoch.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"runtime_epoch_id": CURRENT_EPOCH}) + "\n",
        encoding="utf-8",
    )
    sink_dir = root / "epochs" / CURRENT_EPOCH / f"replay-{CURRENT_EPOCH}-event-{EVENT_ID}"
    sink_dir.mkdir(parents=True)
    (sink_dir / "video.mov").write_bytes(b"cropped replay source")
    metadata_file = sink_dir / "metadata.json"
    metadata_file.write_text(
        json.dumps(
            {
                "type": "VideoFrame",
                "source_id": f"replay-{CURRENT_EPOCH}-event-{EVENT_ID}",
                "pts": 95_000_000_000,
                "attributes": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output_root = tmp_path / "evidence"

    monkeypatch.setenv("RUNTIME_EPOCH_STATE_PATH", str(state_path))
    monkeypatch.setenv("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", "true")
    monkeypatch.setenv("POST_SAVANT_DURATION_GUARD_SLACK_SEC", "1")
    monkeypatch.setenv("EVIDENCE_RUNTIME_EPOCH_STRICT", "true")
    monkeypatch.setattr(worker, "_load_event_context", lambda _conn, _event_id: _event_context())
    monkeypatch.setattr(worker, "load_native_metadata", lambda _path: _metadata_rows_inside_window())
    monkeypatch.setattr(worker, "read_decoded_video_frame_count", lambda _path: 10)
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 10.0)
    monkeypatch.setattr(worker, "_copy_or_crop_video", _write_valid_duration_crop)
    monkeypatch.setattr(worker, "write_frame_cache_identity_sidecar", _sidecar_result)

    bundle = worker._finalize_post_savant_evidence_bundle(
        None,
        event_id=EVENT_ID,
        meta_dir=str(sink_dir),
        metadata_file=str(metadata_file),
        evidence_output_dir=str(output_root),
    )

    raw_clip = output_root / EVENT_ID / "raw_clip.mov"
    summary = json.loads((output_root / EVENT_ID / "summary.json").read_text(encoding="utf-8"))
    metadata = json.loads((output_root / EVENT_ID / "metadata.json").read_text(encoding="utf-8"))

    assert raw_clip.exists()
    assert bundle["raw_clip"] == str(raw_clip)
    assert bundle["clip_status"] == "ready"
    assert summary["epoch_guard_failed"] is False
    assert summary["epoch_guard_status"] == "passed"
    assert summary["runtime_epoch_missing_fields"] == ["sink_metadata_runtime_epoch_id"]
    assert summary["duration_guard_status"] == "passed"
    assert metadata["media"]["raw_clip_path"] == str(raw_clip)
    assert metadata["media"]["epoch_guard_failed"] is False


def _event_context() -> dict[str, Any]:
    return {
        "event_id": EVENT_ID,
        "source_event_id": "source-event-1",
        "event_type": "intrusion",
        "camera_id": "primary_rtsp",
        "source_id": "primary_rtsp",
        "track_id": "track-1",
        "event_ts_ms": 1_780_000_000_000,
        "frame_uuid": "event-frame",
        "frame_pts": 100_000_000_000,
        "payload": {
            "runtime_epoch_id": CURRENT_EPOCH,
            "media": {
                "runtime_epoch_id": CURRENT_EPOCH,
                "frame_uuid": "event-frame",
                "frame_pts": 100_000_000_000,
                "replay_job_id": "job-1",
                "replay_job_request": {
                    "anchor_keyframe": "anchor-frame",
                    "offset": {"seconds": 5},
                    "stop_condition": {"ts_delta_sec": 10},
                    "configuration": {
                        "labels": {
                            "event_id": EVENT_ID,
                            "runtime_epoch_id": CURRENT_EPOCH,
                            "event_frame_uuid": "event-frame",
                            "event_frame_pts": "100000000000",
                            "requested_start_pts": "95000000000",
                            "requested_end_pts": "105000000000",
                        }
                    },
                },
            },
        },
    }


def _metadata_rows_inside_window() -> list[dict[str, Any]]:
    return [
        {
            "type": "VideoFrame",
            "pts": pts * 1_000_000_000,
            "frame_uuid": "event-frame" if pts == 100 else f"frame-{pts}",
        }
        for pts in range(95, 106)
    ]


def _frame_annotation(frame_uuid: str, runtime_epoch_id: str) -> dict[str, Any]:
    return {
        "message_type": "frame_annotation",
        "source_id": "primary_rtsp",
        "camera_id": "primary_rtsp",
        "frame_pts": 100_000_000_000,
        "frame_uuid": frame_uuid,
        "runtime_epoch_id": runtime_epoch_id,
    }


def _write_valid_duration_crop(**kwargs: object) -> dict[str, Any]:
    output_video_path = Path(kwargs["output_video_path"])
    output_video_path.write_bytes(b"valid duration crop")
    return {
        "method": "ffmpeg_time_domain_transcode",
        "crop_video_to_time_window": True,
        "duration_seconds": 10.0,
    }


def _sidecar_result(**kwargs: object) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence_dir = Path(kwargs["evidence_dir"])
    annotations_path = evidence_dir / worker_module().SIDECAR_ANNOTATIONS_FILE
    annotations_path.write_text(
        json.dumps({"frame_uuid": "event-frame", "objects": []}) + "\n",
        encoding="utf-8",
    )
    return (
        {
            "annotation_status": "complete",
            "annotations_written": 1,
            "rows_written": 1,
            "rows_total_input": 1,
            "embedding_vectors_in_output": 0,
            "image_bytes_in_output": 0,
            "production_ready": True,
            "production_ready_failures": [],
        },
        {"written": True, "annotations_path": str(annotations_path)},
    )


def worker_module():
    return sys.modules["app.worker"]
