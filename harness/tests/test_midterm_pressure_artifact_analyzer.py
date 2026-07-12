from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT.parent / "scripts" / "tools" / "analyze_midterm_pressure_artifact.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "analyze_midterm_pressure_artifact",
        SCRIPT,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_artifact(tmp_path: Path) -> Path:
    artifact_dir = tmp_path / "pressure60_test"
    artifact_dir.mkdir()
    report = {
        "run_id": "pressure60_test",
        "status": "passed",
        "kept_evidence_count": 2,
        "kept_evidence": [
            {
                "event_id": "event-a",
                "source_id": "source-a",
                "payload": {
                    "media": {
                        "replay_shard_id": "replay-a",
                        "evidence_diagnostics": {
                            "phase_latency_ms": {
                                "record_request_pending_ms": 70_000,
                            },
                        },
                    },
                },
            },
            {
                "event_id": "event-b",
                "source_id": "source-b",
                "payload": {
                    "media": {
                        "replay_shard_id": "replay-b",
                        "evidence_diagnostics": {
                            "phase_latency_ms": {
                                "record_request_pending_ms": 80_000,
                            },
                        },
                    },
                },
            },
        ],
    }
    downstream = {
        "phase_latency_ms": {
            "clip_worker": {
                "record_request_pending_ms": {
                    "status": "measured",
                    "count": 4,
                    "p50": 50_000,
                    "p95": 90_000,
                    "max": 92_000,
                },
                "proof_wait_ms": {
                    "status": "measured",
                    "count": 4,
                    "p50": 2_000,
                    "p95": 5_000,
                    "max": 6_000,
                },
            },
        },
        "media_worker": {
            "scheduler": {
                "schema_version": "phase0-scheduler-v1",
                "poll_duration_ms": {"status": "measured", "count": 2, "p95": 1200},
            },
            "queue_wait_ms": {
                "status": "measured",
                "count": 4,
                "p50": 70_000,
                "p95": 120_000,
                "max": 125_000,
            },
            "queue_wait_ms_by_source": {
                "source-a": {
                    "status": "measured",
                    "count": 2,
                    "p50": 70_000,
                    "p95": 120_000,
                    "max": 125_000,
                },
                "source-b": {
                    "status": "measured",
                    "count": 2,
                    "p50": 75_000,
                    "p95": 118_000,
                    "max": 120_000,
                },
            },
            "queue_wait_ms_by_shard": {
                "replay-a": {
                    "status": "measured",
                    "count": 2,
                    "p50": 70_000,
                    "p95": 120_000,
                    "max": 125_000,
                },
                "replay-b": {
                    "status": "measured",
                    "count": 2,
                    "p50": 75_000,
                    "p95": 118_000,
                    "max": 120_000,
                },
            },
            "sink_video_to_stable_ms": {
                "status": "measured",
                "count": 4,
                "p50": 50,
                "p95": 60,
                "max": 70,
            },
        },
        "evidence_types": {
            "behavior_video": {"playable_bundle_count": 1},
            "watchlist_image": {"ready_bundle_count": 1},
            "playable_or_ready_total": 2,
        },
    }
    clip_logs = "\n".join(
        [
            (
                "INFO app.worker clip_worker_phase_timing event_id=event-a "
                "record_request_pending_ms=70000 proof_wait_ms=2000 "
                "replay_job_create_ms=20 replay_slot_hold_ms=90000"
            ),
            (
                "INFO app.worker clip_worker_queued "
                "reason=max_concurrent_per_source_reached "
                "error=EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE reached"
            ),
            "INFO app.worker replay_slot_terminal_state event_id=event-c",
        ]
    )
    (artifact_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (artifact_dir / "downstream_observability_summary.json").write_text(
        json.dumps(downstream),
        encoding="utf-8",
    )
    (artifact_dir / "clip_worker_logs_since_start.txt").write_text(
        clip_logs,
        encoding="utf-8",
    )
    return artifact_dir


def test_analyze_artifact_reports_source_shard_distribution_and_counts(tmp_path: Path) -> None:
    module = _load_module()
    artifact_dir = _write_artifact(tmp_path)

    summary = module.analyze_artifact(artifact_dir)

    assert summary["run_id"] == "pressure60_test"
    assert summary["clip_worker"]["record_request_pending_ms"]["overall"]["p95"] == 90_000
    assert (
        summary["clip_worker"]["record_request_pending_ms"][
            "by_source_from_kept_evidence"
        ]["source-a"]["max"]
        == 70_000
    )
    assert summary["source_evidence_counts"] == {"source-a": 1, "source-b": 1}
    assert summary["source_to_shard_from_kept_evidence"]["source-b"] == "replay-b"
    assert summary["clip_worker"]["log_counts"]["clip_worker_queued"] == 1
    assert summary["clip_worker"]["log_counts"]["replay_slot_terminal_state"] == 1
    assert summary["media_scheduler"]["schema_version"] == "phase0-scheduler-v1"
    assert summary["evidence_types"]["playable_or_ready_total"] == 2
    assert summary["diagnosis"]["diagnosis"] == "global_or_shard_outlet_bottleneck_likely"


def test_analyze_artifact_falls_back_to_clip_log_phase_metrics(tmp_path: Path) -> None:
    module = _load_module()
    artifact_dir = tmp_path / "pressure60_log_only"
    artifact_dir.mkdir()
    (artifact_dir / "report.json").write_text(
        json.dumps({"kept_evidence": []}),
        encoding="utf-8",
    )
    (artifact_dir / "clip_worker_logs_since_start.txt").write_text(
        (
            "INFO app.worker clip_worker_phase_timing event_id=event-a "
            "record_request_pending_ms=1000 proof_wait_ms=2000 "
            "replay_job_create_ms=30 replay_slot_hold_ms=4000"
        ),
        encoding="utf-8",
    )

    summary = module.analyze_artifact(artifact_dir)

    assert summary["clip_worker"]["record_request_pending_ms"]["overall"]["count"] == 1
    assert summary["clip_worker"]["record_request_pending_ms"]["overall"]["max"] == 1000


def test_analyze_artifact_derives_explicit_evidence_types_from_legacy_summary(
    tmp_path: Path,
) -> None:
    module = _load_module()
    artifact_dir = tmp_path / "pressure60_legacy_types"
    artifact_dir.mkdir()
    (artifact_dir / "report.json").write_text(
        json.dumps({"kept_evidence": []}),
        encoding="utf-8",
    )
    (artifact_dir / "downstream_observability_summary.json").write_text(
        json.dumps(
            {
                "postgresql": {
                    "run_summary": {
                        "behavior_video_playable_bundles": 252,
                        "face_image_ready_bundles": 119,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    summary = module.analyze_artifact(artifact_dir)

    assert summary["evidence_types"]["behavior_video"]["playable_bundle_count"] == 252
    assert summary["evidence_types"]["watchlist_image"]["ready_bundle_count"] == 119
    assert summary["evidence_types"]["playable_or_ready_total"] == 371
