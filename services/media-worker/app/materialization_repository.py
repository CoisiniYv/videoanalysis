"""Named, fenced lifecycle transitions for evidence materialization.

This adapter is the Phase 1 state owner for rolling materialization.  It keeps
SQL and event compatibility projection out of scheduler decision code, detects
Migration 029 for mixed-order deploys, and never moves the natural ready time
when scheduling a retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

from libs.evidence_lifecycle import (
    CLAIMABLE_MATERIALIZATION_STATUSES,
    MaterializationPhase,
    MaterializationStatus,
    classify_reason,
    compatibility_status_for,
    retry_delay_seconds,
)


logger = logging.getLogger(__name__)

_REQUIRED_V2_COLUMNS = frozenset(
    {
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
    }
)
_SCHEMA_CAPABILITY_CACHE: dict[int, bool] = {}


@dataclass(frozen=True, slots=True)
class MaterializationLease:
    event_id: str
    owner: str
    token: str
    generation: int
    phase: str
    schema_v2: bool = True


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    ready_deadline_expired: int = 0
    running_sla_missed: int = 0
    handoff_recovered: int = 0
    lease_retry_scheduled: int = 0
    lease_deadline_expired: int = 0

    @property
    def changed(self) -> int:
        return (
            self.ready_deadline_expired
            + self.running_sla_missed
            + self.handoff_recovered
            + self.lease_retry_scheduled
            + self.lease_deadline_expired
        )


def clear_schema_capability_cache() -> None:
    _SCHEMA_CAPABILITY_CACHE.clear()


def supports_lifecycle_v2(
    conn: psycopg.Connection,
    *,
    refresh: bool = False,
) -> bool:
    """Return whether Migration 029 fields exist on this connection's DB."""
    key = id(conn)
    if not refresh and key in _SCHEMA_CAPABILITY_CACHE:
        return _SCHEMA_CAPABILITY_CACHE[key]
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'evidence_tasks'
                  AND column_name = ANY(%(columns)s)
                """,
                {"columns": sorted(_REQUIRED_V2_COLUMNS)},
            )
            rows = cur.fetchall()
        observed = {
            str(row.get("column_name") if isinstance(row, dict) else row[0])
            for row in rows
            if row
        }
        supported = observed == _REQUIRED_V2_COLUMNS
    except Exception:
        supported = False
    _SCHEMA_CAPABILITY_CACHE[key] = supported
    return supported


def _project_event(
    conn: psycopg.Connection,
    *,
    event_id: str,
    materialization_status: str,
    phase: str,
    reason: str = "",
    sink_output_path: str = "",
) -> bool:
    evidence_state = materialization_status
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE events
            SET media_status = %(evidence_state)s,
                payload = COALESCE(payload, '{}'::jsonb)
                    || jsonb_build_object(
                        'media',
                        COALESCE(payload->'media', '{}'::jsonb)
                        || jsonb_strip_nulls(jsonb_build_object(
                            'evidence_state', %(evidence_state)s::text,
                            'evidence_reason', NULLIF(%(reason)s::text, ''),
                            'evidence_state_updated_at', now(),
                            'materialization_status', %(materialization_status)s::text,
                            'materialization_phase', NULLIF(%(phase)s::text, ''),
                            'materialization_reason', NULLIF(%(reason)s::text, ''),
                            'sink_output_path', NULLIF(%(sink_output_path)s::text, '')
                        ))
                    ),
                updated_at = now()
            WHERE id = %(event_id)s::uuid
            """,
            {
                "event_id": event_id,
                "evidence_state": evidence_state,
                "materialization_status": materialization_status,
                "phase": phase,
                "reason": reason,
                "sink_output_path": sink_output_path,
            },
        )
        return bool(cur.rowcount and cur.rowcount > 0)


def claim_rolling_task(
    conn: psycopg.Connection,
    *,
    event_id: str,
    worker_id: str,
    phase: str,
    lease_seconds: float,
) -> MaterializationLease | None:
    """Atomically claim one runnable rolling task with a fenced lease."""
    if phase not in {
        MaterializationPhase.IMAGE_RUNNING.value,
        MaterializationPhase.REMUX_RUNNING.value,
    }:
        raise ValueError(f"invalid rolling claim phase: {phase}")
    lease_seconds = max(1.0, float(lease_seconds or 0.0))
    if not supports_lifecycle_v2(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evidence_tasks
                SET status = 'materializing',
                    materialization_status = 'materializing',
                    materialization_attempt_count = materialization_attempt_count + 1,
                    materialization_defer_reason = NULL,
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                  AND materialization_status = ANY(%(statuses)s)
                  AND materialization_ready_at IS NOT NULL
                  AND materialization_ready_at <= now()
                RETURNING materialization_attempt_count
                """,
                {
                    "event_id": event_id,
                    "statuses": sorted(CLAIMABLE_MATERIALIZATION_STATUSES),
                },
            )
            row = cur.fetchone()
        if not row:
            return None
        generation = int(row[0] if not isinstance(row, dict) else row.get("materialization_attempt_count") or 0)
        _project_event(
            conn,
            event_id=event_id,
            materialization_status=MaterializationStatus.RUNNING.value,
            phase=phase,
        )
        return MaterializationLease(
            event_id=event_id,
            owner=worker_id,
            token="",
            generation=generation,
            phase=phase,
            schema_v2=False,
        )

    token = str(uuid.uuid4())
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE evidence_tasks
            SET status = %(compatibility_status)s,
                materialization_status = 'materializing',
                materialization_phase = %(phase)s,
                materialization_phase_updated_at = now(),
                materialization_owner = 'rolling',
                materialization_next_attempt_at = NULL,
                materialization_retry_reason = NULL,
                materialization_defer_reason = NULL,
                materialization_failure_reason = NULL,
                materialization_expired_reason = NULL,
                materialization_lease_owner = %(worker_id)s,
                materialization_lease_token = %(token)s,
                materialization_lease_generation = materialization_lease_generation + 1,
                materialization_lease_expires_at =
                    now() + %(lease_seconds)s::double precision * interval '1 second',
                materialization_lease_heartbeat_at = now(),
                materialization_handoff = '{}'::jsonb,
                materialization_attempt_count = materialization_attempt_count + 1,
                materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                    || jsonb_build_object(
                        'lifecycle_v2_claim',
                        jsonb_build_object(
                            'owner', %(worker_id)s::text,
                            'token', %(token)s::text,
                            'phase', %(phase)s::text,
                            'claimed_at', now()
                        )
                    ),
                updated_at = now()
            WHERE event_id = %(event_id)s::uuid
              AND materialization_status = ANY(%(statuses)s)
              AND materialization_owner = 'rolling'
              AND materialization_ready_at IS NOT NULL
              AND materialization_ready_at <= now()
              AND COALESCE(
                    materialization_next_attempt_at,
                    materialization_ready_at
                  ) <= now()
              AND materialization_lease_token IS NULL
            RETURNING materialization_lease_generation
            """,
            {
                "event_id": event_id,
                "worker_id": worker_id,
                "token": token,
                "phase": phase,
                "compatibility_status": compatibility_status_for(
                    MaterializationStatus.RUNNING.value,
                    phase,
                ),
                "lease_seconds": lease_seconds,
                "statuses": sorted(CLAIMABLE_MATERIALIZATION_STATUSES),
            },
        )
        row = cur.fetchone()
    if not row:
        return None
    generation = int(row.get("materialization_lease_generation") or 0)
    _project_event(
        conn,
        event_id=event_id,
        materialization_status=MaterializationStatus.RUNNING.value,
        phase=phase,
    )
    return MaterializationLease(
        event_id=event_id,
        owner=worker_id,
        token=token,
        generation=generation,
        phase=phase,
    )


