"""Phase 2+ manifest-first materialization policy tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVENT_WORKER_DIR = ROOT / "services" / "event-worker"
CLIP_WORKER_DIR = ROOT / "services" / "clip-worker"
MEDIA_WORKER_DIR = ROOT / "services" / "media-worker"
REPORT_SCRIPT = ROOT / "scripts" / "runtime" / "report_evidence_materialization_phase0.py"


def _activate(path: Path) -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    text = str(path)
    if text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)


def _clip_config(**overrides: Any):
    _activate(CLIP_WORKER_DIR)
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
        "consumer_count": 1,
        "poll_timeout_ms": 1,
        "default_pre_seconds": 5,
        "default_post_seconds": 5,
        "keyframe_lookup_window_s": 10,
        "max_jobs_per_run": 100,
        "run_once": True,
        "max_concurrent_jobs": 4,
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
        "post_savant_frame_proof_fast_path_batch_size": 0,
        "post_savant_frame_proof_fast_path_lag": 0,
        "post_savant_frame_proof_fast_path_pending": 0,
        "frame_annotation_lookup_concurrency": 4,
        "frame_annotation_range_cache_ttl_s": 0.0,
        "frame_annotation_range_cache_bucket_ms": 0,
        "frame_annotation_range_cache_max_entries": 64,
        "post_savant_allow_cross_session_post_window_proof": True,
        "post_savant_allow_truncated_pre_window_proof": True,
        "frame_annotation_stream": "security.frame_annotations",
        "frame_annotation_anchor_lookback_count": 100,
        "frame_annotation_anchor_page_count": 100,
        "frame_annotation_anchor_wall_clock_slack_s": 1.0,
        "frame_annotation_anchor_pts_tolerance_s": 1.0,
        "evidence_materialization_policy": "priority",
        "evidence_high_priority_event_types": ("watchlist_hit", "live_search_hit"),
        "evidence_defer_low_priority": False,
        "evidence_replay_ttl_seconds": 300,
        "evidence_frame_annotation_ttl_seconds": 120,
        "evidence_unknown_source_fail_closed": True,
        "evidence_materialization_max_concurrency": 4,
        "evidence_materialization_max_concurrency_per_shard": 2,
        "evidence_materialization_max_concurrency_per_source": 1,
        "evidence_replay_active_slot_extra_seconds": 5.0,
        "media_poll_interval_s": 0.0,
        "midterm_sink_stability_checks": 0,
        "evidence_replay_sink_stability_budget_s": 0.0,
        "evidence_replay_finalizer_budget_s": 0.0,
        "evidence_replay_slot_grace_s": 5.0,
        "evidence_materialization_event_type_quotas": {},
        "evidence_materialization_pressure_level": "normal",
    }
    values.update(overrides)
    return Config(**values)


def _load_report_module():
    spec = importlib.util.spec_from_file_location(
        "report_evidence_materialization_phase0",
        REPORT_SCRIPT,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_post_savant_materialization_metrics_include_phase_latency() -> None:
    _activate(MEDIA_WORKER_DIR)
    from app import worker

    event_context = {
        "created_at": datetime.fromtimestamp(1, tz=timezone.utc),
        "payload": {
            "media": {
                "evidence_diagnostics": {
                    "proof_wait_ms": 25,
                    "record_request_pending_ms": 100,
                    "replay_job_create_ms": 7,
                    "replay_slot_hold_ms": 15000,
                    "replay_job_created_at": "1970-01-01T00:00:02+00:00",
                }
            }
        },
    }
    phase = {
        "sink_metadata_first_seen_at": "1970-01-01T00:00:05+00:00",
        "sink_video_first_seen_at": "1970-01-01T00:00:06+00:00",
        "sink_video_stable_at": "1970-01-01T00:00:16+00:00",
        "sink_ffprobe_ready_at": "1970-01-01T00:00:17+00:00",
        "finalizer_submitted_at": "1970-01-01T00:00:18+00:00",
        "finalizer_started_at": "1970-01-01T00:00:20+00:00",
        "finalizer_submitted_monotonic": 10.0,
        "finalizer_started_monotonic": 12.0,
    }

    metrics = worker._post_savant_materialization_metrics(
        summary={"video_crop": {"crop_video_to_time_window": True}},
        event_context=event_context,
        started_at=datetime.fromtimestamp(20, tz=timezone.utc),
        finished_at=datetime.fromtimestamp(28, tz=timezone.utc),
        finalization_duration_ms=8000,
        phase_diagnostics=phase,
    )

    assert metrics["queue_wait_ms"] == 19000
    assert metrics["proof_wait_ms"] == 25
    assert metrics["replay_job_create_ms"] == 7
    assert metrics["replay_to_sink_metadata_ms"] == 3000
    assert metrics["sink_metadata_to_video_ms"] == 1000
    assert metrics["sink_video_to_stable_ms"] == 10000
    assert metrics["sink_stable_to_ffprobe_ready_ms"] == 1000
    assert metrics["sink_ffprobe_ready_to_finalizer_start_ms"] == 3000
    assert metrics["finalizer_pool_wait_ms"] == 2000
    assert metrics["phase_latency_ms"]["replay_slot_hold_ms"] == 15000


def test_manifest_first_initial_status_and_ttl_metadata(monkeypatch) -> None:
    _activate(EVENT_WORKER_DIR)
    from app import repository

    event = {
        "event_type": "intrusion",
        "source_event_id": "intrusion:1",
        "source_id": "source_00",
        "event_ts_ms": 1_780_000_000_000,
        "evidence_policy": {"pre_seconds": 5, "post_seconds": 5},
    }

    monkeypatch.setenv("EVIDENCE_MATERIALIZATION_DEFER_LOW_PRIORITY", "true")
    assert repository._evidence_task_initial_status(event) == ("manifest_ready", "")
    assert repository._evidence_task_initial_status(
        {**event, "event_type": "watchlist_hit"}
    ) == ("materialization_pending", "")
    assert repository._evidence_task_initial_status(
        {
            **event,
            "event_type": "watchlist_hit",
            "algorithm_type": "face_intelligence",
            "clip_required": False,
            "snapshot_required": True,
            "evidence_policy": {
                "snapshot_required": True,
                "clip_required": False,
                "evidence_mode": "image_only",
                "playback_kind": "image",
            },
            "payload": {
                "media": {
                    "clip_required": False,
                    "evidence_mode": "image_only",
                    "playback_kind": "image",
                }
            },
        }
    ) == ("materialization_pending", "")

    ttl = repository._materialization_ttl_metadata(event)
    assert ttl["replay_ttl_seconds"] == 300
    assert ttl["frame_annotation_ttl_seconds"] == 120
    assert ttl["materialization_deadline_at"] == ttl["annotation_deadline_at"]

    monkeypatch.setenv("EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS", "5")
    assert repository._materialization_ready_at(
        event,
        event["evidence_policy"],
    ).isoformat() == "2026-05-28T20:26:50+00:00"


def test_event_worker_evidence_admission_limits_active_pending_by_source(
    monkeypatch,
) -> None:
    _activate(EVENT_WORKER_DIR)
    from app import repository

    class FakeCursor:
        def __init__(self) -> None:
            self.params: dict[str, Any] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str, params: dict[str, Any]) -> None:
            self.params = params

        def fetchone(self) -> tuple[int]:
            if self.params.get("source_id") == "source-1":
                return (2,)
            return (0,)

    class FakeConn:
        def __init__(self) -> None:
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL", "10")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE", "2")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE", "")

    decision = repository._evidence_admission_decision(
        FakeConn(),
        {"event_type": "intrusion"},
        initial_status="materialization_pending",
        source_id="source-1",
        event_type="intrusion",
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "admission_source_active_limit_reached"
    assert decision["scope"] == "source"
    assert decision["observed"] == 2


def test_media_worker_materializes_watchlist_image_from_rolling_cache(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _activate(MEDIA_WORKER_DIR)
    from PIL import Image
    from app import worker
    from app.rolling_cache import RollingSegment

    cache_dir = tmp_path / "cache-segment"
    cache_dir.mkdir()
    video_path = cache_dir / "video.mov"
    metadata_path = cache_dir / "metadata.json"
    video_path.write_bytes(b"video")
    metadata_path.write_text(
        json.dumps({"frames": [{"pts": 10_000_000_000, "uuid": "frame-1"}]}),
        encoding="utf-8",
    )
    segment = RollingSegment(
        segment_id="seg-1",
        source_id="camera-1",
        runtime_epoch_id="epoch-1",
        directory=cache_dir,
        video_path=video_path,
        metadata_path=metadata_path,
        first_pts=9_000_000_000,
        last_pts=11_000_000_000,
        frame_count=1,
        size_bytes=4,
    )

    def fake_extract(_video_path: Path, output_path: Path, *, offset_s: float) -> None:
        assert offset_s == 1.0
        Image.new("RGB", (640, 480), color=(20, 30, 40)).save(output_path, "JPEG")

    monkeypatch.setattr(worker, "_extract_full_frame_image", fake_extract)
    cfg = SimpleNamespace(evidence_output_dir=str(tmp_path / "evidence"))
    event_context = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "event_type": "watchlist_hit",
        "source_id": "camera-1",
        "camera_id": "cam-1",
        "frame_uuid": "frame-1",
        "payload": {
            "match": {"source_observation_id": "face:camera-1:7:10000"},
            "matched_person": {"name": "Reese"},
            "observation": {
                "face_bbox": {
                    "format": "cxcywh",
                    "values": [320, 240, 160, 120],
                    "coordinate_space": "pixel",
                }
            },
            "media": {"frame_pts": 10_000_000_000, "frame_uuid": "frame-1"},
        },
    }

    result = worker._materialize_face_image_from_rolling_cache(
        cfg=cfg,
        row={"event_id": event_context["event_id"], "source_id": "camera-1"},
        event_context=event_context,
        segments=[segment],
        runtime_epoch_id="epoch-1",
    )

    assert Path(str(result["full_frame_path"])).is_file()
    assert Path(str(result["face_crop_path"])).is_file()
    assert Path(str(result["annotated_frame_path"])).is_file()
    assert result["crop_status"] == "ready"
    assert result["annotation_box"] == (240, 180, 400, 300)
    assert result["crop_box"] == (208, 156, 432, 324)
    assert result["source_observation_id"] == "face:camera-1:7:10000"


def test_media_worker_watchlist_image_uses_nearest_cache_frame_for_small_gap(
    tmp_path: Path,
) -> None:
    _activate(MEDIA_WORKER_DIR)
    from app import worker
    from app.rolling_cache import RollingSegment

    cache_dir = tmp_path / "cache-segment"
    cache_dir.mkdir()
    video_path = cache_dir / "video.mov"
    metadata_path = cache_dir / "metadata.json"
    video_path.write_bytes(b"video")
    metadata_path.write_text(
        json.dumps(
            {
                "frames": [
                    {"pts": 10_000_000_000, "uuid": "near-frame"},
                    {"pts": 12_000_000_000, "uuid": "far-frame"},
                ]
            }
        ),
        encoding="utf-8",
    )
    segment = RollingSegment(
        segment_id="seg-1",
        source_id="camera-1",
        runtime_epoch_id="epoch-1",
        directory=cache_dir,
        video_path=video_path,
        metadata_path=metadata_path,
        first_pts=9_000_000_000,
        last_pts=10_000_000_000,
        frame_count=2,
        size_bytes=4,
    )
    event_context = {
        "frame_uuid": "missing-frame",
        "payload": {"media": {"frame_pts": 10_200_000_000}},
    }

    frame = worker._rolling_cache_frame_for_image_event(event_context, [segment])

    assert frame["segment"] == segment
    assert frame["pts"] == 10_000_000_000
    assert frame["frame_uuid"] == "near-frame"
    assert frame["frame_selection"] == "nearest_metadata"


def test_media_worker_watchlist_image_rejects_distant_cache_frame(
    tmp_path: Path,
) -> None:
    _activate(MEDIA_WORKER_DIR)
    from app import worker
    from app.rolling_cache import RollingCacheCoverageMiss, RollingSegment

    cache_dir = tmp_path / "cache-segment"
    cache_dir.mkdir()
    video_path = cache_dir / "video.mov"
    metadata_path = cache_dir / "metadata.json"
    video_path.write_bytes(b"video")
    metadata_path.write_text(
        json.dumps({"frames": [{"pts": 10_000_000_000, "uuid": "old-frame"}]}),
        encoding="utf-8",
    )
    segment = RollingSegment(
        segment_id="seg-1",
        source_id="camera-1",
        runtime_epoch_id="epoch-1",
        directory=cache_dir,
        video_path=video_path,
        metadata_path=metadata_path,
        first_pts=9_000_000_000,
        last_pts=10_000_000_000,
        frame_count=1,
        size_bytes=4,
    )
    event_context = {
        "frame_uuid": "missing-frame",
        "payload": {"media": {"frame_pts": 11_000_000_000}},
    }

    try:
        worker._rolling_cache_frame_for_image_event(event_context, [segment])
    except RollingCacheCoverageMiss as exc:
        assert str(exc) == "face_image_frame_not_found"
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("distant frame must not be accepted for face evidence")


def test_event_worker_evidence_admission_source_limit_blocks_watchlist_priority(
    monkeypatch,
) -> None:
    _activate(EVENT_WORKER_DIR)
    from app import repository

    class FakeCursor:
        def __init__(self) -> None:
            self.params: dict[str, Any] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str, params: dict[str, Any]) -> None:
            self.params = params

        def fetchone(self) -> tuple[int]:
            if self.params.get("source_id") == "source-1":
                return (2,)
            return (0,)

    class FakeConn:
        def __init__(self) -> None:
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL", "10")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE", "2")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE", "")

    decision = repository._evidence_admission_decision(
        FakeConn(),
        {"event_type": "watchlist_hit"},
        initial_status="materialization_pending",
        source_id="source-1",
        event_type="watchlist_hit",
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "admission_source_active_limit_reached"
    assert decision["scope"] == "source"
    assert decision["observed"] == 2


def test_event_worker_evidence_admission_event_type_budget_prioritizes_watchlist(
    monkeypatch,
) -> None:
    _activate(EVENT_WORKER_DIR)
    from app import repository

    class FakeCursor:
        def __init__(self) -> None:
            self.params: dict[str, Any] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str, params: dict[str, Any]) -> None:
            self.params = params

        def fetchone(self) -> tuple[int]:
            if self.params.get("event_type") == "intrusion":
                return (40,)
            return (0,)

    class FakeConn:
        def __init__(self) -> None:
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL", "0")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE", "0")
    monkeypatch.setenv(
        "EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE",
        "intrusion:40",
    )

    intrusion = repository._evidence_admission_decision(
        FakeConn(),
        {"event_type": "intrusion"},
        initial_status="materialization_pending",
        source_id="source-1",
        event_type="intrusion",
    )
    watchlist = repository._evidence_admission_decision(
        FakeConn(),
        {"event_type": "watchlist_hit"},
        initial_status="materialization_pending",
        source_id="source-1",
        event_type="watchlist_hit",
    )

    assert intrusion["allowed"] is False
    assert intrusion["reason"] == "admission_event_type_active_limit_reached"
    assert watchlist["allowed"] is True
    assert watchlist["reason"] == "admitted"


def test_event_worker_create_task_persists_admission_decision() -> None:
    source = (EVENT_WORKER_DIR / "app" / "repository.py").read_text(encoding="utf-8")

    assert "admission_decision = _evidence_admission_decision" in source
    assert "evidence_admission_skipped:" in source
    assert '"admission_decision": admission_decision' in source
    assert '"materialization_skipped"' in source


def test_clip_gate_enforces_per_shard_source_quota_and_degrade() -> None:
    _activate(CLIP_WORKER_DIR)
    import app.worker as worker

    active_jobs = [
        worker.ActiveReplayJob(
            until_monotonic=999.0,
            shard_id="replay-a",
            source_id="source_00",
            event_type="intrusion",
        )
    ]
    cfg = _clip_config(
        evidence_materialization_max_concurrency=10,
        evidence_materialization_max_concurrency_per_shard=1,
        evidence_materialization_max_concurrency_per_source=1,
    )

    decision = worker._clip_gate_decision(
        cfg,
        jobs_created=0,
        active_jobs=active_jobs,
        shard_id="replay-a",
        source_id="source_01",
        camera_id="camera-1",
        cooldown_gate_ts_ms=1_780_000_000_000,
        last_job_by_camera={},
        event_type_counts={},
        event_type="intrusion",
    )

    assert decision.allowed is False
    assert decision.reason == "max_concurrent_per_shard_reached"
    assert decision.quota_decision["scope"] == "shard_concurrency"

    quota_cfg = _clip_config(
        evidence_materialization_event_type_quotas={"intrusion": 1}
    )
    quota_decision = worker._clip_gate_decision(
        quota_cfg,
        jobs_created=0,
        active_jobs=[],
        shard_id="replay-a",
        source_id="source_02",
        camera_id="camera-2",
        cooldown_gate_ts_ms=1_780_000_000_000,
        last_job_by_camera={},
        event_type_counts={"intrusion": 1},
        event_type="intrusion",
    )

    assert quota_decision.terminal_defer is True
    assert quota_decision.reason == "event_type_quota_reached"

    pressure_cfg = _clip_config(evidence_materialization_pressure_level="critical")
    pressure_decision = worker._clip_gate_decision(
        pressure_cfg,
        jobs_created=0,
        active_jobs=[],
        shard_id="replay-a",
        source_id="source_03",
        camera_id="camera-3",
        cooldown_gate_ts_ms=1_780_000_000_000,
        last_job_by_camera={},
        event_type_counts={},
        event_type="intrusion",
    )

    assert pressure_decision.terminal_defer is True
    assert pressure_decision.degrade_decision["level"] == "critical"


class _ExpireCursor:
    rowcount = 3

    def __enter__(self) -> "_ExpireCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.sql = sql
        self.params = params or {}

    def fetchone(self) -> tuple[int]:
        return (0,)


class _ExpireConn:
    def __init__(self) -> None:
        self.cursor_obj = _ExpireCursor()

    def cursor(self) -> _ExpireCursor:
        return self.cursor_obj


def test_expire_materialization_deadlines_marks_expired_state() -> None:
    _activate(CLIP_WORKER_DIR)
    from app.repository import expire_materialization_deadlines

    conn = _ExpireConn()

    assert expire_materialization_deadlines(conn) == 3
    assert "materialization_expired" in conn.cursor_obj.sql
    assert "'materializing'" not in conn.cursor_obj.sql
    assert "replay_job_created" in conn.cursor_obj.params["replay_statuses"]
    assert "materialization_deadline_at <= now()" in conn.cursor_obj.sql
    assert "materialization_owner" in conn.cursor_obj.sql
    assert "materialization_audit->'rolling_cache'->>'status'" in conn.cursor_obj.sql
    assert "l.bundle_event_id = evidence_tasks.event_id" in conn.cursor_obj.sql


def test_storage_quota_decision_marks_hard_limit(tmp_path: Path) -> None:
    _activate(MEDIA_WORKER_DIR)
    import app.worker as worker

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    (evidence_root / "raw_clip.mov").write_bytes(b"x" * 32)

    decision = worker._storage_quota_decision(
        evidence_output_dir=str(evidence_root),
        sink_output_dir=str(tmp_path / "sink"),
        evidence_final_root_max_bytes=16,
    )

    assert decision["overall_level"] == "hard"
    assert decision["areas"][0]["area"] == "evidence_final_root"


def test_phase2plus_report_includes_ttl_and_runtime_limited_60_stream() -> None:
    report_module = _load_report_module()

    report = report_module.build_phase0_report(
        [
            {
                "media": {
                    "replay_shard_id": "replay-a",
                    "quota_decision": {"level": "warning"},
                    "materialization_metrics": {
                        "input_bytes": 12_000,
                        "source_metadata_duration_seconds": 12,
                        "ffmpeg_elapsed_ms": 1000,
                        "ffmpeg_child_cpu_seconds": 2.0,
                    },
                }
            }
        ],
        [
            {
                "status": "materialization_deferred",
                "materialization_status": "materialization_deferred",
            }
        ],
        observed_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        phase_label="phase2plus",
        replay_ttl_seconds=300,
        frame_annotation_ttl_seconds=120,
        proposed_deferred_windows_seconds=[600, 900],
    )

    assert report["ttl_sizing"]["bytes_per_second_per_stream"] == 1000.0
    assert report["ttl_sizing"]["replay_rocksdb_estimated_bytes"]["30_streams"]["300"] == 9_000_000
    assert report["ttl_sizing"]["effective_deferred_window_seconds_current"] == 120
    assert report["ttl_sizing"]["frame_annotation_extension_multipliers"] == {
        "300": 2.5,
        "600": 5.0,
        "900": 7.5,
    }
    assert report["pressure_test_acceptance"]["sixty_stream_token"] == (
        "not_run_runtime_limited"
    )
    assert report["quota_and_degrade"]["quota_decision_levels"] == {"warning": 1}


def test_phase2plus_report_can_compare_multiple_reference_reports() -> None:
    report_module = _load_report_module()

    reference = report_module.build_phase0_report(
        [],
        [],
        observed_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        phase_label="phase0",
    )

    report = report_module.build_phase0_report(
        [],
        [],
        observed_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        phase_label="phase2plus",
        reference_reports={"phase0": reference, "phase1a": reference},
    )

    assert sorted(report["reference_report_comparisons"]) == ["phase0", "phase1a"]
    assert report["reference_report_comparisons"]["phase0"]["baseline_phase_label"] == "phase0"
