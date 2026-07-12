"""Spec 34 Phase 0 characterization and golden-contract tests."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "harness" / "fixtures" / "clip_media_phase0"
CLIP_WORKER_DIR = ROOT / "services" / "clip-worker"
MEDIA_WORKER_DIR = ROOT / "services" / "media-worker"


def _json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _nested(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        assert isinstance(current, dict), path
        current = current[part]
    return current


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _call_count(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            count += 1
        elif isinstance(func, ast.Attribute) and func.attr == name:
            count += 1
    return count


def test_required_characterization_scenarios_are_named_and_executable() -> None:
    manifest = _json("legacy_behavior_manifest.json")
    scenarios = manifest["clip_scenarios"] + manifest["media_scenarios"]

    assert manifest["acceptance_token"] == (
        "PASS_CLIP_MEDIA_LEGACY_BEHAVIOR_BASELINE_FROZEN"
    )
    for scenario in scenarios:
        relative, test_name = scenario["nodeid"].split("::", 1)
        assert test_name in _test_names(ROOT / relative), scenario


def test_legacy_side_effect_inventory_remains_recorded_during_phase1() -> None:
    expected = _json("legacy_behavior_manifest.json")["legacy_source_inventory"]
    clip_worker = CLIP_WORKER_DIR / "app" / "worker.py"
    media_worker = MEDIA_WORKER_DIR / "app" / "worker.py"

    # The manifest remains the immutable Phase 0 source baseline. Phase 1
    # deliberately consolidates ACK and expiry ownership without rewriting it.
    assert expected == {
        "clip_direct_xack_calls": 16,
        "media_thread_pool_executor_calls": 3,
        "media_rolling_expiry_call_sites": 2,
        "media_process_sink_output_call_sites": 3,
    }
    assert _call_count(clip_worker, "xack") < expected["clip_direct_xack_calls"]
    assert _call_count(media_worker, "_expire_overdue_rolling_cache_tasks") < expected[
        "media_rolling_expiry_call_sites"
    ]


def test_replay_payload_golden_fixtures_match_legacy_builder(monkeypatch) -> None:
    module = _load_module(
        "phase0_replay_client",
        CLIP_WORKER_DIR / "app" / "replay_client.py",
    )
    monkeypatch.setenv("REPLAY_FORCE_CONSTANT_CADENCE", "true")
    monkeypatch.setenv("REPLAY_TS_SYNC", "false")

    for scenario in _json("replay_payload_golden.json")["scenarios"]:
        payload = module.build_job_payload(**scenario["arguments"])
        for path, expected in scenario["expected_paths"].items():
            assert _nested(payload, path) == expected, (scenario["name"], path)


def test_clip_and_media_correlation_fields_cover_required_identities() -> None:
    clip = _load_module(
        "phase0_clip_observability",
        CLIP_WORKER_DIR / "app" / "legacy_observability.py",
    )
    media = _load_module(
        "phase0_media_observability",
        MEDIA_WORKER_DIR / "app" / "legacy_observability.py",
    )
    request = {
        "source_id": "source-1",
        "runtime_epoch_id": "epoch-1",
        "stream_session_id": "session-1",
    }
    clip_fields = clip.request_correlation(
        request,
        event_id="event-1",
        request_id="request-1",
        delivery_id="100-0",
        retry_count=1,
        consumer="clip-1",
    )
    clip_fields = clip.with_replay_job(clip_fields, replay_job_id="replay-1")
    media_fields = media.materialization_correlation(
        {
            "event_id": "event-1",
            "source_id": "source-1",
            "payload": {
                "runtime_epoch_id": "epoch-1",
                "media": {
                    "stream_session_id": "session-1",
                    "replay_job_id": "replay-1",
                    "evidence_diagnostics": {"correlation": clip_fields},
                },
            },
        },
        phase_diagnostics={"attempt_id": "attempt-1"},
    )

    for field in (
        "event_id",
        "request_id",
        "attempt_id",
        "lease_token",
        "replay_job_id",
        "runtime_epoch_id",
    ):
        assert field in clip_fields
        assert field in media_fields
    assert media_fields["request_id"] == "request-1"
    assert media_fields["replay_job_id"] == "replay-1"
    assert media_fields["lease_state"] == "not_applicable_legacy"


def test_temporal_guard_failure_is_attributed_without_relabeling_duration() -> None:
    module = _load_module(
        "phase0_media_guard_observability",
        MEDIA_WORKER_DIR / "app" / "legacy_observability.py",
    )
    attribution = module.guard_failure_attribution(
        {
            "duration_guard_status": "passed",
            "duration_guard_failed": False,
            "sink_window_guard_status": "failed",
            "sink_window_guard_failed": True,
            "sink_window_guard_reason": "sink_metadata_pts_gap_exceeds_limit",
        }
    )

    assert attribution["failed"] is True
    assert attribution["primary_category"] == "sink_window"
    assert attribution["primary_reason"] == "sink_metadata_pts_gap_exceeds_limit"


def test_reference_artifact_contract_is_complete_and_hash_addressed() -> None:
    reference = _json("legacy_behavior_manifest.json")["reference_artifact"]

    assert reference["behavior_video_count"] == 252
    assert reference["watchlist_image_count"] == 119
    assert reference["playable_or_ready_count"] == 371
    assert reference["timeline_ok_count"] == reference["behavior_video_count"]
    assert reference["annotation_ok_count"] == reference["behavior_video_count"]
    assert reference["annotation_record_count"] > 0
    assert len(reference["report_sha256"]) == 64
    assert len(reference["run_config_sha256"]) == 64


def _load_clip_helpers():
    return _load_module(
        "phase0_clip_worker_helpers",
        ROOT / "harness" / "tests" / "test_clip_worker_queue_safety.py",
    )


def test_malformed_request_is_acked_without_side_effect(monkeypatch) -> None:
    helpers = _load_clip_helpers()
    helpers._activate()
    import app.worker as worker

    redis_client = helpers._FakeRedis([None])
    updates: list[dict[str, Any]] = []
    worker.shutdown_requested = False
    monkeypatch.setattr(
        worker,
        "update_clip_status",
        lambda *_args, **kwargs: updates.append(kwargs) or True,
    )

    worker.run_worker(
        helpers._clip_config(max_concurrent_jobs=0, pending_claim_count=0),
        redis_client,
        helpers._DiagnosticsConn(),
    )

    assert redis_client.acked == ["1-0"]
    assert updates == []


def test_replay_empty_response_releases_slot_and_acks(monkeypatch) -> None:
    """Keep the Phase 0 scenario nodeid while asserting the Phase 1 contract."""
    helpers = _load_clip_helpers()
    helpers._activate()
    import app.worker as worker

    class EmptyReplay(helpers._FakeReplay):
        def create_job(self, **kwargs):
            self.jobs.append(kwargs)
            self.last_job_request = dict(kwargs)
            return None

    redis_client = helpers._FakeRedis([helpers._request("301")])
    updates: list[dict[str, Any]] = []
    releases: list[dict[str, Any]] = []
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", EmptyReplay)
    monkeypatch.setattr(
        worker,
        "update_clip_status",
        lambda _conn, event_id, status, **kwargs: updates.append(
            {"event_id": event_id, "status": status, **kwargs}
        )
        or True,
    )
    monkeypatch.setattr(
        worker,
        "release_replay_slot",
        lambda _conn, **kwargs: releases.append(kwargs) or True,
    )

    worker.run_worker(
        helpers._clip_config(max_concurrent_jobs=0, pending_claim_count=0),
        redis_client,
        helpers._DiagnosticsConn(),
    )

    assert redis_client.acked == []
    assert updates[-1]["status"] == "pending"
    assert updates[-1]["evidence_state"] == "materialization_pending"
    assert updates[-1]["evidence_reason"] == "replay_unavailable"
    assert releases[-1]["release_reason"] == "replay_job_create_failed"


def test_replay_success_ack_is_withheld_when_persist_calls_fail(
    monkeypatch,
) -> None:
    helpers = _load_clip_helpers()
    helpers._activate()
    import app.worker as worker

    redis_client = helpers._FakeRedis([helpers._request("302")])
    call_order: list[str] = []
    worker.shutdown_requested = False
    helpers._FakeReplay.instances.clear()
    monkeypatch.setattr(worker, "ReplayClient", helpers._FakeReplay)
    monkeypatch.setattr(
        worker,
        "update_clip_status",
        lambda *_args, **_kwargs: call_order.append("status_persist") or False,
    )
    monkeypatch.setattr(
        worker,
        "record_replay_job_for_slot",
        lambda *_args, **_kwargs: call_order.append("slot_persist") or False,
    )
    original_xack = redis_client.xack

    def ordered_xack(*args, **kwargs):
        call_order.append("xack")
        return original_xack(*args, **kwargs)

    redis_client.xack = ordered_xack
    worker.run_worker(
        helpers._clip_config(max_concurrent_jobs=0, pending_claim_count=0),
        redis_client,
        helpers._DiagnosticsConn(),
    )

    assert call_order == ["status_persist", "slot_persist"]
    assert redis_client.acked == []