def heartbeat_lease(
    conn: psycopg.Connection,
    lease: MaterializationLease,
    *,
    lease_seconds: float,
) -> bool:
    if not lease.schema_v2:
        return True
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE evidence_tasks
            SET materialization_lease_heartbeat_at = now(),
                materialization_lease_expires_at =
                    now() + %(lease_seconds)s::double precision * interval '1 second',
                updated_at = now()
            WHERE event_id = %(event_id)s::uuid
              AND materialization_status = 'materializing'
              AND materialization_lease_owner = %(owner)s
              AND materialization_lease_token = %(token)s
              AND materialization_lease_generation = %(generation)s
            """,
            {
                "event_id": lease.event_id,
                "owner": lease.owner,
                "token": lease.token,
                "generation": lease.generation,
                "lease_seconds": max(1.0, float(lease_seconds or 0.0)),
            },
        )
        return bool(cur.rowcount and cur.rowcount > 0)


def current_lease(
    conn: psycopg.Connection,
    *,
    event_id: str,
    fallback_owner: str,
    fallback_phase: str,
) -> MaterializationLease | None:
    """Load the current fence for completion/retry callbacks."""
    if not supports_lifecycle_v2(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT materialization_attempt_count
                FROM evidence_tasks
                WHERE event_id = %(event_id)s::uuid
                  AND materialization_status = 'materializing'
                """,
                {"event_id": event_id},
            )
            fetchone = getattr(cur, "fetchone", None)
            row = fetchone() if callable(fetchone) else (1,)
        if not row:
            return None
        generation = int(
            row.get("materialization_attempt_count")
            if isinstance(row, dict)
            else row[0]
        )
        return MaterializationLease(
            event_id=event_id,
            owner=fallback_owner,
            token="",
            generation=max(1, generation),
            phase=fallback_phase,
            schema_v2=False,
        )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT materialization_lease_owner, materialization_lease_token,
                   materialization_lease_generation, materialization_phase
            FROM evidence_tasks
            WHERE event_id = %(event_id)s::uuid
              AND materialization_status = 'materializing'
              AND materialization_lease_owner IS NOT NULL
              AND materialization_lease_token IS NOT NULL
            """,
            {"event_id": event_id},
        )
        row = cur.fetchone()
    if not row:
        return None
    return MaterializationLease(
        event_id=event_id,
        owner=str(row.get("materialization_lease_owner") or ""),
        token=str(row.get("materialization_lease_token") or ""),
        generation=int(row.get("materialization_lease_generation") or 0),
        phase=str(row.get("materialization_phase") or fallback_phase),
    )


def retry_rolling_task(
    conn: psycopg.Connection,
    lease: MaterializationLease,
    *,
    reason: str,
    retry_hint_s: float = 0.0,
) -> bool:
    classification = classify_reason(reason)
    if not classification.retryable:
        return fail_rolling_task(conn, lease, reason=reason)
    phase = (
        MaterializationPhase.WAITING_COVERAGE.value
        if classification.error_class.value == "not_ready"
        else MaterializationPhase.WAITING_READY.value
    )
    delay_s = retry_delay_seconds(
        max(1, lease.generation),
        retry_hint_s=retry_hint_s,
        jitter_key=lease.event_id,
    )
    if not lease.schema_v2:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evidence_tasks
                SET status = 'materialization_pending',
                    materialization_status = 'materialization_pending',
                    materialization_defer_reason = NULL,
                    error_message = %(reason_code)s,
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                  AND materialization_status = 'materializing'
                """,
                {
                    "event_id": lease.event_id,
                    "reason_code": classification.code,
                },
            )
            changed = bool(cur.rowcount and cur.rowcount > 0)
    else:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evidence_tasks
                SET status = 'materialization_pending',
                    materialization_status = 'materialization_pending',
                    materialization_phase = %(phase)s,
                    materialization_phase_updated_at = now(),
                    materialization_owner = 'rolling',
                    materialization_next_attempt_at =
                        now() + %(delay_s)s::double precision * interval '1 second',
                    materialization_retry_reason = %(reason_code)s,
                    materialization_defer_reason = NULL,
                    materialization_failure_reason = NULL,
                    materialization_expired_reason = NULL,
                    materialization_lease_owner = NULL,
                    materialization_lease_token = NULL,
                    materialization_lease_expires_at = NULL,
                    materialization_lease_heartbeat_at = NULL,
                    materialization_handoff = '{}'::jsonb,
                    error_message = %(reason_code)s,
                    materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                        || jsonb_build_object(
                            'lifecycle_v2_retry',
                            jsonb_build_object(
                                'reason_code', %(reason_code)s::text,
                                'detail', %(reason_detail)s::text,
                                'delay_s', %(delay_s)s::double precision,
                                'scheduled_at', now()
                            )
                        ),
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                  AND materialization_status = 'materializing'
                  AND materialization_lease_owner = %(owner)s
                  AND materialization_lease_token = %(token)s
                  AND materialization_lease_generation = %(generation)s
                """,
                {
                    "event_id": lease.event_id,
                    "owner": lease.owner,
                    "token": lease.token,
                    "generation": lease.generation,
                    "phase": phase,
                    "reason_code": classification.code,
                    "reason_detail": str(reason or ""),
                    "delay_s": delay_s,
                },
            )
            changed = bool(cur.rowcount and cur.rowcount > 0)
    if changed:
        _project_event(
            conn,
            event_id=lease.event_id,
            materialization_status=MaterializationStatus.PENDING.value,
            phase=phase,
            reason=classification.code,
        )
    return changed


def retry_finalizer_handoff(
    conn: psycopg.Connection,
    lease: MaterializationLease,
    *,
    reason: str,
    retry_hint_s: float = 0.0,
) -> bool:
    """Release a finalizer lease while retaining its immutable handoff."""
    classification = classify_reason(reason)
    if not classification.retryable:
        return fail_rolling_task(conn, lease, reason=reason)
    if not lease.schema_v2:
        return retry_rolling_task(
            conn,
            lease,
            reason=reason,
            retry_hint_s=retry_hint_s,
        )
    delay_s = retry_delay_seconds(
        max(1, lease.generation),
        retry_hint_s=retry_hint_s,
        jitter_key=lease.event_id,
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE evidence_tasks
            SET status = 'finalizing',
                materialization_status = 'materializing',
                materialization_phase = 'finalizer_pending',
                materialization_phase_updated_at = now(),
                materialization_owner = 'media_finalizer',
                materialization_next_attempt_at =
                    now() + %(delay_s)s::double precision * interval '1 second',
                materialization_retry_reason = %(reason_code)s,
                materialization_defer_reason = NULL,
                materialization_lease_owner = NULL,
                materialization_lease_token = NULL,
                materialization_lease_expires_at = NULL,
                materialization_lease_heartbeat_at = NULL,
                error_message = %(reason_code)s,
                materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                    || jsonb_build_object(
                        'lifecycle_v2_finalizer_retry',
                        jsonb_build_object(
                            'reason_code', %(reason_code)s::text,
                            'detail', %(reason_detail)s::text,
                            'delay_s', %(delay_s)s::double precision,
                            'scheduled_at', now()
                        )
                    ),
                updated_at = now()
            WHERE event_id = %(event_id)s::uuid
              AND materialization_status = 'materializing'
              AND materialization_phase IN ('finalizer_pending', 'finalizing')
              AND materialization_handoff <> '{}'::jsonb
              AND materialization_lease_owner = %(owner)s
              AND materialization_lease_token = %(token)s
              AND materialization_lease_generation = %(generation)s
            """,
            {
                "event_id": lease.event_id,
                "owner": lease.owner,
                "token": lease.token,
                "generation": lease.generation,
                "reason_code": classification.code,
                "reason_detail": str(reason or ""),
                "delay_s": delay_s,
            },
        )
        changed = bool(cur.rowcount and cur.rowcount > 0)
    if changed:
        _project_event(
            conn,
            event_id=lease.event_id,
            materialization_status=MaterializationStatus.RUNNING.value,
            phase=MaterializationPhase.FINALIZER_PENDING.value,
            reason=classification.code,
        )
    return changed


