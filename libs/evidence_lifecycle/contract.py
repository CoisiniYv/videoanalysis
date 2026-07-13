"""Dependency-free authority for evidence materialization lifecycle values.

PostgreSQL remains the state authority.  This module only defines the legal
vocabulary and pure mappings used by producers, workers, APIs and operational
reports.  It must not import Redis, psycopg, HTTP clients or subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import math
from typing import Final


MATERIALIZATION_STATE_CONTRACT_VERSION: Final = "evidence-materialization-v2"


class MaterializationStatus(StrEnum):
    MANIFEST_READY = "manifest_ready"
    PENDING = "materialization_pending"
    RUNNING = "materializing"
    MATERIALIZED = "materialized"
    DEFERRED = "materialization_deferred"
    FAILED = "materialization_failed"
    EXPIRED = "materialization_expired"
    SKIPPED = "materialization_skipped"


class MaterializationPhase(StrEnum):
    WAITING_READY = "waiting_ready"
    WAITING_COVERAGE = "waiting_coverage"
    IMAGE_RUNNING = "image_running"
    REMUX_RUNNING = "remux_running"
    FINALIZER_PENDING = "finalizer_pending"
    FINALIZING = "finalizing"
    TERMINAL = "terminal"
    MANUAL_QUARANTINE = "manual_quarantine"


class EvidenceErrorClass(StrEnum):
    INVALID_INPUT = "invalid_input"
    DUPLICATE_TERMINAL = "duplicate_terminal"
    NOT_READY = "not_ready"
    CAPACITY = "capacity"
    TRANSIENT_DEPENDENCY = "transient_dependency"
    PERMANENT_EXTERNAL = "permanent_external"
    STALE_OWNER = "stale_owner"
    INTENTIONAL_TERMINAL = "intentional_terminal"
    UNKNOWN = "unknown"


class NormalizedReason(StrEnum):
    COVERAGE_NOT_COMPLETE = "coverage_not_complete"
    NO_OVERLAPPING_FINAL_SEGMENT = "no_overlapping_final_segment"
    SEGMENT_NOT_STABLE = "segment_not_stable"
    SEGMENT_DISAPPEARED = "segment_disappeared"
    DB_UNAVAILABLE = "db_unavailable"
    DB_POOL_TIMEOUT = "db_pool_timeout"
    TEMPORARY_IO_ERROR = "temporary_io_error"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    PROOF_NOT_READY = "proof_not_ready"
    COOLDOWN_ACTIVE = "cooldown_active"
    REPLAY_UNAVAILABLE = "replay_unavailable"
    LEGACY_EMPTY_DEFERRED = "legacy_empty_deferred"
    MISSING_EVENT_FRAME_PTS = "missing_event_frame_pts"
    INVALID_REQUESTED_WINDOW = "invalid_requested_window"
    MISSING_SOURCE_ID = "missing_source_id"
    MISSING_RUNTIME_EPOCH = "missing_runtime_epoch"
    STABLE_METADATA_INVALID = "stable_metadata_invalid"
    COVERED_BY_EXISTING_EVIDENCE = "covered_by_existing_evidence"
    POLICY_SKIPPED = "policy_skipped"
    STORAGE_HARD_STOP = "storage_hard_stop"
    BUSINESS_DEADLINE_EXPIRED = "business_deadline_expired"
    AMBIGUOUS_ACTIVE_IDENTITY = "ambiguous_active_identity"
    LEASE_LOST = "lease_lost"
    UNKNOWN = "unknown"


READY_MATERIALIZATION_STATUSES: Final = frozenset(
    {
        MaterializationStatus.MANIFEST_READY.value,
        MaterializationStatus.PENDING.value,
    }
)
CLAIMABLE_MATERIALIZATION_STATUSES: Final = READY_MATERIALIZATION_STATUSES
ACTIVE_MATERIALIZATION_STATUSES: Final = frozenset(
    {*READY_MATERIALIZATION_STATUSES, MaterializationStatus.RUNNING.value}
)
TERMINAL_MATERIALIZATION_STATUSES: Final = frozenset(
    {
        MaterializationStatus.MATERIALIZED.value,
        MaterializationStatus.DEFERRED.value,
        MaterializationStatus.FAILED.value,
        MaterializationStatus.EXPIRED.value,
        MaterializationStatus.SKIPPED.value,
    }
)
CLAIM_TERMINAL_MATERIALIZATION_STATUSES: Final = (
    TERMINAL_MATERIALIZATION_STATUSES
)

MATERIALIZATION_PHASES: Final = frozenset(phase.value for phase in MaterializationPhase)
ROLLING_OWNED_PHASES: Final = frozenset(
    {
        MaterializationPhase.WAITING_READY.value,
        MaterializationPhase.WAITING_COVERAGE.value,
        MaterializationPhase.IMAGE_RUNNING.value,
        MaterializationPhase.REMUX_RUNNING.value,
        MaterializationPhase.FINALIZER_PENDING.value,
        MaterializationPhase.FINALIZING.value,
    }
)

# These are the legacy Clip/Replay compatibility states.  They are not valid
# values for materialization_status, but remain relevant to drain/runtime
# barriers until Coordinator V2 removes the compatibility projection.
REPLAY_OWNED_TASK_STATUSES: Final = frozenset(
    {
        "pending",
        "waiting_proof",
        "queued",
        "replay_job_created",
        "replaying",
    }
)
ACTIVE_COMPATIBILITY_TASK_STATUSES: Final = frozenset(
    {*REPLAY_OWNED_TASK_STATUSES, "materializing", "finalizing"}
)

OPERATOR_EVIDENCE_STATES: Final = frozenset(
    {
        *ACTIVE_MATERIALIZATION_STATUSES,
        *TERMINAL_MATERIALIZATION_STATUSES,
        *ACTIVE_COMPATIBILITY_TASK_STATUSES,
        "ready",
        "failed",
        "not_implemented",
    }
)


_STATUS_TO_MATERIALIZATION: Final = {
    "pending": MaterializationStatus.PENDING.value,
    "waiting_proof": MaterializationStatus.PENDING.value,
    "queued": MaterializationStatus.PENDING.value,
    "replay_job_created": MaterializationStatus.RUNNING.value,
    "replaying": MaterializationStatus.RUNNING.value,
    "finalizing": MaterializationStatus.RUNNING.value,
    "generated": MaterializationStatus.MATERIALIZED.value,
    "ready": MaterializationStatus.MATERIALIZED.value,
    "generated_unverified": MaterializationStatus.MATERIALIZED.value,
    "generated_corrupt": MaterializationStatus.FAILED.value,
    "duration_guard_failed": MaterializationStatus.FAILED.value,
    "generated_annotation_failed": MaterializationStatus.FAILED.value,
    "skipped_by_poc_limit": MaterializationStatus.FAILED.value,
    "not_implemented": MaterializationStatus.SKIPPED.value,
    "failed": MaterializationStatus.FAILED.value,
}


_REASON_RULES: Final = (
    (
        NormalizedReason.COVERED_BY_EXISTING_EVIDENCE,
        EvidenceErrorClass.INTENTIONAL_TERMINAL,
        ("covered_by_existing_evidence", "covered_by_event:"),
    ),
    (
        NormalizedReason.STORAGE_HARD_STOP,
        EvidenceErrorClass.INTENTIONAL_TERMINAL,
        ("storage_hard_limit", "storage_hard_stop"),
    ),
    (
        NormalizedReason.CAPACITY_UNAVAILABLE,
        EvidenceErrorClass.CAPACITY,
        (
            "concurrency_limit",
            "backlog_limit",
            "capacity_unavailable",
            "max_active",
            "max_per_poll",
        ),
    ),
    (
        NormalizedReason.PROOF_NOT_READY,
        EvidenceErrorClass.NOT_READY,
        (
            "proof_not_ready",
            "waiting_proof",
            "missing_post_savant_frame_proof",
            "frame_proof",
        ),
    ),
    (
        NormalizedReason.COOLDOWN_ACTIVE,
        EvidenceErrorClass.CAPACITY,
        ("cooldown_active", "reason=cooldown", "cooldown"),
    ),
    (
        NormalizedReason.REPLAY_UNAVAILABLE,
        EvidenceErrorClass.TRANSIENT_DEPENDENCY,
        (
            "replay_unavailable",
            "replay unavailable",
            "replay dependency",
            "replay job creation returned none",
        ),
    ),
    (
        NormalizedReason.NO_OVERLAPPING_FINAL_SEGMENT,
        EvidenceErrorClass.NOT_READY,
        ("no_overlapping_final_segment", "no_overlapping_segments"),
    ),
    (
        NormalizedReason.SEGMENT_NOT_STABLE,
        EvidenceErrorClass.NOT_READY,
        ("segment_not_stable", "half_written_segment"),
    ),
    (
        NormalizedReason.SEGMENT_DISAPPEARED,
        EvidenceErrorClass.NOT_READY,
        ("segment_disappeared", "segment_deleted"),
    ),
    (
        NormalizedReason.COVERAGE_NOT_COMPLETE,
        EvidenceErrorClass.NOT_READY,
        (
            "coverage_not_complete",
            "coverage_miss",
            "pre_gap_ns=",
            "post_gap_ns=",
            "window_not_covered",
            "event_frame_not_covered",
        ),
    ),
    (
        NormalizedReason.DB_POOL_TIMEOUT,
        EvidenceErrorClass.TRANSIENT_DEPENDENCY,
        ("db_pool_timeout", "pooltimeout"),
    ),
    (
        NormalizedReason.DB_UNAVAILABLE,
        EvidenceErrorClass.TRANSIENT_DEPENDENCY,
        ("db_unavailable", "connection refused", "operationalerror"),
    ),
    (
        NormalizedReason.TEMPORARY_IO_ERROR,
        EvidenceErrorClass.TRANSIENT_DEPENDENCY,
        ("temporary_io_error", "resource temporarily unavailable"),
    ),
    (
        NormalizedReason.MISSING_EVENT_FRAME_PTS,
        EvidenceErrorClass.INVALID_INPUT,
        ("missing_event_frame_pts", "missing event frame pts"),
    ),
    (
        NormalizedReason.INVALID_REQUESTED_WINDOW,
        EvidenceErrorClass.INVALID_INPUT,
        ("invalid_requested_window", "invalid_window"),
    ),
    (
        NormalizedReason.MISSING_SOURCE_ID,
        EvidenceErrorClass.INVALID_INPUT,
        ("missing_source_id", "missing source_id", "missing source id"),
    ),
    (
        NormalizedReason.MISSING_RUNTIME_EPOCH,
        EvidenceErrorClass.INVALID_INPUT,
        ("missing_runtime_epoch", "missing runtime_epoch", "missing runtime epoch"),
    ),
    (
        NormalizedReason.STABLE_METADATA_INVALID,
        EvidenceErrorClass.INVALID_INPUT,
        ("stable_metadata_invalid", "metadata_unreadable"),
    ),
    (
        NormalizedReason.BUSINESS_DEADLINE_EXPIRED,
        EvidenceErrorClass.INVALID_INPUT,
        ("deadline_expired", "deadline_missed"),
    ),
    (
        NormalizedReason.AMBIGUOUS_ACTIVE_IDENTITY,
        EvidenceErrorClass.INVALID_INPUT,
        ("ambiguous_active_identity", "manual_quarantine"),
    ),
    (
        NormalizedReason.LEASE_LOST,
        EvidenceErrorClass.STALE_OWNER,
        ("lease_lost", "stale_owner", "fence_lost"),
    ),
)


@dataclass(frozen=True, slots=True)
class ReasonClassification:
    code: str
    error_class: EvidenceErrorClass
    retryable: bool
    claim_terminal: bool


def materialization_status_for_operator_state(state: str) -> str:
    """Map legacy/operator state to the canonical materialization family."""
    value = str(state or "").strip()
    if value in ACTIVE_MATERIALIZATION_STATUSES | TERMINAL_MATERIALIZATION_STATUSES:
        return value
    return _STATUS_TO_MATERIALIZATION.get(
        value,
        MaterializationStatus.MANIFEST_READY.value,
    )


def compatibility_status_for(status: str, phase: str | None = None) -> str:
    """Return the legacy ``status`` projection for one canonical transition."""
    value = str(status or "")
    if value == MaterializationStatus.MANIFEST_READY.value:
        return "pending"
    if value == MaterializationStatus.PENDING.value:
        return MaterializationStatus.PENDING.value
    if value == MaterializationStatus.RUNNING.value:
        if phase in {
            MaterializationPhase.FINALIZER_PENDING.value,
            MaterializationPhase.FINALIZING.value,
        }:
            return "finalizing"
        return MaterializationStatus.RUNNING.value
    return value


def is_claimable_materialization_status(status: object) -> bool:
    return str(status or "") in CLAIMABLE_MATERIALIZATION_STATUSES


def is_terminal_materialization_status(status: object) -> bool:
    return str(status or "") in TERMINAL_MATERIALIZATION_STATUSES


def classify_reason(reason: object) -> ReasonClassification:
    text = " ".join(str(reason or "").strip().lower().split())
    if not text:
        return ReasonClassification(
            code=NormalizedReason.LEGACY_EMPTY_DEFERRED.value,
            error_class=EvidenceErrorClass.NOT_READY,
            retryable=True,
            claim_terminal=False,
        )
    for code, error_class, fragments in _REASON_RULES:
        if any(fragment in text for fragment in fragments):
            retryable = error_class in {
                EvidenceErrorClass.NOT_READY,
                EvidenceErrorClass.CAPACITY,
                EvidenceErrorClass.TRANSIENT_DEPENDENCY,
            }
            return ReasonClassification(
                code=code.value,
                error_class=error_class,
                retryable=retryable,
                claim_terminal=not retryable,
            )
    return ReasonClassification(
        code=NormalizedReason.UNKNOWN.value,
        error_class=EvidenceErrorClass.UNKNOWN,
        retryable=False,
        claim_terminal=True,
    )


def normalize_reason_code(reason: object) -> str:
    return classify_reason(reason).code


def retry_delay_seconds(
    attempt: int,
    *,
    retry_hint_s: float = 0.0,
    jitter_key: str = "",
) -> float:
    """Return bounded exponential retry delay with deterministic 0-250ms jitter.

    Deterministic jitter keeps tests and audit replay stable while spreading
    retries for different event/attempt keys.
    """
    normalized_attempt = max(1, int(attempt or 1))
    backoff = min(10.0, 0.5 * math.pow(2.0, normalized_attempt - 1))
    base = max(float(retry_hint_s or 0.0), backoff)
    digest = hashlib.sha256(
        f"{jitter_key}:{normalized_attempt}".encode("utf-8")
    ).digest()
    jitter_ms = int.from_bytes(digest[:2], "big") % 251
    return base + jitter_ms / 1000.0
