"""Canonical evidence materialization lifecycle contract.

This package is intentionally dependency-free so worker, API and operational
tooling can share one state/reason vocabulary without importing each other.
"""

from .contract import (  # noqa: F401
    ACTIVE_COMPATIBILITY_TASK_STATUSES,
    ACTIVE_MATERIALIZATION_STATUSES,
    CLAIMABLE_MATERIALIZATION_STATUSES,
    CLAIM_TERMINAL_MATERIALIZATION_STATUSES,
    MATERIALIZATION_PHASES,
    MATERIALIZATION_STATE_CONTRACT_VERSION,
    OPERATOR_EVIDENCE_STATES,
    READY_MATERIALIZATION_STATUSES,
    REPLAY_OWNED_TASK_STATUSES,
    ROLLING_OWNED_PHASES,
    TERMINAL_MATERIALIZATION_STATUSES,
    EvidenceErrorClass,
    MaterializationPhase,
    MaterializationStatus,
    NormalizedReason,
    classify_reason,
    compatibility_status_for,
    is_claimable_materialization_status,
    is_terminal_materialization_status,
    materialization_status_for_operator_state,
    normalize_reason_code,
    retry_delay_seconds,
)