def schedule_unclaimed_retry(
    conn: psycopg.Connection,
    *,
    event_id: str,
    reason: str,
    retry_hint_s: float = 0.0,
    sink_output_path: str = "",
) -> bool:
    """Schedule retry for a task that has not acquired a fenced lease."""
    classification = classify_reason(reason)
    if not classification.retryable:
        return fail_unclaimed_task(
            conn,
            event_id=event_id,
            reason=reason,
            sink_output_path=sink_output_path,
        )
    v2 = supports_lifecycle_v2(conn)
    if not v2:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evidence_tasks
                SET status = 'materialization_pending',
                    materialization_status = 'materialization_pending',
                    materialization_defer_reason = NULL,
                    error_message = %(reason_code)s,
                    sink_output_path = COALESCE(
                        NULLIF(%(sink_output_path)s::text, ''),
                        sink_output_path
                    ),
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                  AND materialization_status = ANY(%(statuses)s)
                """,
                {
                    "event_id": event_id,
                    "reason_code": classification.code,
                    "sink_output_path": sink_output_path,
                    "statuses": sorted(CLAIMABLE_MATERIALIZATION_STATUSES),
                },
            )
            changed = bool(cur.rowcount and cur.rowcount > 0)
    else:
        # Use the persisted attempt count as a deterministic backoff input.
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT materialization_attempt_count
                FROM evidence_tasks
                WHERE event_id = %(event_id)s::uuid
                  AND materialization_status = ANY(%(statuses)s)
                  AND materialization_lease_token IS NULL
                """,
                {
                    "event_id": event_id,
                    "statuses": sorted(CLAIMABLE_MATERIALIZATION_STATUSES),
                },
            )
            row = cur.fetchone()
        if not row:
            return False
        attempt = int(row.get("materialization_attempt_count") or 1)
        delay_s = retry_delay_seconds(
            max(1, attempt),
            retry_hint_s=retry_hint_s,
            jitter_key=event_id,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evidence_tasks
                SET status = 'materialization_pending',
                    materialization_status = 'materialization_pending',
                    materialization_phase = 'waiting_ready',
                    materialization_phase_updated_at = now(),
                    materialization_owner = CASE
                        WHEN NULLIF(%(sink_output_path)s::text, '') IS NOT NULL
                            THEN 'media_finalizer'
                        ELSE materialization_owner
                    END,
                    materialization_next_attempt_at =
                        now() + %(delay_s)s::double precision * interval '1 second',
                    materialization_retry_reason = %(reason_code)s,
                    materialization_defer_reason = NULL,
                    materialization_failure_reason = NULL,
                    materialization_expired_reason = NULL,
                    sink_output_path = COALESCE(
                        NULLIF(%(sink_output_path)s::text, ''),
                        sink_output_path
                    ),
                    error_message = %(reason_code)s,
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                  AND materialization_status = ANY(%(statuses)s)
                  AND materialization_lease_token IS NULL
                """,
                {
                    "event_id": event_id,
                    "reason_code": classification.code,
                    "sink_output_path": sink_output_path,
                    "delay_s": delay_s,
                    "statuses": sorted(CLAIMABLE_MATERIALIZATION_STATUSES),
                },
            )
            changed = bool(cur.rowcount and cur.rowcount > 0)
    if changed:
        _project_event(
            conn,
            event_id=event_id,
            materialization_status=MaterializationStatus.PENDING.value,
            phase=MaterializationPhase.WAITING_READY.value,
            reason=classification.code,
            sink_output_path=sink_output_path,
        )
    return changed


def fail_rolling_task(
    conn: psycopg.Connection,
    lease: MaterializationLease,
    *,
    reason: str,
) -> bool:
    reason_code = classify_reason(reason).code
    params = {
        "event_id": lease.event_id,
        "owner": lease.owner,
        "token": lease.token,
        "generation": lease.generation,
        "reason_code": reason_code,
        "reason_detail": str(reason or ""),
    }
    if not lease.schema_v2:
        predicate = "materialization_status = 'materializing'"
        fields = """
            status = 'materialization_failed',
            materialization_status = 'materialization_failed',
            materialization_defer_reason = NULL,
            materialization_failure_reason = %(reason_code)s,
            error_message = %(reason_code)s,
            updated_at = now()
        """
    else:
        predicate = """
            materialization_status = 'materializing'
            AND materialization_lease_owner = %(owner)s
            AND materialization_lease_token = %(token)s
            AND materialization_lease_generation = %(generation)s
        """
        fields = """
            status = 'materialization_failed',
            materialization_status = 'materialization_failed',
            materialization_phase = 'terminal',
            materialization_phase_updated_at = now(),
            materialization_owner = 'terminal',
            materialization_next_attempt_at = NULL,
            materialization_retry_reason = NULL,
            materialization_defer_reason = NULL,
            materialization_failure_reason = %(reason_code)s,
            materialization_expired_reason = NULL,
            materialization_lease_owner = NULL,
            materialization_lease_token = NULL,
            materialization_lease_expires_at = NULL,
            materialization_lease_heartbeat_at = NULL,
            materialization_handoff = '{}'::jsonb,
            error_message = %(reason_code)s,
            materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                || jsonb_build_object(
                    'lifecycle_v2_failure',
                    jsonb_build_object(
                        'reason_code', %(reason_code)s::text,
                        'detail', %(reason_detail)s::text,
                        'failed_at', now()
                    )
                ),
            updated_at = now()
        """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE evidence_tasks
            SET {fields}
            WHERE event_id = %(event_id)s::uuid
              AND {predicate}
            """,
            params,
        )
        changed = bool(cur.rowcount and cur.rowcount > 0)
    if changed:
        _project_event(
            conn,
            event_id=lease.event_id,
            materialization_status=MaterializationStatus.FAILED.value,
            phase=MaterializationPhase.TERMINAL.value,
            reason=reason_code,
        )
    return changed


