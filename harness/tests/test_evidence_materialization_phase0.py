"""Phase 0 evidence materialization measurement tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
REPORT_SCRIPT = REPO_ROOT / "scripts" / "runtime" / "report_evidence_materialization_phase0.py"


def _load_report_module():
    spec = importlib.util.spec_from_file_location(
        "report_evidence_materialization_phase0",
        REPORT_SCRIPT,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _activate_media_worker():
    media_worker_root = str(REPO_ROOT / "services" / "media-worker")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if media_worker_root in sys.path:
        sys.path.remove(media_worker_root)
    sys.path.insert(0, media_worker_root)
    from app import worker  # noqa: E402

    return worker


def test_phase0_report_identifies_transcode_cpu_and_shard_counts() -> None:
    report_module = _load_report_module()
    now = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
    event_rows = [
        {
            "media": {
                "replay_shard_id": "replay-a",
                "replay_job_id": "job-a",
                "sink_output_cleanup_deleted_bytes": 4096,
                "materialization_metrics": {
                    "materialization_mode": "baseline_crop",
                    "input_bytes": 8_000_000,
                    "input_duration_seconds": 18.0,
                    "output_bytes": 2_000_000,
                    "source_metadata_duration_seconds": 18.0,
                    "finalization_elapsed_ms": 1500,
                    "finalization_process_cpu_seconds": 0.2,
                    "queue_wait_ms": 200,
                    "ffmpeg_elapsed_ms": 1200,
                    "ffmpeg_child_cpu_seconds": 1.05,
                },
            }
        },
        {
            "media": {
                "replay_shard_id": "replay-b",
                "replay_job_id": "job-b",
                "materialization_metrics": {
                    "materialization_mode": "baseline_crop",
                    "input_bytes": 10_000_000,
                    "input_duration_seconds": 20.0,
                    "output_bytes": 2_500_000,
                    "source_metadata_duration_seconds": 20.0,
                    "finalization_elapsed_ms": 1800,
                    "finalization_process_cpu_seconds": 0.3,
                    "queue_wait_ms": 250,
                    "ffmpeg_elapsed_ms": 1400,
                    "ffmpeg_child_cpu_seconds": 1.3,
                },
            }
        },
    ]
    task_rows = [
        {
            "status": "ready",
            "created_at": now.replace(minute=0),
            "updated_at": now.replace(minute=1),
        },
        {
            "status": "pending",
            "created_at": now.replace(minute=2),
            "updated_at": now.replace(minute=3),
        },
    ]

    report = report_module.build_phase0_report(
        event_rows,
        task_rows,
        observed_at=now,
    )

    assert report["status"] == "ok"
    assert report["sample_quality"] == "low"
    assert report["dominant_current_cost"] == "transcode_cpu"
    assert report["dominant_active_materialization_cost"] == "transcode_cpu"
    assert report["replay_job_counts_by_shard"] == {"replay-a": 1, "replay-b": 1}
    assert report["replay_sink"]["input_bytes_total"] == 18_000_000
    assert report["cleanup"]["deleted_bytes_total"] == 4096
    assert report["queue"]["current_depth"] == 1
    assert report["materialization"]["mode_counts"] == {"baseline_crop": 2}
    assert (
        report["materialization"]["media_worker_finalization_process_cpu_seconds"]["count"]
        == 2
    )


def test_phase1a_report_compares_against_phase0_baseline() -> None:
    report_module = _load_report_module()
    baseline = report_module.build_phase0_report(
        [
            {
                "media": {
                    "materialization_metrics": {
                        "materialization_mode": "baseline_crop",
                        "guardrail_mode": "baseline_crop",
                        "queue_wait_ms": 4000,
                        "ffmpeg_elapsed_ms": 1000,
                        "ffmpeg_child_cpu_seconds": 10.0,
                    }
                }
            }
        ],
        [{"status": "pending"}],
        observed_at=datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
    )
    current = report_module.build_phase0_report(
        [
            {
                "media": {
                    "materialization_status": "materialized",
                    "materialization_metrics": {
                        "materialization_mode": "baseline_crop",
                        "guardrail_mode": "bounded_crop",
                        "queue_wait_ms": 2000,
                        "ffmpeg_elapsed_ms": 1200,
                        "ffmpeg_child_cpu_seconds": 11.0,
                    },
                }
            },
            {
                "media": {
                    "materialization_status": "materialization_deferred",
                }
            },
        ],
        [{"status": "ready"}, {"status": "materialization_deferred"}],
        observed_at=datetime(2026, 6, 20, 12, 5, tzinfo=timezone.utc),
        phase_label="phase1a",
        baseline_report=baseline,
    )

    assert current["phase_label"] == "phase1a"
    assert current["queue"]["materialization_status_counts"] == {
        "materialization_deferred": 1,
        "materialized": 1,
    }
    assert current["materialization"]["guardrail_mode_counts"] == {
        "bounded_crop": 1
    }
    comparison = current["baseline_comparison"]
    assert comparison["dominant_current_cost_before"] == "queue_wait"
    assert comparison["dominant_current_cost_after"] == "queue_wait"
    assert comparison["metrics"]["queue_wait_avg_seconds"]["delta"] == -2.0
    assert comparison["metrics"]["ffmpeg_elapsed_avg_seconds"]["delta"] == 0.2


def test_phase0_report_is_explicit_when_no_instrumented_samples() -> None:
    report_module = _load_report_module()

    report = report_module.build_phase0_report(
        [{"media": {"replay_shard_id": "replay-a", "replay_job_id": "job-a"}}],
        [],
        observed_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
    )

    assert report["status"] == "insufficient_runtime_samples"
    assert report["sample_quality"] == "none"
    assert report["dominant_current_cost"] == "insufficient_runtime_samples"
    assert report["dominant_active_materialization_cost"] == "insufficient_runtime_samples"
    assert report["instrumented_materialization_sample_count"] == 0


def test_media_worker_phase0_materialization_metrics_flow_to_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    worker = _activate_media_worker()
    event_id = "11111111-1111-4111-8111-111111111111"
    sink_dir = tmp_path / "sink" / event_id
    sink_dir.mkdir(parents=True)
    (sink_dir / "video.mov").write_bytes(b"replay sink video")
    metadata_file = sink_dir / "metadata.json"
    metadata_file.write_text(
        json.dumps({"job_id": "job-1", "labels": {"event_id": event_id}}) + "\n",
        encoding="utf-8",
    )
    output_root = tmp_path / "evidence"

    def fake_event_context(_conn: object, _event_id: str) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "source_event_id": "source-event-1",
            "event_type": "intrusion",
            "camera_id": "primary_rtsp",
            "source_id": "primary_rtsp",
            "track_id": "track-1",
            "event_ts_ms": 1_780_000_000_000,
            "frame_uuid": "event-frame",
            "created_at": datetime(2026, 6, 20, 11, 59, 59, tzinfo=timezone.utc),
            "payload": {
                "media": {
                    "replay_job_id": "job-1",
                    "replay_job_request": {
                        "configuration": {
                            "labels": {
                                "event_id": event_id,
                                "requested_start_pts": "95000000000",
                                "requested_end_pts": "105000000000",
                                "event_frame_uuid": "event-frame",
                                "event_frame_pts": "100000000000",
                            }
                        }
                    },
                }
            },
        }

    def fake_crop(**kwargs: object) -> dict[str, Any]:
        output_video_path = Path(kwargs["output_video_path"])
        output_video_path.write_bytes(b"raw clip")
        return {
            "measurement_schema_version": "phase0-materialization-v1",
            "method": "ffmpeg_segment_normalized_transcode",
            "materialization_mode": "baseline_crop",
            "crop_video_to_time_window": True,
            "input_bytes": 16,
            "input_duration_seconds": 10.0,
            "output_bytes": 8,
            "source_metadata_frame_count": 11,
            "source_metadata_duration_seconds": 10.0,
            "ffmpeg_returncode": 0,
            "ffmpeg_elapsed_ms": 1200,
            "ffmpeg_child_cpu_seconds": 1.1,
            "decoded_frame_count": 10,
        }

    monkeypatch.setenv("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", "true")
    monkeypatch.setenv("POST_SAVANT_DURATION_GUARD_SLACK_SEC", "1")
    monkeypatch.setenv("EVIDENCE_RUNTIME_EPOCH_STRICT", "false")
    monkeypatch.setattr(worker, "_load_event_context", fake_event_context)
    monkeypatch.setattr(worker, "load_native_metadata", lambda _path: _metadata_rows())
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 10.0)
    monkeypatch.setattr(worker, "build_post_savant_evidence_bundle", _fake_builder(fake_crop))

    bundle = worker._finalize_post_savant_evidence_bundle(
        None,
        event_id=event_id,
        meta_dir=str(sink_dir),
        metadata_file=str(metadata_file),
        evidence_output_dir=str(output_root),
    )

    summary = json.loads((output_root / event_id / "summary.json").read_text(encoding="utf-8"))
    metadata = json.loads((output_root / event_id / "metadata.json").read_text(encoding="utf-8"))

    metrics = summary["materialization_metrics"]
    assert bundle["materialization_metrics"] == metrics
    assert metadata["media"]["materialization_metrics"] == metrics
    assert metrics["materialization_mode"] == "baseline_crop"
    assert metrics["ffmpeg_elapsed_ms"] == 1200
    assert metrics["ffmpeg_child_cpu_seconds"] == 1.1
    assert metrics["input_bytes"] == 16
    assert metrics["input_duration_seconds"] == 10.0
    assert metrics["output_bytes"] == 8
    assert metrics["queue_wait_ms"] is not None
    assert metrics["finalization_process_cpu_seconds"] is not None


def test_media_worker_phase1a_guard_defers_when_concurrency_full() -> None:
    worker = _activate_media_worker()
    guard = worker._MaterializationGuard(max_active=1)

    assert guard.acquire() is True
    assert guard.acquire() is False
    snapshot = worker._materialization_guardrails(
        guard=guard,
        timeout_s=30.0,
        max_backlog=10,
        backlog_depth=9,
        admission_status="deferred",
        reason="materialization_concurrency_limit_exceeded",
    )
    assert snapshot["mode"] == "bounded_crop"
    assert snapshot["max_active"] == 1
    assert snapshot["active_at_decision"] == 1
    assert snapshot["timeout_s"] == 30.0
    assert snapshot["backlog_depth_at_decision"] == 9
    guard.release()
    assert guard.snapshot()["active"] == 0


def test_media_worker_phase1a_backlog_limit_is_disabled_by_zero() -> None:
    worker = _activate_media_worker()

    assert worker._materialization_backlog_limit_exceeded(
        backlog_depth=500,
        max_backlog=0,
    ) is False
    assert worker._materialization_backlog_limit_exceeded(
        backlog_depth=9,
        max_backlog=10,
    ) is False
    assert worker._materialization_backlog_limit_exceeded(
        backlog_depth=10,
        max_backlog=10,
    ) is True


def test_media_worker_phase1a_deferred_state_updates_event_and_task() -> None:
    worker = _activate_media_worker()
    conn = _FakeConnection()
    guardrails = {
        "schema_version": "phase1a-materialization-guardrails-v1",
        "mode": "bounded_crop",
        "admission_status": "deferred",
    }

    worker._mark_media_materialization_deferred(
        conn,
        event_id="11111111-1111-4111-8111-111111111111",
        sink_path="/media/replay-sink-output/example",
        reason="materialization_backlog_limit_exceeded:10>=10",
        guardrails=guardrails,
    )

    assert len(conn.cursor_obj.executions) == 2
    first_query, first_params = conn.cursor_obj.executions[0]
    second_query, second_params = conn.cursor_obj.executions[1]
    assert "materialization_status" in first_query
    assert first_params["state"] == "materialization_deferred"
    assert json.loads(first_params["guardrails"]) == guardrails
    assert "UPDATE evidence_tasks" in second_query
    assert second_params["state"] == "materialization_deferred"


def test_media_worker_phase1a_failed_state_updates_event_and_task() -> None:
    worker = _activate_media_worker()
    conn = _FakeConnection()
    guardrails = {
        "schema_version": "phase1a-materialization-guardrails-v1",
        "mode": "bounded_crop",
        "admission_status": "failed",
    }

    worker._mark_media_materialization_failed(
        conn,
        event_id="11111111-1111-4111-8111-111111111111",
        sink_path="/media/replay-sink-output/example",
        reason="RuntimeError:video_time_domain_crop_failed:timeout:1s",
        guardrails=guardrails,
    )

    assert len(conn.cursor_obj.executions) == 2
    first_query, first_params = conn.cursor_obj.executions[0]
    second_query, second_params = conn.cursor_obj.executions[1]
    assert "materialization_status" in first_query
    assert first_params["state"] == "materialization_failed"
    assert json.loads(first_params["guardrails"]) == guardrails
    assert "UPDATE evidence_tasks" in second_query
    assert second_params["state"] == "materialization_failed"


def _metadata_rows() -> list[dict[str, Any]]:
    rows = []
    for pts in range(95, 106):
        rows.append(
            {
                "type": "VideoFrame",
                "pts": pts * 1_000_000_000,
                "frame_uuid": "event-frame" if pts == 100 else f"frame-{pts}",
                "objects": [{"label": "person"}],
            }
        )
    return rows


def _fake_builder(fake_crop):
    def build(**kwargs: object) -> object:
        worker = sys.modules["app.worker"]
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_clip_path = output_dir / "raw_clip.mov"
        sink_metadata_path = output_dir / "sink_metadata.json"
        annotations_path = output_dir / "annotations.frame_cache.identity.jsonl"
        summary_path = output_dir / "summary.json"
        video_crop = fake_crop(output_video_path=raw_clip_path)
        sink_metadata_path.write_text(
            json.dumps(_metadata_rows()[0]) + "\n",
            encoding="utf-8",
        )
        annotations_path.write_text(
            json.dumps({"frame_uuid": "event-frame", "objects": [{"label": "person"}]})
            + "\n",
            encoding="utf-8",
        )
        summary = {
            "schema_version": "2.0-midterm",
            "evidence_topology": "post_savant_replay",
            "annotation_status": "complete",
            "annotation_source": "post_savant_sink_metadata",
            "production_ready": True,
            "sidecar_frame_count": 1,
            "frame_count": 1,
            "object_counts": {"person": 1, "face": 0, "known_face": 0},
            "video_crop": video_crop,
            "time_window": {
                "time_domain_crop_applied": True,
                "requested_start_pts": 95_000_000_000,
                "requested_end_pts": 105_000_000_000,
                "requested_duration_s": 10.0,
            },
            "limitations": [],
        }
        summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")
        return worker._EvidenceBundleView(
            output_dir=output_dir,
            raw_clip_path=raw_clip_path,
            sink_metadata_path=sink_metadata_path,
            production_sidecar_path=annotations_path,
            summary_path=summary_path,
            summary=summary,
        )

    return build


class _FakeCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, dict[str, Any]]] = []
        self.rowcount = 1

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        self.executions.append((query, params or {}))

    def fetchone(self) -> tuple[int]:
        return (0,)


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj
