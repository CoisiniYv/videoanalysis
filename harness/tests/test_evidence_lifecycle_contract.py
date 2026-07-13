"""Cross-module contract tests for Spec 33/34 Phase 1."""

from __future__ import annotations

import ast
from pathlib import Path

from libs.evidence_lifecycle.contract import (
    ACTIVE_MATERIALIZATION_STATUSES,
    CLAIMABLE_MATERIALIZATION_STATUSES,
    MATERIALIZATION_STATE_CONTRACT_VERSION,
    READY_MATERIALIZATION_STATUSES,
    TERMINAL_MATERIALIZATION_STATUSES,
    EvidenceErrorClass,
    MaterializationPhase,
    classify_reason,
    compatibility_status_for,
    materialization_status_for_operator_state,
    retry_delay_seconds,
)


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_029 = ROOT / "db/migrations/029_evidence_materialization_lifecycle_contract.sql"
MIGRATION_030 = ROOT / "db/migrations/030_evidence_materialization_lifecycle_indexes.sql"


def test_canonical_state_sets_are_disjoint_and_deferred_is_terminal() -> None:
    assert MATERIALIZATION_STATE_CONTRACT_VERSION == "evidence-materialization-v2"
    assert READY_MATERIALIZATION_STATUSES == {
        "manifest_ready",
        "materialization_pending",
    }
    assert CLAIMABLE_MATERIALIZATION_STATUSES == READY_MATERIALIZATION_STATUSES
    assert ACTIVE_MATERIALIZATION_STATUSES == {
        "manifest_ready",
        "materialization_pending",
        "materializing",
    }
    assert "materialization_deferred" in TERMINAL_MATERIALIZATION_STATUSES
    assert "materialization_deferred" not in ACTIVE_MATERIALIZATION_STATUSES
    assert ACTIVE_MATERIALIZATION_STATUSES.isdisjoint(
        TERMINAL_MATERIALIZATION_STATUSES
    )


def test_compatibility_projection_does_not_create_second_state_authority() -> None:
    assert materialization_status_for_operator_state("pending") == (
        "materialization_pending"
    )
    assert materialization_status_for_operator_state("replay_job_created") == (
        "materializing"
    )
    assert materialization_status_for_operator_state("ready") == "materialized"
    assert materialization_status_for_operator_state("not_implemented") == (
        "materialization_skipped"
    )
    assert compatibility_status_for("manifest_ready") == "pending"
    assert compatibility_status_for(
        "materializing",
        MaterializationPhase.FINALIZER_PENDING.value,
    ) == "finalizing"


def test_reason_taxonomy_separates_retryable_and_terminal_outcomes() -> None:
    coverage = classify_reason("post_gap_ns=3000000000 coverage miss")
    event_frame_tail = classify_reason("rolling_cache_event_frame_not_covered")
    capacity = classify_reason("materialization_concurrency_limit_exceeded")
    invalid = classify_reason("rolling_cache_missing_event_frame_pts")
    covered = classify_reason("covered_by_existing_evidence")
    unknown = classify_reason("legacy operator decision with no known prefix")

    assert coverage.code == "coverage_not_complete"
    assert event_frame_tail.code == "coverage_not_complete"
    assert event_frame_tail.retryable is True
    assert coverage.error_class is EvidenceErrorClass.NOT_READY
    assert coverage.retryable is True
    assert capacity.code == "capacity_unavailable"
    assert capacity.retryable is True
    assert invalid.code == "missing_event_frame_pts"
    assert invalid.retryable is False
    assert covered.code == "covered_by_existing_evidence"
    assert covered.claim_terminal is True
    assert unknown.code == "unknown"
    assert unknown.claim_terminal is True


def test_retry_delay_is_bounded_deterministic_and_respects_hint() -> None:
    observed = [
        retry_delay_seconds(attempt, jitter_key="event-1")
        for attempt in range(1, 8)
    ]
    assert observed == [
        retry_delay_seconds(attempt, jitter_key="event-1")
        for attempt in range(1, 8)
    ]
    assert 0.5 <= observed[0] <= 0.75
    assert 10.0 <= observed[-1] <= 10.25
    assert retry_delay_seconds(1, retry_hint_s=7.0, jitter_key="x") >= 7.0


def test_migration_029_adds_retry_lease_phase_owner_and_handoff_fields() -> None:
    source = MIGRATION_029.read_text(encoding="utf-8")
    for column in (
        "materialization_phase",
        "materialization_next_attempt_at",
        "materialization_retry_reason",
        "materialization_owner",
        "materialization_lease_owner",
        "materialization_lease_token",
        "materialization_lease_generation",
        "materialization_lease_expires_at",
        "materialization_lease_heartbeat_at",
        "materialization_handoff",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in source
    assert "video_analytics.materialization_segment_seconds" in source
    assert "video_analytics.materialization_ready_grace_seconds" in source
    assert "ambiguous_active_identity" in source
    assert "legacy_empty_deferred" in source
    assert "materialization_ready_at =\n                    now()" not in source


def _index_statement(source: str, name: str) -> str:
    marker = f"CREATE INDEX CONCURRENTLY {name}"
    if marker not in source:
        marker = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name}"
    start = source.index(marker)
    end = source.index(";", start)
    return source[start : end + 1]


def test_migration_030_ready_active_and_finalizer_indexes_match_contract() -> None:
    source = MIGRATION_030.read_text(encoding="utf-8")
    ready = _index_statement(source, "evidence_tasks_materialization_ready_idx")
    runtime = _index_statement(source, "evidence_tasks_runtime_epoch_active_idx")
    finalizer = _index_statement(source, "evidence_tasks_finalizer_pending_idx")

    for statement in (ready, runtime):
        assert "materialization_deferred" not in statement
    assert "'manifest_ready', 'materialization_pending'" in ready
    assert "COALESCE(materialization_next_attempt_at, materialization_ready_at)" in ready
    assert "'manifest_ready', 'materialization_pending', 'materializing'" in runtime
    assert "materialization_phase = 'finalizer_pending'" in finalizer
    assert "materialization_handoff <> '{}'::jsonb" in finalizer


def test_contract_module_has_no_infrastructure_dependency() -> None:
    source = (
        ROOT / "libs/evidence_lifecycle/contract.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (
            node.names
            if isinstance(node, ast.Import)
            else [ast.alias(name=node.module or "")]
        )
    }
    assert imported.isdisjoint({"psycopg", "redis", "httpx", "subprocess"})