def defer_terminal_task(
    conn: psycopg.Connection,
    *,
    event_id: str,
    reason: str,
    sink_output_path: str = "",
    lease: MaterializationLease | None = None,
) -> bool:
    """Write an intentional, claim-terminal deferred outcome."""
    classification = classify_reason(reason)
    if classification.retryable:
        raise ValueError(
            f"retryable reason cannot become terminal deferred: {classification.code}"
        )
    reason_code = classification.code if classification.code != "unknown" else str(reason)
    v2 = supports_lifecycle_v2(conn)
    lifecycle_fields = ""
    lease_predicate = ""
    lease_params: dict[str, object] = {}
    if v2:
        lifecycle_fields = """
            materialization_phase = 'terminal',
            materialization_phase_updated_at = now(),
            materialization_owner = 'terminal',
            materialization_next_attempt_at = NULL,
            materialization_retry_reason = NULL,
            materialization_lease_owner = NULL,
            materialization_lease_token = NULL,
            materialization_lease_expires_at = NULL,
            materialization_lease_heartbeat_at = NULL,
            materialization_handoff = '{}'::jsonb,
        """
        if lease is not None:
            lease_predicate = """
              AND materialization_lease_owner = %(lease_owner)s
              AND materialization_lease_token = %(lease_token)s
              AND materialization_lease_generation = %(lease_generation)s
            """
            lease_params = {
                "lease_owner": lease.owner,
                "lease_token": lease.token,
                "lease_generation": lease.generation,
            }
        else:
            lease_predicate = "AND materialization_lease_token IS NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE evidence_tasks
            SET status = 'materialization_deferred',
                materialization_status = 'materialization_deferred',
                {lifecycle_fields}
                materialization_defer_reason = %(reason_code)s,
                materialization_failure_reason = NULL,
                materialization_expired_reason = NULL,
                sink_output_path = COALESCE(
                    NULLIF(%(sink_output_path)s::text, ''),
                    sink_output_path
                ),
                error_message = %(reason_code)s,
                updated_at = now()
            WHERE event_id = %(event_id)s::uuid
              AND materialization_status NOT IN (
                  'materialized', 'materialization_deferred',
                  'materialization_failed', 'materialization_expired',
                  'materialization_skipped'
              )
              {lease_predicate}
            """,
            {
                "event_id": event_id,
                "reason_code": reason_code,
                "sink_output_path": sink_output_path,
                **lease_params,
            },
        )
        changed = bool(cur.rowcount and cur.rowcount > 0)
    if changed:
        _project_event(
            conn,
            event_id=event_id,
            materialization_status=MaterializationStatus.DEFERRED.value,
            phase=MaterializationPhase.TERMINAL.value,
            reason=reason_code,
            sink_output_path=sink_output_path,
        )
    return changed


def fail_unclaimed_task(
    conn: psycopg.Connection,
    *,
    event_id: str,
    reason: str,
    sink_output_path: str = "",
) -> bool:
    """Fail a stable invalid input only when no fenced worker owns it."""
    reason_code = classify_reason(reason).code
    if reason_code == "unknown":
        reason_code = str(reason or "materialization_failed")
    v2 = supports_lifecycle_v2(conn)
    lifecycle_fields = ""
    lease_predicate = ""
    if v2:
        lifecycle_fields = """
            materialization_phase = 'terminal',
            materialization_phase_updated_at = now(),
            materialization_owner = 'terminal',
            materialization_next_attempt_at = NULL,
            materialization_retry_reason = NULL,
            materialization_lease_owner = NULL,
            materialization_lease_token = NULL,
            materialization_lease_expires_at = NULL,
            materialization_lease_heartbeat_at = NULL,
            materialization_handoff = '{}'::jsonb,
        """
        lease_predicate = "AND materialization_lease_token IS NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE evidence_tasks
            SET status = 'materialization_failed',
                materialization_status = 'materialization_failed',
                {lifecycle_fields}
                materialization_defer_reason = NULL,
                materialization_failure_reason = %(reason_code)s,
                materialization_expired_reason = NULL,
                sink_output_path = COALESCE(
                    NULLIF(%(sink_output_path)s::text, ''),
                    sink_output_path
                ),
                error_message = %(reason_code)s,
                updated_at = now()
            WHERE event_id = %(event_id)s::uuid
              AND materialization_status NOT IN (
                  'materialized', 'materialization_deferred',
                  'materialization_failed', 'materialization_expired',
                  'materialization_skipped'
              )
              {lease_predicate}
            """,
            {
                "event_id": event_id,
                "reason_code": reason_code,
                "sink_output_path": sink_output_path,
            },
        )
        changed = bool(cur.rowcount and cur.rowcount > 0)
    if changed:
        _project_event(
            conn,
            event_id=event_id,
            materialization_status=MaterializationStatus.FAILED.value,
            phase=MaterializationPhase.TERMINAL.value,
            reason=reason_code,
            sink_output_path=sink_output_path,
        )
    return changed


