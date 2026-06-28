"""Phase 2+ manifest-first materialization policy tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
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

    ttl = repository._materialization_ttl_metadata(event)
    assert ttl["replay_ttl_seconds"] == 300
    assert ttl["frame_annotation_ttl_seconds"] == 120
    assert ttl["materialization_deadline_at"] == ttl["annotation_deadline_at"]


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


def test_event_worker_evidence_admission_source_limit_allows_watchlist_priority(
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

    assert decision["allowed"] is True
    assert decision["high_priority"] is True
    assert decision["source_limit_bypassed_for_priority"] is True
    assert decision["source_observed"] == 2


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

    def execute(self, sql: str) -> None:
        self.sql = sql


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
    assert "'materializing'" in conn.cursor_obj.sql
    assert "'replay_job_created'" in conn.cursor_obj.sql
    assert "materialization_deadline_at <= now()" in conn.cursor_obj.sql


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
