"""Spec 34 Phase 2 pure Clip planner and proof-port parity contracts."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLIP_ROOT = ROOT / "services" / "clip-worker"
PHASE0_FIXTURES = ROOT / "harness" / "fixtures" / "clip_media_phase0"
PHASE2_FIXTURES = ROOT / "harness" / "fixtures" / "clip_media_phase2"


def _activate():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    root = str(CLIP_ROOT)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    import app.contracts as contracts
    import app.proof_resolver as proof_resolver
    import app.replay_client as replay_client
    import app.replay_planner as planner
    import app.worker as worker

    return contracts, proof_resolver, replay_client, planner, worker


def _queue_helpers():
    path = ROOT / "harness" / "tests" / "test_clip_worker_queue_safety.py"
    spec = importlib.util.spec_from_file_location("phase2_clip_queue_helpers", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _public_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _call_count(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    )


def _request(**overrides: Any) -> dict[str, Any]:
    request = {
        "request_id": "request-1",
        "event_id": "11111111-1111-4111-8111-111111111111",
        "source_event_id": "intrusion:source-1:1",
        "source_id": "source-1",
        "camera_id": "camera-1",
        "event_ts_ms": 1_780_000_000_000,
        "event_type": "intrusion",
        "strategy": "savant_replay",
        "pre_seconds": 5,
        "post_seconds": 5,
        "frame_uuid": "event-frame",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "anchor_keyframe_uuid": "start-keyframe",
        "runtime_epoch_id": "epoch-1",
        "stream_session_id": "session-1",
        "replay_source_kind": "post_savant",
        "metadata_domain": "video_frame",
    }
    request.update(overrides)
    return request


def _proofs(contracts, *, truncated: bool = False, cross_session: bool = False):
    effective_start = 9_250_000_000 if truncated else 5_000_000_000
    start = contracts.FrameAnnotationAnchor(
        frame_uuid="start-frame",
        frame_pts=effective_start,
        stream_id="100-0",
        source_id="source-1",
        camera_id="camera-1",
        stream_session_id="session-1",
        keyframe_uuid="start-keyframe",
        keyframe_pts=effective_start,
        anchor_method=(
            "frame_annotation_truncated_start_window"
            if truncated
            else "frame_annotation"
        ),
    )
    post = contracts.FrameAnnotationAnchor(
        frame_uuid="post-frame",
        frame_pts=15_000_000_000,
        stream_id="200-0",
        source_id="source-1",
        camera_id="camera-1",
        stream_session_id="session-2" if cross_session else "session-1",
        keyframe_uuid="post-keyframe",
        keyframe_pts=14_000_000_000,
    )
    return contracts.ReplayFrameDomainProofs(
        start_window_frame=start,
        post_window_frame=post,
        requested_start_pts=5_000_000_000,
        effective_start_pts=effective_start,
        pre_window_truncated=truncated,
        pre_window_policy=(
            "truncated_to_current_session" if truncated else "full_requested_window"
        ),
    )


def test_contracts_and_planner_have_no_runtime_side_effect_dependencies() -> None:
    forbidden = {"redis", "psycopg", "httpx", "subprocess"}
    contracts_path = CLIP_ROOT / "app" / "contracts.py"
    planner_path = CLIP_ROOT / "app" / "replay_planner.py"
    proof_path = CLIP_ROOT / "app" / "proof_resolver.py"

    assert _public_import_roots(contracts_path).isdisjoint(forbidden)
    assert _public_import_roots(planner_path).isdisjoint(forbidden)
    assert _public_import_roots(proof_path).isdisjoint(forbidden)
    planner_source = planner_path.read_text(encoding="utf-8")
    proof_source = proof_path.read_text(encoding="utf-8")
    assert "create_job(" not in planner_source
    assert "create_job(" not in proof_source
    assert "xack(" not in planner_source
    assert "cursor(" not in planner_source


def test_typed_contracts_are_frozen_and_round_trip_canonical_data() -> None:
    contracts, _resolver, _client, planner, _worker = _activate()
    normalized = planner.normalize_record_request(
        _request(),
        default_pre_seconds=5,
        default_post_seconds=5,
    )

    with pytest.raises(FrozenInstanceError):
        normalized.source_id = "mutated"
    assert normalized.as_dict()["source_id"] == "source-1"
    assert normalized.request_id == "intrusion:source-1:1:savant_replay"
    assert normalized.post_savant_media_request is True
    assert contracts.ProofResolutionKind.READY.value == "ready"


@pytest.mark.parametrize(
    "request_data",
    [
        _request(),
        _request(
            event_type="",
            source_event_id="watchlist_hit:source-1:2",
            anchor_keyframe_uuid="",
            previous_keyframe_uuid="previous-keyframe",
        ),
        _request(
            replay_source_kind="",
            evidence_topology="post_savant_replay",
            annotation_source_policy="",
        ),
    ],
)
def test_request_normalization_matches_legacy_field_decisions(request_data) -> None:
    _contracts, _resolver, _client, planner, worker = _activate()
    normalized = planner.normalize_record_request(
        request_data,
        default_pre_seconds=7,
        default_post_seconds=9,
    )

    assert normalized.request_id == worker._legacy_request_identity(request_data)
    assert normalized.event_type == worker._legacy_record_request_event_type(request_data)
    assert (normalized.keyframe_uuid, normalized.keyframe_source) == (
        worker._legacy_keyframe_from_request(request_data)
    )
    assert normalized.post_savant_media_request == (
        worker._legacy_is_post_savant_media_request(request_data)
    )


@pytest.mark.parametrize(
    ("truncated", "cross_session"),
    ((False, False), (True, False), (False, True), (True, True)),
)
def test_window_anchor_and_label_plan_match_legacy(
    truncated: bool,
    cross_session: bool,
) -> None:
    contracts, _resolver, _client, planner, worker = _activate()
    request = _request()
    proofs = _proofs(
        contracts,
        truncated=truncated,
        cross_session=cross_session,
    )

    legacy_window = worker._legacy_requested_pts_window(
        request,
        pre_seconds=5,
        post_seconds=5,
    )
    planned_window = planner.requested_pts_window(
        request,
        pre_seconds=5,
        post_seconds=5,
    )
    assert planned_window == legacy_window

    arguments = {
        "proofs": proofs,
        "anchor_keyframe_uuid": "start-keyframe",
        "anchor_keyframe_pts": proofs.start_window_frame.keyframe_pts,
        "anchor_keyframe_source": "anchor_keyframe_uuid",
        "pre_seconds": 5,
        "post_seconds": 5,
        "replay_duration_extra_slack_s": 0.75,
    }
    legacy = worker._legacy_apply_replay_anchor_to_request(request, **arguments)
    planned = planner.apply_replay_anchor_to_request(request, **arguments)
    assert planned == legacy

    legacy_labels = worker._legacy_replay_job_labels(
        request["event_id"],
        legacy,
        replay_offset_seconds=legacy.get("replay_offset_seconds"),
        replay_duration_seconds=legacy.get("replay_duration_seconds"),
    )
    planned_labels = planner.replay_job_labels(
        request["event_id"],
        planned,
        replay_offset_seconds=planned.get("replay_offset_seconds"),
        replay_duration_seconds=planned.get("replay_duration_seconds"),
    )
    assert planned_labels == legacy_labels


def _gate_cases(config):
    return (
        (config, {}, ""),
        (replace(config, evidence_materialization_pressure_level="hard"), {}, ""),
        (
            replace(config, evidence_materialization_pressure_level="critical"),
            {},
            "intrusion",
        ),
        (
            replace(config, evidence_materialization_pressure_level="critical"),
            {},
            "watchlist_hit",
        ),
        (replace(config, run_once=True, max_jobs_per_run=1), {}, ""),
        (
            replace(
                config,
                evidence_materialization_event_type_quotas={"intrusion": 1},
            ),
            {},
            "intrusion",
        ),
        (
            replace(config, evidence_materialization_max_concurrency=1),
            {"replay_active_global_count": 1},
            "",
        ),
        (
            replace(config, evidence_materialization_max_concurrency_per_shard=1),
            {"replay_active_shard_count": 1},
            "",
        ),
        (
            replace(config, evidence_materialization_max_concurrency_per_source=1),
            {"replay_active_source_count": 1},
            "",
        ),
        (replace(config, per_camera_cooldown_seconds=60), {}, "intrusion"),
    )


def test_gate_decisions_match_legacy_for_all_policy_branches() -> None:
    helpers = _queue_helpers()
    helpers._activate()
    import app.worker as worker

    base = helpers._clip_config(
        max_concurrent_jobs=0,
        per_camera_cooldown_seconds=0,
        evidence_materialization_max_concurrency=0,
        evidence_materialization_max_concurrency_per_shard=0,
        evidence_materialization_max_concurrency_per_source=0,
    )
    for config, count_overrides, event_type in _gate_cases(base):
        active_counts = {
            "replay_active_global_count": 0,
            "replay_active_shard_count": 0,
            "replay_active_source_count": 0,
            **count_overrides,
        }
        arguments = {
            "jobs_created": 1 if config.run_once else 0,
            "active_jobs": [],
            "active_counts": active_counts,
            "shard_id": "default",
            "source_id": "source-1",
            "camera_id": "camera-1",
            "cooldown_gate_ts_ms": 1_780_000_000_000,
            "last_job_by_camera": {"camera-1": 1_780_000_000_000},
            "event_type_counts": {event_type: 1} if event_type else {},
            "event_type": event_type,
        }
        assert worker._clip_gate_decision(config, **arguments) == (
            worker._legacy_clip_gate_decision(config, **arguments)
        )


def test_replay_payload_and_plan_hash_match_frozen_legacy_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contracts, _resolver, replay_client, planner, _worker = _activate()
    monkeypatch.setenv("REPLAY_FORCE_CONSTANT_CADENCE", "true")
    monkeypatch.setenv("REPLAY_TS_SYNC", "false")
    phase0 = json.loads(
        (PHASE0_FIXTURES / "replay_payload_golden.json").read_text(encoding="utf-8")
    )
    expected_hashes = json.loads(
        (PHASE2_FIXTURES / "replay_plan_hashes.json").read_text(encoding="utf-8")
    )["plan_hashes"]

    for scenario in phase0["scenarios"]:
        arguments = dict(scenario["arguments"])
        legacy = replay_client.build_job_payload(**arguments)
        plan = planner.build_replay_plan(
            **arguments,
            force_constant_cadence=True,
            ts_sync=False,
        )
        assert plan.payload() == legacy
        assert plan.plan_hash == expected_hashes[scenario["name"]]
        assert len(plan.plan_hash) == 64


def test_plan_hash_is_stable_across_mapping_order_and_sensitive_to_contract() -> None:
    _contracts, _resolver, _client, planner, _worker = _activate()
    common = {
        "source_id": "source-1",
        "keyframe_uuid": "keyframe-1",
        "pre_seconds": 5,
        "post_seconds": 5,
        "sink_endpoint": "dealer+connect:tcp://sink:6666",
        "stop_condition_mode": "ts_delta_sec",
        "fps": 30,
        "force_constant_cadence": True,
        "ts_sync": False,
    }
    first = planner.build_replay_plan(labels={"b": "2", "a": "1"}, **common)
    reordered = planner.build_replay_plan(labels={"a": "1", "b": "2"}, **common)
    changed = planner.build_replay_plan(labels={"a": "1", "b": "3"}, **common)

    assert first.plan_hash == reordered.plan_hash
    assert first.canonical_payload_json == reordered.canonical_payload_json
    assert changed.plan_hash != first.plan_hash

    with pytest.raises(planner.ReplayPlanParityError):
        planner.assert_replay_plan_parity(
            first,
            {**first.payload(), "anchor_keyframe": "different"},
        )


def test_bounded_proof_port_returns_typed_outcomes_and_limits_concurrency() -> None:
    contracts, proof_module, _client, _planner, _worker = _activate()
    lock = threading.Lock()
    active = 0
    max_active = 0

    def resolve(value: int):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        if value < 0:
            return None, None, "missing", "missing proof"
        return {"value": value}, f"keyframe-{value}", "fixture", None

    resolver = proof_module.BoundedProofResolver(resolve, max_concurrent=2)
    with ThreadPoolExecutor(max_workers=6) as executor:
        outcomes = list(executor.map(resolver.resolve, range(6)))

    assert max_active == 2
    assert all(isinstance(outcome, contracts.ReadyProof) for outcome in outcomes)
    assert outcomes[3].request()["value"] == 3
    not_ready = resolver.resolve(-1)
    assert isinstance(not_ready, contracts.NotReadyProof)
    assert not_ready.error == "missing proof"

    invalid_resolver = proof_module.BoundedProofResolver(
        lambda: (None, None, "missing", "missing_stream_session_id source_id=x")
    )
    invalid = invalid_resolver.resolve()
    assert isinstance(invalid, contracts.InvalidProof)


def test_planner_shadow_cannot_create_a_second_replay_job() -> None:
    worker_path = CLIP_ROOT / "app" / "worker.py"
    planner_path = CLIP_ROOT / "app" / "replay_planner.py"

    assert _call_count(worker_path, "create_job") == 1
    assert _call_count(planner_path, "create_job") == 0