def persist_finalizer_handoff(
    conn: psycopg.Connection,
    lease: MaterializationLease,
    *,
    sink_output_path: str,
    handoff: dict[str, Any],
    lease_seconds: float,
) -> bool:
    """Persist immutable remux identity before handing work to finalization."""
    if not lease.schema_v2:
        return True
    required = {
        "attempt_token",
        "source_id",
        "runtime_epoch_id",
        "requested_window",
        "selected_segment_ids",
        "staging_path",
        "canonical_path",
        "size",
        "mtime_ns",
    }
    missing = sorted(required - set(handoff))
    if missing:
        raise ValueError(f"materialization handoff missing fields: {','.join(missing)}")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE evidence_tasks
            SET status = 'finalizing',
                materialization_status = 'materializing',
                materialization_phase = 'finalizer_pending',
                materialization_phase_updated_at = now(),
                materialization_owner = 'media_finalizer',
                sink_output_path = %(sink_output_path)s,
                materialization_handoff = %(handoff)s::jsonb,
                materialization_lease_heartbeat_at = now(),
                materialization_lease_expires_at =
                    now() + %(lease_seconds)s::double precision * interval '1 second',
                materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                    || jsonb_build_object(
                        'lifecycle_v2_handoff',
                        jsonb_build_object(
                            'attempt_token', %(attempt_token)s::text,
                            'persisted_at', now()
                        )
                    ),
                updated_at = now()
            WHERE event_id = %(event_id)s::uuid
              AND materialization_status = 'materializing'
              AND materialization_phase = %(expected_phase)s
              AND materialization_lease_owner = %(owner)s
              AND materialization_lease_token = %(token)s
              AND materialization_lease_generation = %(generation)s
            """,
            {
                "event_id": lease.event_id,
                "expected_phase": lease.phase,
                "owner": lease.owner,
                "token": lease.token,
                "generation": lease.generation,
                "sink_output_path": sink_output_path,
                "handoff": json.dumps(handoff, sort_keys=True),
                "attempt_token": str(handoff["attempt_token"]),
                "lease_seconds": max(1.0, float(lease_seconds or 0.0)),
            },
        )
        changed = bool(cur.rowcount and cur.rowcount > 0)
    if changed:
        _project_event(
            conn,
            event_id=lease.event_id,
            materialization_status=MaterializationStatus.RUNNING.value,
            phase=MaterializationPhase.FINALIZER_PENDING.value,
            sink_output_path=sink_output_path,
        )
    return changed


def claim_finalizer_task(
    conn: psycopg.Connection,
    *,
    event_id: str,
    sink_output_path: str,
    worker_id: str,
    lease_seconds: float,
) -> dict[str, object]:
    """Claim/transfer one task into finalizing with a durable fence."""
    terminal = sorted(
        {
            MaterializationStatus.MATERIALIZED.value,
            MaterializationStatus.DEFERRED.value,
            MaterializationStatus.FAILED.value,
            MaterializationStatus.EXPIRED.value,
            MaterializationStatus.SKIPPED.value,
        }
    )
    lease_seconds = max(1.0, float(lease_seconds or 0.0))
    if not supports_lifecycle_v2(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidate AS (
                    SELECT event_id
                    FROM evidence_tasks
                    WHERE event_id = %(event_id)s::uuid
                      AND COALESCE(materialization_status, status, '')
                            <> ALL(%(terminal_states)s)
                    FOR UPDATE SKIP LOCKED
                ), claimed AS (
                    UPDATE evidence_tasks et
                    SET status = 'finalizing',
                        materialization_status = 'materializing',
                        sink_output_path = %(sink_output_path)s,
                        updated_at = now()
                    FROM candidate c
                    WHERE et.event_id = c.event_id
                    RETURNING et.event_id
                )
                SELECT CASE
                    WHEN EXISTS (SELECT 1 FROM claimed) THEN 'claimed'
                    WHEN EXISTS (
                        SELECT 1 FROM evidence_tasks
                        WHERE event_id = %(event_id)s::uuid
                          AND COALESCE(materialization_status, status, '')
                                = ANY(%(terminal_states)s)
                    ) THEN 'terminal'
                    WHEN EXISTS (
                        SELECT 1 FROM evidence_tasks
                        WHERE event_id = %(event_id)s::uuid
                    ) THEN 'busy'
                    ELSE 'missing'
                END
                """,
                {
                    "event_id": event_id,
                    "sink_output_path": sink_output_path,
                    "terminal_states": terminal,
                },
            )
            row = cur.fetchone()
        status = str(row[0]) if row else "missing"
        lease = (
            MaterializationLease(
                event_id=event_id,
                owner=worker_id,
                token="",
                generation=1,
                phase=MaterializationPhase.FINALIZING.value,
                schema_v2=False,
            )
            if status == "claimed"
            else None
        )
        return {"status": status, "claimed": status == "claimed", "lease": lease}

    new_token = str(uuid.uuid4())
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH candidate AS (
                SELECT event_id, materialization_lease_token,
                       materialization_lease_generation,
                       materialization_handoff
                FROM evidence_tasks
                WHERE event_id = %(event_id)s::uuid
                  AND materialization_status NOT IN (
                      'materialized', 'materialization_deferred',
                      'materialization_failed', 'materialization_expired',
                      'materialization_skipped'
                  )
                  AND (
                      materialization_lease_token IS NULL
                      OR (
                          materialization_phase = 'finalizer_pending'
                          AND materialization_handoff <> '{}'::jsonb
                          AND materialization_handoff->>'attempt_token'
                                = materialization_lease_token
                      )
                  )
                  AND COALESCE(
                        materialization_next_attempt_at,
                        materialization_ready_at,
                        now()
                      ) <= now()
                FOR UPDATE SKIP LOCKED
            ), claimed AS (
                UPDATE evidence_tasks et
                SET status = 'finalizing',
                    materialization_status = 'materializing',
                    materialization_phase = 'finalizing',
                    materialization_phase_updated_at = now(),
                    materialization_owner = 'media_finalizer',
                    sink_output_path = %(sink_output_path)s,
                    materialization_next_attempt_at = NULL,
                    materialization_retry_reason = NULL,
                    materialization_defer_reason = NULL,
                    materialization_lease_owner = %(worker_id)s,
                    materialization_lease_token = COALESCE(
                        c.materialization_lease_token,
                        %(new_token)s
                    ),
                    materialization_lease_generation = CASE
                        WHEN c.materialization_lease_token IS NULL
                            THEN et.materialization_lease_generation + 1
                        ELSE et.materialization_lease_generation
                    END,
                    materialization_lease_expires_at =
                        now() + %(lease_seconds)s::double precision * interval '1 second',
                    materialization_lease_heartbeat_at = now(),
                    materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                        || jsonb_build_object(
                            'lifecycle_v2_finalizer_claim',
                            jsonb_build_object(
                                'worker_id', %(worker_id)s::text,
                                'claimed_at', now()
                            )
                        ),
                    updated_at = now()
                FROM candidate c
                WHERE et.event_id = c.event_id
                RETURNING et.materialization_lease_token,
                          et.materialization_lease_generation
            )
            SELECT
                CASE
                    WHEN EXISTS (SELECT 1 FROM claimed) THEN 'claimed'
                    WHEN EXISTS (
                        SELECT 1 FROM evidence_tasks
                        WHERE event_id = %(event_id)s::uuid
                          AND materialization_status = ANY(%(terminal_states)s)
                    ) THEN 'terminal'
                    WHEN EXISTS (
                        SELECT 1 FROM evidence_tasks
                        WHERE event_id = %(event_id)s::uuid
                    ) THEN 'busy'
                    ELSE 'missing'
                END AS claim_status,
                (SELECT materialization_lease_token FROM claimed LIMIT 1)
                    AS lease_token,
                (SELECT materialization_lease_generation FROM claimed LIMIT 1)
                    AS lease_generation
            """,
            {
                "event_id": event_id,
                "sink_output_path": sink_output_path,
                "worker_id": worker_id,
                "new_token": new_token,
                "lease_seconds": lease_seconds,
                "terminal_states": terminal,
            },
        )
        row = cur.fetchone()
    status = str(row.get("claim_status") or "missing") if row else "missing"
    lease = None
    if row and status == "claimed":
        lease = MaterializationLease(
            event_id=event_id,
            owner=worker_id,
            token=str(row.get("lease_token") or ""),
            generation=int(row.get("lease_generation") or 0),
            phase=MaterializationPhase.FINALIZING.value,
        )
        _project_event(
            conn,
            event_id=event_id,
            materialization_status=MaterializationStatus.RUNNING.value,
            phase=MaterializationPhase.FINALIZING.value,
            sink_output_path=sink_output_path,
        )
    return {"status": status, "claimed": status == "claimed", "lease": lease}


def complete_finalizer_task(
    conn: psycopg.Connection,
    lease: MaterializationLease,
    *,
    materialization_status: str,
    reason: str = "",
    clip_path: str = "",
    metadata_path: str = "",
    output_root: str = "",
) -> bool:
    """Atomically commit a fenced finalizer outcome and its event projection.

    The task is the lifecycle authority.  The writable CTE ensures a stale
    lease cannot update the compatibility event projection before losing its
    task CAS.  Rich bundle/index fields remain follow-on, idempotent writes
    performed only after this transition succeeds.
    """
    allowed = {
        MaterializationStatus.MATERIALIZED.value,
        MaterializationStatus.FAILED.value,
    }
    if materialization_status not in allowed:
        raise ValueError(
            f"invalid finalizer terminal status: {materialization_status}"
        )
    classification = classify_reason(reason)
    reason_code = ""
    if materialization_status == MaterializationStatus.FAILED.value:
        reason_code = (
            classification.code
            if classification.code != "unknown"
            else str(reason or MaterializationStatus.FAILED.value)
        )
    lifecycle_fields = ""
    lease_predicate = "AND materialization_status = 'materializing'"
    if lease.schema_v2:
        lifecycle_fields = """
                    materialization_phase = 'terminal',
                    materialization_phase_updated_at = now(),
                    materialization_owner = 'terminal',
                    materialization_next_attempt_at = NULL,
                    materialization_retry_reason = NULL,
                    materialization_lease_owner = NULL,
                    materialization_lease_token = NULL,
                    materialization_lease_expires_at = NULL,
                    materialization_lease_heartbeat_at = NULL,
        """
        lease_predicate = """
              AND materialization_status = 'materializing'
              AND materialization_lease_owner = %(owner)s
              AND materialization_lease_token = %(token)s
              AND materialization_lease_generation = %(generation)s
        """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH transitioned AS (
                UPDATE evidence_tasks
                SET status = %(materialization_status)s,
                    materialization_status = %(materialization_status)s,
                    {lifecycle_fields}
                    clip_path = COALESCE(
                        NULLIF(%(clip_path)s::text, ''),
                        clip_path
                    ),
                    metadata_path = COALESCE(
                        NULLIF(%(metadata_path)s::text, ''),
                        metadata_path
                    ),
                    output_root = COALESCE(
                        NULLIF(%(output_root)s::text, ''),
                        output_root
                    ),
                    last_materialization_at = CASE
                        WHEN %(materialization_status)s::text = 'materialized'
                            THEN now()
                        ELSE last_materialization_at
                    END,
                    materialization_defer_reason = NULL,
                    materialization_failure_reason = CASE
                        WHEN %(materialization_status)s::text =
                                'materialization_failed'
                            THEN NULLIF(%(reason_code)s::text, '')
                        ELSE NULL
                    END,
                    materialization_expired_reason = NULL,
                    error_message = CASE
                        WHEN NULLIF(%(reason_code)s::text, '') IS NOT NULL
                            THEN %(reason_code)s::text
                        WHEN %(materialization_status)s::text = 'materialized'
                            THEN NULL
                        ELSE error_message
                    END,
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                  {lease_predicate}
                RETURNING event_id
            )
            UPDATE events e
            SET media_status = %(materialization_status)s,
                payload = COALESCE(e.payload, '{{}}'::jsonb)
                    || jsonb_build_object(
                        'media',
                        COALESCE(e.payload->'media', '{{}}'::jsonb)
                        || jsonb_strip_nulls(jsonb_build_object(
                            'evidence_state', %(materialization_status)s::text,
                            'evidence_reason',
                                NULLIF(%(reason_code)s::text, ''),
                            'evidence_state_updated_at', now(),
                            'materialization_status',
                                %(materialization_status)s::text,
                            'materialization_phase', 'terminal',
                            'materialization_reason',
                                NULLIF(%(reason_code)s::text, '')
                        ))
                    ),
                updated_at = now()
            FROM transitioned t
            WHERE e.id = t.event_id
            """,
            {
                "event_id": lease.event_id,
                "owner": lease.owner,
                "token": lease.token,
                "generation": lease.generation,
                "materialization_status": materialization_status,
                "reason_code": reason_code,
                "clip_path": clip_path,
                "metadata_path": metadata_path,
                "output_root": output_root,
            },
        )
        return bool(cur.rowcount and cur.rowcount > 0)


def _transition_event_ids(
    conn: psycopg.Connection,
    sql: str,
    params: dict[str, Any],
) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    event_ids: list[str] = []
    for row in rows:
        value = row.get("event_id") if isinstance(row, dict) else row[0]
        if value is not None:
            event_ids.append(str(value))
    return event_ids


def _project_recovery_events(
    conn: psycopg.Connection,
    event_ids: list[str],
    *,
    materialization_status: str,
    phase: str,
    reason: str,
) -> None:
    for event_id in dict.fromkeys(event_ids):
        _project_event(
            conn,
            event_id=event_id,
            materialization_status=materialization_status,
            phase=phase,
            reason=reason,
        )


def recover_and_expire_rolling_tasks(
    conn: psycopg.Connection,
    *,
    source_ids: tuple[str, ...] = (),
) -> RecoveryResult:
    """Run the sole rolling deadline/lease recovery pass for one tick."""
    if not supports_lifecycle_v2(conn):
        expired_event_ids = _transition_event_ids(
            conn,
            """
            UPDATE evidence_tasks
            SET status = 'materialization_expired',
                materialization_status = 'materialization_expired',
                materialization_defer_reason = NULL,
                materialization_failure_reason = NULL,
                materialization_expired_reason = 'business_deadline_expired',
                error_message = 'business_deadline_expired',
                updated_at = now()
            WHERE materialization_status IN (
                'manifest_ready', 'materialization_pending'
            )
              AND materialization_deadline_at IS NOT NULL
              AND materialization_deadline_at <= now()
              AND (
                  %(sources_empty)s
                  OR COALESCE(source_id, replay_source_id, '') = ANY(%(sources)s)
              )
              AND COALESCE(
                    materialization_audit->'rolling_cache'->>'status',
                    ''
                  ) <> ''
            RETURNING event_id
            """,
            {
                "sources_empty": not bool(source_ids),
                "sources": list(source_ids),
            },
        )
        _project_recovery_events(
            conn,
            expired_event_ids,
            materialization_status=MaterializationStatus.EXPIRED.value,
            phase=MaterializationPhase.TERMINAL.value,
            reason="business_deadline_expired",
        )
        return RecoveryResult(ready_deadline_expired=len(expired_event_ids))

    params = {
        "sources_empty": not bool(source_ids),
        "sources": list(source_ids),
    }
    source_predicate = """
        AND (
            %(sources_empty)s
            OR COALESCE(source_id, replay_source_id, '') = ANY(%(sources)s)
        )
    """
    ready_expired_event_ids = _transition_event_ids(
        conn,
        f"""
        UPDATE evidence_tasks
        SET status = 'materialization_expired',
            materialization_status = 'materialization_expired',
            materialization_phase = 'terminal',
            materialization_phase_updated_at = now(),
            materialization_owner = 'terminal',
            materialization_next_attempt_at = NULL,
            materialization_retry_reason = NULL,
            materialization_defer_reason = NULL,
            materialization_failure_reason = NULL,
            materialization_expired_reason = 'business_deadline_expired',
            error_message = 'business_deadline_expired',
            updated_at = now()
        WHERE materialization_owner = 'rolling'
          AND materialization_status IN (
              'manifest_ready', 'materialization_pending'
          )
          AND materialization_deadline_at IS NOT NULL
          AND materialization_deadline_at <= now()
          AND materialization_lease_token IS NULL
          {source_predicate}
        RETURNING event_id
        """,
        params,
    )
    sla_missed_event_ids = _transition_event_ids(
        conn,
        f"""
        UPDATE evidence_tasks
        SET materialization_audit = COALESCE(materialization_audit, '{{}}'::jsonb)
                || jsonb_build_object(
                    'business_deadline',
                    jsonb_build_object(
                        'status', 'missed_during_valid_lease',
                        'observed_at', now()
                    )
                ),
            updated_at = now()
        WHERE materialization_status = 'materializing'
          AND materialization_deadline_at IS NOT NULL
          AND materialization_deadline_at <= now()
          AND materialization_lease_token IS NOT NULL
          AND materialization_lease_expires_at > now()
          AND NOT (
              COALESCE(materialization_audit->'business_deadline'->>'status', '')
                  = 'missed_during_valid_lease'
          )
          {source_predicate}
        RETURNING event_id
        """,
        params,
    )
    handoff_recovered_event_ids = _transition_event_ids(
        conn,
        f"""
        UPDATE evidence_tasks
        SET status = 'finalizing',
            materialization_phase = 'finalizer_pending',
            materialization_phase_updated_at = now(),
            materialization_owner = 'media_finalizer',
            materialization_lease_owner = NULL,
            materialization_lease_token = NULL,
            materialization_lease_expires_at = NULL,
            materialization_lease_heartbeat_at = NULL,
            materialization_audit = COALESCE(materialization_audit, '{{}}'::jsonb)
                || jsonb_build_object(
                    'lifecycle_v2_recovery',
                    jsonb_build_object(
                        'action', 'recover_finalizer_handoff',
                        'observed_at', now()
                    )
                ),
            updated_at = now()
        WHERE materialization_status = 'materializing'
          AND materialization_lease_token IS NOT NULL
          AND materialization_lease_expires_at <= now()
          AND materialization_handoff <> '{{}}'::jsonb
          {source_predicate}
        RETURNING event_id
        """,
        params,
    )
    lease_expired_event_ids = _transition_event_ids(
        conn,
        f"""
        UPDATE evidence_tasks
        SET status = 'materialization_expired',
            materialization_status = 'materialization_expired',
            materialization_phase = 'terminal',
            materialization_phase_updated_at = now(),
            materialization_owner = 'terminal',
            materialization_next_attempt_at = NULL,
            materialization_retry_reason = NULL,
            materialization_defer_reason = NULL,
            materialization_failure_reason = NULL,
            materialization_expired_reason = 'business_deadline_expired',
            materialization_lease_owner = NULL,
            materialization_lease_token = NULL,
            materialization_lease_expires_at = NULL,
            materialization_lease_heartbeat_at = NULL,
            error_message = 'business_deadline_expired',
            updated_at = now()
        WHERE materialization_status = 'materializing'
          AND materialization_lease_token IS NOT NULL
          AND materialization_lease_expires_at <= now()
          AND materialization_deadline_at IS NOT NULL
          AND materialization_deadline_at <= now()
          AND materialization_handoff = '{{}}'::jsonb
          {source_predicate}
        RETURNING event_id
        """,
        params,
    )
    lease_retry_event_ids = _transition_event_ids(
        conn,
        f"""
        UPDATE evidence_tasks
        SET status = 'materialization_pending',
            materialization_status = 'materialization_pending',
            materialization_phase = 'waiting_ready',
            materialization_phase_updated_at = now(),
            materialization_owner = 'rolling',
            materialization_next_attempt_at = now(),
            materialization_retry_reason = 'lease_lost',
            materialization_defer_reason = NULL,
            materialization_failure_reason = NULL,
            materialization_expired_reason = NULL,
            materialization_lease_owner = NULL,
            materialization_lease_token = NULL,
            materialization_lease_expires_at = NULL,
            materialization_lease_heartbeat_at = NULL,
            error_message = 'lease_lost',
            updated_at = now()
        WHERE materialization_status = 'materializing'
          AND materialization_lease_token IS NOT NULL
          AND materialization_lease_expires_at <= now()
          AND (
              materialization_deadline_at IS NULL
              OR materialization_deadline_at > now()
          )
          AND materialization_handoff = '{{}}'::jsonb
          {source_predicate}
        RETURNING event_id
        """,
        params,
    )
    _project_recovery_events(
        conn,
        ready_expired_event_ids,
        materialization_status=MaterializationStatus.EXPIRED.value,
        phase=MaterializationPhase.TERMINAL.value,
        reason="business_deadline_expired",
    )
    _project_recovery_events(
        conn,
        handoff_recovered_event_ids,
        materialization_status=MaterializationStatus.RUNNING.value,
        phase=MaterializationPhase.FINALIZER_PENDING.value,
        reason="lease_lost",
    )
    _project_recovery_events(
        conn,
        lease_expired_event_ids,
        materialization_status=MaterializationStatus.EXPIRED.value,
        phase=MaterializationPhase.TERMINAL.value,
        reason="business_deadline_expired",
    )
    _project_recovery_events(
        conn,
        lease_retry_event_ids,
        materialization_status=MaterializationStatus.PENDING.value,
        phase=MaterializationPhase.WAITING_READY.value,
        reason="lease_lost",
    )
    return RecoveryResult(
        ready_deadline_expired=len(ready_expired_event_ids),
        running_sla_missed=len(sla_missed_event_ids),
        handoff_recovered=len(handoff_recovered_event_ids),
        lease_retry_scheduled=len(lease_retry_event_ids),
        lease_deadline_expired=len(lease_expired_event_ids),
    )
