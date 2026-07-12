"""Clip-worker repository — update media status in events table."""

from __future__ import annotations

import json
import logging
import uuid

import psycopg

from libs.evidence_lifecycle import (
    ACTIVE_MATERIALIZATION_STATUSES,
    MaterializationPhase,
    MaterializationStatus,
    OPERATOR_EVIDENCE_STATES,
    REPLAY_OWNED_TASK_STATUSES,
    TERMINAL_MATERIALIZATION_STATUSES,
    classify_reason,
    compatibility_status_for,
    materialization_status_for_operator_state,
)

logger = logging.getLogger(__name__)

_LIFECYCLE_V2_SUPPORTED: dict[int, bool] = {}


def _supports_lifecycle_v2(pg_conn: psycopg.Connection) -> bool:
    cache_key = id(pg_conn)
    if cache_key in _LIFECYCLE_V2_SUPPORTED:
        return _LIFECYCLE_V2_SUPPORTED[cache_key]
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'evidence_tasks'
                  AND column_name IN (
                      'materialization_phase', 'materialization_owner',
                      'materialization_retry_reason',
                      'materialization_lease_token'
                  )
                """
            )
            row = cur.fetchone()
        count = int(row.get("count") if isinstance(row, dict) else row[0]) if row else 0
        supported = count == 4
    except Exception:
        supported = False
    _LIFECYCLE_V2_SUPPORTED[cache_key] = supported
    return supported


def clear_lifecycle_schema_capability_cache() -> None:
    """Clear mixed-order deployment schema detection state for tests/postflight."""
    _LIFECYCLE_V2_SUPPORTED.clear()

EVIDENCE_STATES = {
    *OPERATOR_EVIDENCE_STATES,
    "manifest_ready",
    "materialization_pending",
    "materializing",
    "materialized",
    "materialization_deferred",
    "materialization_failed",
    "materialization_expired",
    "materialization_skipped",
    "pending",
    "waiting_proof",
    "queued",
    "replaying",
    "finalizing",
    "ready",
    "failed",
}

TERMINAL_EVIDENCE_STATES = {
    "failed",
    "generated",
    "generated_annotation_failed",
    "generated_corrupt",
    "generated_unverified",
    "materialization_expired",
    "materialization_failed",
    "materialization_skipped",
    "materialized",
    "media_deleted",
    "media_expired",
    "ready",
    "skipped_by_poc_limit",
}

_STATUS_TO_EVIDENCE_STATE = {
    "pending": "materialization_pending",
    "replay_job_created": "materializing",
    "generated": "materialized",
    "ready": "materialized",
    "generated_corrupt": "materialization_failed",
    "generated_unverified": "materialized",
    "duration_guard_failed": "materialization_failed",
    "generated_annotation_failed": "materialization_failed",
    "skipped_by_poc_limit": "materialization_failed",
    "failed": "materialization_failed",
}


def evidence_state_for_status(status: str, override: str | None = None) -> str:
    """Map legacy clip status values onto the operator-visible state family."""
    if override:
        state = str(override)
    else:
        state = _STATUS_TO_EVIDENCE_STATE.get(str(status), str(status))
    return state if state in EVIDENCE_STATES else "failed"


def materialization_status_for_state(state: str) -> str:
    """Return the manifest-first materialization status for an evidence state."""
    return materialization_status_for_operator_state(state)


def _json_or_null(value: object) -> str:
    return json.dumps(value)


def _reason_for_state(status: str, state: str, reason: str, error_message: str) -> str:
    if reason:
        return reason
    if error_message:
        return error_message
    if status != state:
        return status
    return ""


def _replay_shard_value(replay_shard: dict | None, key: str) -> str:
    if not isinstance(replay_shard, dict):
        return ""
    return str(replay_shard.get(key) or "")


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def release_timed_out_replay_slots(pg_conn: psycopg.Connection) -> int:
    """Release active Replay admission slots whose fallback deadline elapsed."""
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return 0
    try:
        with cursor_factory() as cur:
            cur.execute(
                """
                WITH timed_out AS (
                    UPDATE evidence_tasks
                    SET replay_slot_status = 'timeout',
                        replay_slot_released_at = now(),
                        replay_slot_release_reason = 'timeout',
                        replay_slot_timeout_budget_s = EXTRACT(
                            EPOCH FROM (replay_slot_deadline_at - replay_slot_acquired_at)
                        ),
                        replay_slot_active_age_s = EXTRACT(
                            EPOCH FROM (now() - replay_slot_acquired_at)
                        ),
                        materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                            || jsonb_build_object(
                                'replay_slot',
                                jsonb_strip_nulls(jsonb_build_object(
                                    'status', 'timeout',
                                    'release_reason', 'timeout',
                                    'released_at', now(),
                                    'timeout_budget_s', EXTRACT(
                                        EPOCH FROM (
                                            replay_slot_deadline_at
                                            - replay_slot_acquired_at
                                        )
                                    ),
                                    'active_age_s', EXTRACT(
                                        EPOCH FROM (now() - replay_slot_acquired_at)
                                    ),
                                    'replay_job_id', replay_job_id,
                                    'resulting_stream_id', replay_resulting_stream_id
                                ))
                            ),
                        updated_at = now()
                    WHERE replay_slot_status = 'active'
                      AND replay_slot_deadline_at IS NOT NULL
                      AND replay_slot_deadline_at <= now()
                    RETURNING event_id,
                              replay_job_id,
                              replay_resulting_stream_id,
                              replay_slot_timeout_budget_s,
                              replay_slot_active_age_s
                )
                UPDATE events e
                SET payload = COALESCE(e.payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(e.payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'replay_slot_status', 'timeout',
                                'replay_slot_released_at', now(),
                                'replay_slot_release_reason', 'timeout',
                                'replay_slot_timeout_budget_s',
                                    timed_out.replay_slot_timeout_budget_s,
                                'replay_slot_active_age_s',
                                    timed_out.replay_slot_active_age_s,
                                'replay_job_id', timed_out.replay_job_id,
                                'resulting_stream_id',
                                    timed_out.replay_resulting_stream_id
                            ))
                        ),
                    updated_at = now()
                FROM timed_out
                WHERE e.id = timed_out.event_id
                """,
            )
            released = int(getattr(cur, "rowcount", 0) or 0)
            if released:
                logger.info(
                    "replay_slot_timeout_transition result=updated count=%s",
                    released,
                )
            else:
                logger.debug("replay_slot_timeout_transition result=noop")
            return released
    except Exception:
        logger.exception("release_timed_out_replay_slots failed")
        return 0


def active_replay_slot_counts(
    pg_conn: psycopg.Connection,
    *,
    shard_id: str,
    source_id: str,
) -> dict[str, int] | None:
    """Return DB-backed active Replay admission counts after timeout cleanup."""
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return None
    release_timed_out_replay_slots(pg_conn)
    try:
        with cursor_factory() as cur:
            cur.execute(
                """
                SELECT
                    count(*) AS global_count,
                    count(*) FILTER (
                        WHERE replay_shard_id = %(shard_id)s::text
                    ) AS shard_count,
                    count(*) FILTER (
                        WHERE COALESCE(replay_source_id, source_id) = %(source_id)s::text
                    ) AS source_count
                FROM evidence_tasks
                WHERE replay_slot_status = 'active'
                  AND (
                    replay_slot_deadline_at IS NULL
                    OR replay_slot_deadline_at > now()
                  )
                """,
                {"shard_id": shard_id, "source_id": source_id},
            )
            row = cur.fetchone()
    except Exception:
        logger.exception(
            "active_replay_slot_counts failed shard_id=%s source_id=%s",
            shard_id,
            source_id,
        )
        return None
    if row is None:
        return {
            "replay_active_global_count": 0,
            "replay_active_shard_count": 0,
            "replay_active_source_count": 0,
        }
    if isinstance(row, dict):
        if not {"global_count", "shard_count", "source_count"}.issubset(row):
            return None
        values = row
    else:
        values = {
            "global_count": row[0] if len(row) > 0 else 0,
            "shard_count": row[1] if len(row) > 1 else 0,
            "source_count": row[2] if len(row) > 2 else 0,
        }
    return {
        "replay_active_global_count": _safe_int(values.get("global_count")),
        "replay_active_shard_count": _safe_int(values.get("shard_count")),
        "replay_active_source_count": _safe_int(values.get("source_count")),
    }


def try_acquire_replay_slot(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    source_id: str,
    camera_id: str,
    replay_shard: dict | None,
    sink_instance: str,
    replay_duration_seconds_effective: float,
    replay_duration_effective_reason: str,
    timeout_budget_s: float,
    max_global: int,
    max_per_shard: int,
    max_per_source: int,
) -> dict[str, object] | None:
    """Atomically check Replay admission limits and reserve an active slot.

    The active-count decision and the slot reservation happen in one SQL
    statement behind a short PostgreSQL advisory lock. That makes the function
    safe to reuse when multiple clip-worker consumers are introduced later.
    """
    if not event_id:
        return None
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return None
    release_timed_out_replay_slots(pg_conn)
    replay_shard_id = _replay_shard_value(replay_shard, "shard_id")
    replay_api_url = _replay_shard_value(replay_shard, "replay_api_url")
    sink_url = _replay_shard_value(replay_shard, "replay_job_sink_url")
    duration_s = max(_safe_float(replay_duration_seconds_effective), 0.0)
    timeout_s = max(_safe_float(timeout_budget_s), 0.0)
    max_global = max(_safe_int(max_global), 0)
    max_per_shard = max(_safe_int(max_per_shard), 0)
    max_per_source = max(_safe_int(max_per_source), 0)
    try:
        with cursor_factory() as cur:
            params = {
                "event_id": event_id,
                "source_id": source_id,
                "camera_id": camera_id,
                "replay_shard_id": replay_shard_id,
                "replay_api_url": replay_api_url,
                "sink_url": sink_url,
                "sink_instance": sink_instance,
                "duration_s": duration_s,
                "duration_reason": replay_duration_effective_reason,
                "timeout_s": timeout_s,
                "max_global": max_global,
                "max_per_shard": max_per_shard,
                "max_per_source": max_per_source,
            }
            cur.execute(
                """
                WITH admission_lock AS (
                    SELECT pg_advisory_xact_lock(
                        hashtext('clip_worker_replay_admission_v1')
                    )
                ),
                target AS (
                    SELECT
                        et.event_id,
                        et.replay_slot_status AS current_slot_status,
                        et.status AS current_task_status,
                        et.materialization_status AS current_materialization_status
                    FROM evidence_tasks et, admission_lock
                    WHERE et.event_id = %(event_id)s::uuid
                    FOR UPDATE
                ),
                active AS (
                    SELECT
                        count(*) AS global_count,
                        count(*) FILTER (
                            WHERE replay_shard_id = %(replay_shard_id)s::text
                        ) AS shard_count,
                        count(*) FILTER (
                            WHERE COALESCE(replay_source_id, source_id)
                                = %(source_id)s::text
                        ) AS source_count
                    FROM evidence_tasks, admission_lock
                    WHERE replay_slot_status = 'active'
                      AND (
                        replay_slot_deadline_at IS NULL
                        OR replay_slot_deadline_at > now()
                      )
                ),
                decision AS (
                    SELECT
                        global_count,
                        shard_count,
                        source_count,
                        CASE
                            WHEN NOT EXISTS (SELECT 1 FROM target)
                                THEN 'replay_slot_target_missing'
                            WHEN EXISTS (
                                SELECT 1
                                FROM target
                                WHERE current_slot_status IN ('released', 'timeout')
                            )
                                THEN 'replay_slot_terminal_state'
                            WHEN %(max_global)s::int > 0
                             AND global_count >= %(max_global)s::int
                                THEN 'max_concurrent_reached'
                            WHEN %(max_per_shard)s::int > 0
                             AND shard_count >= %(max_per_shard)s::int
                                THEN 'max_concurrent_per_shard_reached'
                            WHEN %(max_per_source)s::int > 0
                             AND source_count >= %(max_per_source)s::int
                                THEN 'max_concurrent_per_source_reached'
                            ELSE ''
                        END AS deny_reason
                    FROM active
                ),
                reserved AS (
                    UPDATE evidence_tasks et
                    SET replay_shard_id = NULLIF(%(replay_shard_id)s::text, ''),
                        replay_api_url = NULLIF(%(replay_api_url)s::text, ''),
                        replay_job_sink_url = NULLIF(%(sink_url)s::text, ''),
                        replay_source_id = NULLIF(%(source_id)s::text, ''),
                        replay_sink_instance = NULLIF(%(sink_instance)s::text, ''),
                        replay_duration_seconds_effective = %(duration_s)s,
                        replay_duration_effective_reason = %(duration_reason)s,
                        replay_slot_status = 'active',
                        replay_slot_acquired_at = now(),
                        replay_slot_deadline_at = now() + (
                            %(timeout_s)s::double precision * interval '1 second'
                        ),
                        replay_slot_released_at = NULL,
                        replay_slot_release_reason = NULL,
                        replay_slot_timeout_budget_s = %(timeout_s)s,
                        replay_slot_active_age_s = NULL,
                        replay_window = COALESCE(replay_window, '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'duration_seconds_effective', %(duration_s)s,
                                'duration_effective_reason',
                                    %(duration_reason)s::text
                            )),
                        materialization_audit = COALESCE(
                            materialization_audit,
                            '{}'::jsonb
                        )
                            || jsonb_build_object(
                                'replay_slot',
                                jsonb_strip_nulls(jsonb_build_object(
                                    'status', 'active',
                                    'event_id', %(event_id)s::text,
                                    'source_id', NULLIF(%(source_id)s::text, ''),
                                    'camera_id', NULLIF(%(camera_id)s::text, ''),
                                    'replay_shard_id',
                                        NULLIF(%(replay_shard_id)s::text, ''),
                                    'sink_url', NULLIF(%(sink_url)s::text, ''),
                                    'sink_instance',
                                        NULLIF(%(sink_instance)s::text, ''),
                                    'replay_duration_seconds_effective',
                                        %(duration_s)s,
                                    'replay_duration_effective_reason',
                                        %(duration_reason)s::text,
                                    'acquired_at', now(),
                                    'deadline_at', now() + (
                                        %(timeout_s)s::double precision
                                        * interval '1 second'
                                    ),
                                    'timeout_budget_s', %(timeout_s)s,
                                    'admission_mode', 'atomic'
                                ))
                            ),
                        updated_at = now()
                    FROM decision
                    WHERE et.event_id = %(event_id)s::uuid
                      AND decision.deny_reason = ''
                      AND COALESCE(et.replay_slot_status, '') NOT IN (
                        'released',
                        'timeout'
                      )
                    RETURNING et.event_id
                ),
                event_reserved AS (
                    UPDATE events e
                    SET payload = COALESCE(payload, '{}'::jsonb)
                            || jsonb_build_object(
                                'media',
                                COALESCE(payload->'media', '{}'::jsonb)
                                || jsonb_strip_nulls(jsonb_build_object(
                                    'replay_slot_status', 'active',
                                    'replay_slot_admission_mode', 'atomic',
                                    'source_id', NULLIF(%(source_id)s::text, ''),
                                    'camera_id', NULLIF(%(camera_id)s::text, ''),
                                    'replay_shard_id',
                                        NULLIF(%(replay_shard_id)s::text, ''),
                                    'replay_job_sink_url',
                                        NULLIF(%(sink_url)s::text, ''),
                                    'sink_instance',
                                        NULLIF(%(sink_instance)s::text, ''),
                                    'replay_duration_seconds_effective',
                                        %(duration_s)s,
                                    'replay_duration_effective_reason',
                                        %(duration_reason)s::text,
                                    'replay_slot_acquired_at', now(),
                                    'replay_slot_deadline_at', now() + (
                                        %(timeout_s)s::double precision
                                        * interval '1 second'
                                    ),
                                    'replay_slot_released_at', NULL,
                                    'replay_slot_release_reason', NULL,
                                    'replay_slot_active_age_s', NULL,
                                    'replay_slot_timeout_budget_s', %(timeout_s)s
                                ))
                            ),
                        updated_at = now()
                    WHERE e.id IN (SELECT event_id FROM reserved)
                    RETURNING e.id
                )
                SELECT
                    decision.global_count,
                    decision.shard_count,
                    decision.source_count,
                    decision.deny_reason,
                    EXISTS (SELECT 1 FROM reserved) AS acquired,
                    EXISTS (SELECT 1 FROM event_reserved) AS event_updated
                FROM decision
                """,
                params,
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("try_acquire_replay_slot failed event_id=%s", event_id)
        return None
    if row is None:
        return None
    if isinstance(row, dict):
        values = row
    else:
        values = {
            "global_count": row[0] if len(row) > 0 else 0,
            "shard_count": row[1] if len(row) > 1 else 0,
            "source_count": row[2] if len(row) > 2 else 0,
            "deny_reason": row[3] if len(row) > 3 else "",
            "acquired": row[4] if len(row) > 4 else False,
            "event_updated": row[5] if len(row) > 5 else False,
        }
    counts = {
        "replay_active_global_count": _safe_int(values.get("global_count")),
        "replay_active_shard_count": _safe_int(values.get("shard_count")),
        "replay_active_source_count": _safe_int(values.get("source_count")),
    }
    reason = str(values.get("deny_reason") or "")
    acquired = bool(values.get("acquired"))
    if not acquired and not reason:
        reason = "replay_slot_target_missing"
    quota_decision: dict[str, object] = {}
    error_message = ""
    if reason == "max_concurrent_reached":
        quota_decision = {
            "scope": "global_concurrency",
            "limit": max_global,
            "observed": counts["replay_active_global_count"],
            "admission_mode": "atomic",
        }
        error_message = "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY reached"
    elif reason == "max_concurrent_per_shard_reached":
        quota_decision = {
            "scope": "shard_concurrency",
            "shard_id": replay_shard_id,
            "limit": max_per_shard,
            "observed": counts["replay_active_shard_count"],
            "admission_mode": "atomic",
        }
        error_message = "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD reached"
    elif reason == "max_concurrent_per_source_reached":
        quota_decision = {
            "scope": "source_concurrency",
            "source_id": source_id,
            "limit": max_per_source,
            "observed": counts["replay_active_source_count"],
            "admission_mode": "atomic",
        }
        error_message = "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE reached"
    elif reason:
        quota_decision = {"scope": "replay_admission", "admission_mode": "atomic"}
        error_message = reason
    if acquired:
        logger.info(
            "replay_slot_admission_transition event_id=%s source_id=%s "
            "result=active shard_id=%s counts=%s",
            event_id,
            source_id,
            replay_shard_id,
            counts,
        )
    elif reason in {"replay_slot_terminal_state", "replay_slot_target_missing"}:
        logger.info(
            "replay_slot_admission_transition event_id=%s source_id=%s "
            "result=noop reason=%s shard_id=%s counts=%s",
            event_id,
            source_id,
            reason,
            replay_shard_id,
            counts,
        )
    return {
        "acquired": acquired,
        "reason": reason,
        "error_message": error_message,
        "counts": counts,
        "quota_decision": quota_decision,
        "event_updated": bool(values.get("event_updated")),
    }


def record_replay_job_for_slot(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    replay_job_id: str,
    resulting_stream_id: str,
) -> bool:
    """Attach a created Replay job to an already-reserved active slot."""
    if not event_id or not replay_job_id:
        return False
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return False
    try:
        with cursor_factory() as cur:
            params = {
                "event_id": event_id,
                "replay_job_id": replay_job_id,
                "resulting_stream_id": resulting_stream_id,
            }
            cur.execute(
                """
                WITH updated_task AS (
                    UPDATE evidence_tasks
                    SET replay_job_id = %(replay_job_id)s,
                        replay_resulting_stream_id =
                            NULLIF(%(resulting_stream_id)s::text, ''),
                        replay_window = COALESCE(replay_window, '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'resulting_stream_id',
                                    NULLIF(%(resulting_stream_id)s::text, '')
                            )),
                        materialization_audit = COALESCE(
                            materialization_audit,
                            '{}'::jsonb
                        )
                            || jsonb_build_object(
                                'replay_slot',
                                jsonb_strip_nulls(
                                    COALESCE(
                                        materialization_audit->'replay_slot',
                                        '{}'::jsonb
                                    )
                                    || jsonb_build_object(
                                        'replay_job_id', %(replay_job_id)s::text,
                                        'resulting_stream_id',
                                            NULLIF(%(resulting_stream_id)s::text, '')
                                    )
                                )
                            ),
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                      AND replay_slot_status = 'active'
                    RETURNING event_id
                ),
                updated_event AS (
                    UPDATE events e
                    SET payload = COALESCE(payload, '{}'::jsonb)
                            || jsonb_build_object(
                                'media',
                                COALESCE(payload->'media', '{}'::jsonb)
                                || jsonb_strip_nulls(jsonb_build_object(
                                    'replay_job_id', %(replay_job_id)s::text,
                                    'resulting_stream_id',
                                        NULLIF(%(resulting_stream_id)s::text, '')
                                ))
                            ),
                        updated_at = now()
                    WHERE e.id IN (SELECT event_id FROM updated_task)
                    RETURNING e.id
                )
                SELECT
                    EXISTS (SELECT 1 FROM updated_task) AS task_updated,
                    EXISTS (SELECT 1 FROM updated_event) AS event_updated
                """,
                params,
            )
            row = cur.fetchone()
            if row is None:
                return False
            if isinstance(row, dict):
                return bool(row.get("task_updated"))
            return bool(row[0] if len(row) > 0 else False)
    except Exception:
        logger.exception("record_replay_job_for_slot failed event_id=%s", event_id)
        return False


def _fenced_slot_result(row: object) -> dict[str, object] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        values = dict(row)
    else:
        names = (
            "global_count",
            "shard_count",
            "source_count",
            "deny_reason",
            "acquired",
            "event_updated",
            "slot_owner",
            "slot_token",
            "slot_generation",
            "create_state",
            "replay_job_id",
            "resulting_stream_id",
            "plan_hash",
        )
        values = {
            name: row[index] if index < len(row) else None
            for index, name in enumerate(names)
        }
    counts = {
        "replay_active_global_count": _safe_int(values.get("global_count")),
        "replay_active_shard_count": _safe_int(values.get("shard_count")),
        "replay_active_source_count": _safe_int(values.get("source_count")),
    }
    result = {
        **values,
        "counts": counts,
        "acquired": bool(values.get("acquired")),
        "event_updated": bool(values.get("event_updated")),
        "deny_reason": str(values.get("deny_reason") or ""),
        "slot_owner": str(values.get("slot_owner") or ""),
        "slot_token": str(values.get("slot_token") or ""),
        "slot_generation": _safe_int(values.get("slot_generation")),
        "create_state": str(values.get("create_state") or ""),
        "replay_job_id": str(values.get("replay_job_id") or ""),
        "resulting_stream_id": str(values.get("resulting_stream_id") or ""),
        "plan_hash": str(values.get("plan_hash") or ""),
    }
    reason = result["deny_reason"]
    result["reason"] = reason
    result["error_message"] = reason
    if reason == "max_concurrent_reached":
        result["quota_decision"] = {
            "scope": "global_concurrency",
            "admission_mode": "fenced",
        }
    elif reason == "max_concurrent_per_shard_reached":
        result["quota_decision"] = {
            "scope": "shard_concurrency",
            "admission_mode": "fenced",
        }
    elif reason == "max_concurrent_per_source_reached":
        result["quota_decision"] = {
            "scope": "source_concurrency",
            "admission_mode": "fenced",
        }
    else:
        result["quota_decision"] = {}
    return result


def try_acquire_fenced_replay_slot(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    owner: str,
    slot_token: str,
    request_id: str,
    delivery_id: str,
    plan_hash: str,
    source_id: str,
    camera_id: str,
    replay_shard: dict | None,
    sink_instance: str,
    replay_duration_seconds_effective: float,
    replay_duration_effective_reason: str,
    timeout_budget_s: float,
    max_global: int,
    max_per_shard: int,
    max_per_source: int,
) -> dict[str, object] | None:
    """Reserve one Replay slot and bind its first owner/fence atomically."""
    if not event_id or not owner or not slot_token or not plan_hash:
        return None
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return None
    replay_shard_id = _replay_shard_value(replay_shard, "shard_id")
    replay_api_url = _replay_shard_value(replay_shard, "replay_api_url")
    sink_url = _replay_shard_value(replay_shard, "replay_job_sink_url")
    params = {
        "event_id": event_id,
        "owner": owner,
        "slot_token": slot_token,
        "request_id": request_id,
        "delivery_id": delivery_id,
        "plan_hash": plan_hash,
        "source_id": source_id,
        "camera_id": camera_id,
        "replay_shard_id": replay_shard_id,
        "replay_api_url": replay_api_url,
        "sink_url": sink_url,
        "sink_instance": sink_instance,
        "duration_s": max(_safe_float(replay_duration_seconds_effective), 0.0),
        "duration_reason": replay_duration_effective_reason,
        "timeout_s": max(_safe_float(timeout_budget_s), 0.0),
        "max_global": max(_safe_int(max_global), 0),
        "max_per_shard": max(_safe_int(max_per_shard), 0),
        "max_per_source": max(_safe_int(max_per_source), 0),
    }
    try:
        with cursor_factory() as cur:
            cur.execute(
                """
                WITH admission_lock AS (
                    SELECT pg_advisory_xact_lock(
                        hashtext('clip_worker_replay_admission_v2')
                    )
                ),
                target AS (
                    SELECT
                        et.event_id,
                        et.replay_slot_status AS current_slot_status,
                        et.replay_slot_owner AS current_slot_owner,
                        et.replay_slot_token AS current_slot_token,
                        et.replay_slot_generation AS current_slot_generation,
                        et.replay_create_state AS current_create_state,
                        et.replay_job_id AS current_replay_job_id,
                        et.replay_resulting_stream_id AS current_resulting_stream_id,
                        et.replay_plan_hash AS current_plan_hash
                    FROM evidence_tasks et, admission_lock
                    WHERE et.event_id = %(event_id)s::uuid
                    FOR UPDATE
                ),
                active AS (
                    SELECT
                        count(*) AS global_count,
                        count(*) FILTER (
                            WHERE replay_shard_id = %(replay_shard_id)s::text
                        ) AS shard_count,
                        count(*) FILTER (
                            WHERE COALESCE(replay_source_id, source_id)
                                = %(source_id)s::text
                        ) AS source_count
                    FROM evidence_tasks, admission_lock
                    WHERE replay_slot_status = 'active'
                      AND (
                          replay_slot_deadline_at IS NULL
                          OR replay_slot_deadline_at > now()
                      )
                ),
                decision AS (
                    SELECT
                        active.global_count,
                        active.shard_count,
                        active.source_count,
                        CASE
                            WHEN NOT EXISTS (SELECT 1 FROM target)
                                THEN 'replay_slot_target_missing'
                            WHEN EXISTS (
                                SELECT 1 FROM target
                                WHERE current_slot_status = 'active'
                            )
                                THEN 'replay_slot_active_existing'
                            WHEN EXISTS (
                                SELECT 1 FROM target
                                WHERE current_slot_status IN ('released', 'timeout')
                            )
                                THEN 'replay_slot_terminal_state'
                            WHEN %(max_global)s::int > 0
                             AND active.global_count >= %(max_global)s::int
                                THEN 'max_concurrent_reached'
                            WHEN %(max_per_shard)s::int > 0
                             AND active.shard_count >= %(max_per_shard)s::int
                                THEN 'max_concurrent_per_shard_reached'
                            WHEN %(max_per_source)s::int > 0
                             AND active.source_count >= %(max_per_source)s::int
                                THEN 'max_concurrent_per_source_reached'
                            ELSE ''
                        END AS deny_reason
                    FROM active
                ),
                reserved AS (
                    UPDATE evidence_tasks et
                    SET replay_shard_id = NULLIF(%(replay_shard_id)s::text, ''),
                        replay_api_url = NULLIF(%(replay_api_url)s::text, ''),
                        replay_job_sink_url = NULLIF(%(sink_url)s::text, ''),
                        replay_source_id = NULLIF(%(source_id)s::text, ''),
                        replay_sink_instance = NULLIF(%(sink_instance)s::text, ''),
                        replay_duration_seconds_effective = %(duration_s)s,
                        replay_duration_effective_reason = %(duration_reason)s,
                        replay_slot_status = 'active',
                        replay_slot_owner = %(owner)s,
                        replay_slot_token = %(slot_token)s,
                        replay_slot_generation = replay_slot_generation + 1,
                        replay_create_state = 'reserved',
                        replay_create_started_at = NULL,
                        replay_create_committed_at = NULL,
                        replay_plan_hash = %(plan_hash)s,
                        replay_request_id = NULLIF(%(request_id)s::text, ''),
                        replay_delivery_id = NULLIF(%(delivery_id)s::text, ''),
                        replay_job_id = NULL,
                        replay_resulting_stream_id = NULL,
                        replay_slot_acquired_at = now(),
                        replay_slot_deadline_at = now() + (
                            %(timeout_s)s::double precision * interval '1 second'
                        ),
                        replay_slot_released_at = NULL,
                        replay_slot_release_reason = NULL,
                        replay_slot_timeout_budget_s = %(timeout_s)s,
                        replay_slot_active_age_s = NULL,
                        replay_window = COALESCE(replay_window, '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'duration_seconds_effective',
                                    %(duration_s)s::double precision,
                                'duration_effective_reason',
                                    %(duration_reason)s::text
                            )),
                        materialization_audit = COALESCE(
                            materialization_audit,
                            '{}'::jsonb
                        ) || jsonb_build_object(
                            'replay_slot',
                            jsonb_strip_nulls(jsonb_build_object(
                                'status', 'active',
                                'owner', %(owner)s::text,
                                'token', %(slot_token)s::text,
                                'generation', replay_slot_generation + 1,
                                'create_state', 'reserved',
                                'plan_hash', %(plan_hash)s::text,
                                'request_id', NULLIF(%(request_id)s::text, ''),
                                'delivery_id', NULLIF(%(delivery_id)s::text, ''),
                                'source_id', NULLIF(%(source_id)s::text, ''),
                                'camera_id', NULLIF(%(camera_id)s::text, ''),
                                'replay_shard_id',
                                    NULLIF(%(replay_shard_id)s::text, ''),
                                'sink_instance',
                                    NULLIF(%(sink_instance)s::text, ''),
                                'acquired_at', now(),
                                'deadline_at', now() + (
                                    %(timeout_s)s::double precision
                                    * interval '1 second'
                                )
                            ))
                        ),
                        updated_at = now()
                    FROM decision
                    WHERE et.event_id = %(event_id)s::uuid
                      AND decision.deny_reason = ''
                      AND COALESCE(et.replay_slot_status, '') NOT IN (
                          'active', 'released', 'timeout'
                      )
                    RETURNING
                        et.event_id,
                        et.replay_slot_owner AS slot_owner,
                        et.replay_slot_token AS slot_token,
                        et.replay_slot_generation AS slot_generation,
                        et.replay_create_state AS create_state,
                        et.replay_job_id,
                        et.replay_resulting_stream_id AS resulting_stream_id,
                        et.replay_plan_hash AS plan_hash
                ),
                event_reserved AS (
                    UPDATE events e
                    SET payload = COALESCE(e.payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(e.payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'replay_slot_status', 'active',
                                'replay_slot_owner', reserved.slot_owner,
                                'replay_slot_token', reserved.slot_token,
                                'replay_slot_generation', reserved.slot_generation,
                                'replay_create_state', reserved.create_state,
                                'replay_plan_hash', reserved.plan_hash,
                                'replay_slot_acquired_at', now(),
                                'replay_slot_deadline_at', now() + (
                                    %(timeout_s)s::double precision
                                    * interval '1 second'
                                )
                            ))
                        ),
                        updated_at = now()
                    FROM reserved
                    WHERE e.id = reserved.event_id
                    RETURNING e.id
                )
                SELECT
                    decision.global_count,
                    decision.shard_count,
                    decision.source_count,
                    decision.deny_reason,
                    EXISTS (SELECT 1 FROM reserved) AS acquired,
                    EXISTS (SELECT 1 FROM event_reserved) AS event_updated,
                    COALESCE(
                        (SELECT slot_owner FROM reserved),
                        (SELECT current_slot_owner FROM target)
                    ) AS slot_owner,
                    COALESCE(
                        (SELECT slot_token FROM reserved),
                        (SELECT current_slot_token FROM target)
                    ) AS slot_token,
                    COALESCE(
                        (SELECT slot_generation FROM reserved),
                        (SELECT current_slot_generation FROM target)
                    ) AS slot_generation,
                    COALESCE(
                        (SELECT create_state FROM reserved),
                        (SELECT current_create_state FROM target)
                    ) AS create_state,
                    COALESCE(
                        (SELECT replay_job_id FROM reserved),
                        (SELECT current_replay_job_id FROM target)
                    ) AS replay_job_id,
                    COALESCE(
                        (SELECT resulting_stream_id FROM reserved),
                        (SELECT current_resulting_stream_id FROM target)
                    ) AS resulting_stream_id,
                    COALESCE(
                        (SELECT plan_hash FROM reserved),
                        (SELECT current_plan_hash FROM target)
                    ) AS plan_hash
                FROM decision
                """,
                params,
            )
            result = _fenced_slot_result(cur.fetchone())
    except Exception:
        logger.exception(
            "try_acquire_fenced_replay_slot failed event_id=%s owner=%s",
            event_id,
            owner,
        )
        return None
    if result and result.get("acquired") and not result.get("event_updated"):
        logger.error(
            "fenced_replay_slot_event_projection_missing event_id=%s owner=%s",
            event_id,
            owner,
        )
        return None
    return result


def takeover_fenced_replay_slot(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    owner: str,
    expected_token: str,
    expected_generation: int,
    delivery_id: str,
) -> dict[str, object] | None:
    """Transfer an uncommitted active slot to a reclaimed Redis delivery."""
    if not event_id or not owner or not expected_token:
        return None
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                WITH claimed AS (
                    UPDATE evidence_tasks
                    SET replay_slot_owner = %(owner)s,
                        replay_slot_generation = replay_slot_generation + 1,
                        replay_delivery_id = NULLIF(%(delivery_id)s::text, ''),
                        materialization_audit = COALESCE(
                            materialization_audit,
                            '{}'::jsonb
                        ) || jsonb_build_object(
                            'replay_slot',
                            COALESCE(
                                materialization_audit->'replay_slot',
                                '{}'::jsonb
                            ) || jsonb_build_object(
                                'owner', %(owner)s::text,
                                'generation', replay_slot_generation + 1,
                                'delivery_id',
                                    NULLIF(%(delivery_id)s::text, ''),
                                'taken_over_at', now()
                            )
                        ),
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                      AND replay_slot_status = 'active'
                      AND replay_slot_token = %(expected_token)s
                      AND replay_slot_generation = %(expected_generation)s
                      AND replay_job_id IS NULL
                    RETURNING
                        event_id,
                        replay_slot_owner AS slot_owner,
                        replay_slot_token AS slot_token,
                        replay_slot_generation AS slot_generation,
                        replay_create_state AS create_state,
                        replay_job_id,
                        replay_resulting_stream_id AS resulting_stream_id,
                        replay_plan_hash AS plan_hash
                ),
                event_claimed AS (
                    UPDATE events e
                    SET payload = COALESCE(e.payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(e.payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'replay_slot_owner', claimed.slot_owner,
                                'replay_slot_token', claimed.slot_token,
                                'replay_slot_generation',
                                    claimed.slot_generation,
                                'replay_create_state', claimed.create_state
                            )
                        ),
                        updated_at = now()
                    FROM claimed
                    WHERE e.id = claimed.event_id
                    RETURNING e.id
                )
                SELECT
                    EXISTS (SELECT 1 FROM claimed) AS claimed,
                    EXISTS (SELECT 1 FROM event_claimed) AS event_updated,
                    (SELECT slot_owner FROM claimed) AS slot_owner,
                    (SELECT slot_token FROM claimed) AS slot_token,
                    (SELECT slot_generation FROM claimed) AS slot_generation,
                    (SELECT create_state FROM claimed) AS create_state,
                    (SELECT replay_job_id FROM claimed) AS replay_job_id,
                    (SELECT resulting_stream_id FROM claimed)
                        AS resulting_stream_id,
                    (SELECT plan_hash FROM claimed) AS plan_hash
                """,
                {
                    "event_id": event_id,
                    "owner": owner,
                    "expected_token": expected_token,
                    "expected_generation": max(_safe_int(expected_generation), 0),
                    "delivery_id": delivery_id,
                },
            )
            row = cur.fetchone()
    except Exception:
        logger.exception(
            "takeover_fenced_replay_slot failed event_id=%s owner=%s",
            event_id,
            owner,
        )
        return None
    if row is None:
        return None
    values = dict(row) if isinstance(row, dict) else {
        "claimed": row[0] if len(row) > 0 else False,
        "event_updated": row[1] if len(row) > 1 else False,
        "slot_owner": row[2] if len(row) > 2 else "",
        "slot_token": row[3] if len(row) > 3 else "",
        "slot_generation": row[4] if len(row) > 4 else 0,
        "create_state": row[5] if len(row) > 5 else "",
        "replay_job_id": row[6] if len(row) > 6 else "",
        "resulting_stream_id": row[7] if len(row) > 7 else "",
        "plan_hash": row[8] if len(row) > 8 else "",
    }
    values["claimed"] = bool(values.get("claimed"))
    values["event_updated"] = bool(values.get("event_updated"))
    values["slot_generation"] = _safe_int(values.get("slot_generation"))
    if values["claimed"] and not values["event_updated"]:
        return None
    return values


def mark_replay_create_started(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    owner: str,
    slot_token: str,
    slot_generation: int,
    plan_hash: str,
    replay_job_request: dict | None = None,
) -> bool:
    """Persist the immutable request before the external Replay side effect.

    The planned request is not a success claim.  It lets Media Worker verify a
    deterministic sink receipt if Clip Worker dies after Replay accepts the
    request but before the job handoff transaction.
    """
    params = {
        "event_id": event_id,
        "owner": owner,
        "slot_token": slot_token,
        "slot_generation": max(_safe_int(slot_generation), 0),
        "plan_hash": plan_hash,
        "replay_job_request": json.dumps(replay_job_request or {}),
    }
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                WITH started AS (
                    UPDATE evidence_tasks
                    SET replay_create_state = 'submitting',
                        replay_create_started_at = COALESCE(
                            replay_create_started_at,
                            now()
                        ),
                        materialization_audit = COALESCE(
                            materialization_audit,
                            '{}'::jsonb
                        ) || jsonb_build_object(
                            'replay_create',
                            jsonb_build_object(
                                'state', 'submitting',
                                'started_at', now(),
                                'owner', %(owner)s::text,
                                'token', %(slot_token)s::text,
                                'generation', %(slot_generation)s::bigint,
                                'plan_hash', %(plan_hash)s::text,
                                'planned_request',
                                    %(replay_job_request)s::jsonb
                            )
                        ),
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                      AND replay_slot_status = 'active'
                      AND replay_slot_owner = %(owner)s
                      AND replay_slot_token = %(slot_token)s
                      AND replay_slot_generation = %(slot_generation)s
                      AND replay_plan_hash = %(plan_hash)s
                      AND replay_job_id IS NULL
                      AND replay_create_state IN ('reserved', 'submitting')
                    RETURNING event_id
                )
                UPDATE events e
                SET payload = COALESCE(e.payload, '{}'::jsonb)
                    || jsonb_build_object(
                        'media',
                        COALESCE(e.payload->'media', '{}'::jsonb)
                        || jsonb_build_object(
                            'replay_job_request',
                                %(replay_job_request)s::jsonb,
                            'replay_create_state', 'submitting',
                            'replay_plan_hash', %(plan_hash)s::text,
                            'replay_slot_owner', %(owner)s::text,
                            'replay_slot_token', %(slot_token)s::text,
                            'replay_slot_generation',
                                %(slot_generation)s::bigint
                        )
                    ),
                    updated_at = now()
                FROM started
                WHERE e.id = started.event_id
                """,
                params,
            )
            return bool(getattr(cur, "rowcount", 0) or 0)
    except Exception:
        logger.exception(
            "mark_replay_create_started failed event_id=%s owner=%s",
            event_id,
            owner,
        )
        return False


def mark_replay_create_uncertain(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    owner: str,
    slot_token: str,
    slot_generation: int,
    reason: str,
) -> bool:
    """Keep an ambiguous external create active without allowing a retry."""
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evidence_tasks
                SET replay_create_state = 'uncertain',
                    error_message = NULLIF(%(reason)s::text, ''),
                    materialization_audit = COALESCE(
                        materialization_audit,
                        '{}'::jsonb
                    ) || jsonb_build_object(
                        'replay_create',
                        COALESCE(
                            materialization_audit->'replay_create',
                            '{}'::jsonb
                        ) || jsonb_build_object(
                            'state', 'uncertain',
                            'reason', %(reason)s::text,
                            'uncertain_at', now(),
                            'owner', %(owner)s::text,
                            'token', %(slot_token)s::text,
                            'generation', %(slot_generation)s::bigint
                        )
                    ),
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                  AND replay_slot_status = 'active'
                  AND replay_slot_owner = %(owner)s
                  AND replay_slot_token = %(slot_token)s
                  AND replay_slot_generation = %(slot_generation)s
                  AND replay_job_id IS NULL
                  AND replay_create_state IN ('submitting', 'uncertain')
                """,
                {
                    "event_id": event_id,
                    "owner": owner,
                    "slot_token": slot_token,
                    "slot_generation": max(_safe_int(slot_generation), 0),
                    "reason": reason,
                },
            )
            return bool(getattr(cur, "rowcount", 0) or 0)
    except Exception:
        logger.exception(
            "mark_replay_create_uncertain failed event_id=%s owner=%s",
            event_id,
            owner,
        )
        return False


def abort_fenced_replay_create(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    owner: str,
    slot_token: str,
    slot_generation: int,
    plan_hash: str,
    reason: str,
    diagnostics: dict | None = None,
) -> bool:
    """Terminal-fail one definitive Replay rejection under the slot fence.

    A non-retryable Replay 4xx proves that no job was created.  Task terminal
    state, slot release, create state, and the event projection therefore move
    in one SQL statement before the Redis delivery may be acknowledged.
    """
    if not event_id or not owner or not slot_token or not plan_hash:
        return False
    params = {
        "event_id": event_id,
        "owner": owner,
        "slot_token": slot_token,
        "slot_generation": max(_safe_int(slot_generation), 0),
        "plan_hash": plan_hash,
        "reason": reason or "Replay request permanently rejected",
        "reason_code": "replay_permanent_rejected",
        "diagnostics": json.dumps(diagnostics or {}),
    }
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                WITH aborted_task AS (
                    UPDATE evidence_tasks et
                    SET status = 'materialization_failed',
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
                        replay_slot_status = 'released',
                        replay_slot_released_at = now(),
                        replay_slot_release_reason = %(reason_code)s,
                        replay_slot_active_age_s = EXTRACT(
                            EPOCH FROM (now() - replay_slot_acquired_at)
                        ),
                        replay_create_state = 'aborted',
                        error_message = %(reason)s,
                        materialization_audit = COALESCE(
                            materialization_audit,
                            '{}'::jsonb
                        ) || jsonb_build_object(
                            'replay_create',
                            COALESCE(
                                materialization_audit->'replay_create',
                                '{}'::jsonb
                            ) || jsonb_build_object(
                                'state', 'aborted',
                                'reason', %(reason)s,
                                'aborted_at', now(),
                                'owner', %(owner)s,
                                'token', %(slot_token)s,
                                'generation', %(slot_generation)s::bigint,
                                'plan_hash', %(plan_hash)s
                            ),
                            'replay_slot',
                            COALESCE(
                                materialization_audit->'replay_slot',
                                '{}'::jsonb
                            ) || jsonb_build_object(
                                'status', 'released',
                                'release_reason', %(reason_code)s,
                                'released_at', now(),
                                'active_age_s', EXTRACT(
                                    EPOCH FROM (now() - replay_slot_acquired_at)
                                )
                            )
                        ),
                        updated_at = now()
                    WHERE et.event_id = %(event_id)s::uuid
                      AND et.replay_slot_status = 'active'
                      AND et.replay_slot_owner = %(owner)s
                      AND et.replay_slot_token = %(slot_token)s
                      AND et.replay_slot_generation = %(slot_generation)s
                      AND et.replay_plan_hash = %(plan_hash)s
                      AND et.replay_job_id IS NULL
                      AND et.replay_create_state = 'submitting'
                    RETURNING
                        et.event_id,
                        et.replay_slot_active_age_s
                ),
                aborted_event AS (
                    UPDATE events e
                    SET payload = COALESCE(e.payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(e.payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'clip_status', 'failed',
                                'evidence_state', 'materialization_failed',
                                'evidence_reason', %(reason_code)s,
                                'materialization_status',
                                    'materialization_failed',
                                'materialization_phase', 'terminal',
                                'materialization_owner', 'terminal',
                                'replay_create_state', 'aborted',
                                'replay_slot_status', 'released',
                                'replay_slot_release_reason', %(reason_code)s,
                                'replay_slot_owner', %(owner)s,
                                'replay_slot_token', %(slot_token)s,
                                'replay_slot_generation',
                                    %(slot_generation)s::bigint,
                                'replay_plan_hash', %(plan_hash)s,
                                'replay_slot_released_at', now(),
                                'replay_slot_active_age_s',
                                    aborted_task.replay_slot_active_age_s,
                                'error', %(reason)s,
                                'evidence_diagnostics',
                                    %(diagnostics)s::jsonb
                            )
                        ),
                        media_status = 'failed',
                        updated_at = now()
                    FROM aborted_task
                    WHERE e.id = aborted_task.event_id
                    RETURNING e.id
                )
                SELECT
                    EXISTS (SELECT 1 FROM aborted_task) AS task_updated,
                    EXISTS (SELECT 1 FROM aborted_event) AS event_updated
                """,
                params,
            )
            row = cur.fetchone()
    except Exception:
        logger.exception(
            "abort_fenced_replay_create failed event_id=%s owner=%s",
            event_id,
            owner,
        )
        return False
    if row is None:
        return False
    if isinstance(row, dict):
        return bool(row.get("task_updated") and row.get("event_updated"))
    return bool(len(row) > 1 and row[0] and row[1])


def commit_replay_job_handoff(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    owner: str,
    slot_token: str,
    slot_generation: int,
    plan_hash: str,
    replay_job_id: str,
    resulting_stream_id: str,
    replay_job_request: dict | None,
    diagnostics: dict | None,
    replay_shard: dict | None,
) -> bool:
    """Atomically persist Replay job, handoff, task state, and event projection."""
    if not event_id or not replay_job_id:
        return False
    params = {
        "event_id": event_id,
        "owner": owner,
        "slot_token": slot_token,
        "slot_generation": max(_safe_int(slot_generation), 0),
        "plan_hash": plan_hash,
        "replay_job_id": replay_job_id,
        "resulting_stream_id": resulting_stream_id,
        "replay_job_request": json.dumps(replay_job_request or {}),
        "diagnostics": json.dumps(diagnostics or {}),
        "replay_shard_id": _replay_shard_value(replay_shard, "shard_id"),
        "replay_api_url": _replay_shard_value(replay_shard, "replay_api_url"),
        "replay_job_sink_url": _replay_shard_value(
            replay_shard,
            "replay_job_sink_url",
        ),
    }
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                WITH target_event AS (
                    SELECT id
                    FROM events
                    WHERE id = %(event_id)s::uuid
                ),
                updated_task AS (
                    UPDATE evidence_tasks et
                    SET status = 'materializing',
                        materialization_status = 'materializing',
                        materialization_phase = 'waiting_ready',
                        materialization_phase_updated_at = now(),
                        materialization_owner = 'replay',
                        replay_job_id = %(replay_job_id)s,
                        replay_resulting_stream_id = NULLIF(
                            %(resulting_stream_id)s::text,
                            ''
                        ),
                        replay_create_state = 'committed',
                        replay_create_committed_at = now(),
                        replay_shard_id = COALESCE(
                            NULLIF(%(replay_shard_id)s::text, ''),
                            replay_shard_id
                        ),
                        replay_api_url = COALESCE(
                            NULLIF(%(replay_api_url)s::text, ''),
                            replay_api_url
                        ),
                        replay_job_sink_url = COALESCE(
                            NULLIF(%(replay_job_sink_url)s::text, ''),
                            replay_job_sink_url
                        ),
                        replay_window = COALESCE(replay_window, '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'resulting_stream_id',
                                    NULLIF(%(resulting_stream_id)s::text, '')
                            )),
                        materialization_handoff = jsonb_strip_nulls(
                            jsonb_build_object(
                                'kind', 'replay',
                                'event_id', %(event_id)s::text,
                                'replay_job_id', %(replay_job_id)s::text,
                                'resulting_stream_id',
                                    NULLIF(%(resulting_stream_id)s::text, ''),
                                'slot_owner', %(owner)s::text,
                                'slot_token', %(slot_token)s::text,
                                'slot_generation', %(slot_generation)s::bigint,
                                'plan_hash', %(plan_hash)s::text,
                                'committed_at', now()
                            )
                        ),
                        materialization_audit = COALESCE(
                            materialization_audit,
                            '{}'::jsonb
                        ) || jsonb_build_object(
                            'replay_slot',
                            COALESCE(
                                materialization_audit->'replay_slot',
                                '{}'::jsonb
                            ) || jsonb_build_object(
                                'create_state', 'committed',
                                'replay_job_id', %(replay_job_id)s::text,
                                'resulting_stream_id',
                                    NULLIF(%(resulting_stream_id)s::text, ''),
                                'committed_at', now()
                            )
                        ),
                        error_message = NULL,
                        updated_at = now()
                    WHERE et.event_id = %(event_id)s::uuid
                      AND EXISTS (SELECT 1 FROM target_event)
                      AND et.replay_slot_status = 'active'
                      AND et.replay_slot_owner = %(owner)s
                      AND et.replay_slot_token = %(slot_token)s
                      AND et.replay_slot_generation = %(slot_generation)s
                      AND et.replay_plan_hash = %(plan_hash)s
                      AND et.replay_create_state IN ('submitting', 'committed')
                      AND (
                          et.replay_job_id IS NULL
                          OR et.replay_job_id = %(replay_job_id)s
                      )
                    RETURNING et.event_id
                ),
                updated_event AS (
                    UPDATE events e
                    SET payload = COALESCE(e.payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(e.payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'clip_status', 'replay_job_created',
                                'recording_strategy', 'savant_replay',
                                'replay_job_id', %(replay_job_id)s::text,
                                'resulting_stream_id',
                                    NULLIF(%(resulting_stream_id)s::text, ''),
                                'replay_job_request',
                                    %(replay_job_request)s::jsonb,
                                'evidence_state', 'materializing',
                                'materialization_status', 'materializing',
                                'materialization_phase', 'waiting_ready',
                                'materialization_owner', 'replay',
                                'evidence_diagnostics',
                                    %(diagnostics)s::jsonb,
                                'replay_shard_id',
                                    NULLIF(%(replay_shard_id)s::text, ''),
                                'replay_api_url',
                                    NULLIF(%(replay_api_url)s::text, ''),
                                'replay_job_sink_url',
                                    NULLIF(%(replay_job_sink_url)s::text, ''),
                                'replay_slot_owner', %(owner)s::text,
                                'replay_slot_token', %(slot_token)s::text,
                                'replay_slot_generation',
                                    %(slot_generation)s::bigint,
                                'replay_create_state', 'committed',
                                'replay_plan_hash', %(plan_hash)s::text
                            ))
                        ),
                        media_status = 'materializing',
                        updated_at = now()
                    FROM updated_task
                    WHERE e.id = updated_task.event_id
                    RETURNING e.id
                )
                SELECT
                    EXISTS (SELECT 1 FROM updated_task) AS task_updated,
                    EXISTS (SELECT 1 FROM updated_event) AS event_updated
                """,
                params,
            )
            row = cur.fetchone()
    except Exception:
        logger.exception(
            "commit_replay_job_handoff failed event_id=%s owner=%s job_id=%s",
            event_id,
            owner,
            replay_job_id,
        )
        return False
    if row is None:
        return False
    if isinstance(row, dict):
        return bool(row.get("task_updated") and row.get("event_updated"))
    return bool(
        len(row) > 1
        and row[0]
        and row[1]
    )


def get_replay_slot_state(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
) -> dict[str, object] | None:
    """Read the durable Replay slot identity used by reclaim/recovery."""
    if not event_id:
        return None
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    event_id,
                    replay_slot_status,
                    replay_slot_owner,
                    replay_slot_token,
                    replay_slot_generation,
                    replay_create_state,
                    replay_create_started_at,
                    replay_create_committed_at,
                    replay_plan_hash,
                    replay_request_id,
                    replay_delivery_id,
                    replay_job_id,
                    replay_resulting_stream_id,
                    replay_slot_deadline_at
                FROM evidence_tasks
                WHERE event_id = %(event_id)s::uuid
                """,
                {"event_id": event_id},
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("get_replay_slot_state failed event_id=%s", event_id)
        return None
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    names = (
        "event_id",
        "replay_slot_status",
        "replay_slot_owner",
        "replay_slot_token",
        "replay_slot_generation",
        "replay_create_state",
        "replay_create_started_at",
        "replay_create_committed_at",
        "replay_plan_hash",
        "replay_request_id",
        "replay_delivery_id",
        "replay_job_id",
        "replay_resulting_stream_id",
        "replay_slot_deadline_at",
    )
    return {
        name: row[index] if index < len(row) else None
        for index, name in enumerate(names)
    }


def acquire_replay_slot(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    replay_job_id: str,
    source_id: str,
    camera_id: str,
    replay_shard: dict | None,
    sink_instance: str,
    resulting_stream_id: str,
    replay_duration_seconds_effective: float,
    replay_duration_effective_reason: str,
    timeout_budget_s: float,
) -> bool:
    """Persist an active completion-aware Replay slot."""
    if not event_id or not replay_job_id:
        return False
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return False
    replay_shard_id = _replay_shard_value(replay_shard, "shard_id")
    replay_api_url = _replay_shard_value(replay_shard, "replay_api_url")
    sink_url = _replay_shard_value(replay_shard, "replay_job_sink_url")
    duration_s = max(_safe_float(replay_duration_seconds_effective), 0.0)
    timeout_s = max(_safe_float(timeout_budget_s), 0.0)
    try:
        with cursor_factory() as cur:
            params = {
                "event_id": event_id,
                "replay_job_id": replay_job_id,
                "source_id": source_id,
                "camera_id": camera_id,
                "replay_shard_id": replay_shard_id,
                "replay_api_url": replay_api_url,
                "sink_url": sink_url,
                "sink_instance": sink_instance,
                "resulting_stream_id": resulting_stream_id,
                "duration_s": duration_s,
                "duration_reason": replay_duration_effective_reason,
                "timeout_s": timeout_s,
            }
            cur.execute(
                """
                UPDATE evidence_tasks
                SET replay_job_id = %(replay_job_id)s,
                    replay_shard_id = NULLIF(%(replay_shard_id)s::text, ''),
                    replay_api_url = NULLIF(%(replay_api_url)s::text, ''),
                    replay_job_sink_url = NULLIF(%(sink_url)s::text, ''),
                    replay_source_id = NULLIF(%(source_id)s::text, ''),
                    replay_resulting_stream_id = NULLIF(%(resulting_stream_id)s::text, ''),
                    replay_sink_instance = NULLIF(%(sink_instance)s::text, ''),
                    replay_duration_seconds_effective = %(duration_s)s,
                    replay_duration_effective_reason = %(duration_reason)s,
                    replay_slot_status = 'active',
                    replay_slot_acquired_at = now(),
                    replay_slot_deadline_at = now() + (
                        %(timeout_s)s::double precision * interval '1 second'
                    ),
                    replay_slot_released_at = NULL,
                    replay_slot_release_reason = NULL,
                    replay_slot_timeout_budget_s = %(timeout_s)s,
                    replay_slot_active_age_s = NULL,
                    replay_window = COALESCE(replay_window, '{}'::jsonb)
                        || jsonb_strip_nulls(jsonb_build_object(
                            'duration_seconds_effective', %(duration_s)s,
                            'duration_effective_reason',
                                %(duration_reason)s::text,
                            'resulting_stream_id', NULLIF(%(resulting_stream_id)s::text, '')
                        )),
                    materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                        || jsonb_build_object(
                            'replay_slot',
                            jsonb_strip_nulls(jsonb_build_object(
                                'status', 'active',
                                'event_id', %(event_id)s::text,
                                'replay_job_id', %(replay_job_id)s::text,
                                'source_id', NULLIF(%(source_id)s::text, ''),
                                'camera_id', NULLIF(%(camera_id)s::text, ''),
                                'replay_shard_id', NULLIF(%(replay_shard_id)s::text, ''),
                                'sink_url', NULLIF(%(sink_url)s::text, ''),
                                'sink_instance', NULLIF(%(sink_instance)s::text, ''),
                                'resulting_stream_id',
                                    NULLIF(%(resulting_stream_id)s::text, ''),
                                'replay_duration_seconds_effective', %(duration_s)s,
                                'replay_duration_effective_reason',
                                    %(duration_reason)s::text,
                                'acquired_at', now(),
                                'deadline_at', now() + (
                                    %(timeout_s)s::double precision * interval '1 second'
                                ),
                                'timeout_budget_s', %(timeout_s)s
                            ))
                        ),
                    updated_at = now()
                WHERE event_id = %(event_id)s::uuid
                """,
                params,
            )
            task_updated = bool(getattr(cur, "rowcount", 0) or 0)
            cur.execute(
                """
                UPDATE events
                SET payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'replay_slot_status', 'active',
                                'replay_job_id', %(replay_job_id)s::text,
                                'source_id', NULLIF(%(source_id)s::text, ''),
                                'camera_id', NULLIF(%(camera_id)s::text, ''),
                                'replay_shard_id', NULLIF(%(replay_shard_id)s::text, ''),
                                'replay_job_sink_url', NULLIF(%(sink_url)s::text, ''),
                                'sink_instance', NULLIF(%(sink_instance)s::text, ''),
                                'resulting_stream_id',
                                    NULLIF(%(resulting_stream_id)s::text, ''),
                                'replay_duration_seconds_effective', %(duration_s)s,
                                'replay_duration_effective_reason',
                                    %(duration_reason)s::text,
                                'replay_slot_acquired_at', now(),
                                'replay_slot_deadline_at', now() + (
                                    %(timeout_s)s::double precision * interval '1 second'
                                ),
                                'replay_slot_timeout_budget_s', %(timeout_s)s
                            ))
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                params,
            )
            return task_updated or bool(getattr(cur, "rowcount", 0) or 0)
    except Exception:
        logger.exception("acquire_replay_slot failed event_id=%s", event_id)
        return False


def release_replay_slot(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    release_reason: str,
    sink_video_to_stable_ms: int | None = None,
    finalization_duration_ms: int | None = None,
) -> bool:
    """Release an active Replay admission slot for one event."""
    if not event_id:
        return False
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return False
    try:
        with cursor_factory() as cur:
            params = {
                "event_id": event_id,
                "release_reason": release_reason,
                "sink_video_to_stable_ms": sink_video_to_stable_ms,
                "finalization_duration_ms": finalization_duration_ms,
            }
            cur.execute(
                """
                WITH released AS (
                    UPDATE evidence_tasks
                    SET replay_slot_status = 'released',
                        replay_slot_released_at = now(),
                        replay_slot_release_reason = %(release_reason)s,
                        replay_slot_active_age_s = EXTRACT(
                            EPOCH FROM (now() - replay_slot_acquired_at)
                        ),
                        sink_video_to_stable_ms = COALESCE(
                            %(sink_video_to_stable_ms)s::int,
                            sink_video_to_stable_ms
                        ),
                        finalization_duration_ms = COALESCE(
                            %(finalization_duration_ms)s::int,
                            finalization_duration_ms
                        ),
                        materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                            || jsonb_build_object(
                                'replay_slot',
                                jsonb_strip_nulls(jsonb_build_object(
                                    'status', 'released',
                                    'release_reason', %(release_reason)s::text,
                                    'released_at', now(),
                                    'active_age_s', EXTRACT(
                                        EPOCH FROM (now() - replay_slot_acquired_at)
                                    ),
                                    'sink_video_to_stable_ms',
                                        %(sink_video_to_stable_ms)s::int,
                                    'finalization_duration_ms',
                                        %(finalization_duration_ms)s::int,
                                    'replay_job_id', replay_job_id,
                                    'resulting_stream_id', replay_resulting_stream_id
                                ))
                            ),
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                      AND replay_slot_status = 'active'
                    RETURNING event_id,
                              replay_job_id,
                              replay_resulting_stream_id,
                              replay_slot_active_age_s
                )
                UPDATE events e
                SET payload = COALESCE(e.payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(e.payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'replay_slot_status', 'released',
                                'replay_slot_released_at', now(),
                                'replay_slot_release_reason',
                                    %(release_reason)s::text,
                                'replay_slot_active_age_s',
                                    released.replay_slot_active_age_s,
                                'sink_video_to_stable_ms',
                                    %(sink_video_to_stable_ms)s::int,
                                'finalization_duration_ms',
                                    %(finalization_duration_ms)s::int,
                                'replay_job_id', released.replay_job_id,
                                'resulting_stream_id',
                                    released.replay_resulting_stream_id
                            ))
                        ),
                    updated_at = now()
                FROM released
                WHERE e.id = released.event_id
                """,
                params,
            )
            released = bool(getattr(cur, "rowcount", 0) or 0)
            logger.info(
                "replay_slot_release_transition event_id=%s reason=%s result=%s",
                event_id,
                release_reason,
                "released" if released else "noop",
            )
            return released
    except Exception:
        logger.exception("release_replay_slot failed event_id=%s", event_id)
        return False


def expire_materialization_deadlines(pg_conn: psycopg.Connection) -> int:
    """Expire only Replay-owned waiting work after its business deadline."""
    if not callable(getattr(pg_conn, "cursor", None)):
        return 0
    try:
        lifecycle_fields = ""
        if _supports_lifecycle_v2(pg_conn):
            lifecycle_fields = """
                        materialization_phase = 'terminal',
                        materialization_phase_updated_at = now(),
                        materialization_owner = 'terminal',
                        materialization_next_attempt_at = NULL,
                        materialization_retry_reason = NULL,
                        materialization_defer_reason = NULL,
                        materialization_failure_reason = NULL,
                        materialization_lease_owner = NULL,
                        materialization_lease_token = NULL,
                        materialization_lease_expires_at = NULL,
                        materialization_lease_heartbeat_at = NULL,
            """
        with pg_conn.cursor() as cur:
            cur.execute(
                f"""
                WITH expired AS (
                    UPDATE evidence_tasks
                    SET status = 'materialization_expired',
                        materialization_status = 'materialization_expired',
                        {lifecycle_fields}
                        materialization_expired_reason = concat(
                            'materialization_deadline_expired:',
                            COALESCE(materialization_deadline_at::text, '')
                        ),
                        error_message = concat(
                            'materialization_deadline_expired:',
                            COALESCE(materialization_deadline_at::text, '')
                        ),
                        updated_at = now()
                    WHERE (
                        materialization_status IN (
                            'manifest_ready', 'materialization_pending'
                        )
                        OR status = ANY(%(replay_statuses)s)
                    )
                      AND materialization_deadline_at IS NOT NULL
                      AND materialization_deadline_at <= now()
                      AND COALESCE(replay_slot_status, '') <> 'active'
                      AND (
                          COALESCE(
                              to_jsonb(evidence_tasks)->>'materialization_owner',
                              ''
                          ) = 'replay'
                          OR (
                              COALESCE(
                                  to_jsonb(evidence_tasks)->>'materialization_owner',
                                  ''
                              ) = ''
                              AND status = ANY(%(replay_statuses)s)
                              AND COALESCE(
                                  materialization_audit->'rolling_cache'->>'status',
                                  ''
                              ) = ''
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM evidence_event_links l
                          WHERE l.event_id = evidence_tasks.event_id
                            AND l.relation = 'covered_by'
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM evidence_event_links l
                          WHERE l.bundle_event_id = evidence_tasks.event_id
                            AND l.relation = 'covered_by'
                      )
                    RETURNING event_id, materialization_deadline_at
                )
                UPDATE events e
                SET media_status = 'materialization_expired',
                    payload = COALESCE(e.payload, '{{}}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(e.payload->'media', '{{}}'::jsonb)
                            || jsonb_build_object(
                                'evidence_state', 'materialization_expired',
                                'evidence_reason',
                                    'materialization_deadline_expired',
                                'evidence_state_updated_at', now(),
                                'materialization_status',
                                    'materialization_expired',
                                'materialization_reason',
                                    'materialization_deadline_expired',
                                'materialization_expired_at', now()
                            )
                        ),
                    updated_at = now()
                WHERE e.id IN (SELECT event_id FROM expired)
                """,
                {"replay_statuses": sorted(REPLAY_OWNED_TASK_STATUSES)},
            )
            return int(getattr(cur, "rowcount", 0) or 0)
    except Exception:
        logger.exception("expire_materialization_deadlines failed")
        return 0


def get_evidence_diagnostics(pg_conn: psycopg.Connection, event_id: str) -> dict:
    """Return persisted clip-worker diagnostics for an event."""
    if not event_id:
        return {}
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return {}
    try:
        with cursor_factory() as cur:
            cur.execute(
                """
                SELECT payload->'media'->'evidence_diagnostics' AS diagnostics
                FROM events
                WHERE id = %(event_id)s::uuid
                """,
                {"event_id": event_id},
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("get_evidence_diagnostics failed event_id=%s", event_id)
        return {}
    if row is None:
        return {}
    if isinstance(row, dict):
        diagnostics = row.get("diagnostics")
    else:
        diagnostics = row[0] if row else None
    return diagnostics if isinstance(diagnostics, dict) else {}


def terminal_evidence_state(pg_conn: psycopg.Connection, event_id: str) -> str:
    """Return the terminal evidence state for an event, or empty when active."""
    if not event_id:
        return ""
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return ""
    try:
        with cursor_factory() as cur:
            cur.execute(
                """
                SELECT
                    e.media_status,
                    e.payload->'media'->>'evidence_state' AS evidence_state,
                    latest_task.status AS latest_task_status,
                    latest_task.materialization_status
                        AS latest_materialization_status
                FROM events e
                LEFT JOIN LATERAL (
                    SELECT et.status, et.materialization_status
                    FROM evidence_tasks et
                    WHERE et.event_id = e.id
                       OR et.source_event_id = e.source_event_id
                    ORDER BY et.updated_at DESC, et.created_at DESC, et.task_id DESC
                    LIMIT 1
                ) latest_task ON true
                WHERE e.id = %(event_id)s::uuid
                """,
                {"event_id": event_id},
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("terminal_evidence_state failed event_id=%s", event_id)
        return ""
    if row is None:
        return ""
    if isinstance(row, dict):
        values = (
            row.get("latest_materialization_status"),
            row.get("latest_task_status"),
            row.get("evidence_state"),
            row.get("media_status"),
        )
    else:
        values = tuple(row)
    for value in values:
        state = str(value or "")
        if state in TERMINAL_EVIDENCE_STATES:
            return state
    return ""


def record_request_target_exists(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    source_event_id: str = "",
) -> bool | None:
    """Return whether a record request still has a DB event/task target.

    ``None`` means the target could not be checked safely, so callers should
    keep the existing retry/failure behavior instead of acking the message.
    """
    event_id_text = str(event_id or "")
    source_event_id_text = str(source_event_id or "")
    if not event_id_text and not source_event_id_text:
        return None
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return None
    event_id_uuid = None
    if event_id_text:
        try:
            event_id_uuid = str(uuid.UUID(event_id_text))
        except ValueError:
            event_id_uuid = None
    try:
        with cursor_factory() as cur:
            cur.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM events
                        WHERE (
                            %(event_id_uuid)s::uuid IS NOT NULL
                            AND id = %(event_id_uuid)s::uuid
                        )
                        OR (
                            %(source_event_id)s::text <> ''
                            AND source_event_id = %(source_event_id)s::text
                        )
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM evidence_tasks
                        WHERE (
                            %(event_id_uuid)s::uuid IS NOT NULL
                            AND event_id = %(event_id_uuid)s::uuid
                        )
                        OR (
                            %(source_event_id)s::text <> ''
                            AND source_event_id = %(source_event_id)s::text
                        )
                    ) AS target_exists
                """,
                {
                    "event_id_uuid": event_id_uuid,
                    "source_event_id": source_event_id_text,
                },
            )
            row = cur.fetchone()
    except Exception:
        logger.exception(
            "record_request_target_exists failed event_id=%s source_event_id=%s",
            event_id_text,
            source_event_id_text,
        )
        return None
    if row is None:
        return None
    if isinstance(row, dict):
        value = row.get("target_exists")
    else:
        value = row[0] if row else None
    if value is None:
        return None
    return bool(value)


def _clip_transition_phase(materialization_status: str) -> str:
    if materialization_status in TERMINAL_MATERIALIZATION_STATUSES:
        return MaterializationPhase.TERMINAL.value
    # Replay admission has its own fenced slot lifecycle.  The shared
    # materialization phase remains waiting_ready until media takes ownership.
    return MaterializationPhase.WAITING_READY.value


def _clip_transition_owner(materialization_status: str) -> str:
    if materialization_status in TERMINAL_MATERIALIZATION_STATUSES:
        return "terminal"
    return "replay"


def _clip_expected_materialization_statuses(
    materialization_status: str,
) -> list[str]:
    expected = {
        MaterializationStatus.MANIFEST_READY.value,
        MaterializationStatus.PENDING.value,
        "pending",
        "waiting_proof",
        "queued",
    }
    if materialization_status != MaterializationStatus.PENDING.value:
        expected.update(
            {
                MaterializationStatus.RUNNING.value,
                "replay_job_created",
                "replaying",
            }
        )
    # Repeating the same durable transition is required for the crash window
    # where the task CAS committed but the event projection or Redis ACK did
    # not.  Other terminal states are never overwritten.
    expected.add(materialization_status)
    return sorted(expected)


def _normalized_clip_reason(
    *,
    status: str,
    materialization_status: str,
    explicit_reason: str,
    error_message: str,
) -> tuple[str, str]:
    detail = _reason_for_state(
        status,
        materialization_status,
        explicit_reason,
        error_message,
    )
    if not detail:
        return "", ""
    classified = classify_reason(detail)
    if classified.code != "unknown":
        return classified.code, detail
    # Explicit short identifiers are already stable reason codes.  Free-form
    # exception prose is retained only as diagnostic detail.
    if explicit_reason:
        candidate = explicit_reason.strip()
        if (
            candidate
            and len(candidate) <= 128
            and all(ch.isalnum() or ch in "_.-" for ch in candidate)
        ):
            return candidate, detail
    if materialization_status == MaterializationStatus.FAILED.value:
        return MaterializationStatus.FAILED.value, detail
    if materialization_status == MaterializationStatus.DEFERRED.value:
        return MaterializationStatus.DEFERRED.value, detail
    return str(status or materialization_status), detail


def update_clip_status(
    pg_conn: psycopg.Connection,
    event_id: str,
    status: str,
    replay_job_id: str = "",
    error_message: str = "",
    replay_job_request: dict | None = None,
    evidence_state: str | None = None,
    evidence_reason: str = "",
    request_id: str = "",
    attempt_count: int | None = None,
    diagnostics: dict | None = None,
    replay_shard: dict | None = None,
    materialization_deadline_at: str = "",
    quota_decision: dict | None = None,
    degrade_decision: dict | None = None,
) -> bool:
    """CAS the Replay-owned task first, then project operator-visible state.

    The task row is the durable scheduling authority.  Returning ``True``
    means both the task transition and event projection were persisted, so a
    caller may safely ACK its Redis delivery.  A failed/stale CAS returns
    ``False`` and must not be treated as a terminal delivery outcome.
    """
    if not event_id:
        return False
    state = evidence_state_for_status(status, evidence_state)
    materialization_state = materialization_status_for_state(state)
    phase = _clip_transition_phase(materialization_state)
    owner = _clip_transition_owner(materialization_state)
    compatibility_status = compatibility_status_for(materialization_state, phase)
    reason_code, reason_detail = _normalized_clip_reason(
        status=status,
        materialization_status=materialization_state,
        explicit_reason=evidence_reason,
        error_message=error_message,
    )
    diagnostics_payload = dict(diagnostics or {})
    if reason_detail and reason_detail != reason_code:
        diagnostics_payload.setdefault("lifecycle_reason_detail", reason_detail)
    expected_statuses = _clip_expected_materialization_statuses(
        materialization_state
    )
    params = {
        "status_text": status,
        "compatibility_status": compatibility_status,
        "replay_job_id": replay_job_id,
        "replay_job_request": _json_or_null(replay_job_request),
        "evidence_state": state,
        "materialization_status": materialization_state,
        "materialization_phase": phase,
        "materialization_owner": owner,
        "evidence_reason": reason_code,
        "error_message": error_message or reason_detail,
        "request_id": request_id,
        "attempt_count": attempt_count,
        "diagnostics": _json_or_null(diagnostics_payload),
        "quota_decision": _json_or_null(quota_decision or {}),
        "degrade_decision": _json_or_null(degrade_decision or {}),
        "materialization_deadline_at": materialization_deadline_at,
        "replay_shard_id": _replay_shard_value(replay_shard, "shard_id"),
        "replay_api_url": _replay_shard_value(replay_shard, "replay_api_url"),
        "replay_job_sink_url": _replay_shard_value(
            replay_shard,
            "replay_job_sink_url",
        ),
        "event_id": event_id,
        "expected_statuses": expected_statuses,
    }
    try:
        lifecycle_v2 = _supports_lifecycle_v2(pg_conn)
        with pg_conn.cursor() as cur:
            if lifecycle_v2:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(compatibility_status)s,
                        materialization_status = %(materialization_status)s,
                        materialization_phase = %(materialization_phase)s,
                        materialization_phase_updated_at = now(),
                        materialization_owner = %(materialization_owner)s,
                        materialization_next_attempt_at = CASE
                            WHEN %(materialization_status)s = 'materialization_pending'
                                THEN now()
                            ELSE NULL
                        END,
                        materialization_retry_reason = CASE
                            WHEN %(materialization_status)s = 'materialization_pending'
                                THEN NULLIF(%(evidence_reason)s, '')
                            ELSE NULL
                        END,
                        materialization_defer_reason = CASE
                            WHEN %(materialization_status)s = 'materialization_deferred'
                                THEN NULLIF(%(evidence_reason)s, '')
                            ELSE NULL
                        END,
                        materialization_failure_reason = CASE
                            WHEN %(materialization_status)s = 'materialization_failed'
                                THEN NULLIF(%(evidence_reason)s, '')
                            ELSE NULL
                        END,
                        materialization_expired_reason = CASE
                            WHEN %(materialization_status)s = 'materialization_expired'
                                THEN NULLIF(%(evidence_reason)s, '')
                            ELSE NULL
                        END,
                        materialization_lease_owner = NULL,
                        materialization_lease_token = NULL,
                        materialization_lease_expires_at = NULL,
                        materialization_lease_heartbeat_at = NULL,
                        materialization_handoff = '{}'::jsonb,
                        replay_job_id = COALESCE(
                            NULLIF(%(replay_job_id)s, ''), replay_job_id
                        ),
                        error_message = CASE
                            WHEN %(error_message)s <> '' THEN %(error_message)s
                            WHEN %(materialization_status)s = 'materialized' THEN NULL
                            ELSE error_message
                        END,
                        replay_shard_id = COALESCE(
                            NULLIF(%(replay_shard_id)s, ''), replay_shard_id
                        ),
                        replay_api_url = COALESCE(
                            NULLIF(%(replay_api_url)s, ''), replay_api_url
                        ),
                        replay_job_sink_url = COALESCE(
                            NULLIF(%(replay_job_sink_url)s, ''), replay_job_sink_url
                        ),
                        materialization_deadline_at = COALESCE(
                            NULLIF(%(materialization_deadline_at)s, '')::timestamptz,
                            materialization_deadline_at
                        ),
                        materialization_attempt_count = GREATEST(
                            materialization_attempt_count,
                            COALESCE(%(attempt_count)s, materialization_attempt_count)
                        ),
                        quota_decision = CASE
                            WHEN %(quota_decision)s::jsonb <> '{}'::jsonb
                                THEN %(quota_decision)s::jsonb
                            ELSE quota_decision
                        END,
                        degrade_decision = CASE
                            WHEN %(degrade_decision)s::jsonb <> '{}'::jsonb
                                THEN %(degrade_decision)s::jsonb
                            ELSE degrade_decision
                        END,
                        retry_count = GREATEST(
                            retry_count,
                            COALESCE(%(attempt_count)s, retry_count)
                        ),
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                      AND COALESCE(task_type, '') <> 'image_only'
                      AND COALESCE(
                            NULLIF(materialization_status, ''), status, ''
                          ) = ANY(%(expected_statuses)s)
                      AND (
                          materialization_owner = 'replay'
                          OR (
                              COALESCE(materialization_owner, '') = ''
                              AND COALESCE(
                                  materialization_audit->'rolling_cache'->>'status',
                                  ''
                              ) = ''
                          )
                          OR (
                              materialization_status = %(materialization_status)s
                              AND materialization_owner = 'terminal'
                          )
                      )
                    """,
                    params,
                )
            else:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(compatibility_status)s,
                        materialization_status = %(materialization_status)s,
                        replay_job_id = COALESCE(
                            NULLIF(%(replay_job_id)s, ''), replay_job_id
                        ),
                        error_message = CASE
                            WHEN %(error_message)s <> '' THEN %(error_message)s
                            WHEN %(materialization_status)s = 'materialized' THEN NULL
                            ELSE error_message
                        END,
                        replay_shard_id = COALESCE(
                            NULLIF(%(replay_shard_id)s::text, ''),
                            replay_shard_id
                        ),
                        replay_api_url = COALESCE(
                            NULLIF(%(replay_api_url)s::text, ''),
                            replay_api_url
                        ),
                        replay_job_sink_url = COALESCE(
                            NULLIF(%(replay_job_sink_url)s::text, ''),
                            replay_job_sink_url
                        ),
                        materialization_deadline_at = COALESCE(
                            NULLIF(%(materialization_deadline_at)s::text, '')::timestamptz,
                            materialization_deadline_at
                        ),
                        materialization_attempt_count = GREATEST(
                            materialization_attempt_count,
                            COALESCE(%(attempt_count)s::int, materialization_attempt_count)
                        ),
                        materialization_defer_reason = CASE
                            WHEN %(materialization_status)s::text = 'materialization_deferred'
                                THEN %(evidence_reason)s::text
                            ELSE NULL
                        END,
                        materialization_failure_reason = CASE
                            WHEN %(materialization_status)s::text = 'materialization_failed'
                                THEN %(evidence_reason)s::text
                            ELSE NULL
                        END,
                        materialization_expired_reason = CASE
                            WHEN %(materialization_status)s::text = 'materialization_expired'
                                THEN %(evidence_reason)s::text
                            ELSE NULL
                        END,
                        quota_decision = CASE
                            WHEN %(quota_decision)s::jsonb <> '{}'::jsonb
                                THEN %(quota_decision)s::jsonb
                            ELSE quota_decision
                        END,
                        degrade_decision = CASE
                            WHEN %(degrade_decision)s::jsonb <> '{}'::jsonb
                                THEN %(degrade_decision)s::jsonb
                            ELSE degrade_decision
                        END,
                        retry_count = GREATEST(
                            retry_count,
                            COALESCE(%(attempt_count)s::int, retry_count)
                        ),
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                      AND COALESCE(task_type, '') <> 'image_only'
                      AND COALESCE(
                            NULLIF(materialization_status, ''), status, ''
                          ) = ANY(%(expected_statuses)s)
                      AND COALESCE(
                            materialization_audit->'rolling_cache'->>'status',
                            ''
                          ) = ''
                    """,
                    params,
                )
            task_updated = bool(cur.rowcount and cur.rowcount > 0)
            if not task_updated:
                logger.warning(
                    "clip_task_transition_cas_missed event_id=%s status=%s "
                    "materialization_status=%s",
                    event_id,
                    status,
                    materialization_state,
                )
                return False

            cur.execute(
                """
                UPDATE events
                SET payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'clip_status', %(status_text)s::text,
                                'recording_strategy', 'savant_replay',
                                'replay_job_id',
                                    NULLIF(%(replay_job_id)s::text, ''),
                                'replay_job_request', %(replay_job_request)s::jsonb,
                                'evidence_state', %(evidence_state)s::text,
                                'evidence_reason',
                                    NULLIF(%(evidence_reason)s::text, ''),
                                'evidence_state_updated_at', now(),
                                'materialization_status',
                                    %(materialization_status)s::text,
                                'materialization_phase',
                                    %(materialization_phase)s::text,
                                'materialization_owner',
                                    %(materialization_owner)s::text,
                                'materialization_reason',
                                    NULLIF(%(evidence_reason)s::text, ''),
                                'materialization_deadline_at',
                                    NULLIF(
                                        %(materialization_deadline_at)s::text,
                                        ''
                                    ),
                                'quota_decision', %(quota_decision)s::jsonb,
                                'degrade_decision', %(degrade_decision)s::jsonb,
                                'evidence_request_id',
                                    NULLIF(%(request_id)s::text, ''),
                                'evidence_attempt_count', %(attempt_count)s::int,
                                'evidence_diagnostics', %(diagnostics)s::jsonb,
                                'replay_shard_id',
                                    NULLIF(%(replay_shard_id)s::text, ''),
                                'replay_api_url',
                                    NULLIF(%(replay_api_url)s::text, ''),
                                'replay_job_sink_url',
                                    NULLIF(%(replay_job_sink_url)s::text, ''),
                                'error_message',
                                    NULLIF(%(error_message)s::text, '')
                            ))
                        ),
                    media_status = %(evidence_state)s::text,
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                params,
            )
            event_updated = bool(cur.rowcount and cur.rowcount > 0)
            if not event_updated:
                logger.error(
                    "clip_event_projection_missing event_id=%s status=%s",
                    event_id,
                    status,
                )
            return event_updated
    except Exception:
        logger.exception("update_clip_status failed event_id=%s status=%s", event_id, status)
        return False


def update_evidence_media_result(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    task_id: str | None,
    media_status: str,
    snapshot_status: str,
    metadata_status: str,
    clip_status: str,
    snapshot_path: str | None,
    metadata_path: str,
    output_root: str,
    clip_path: str | None = None,
    storage_fallback_used: bool = False,
    storage_fallback_reason: str | None = None,
    error_message: str | None = None,
) -> bool:
    """Update events and evidence_tasks after midterm metadata/snapshot output."""
    if not event_id:
        return False
    evidence_state = evidence_state_for_status(media_status)
    materialization_state = materialization_status_for_state(evidence_state)
    evidence_reason = error_message or (media_status if media_status != evidence_state else "")
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET snapshot_path = COALESCE(%(snapshot_path)s, snapshot_path),
                    clip_path = COALESCE(%(clip_path)s, clip_path),
                    media_status = %(media_status)s,
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'snapshot_status', %(snapshot_status)s::text,
                                'metadata_status', %(metadata_status)s::text,
                                'clip_status', %(clip_status)s::text,
                                'snapshot_path', %(snapshot_path)s::text,
                                'clip_path', %(clip_path)s::text,
                                'metadata_path', %(metadata_path)s::text,
                                'raw_clip_path', %(clip_path)s::text,
                                'annotated_clip_path', NULL,
                                'evidence_state', %(evidence_state)s::text,
                                'evidence_reason', NULLIF(%(evidence_reason)s::text, ''),
                                'evidence_state_updated_at', now(),
                                'materialization_status', %(materialization_status)s::text,
                                'materialization_reason', NULLIF(%(evidence_reason)s::text, ''),
                                'clip_error_message', %(clip_error_message)s::text,
                                'error_message', %(error_message)s::text
                            )
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "event_id": event_id,
                    "media_status": media_status,
                    "evidence_state": evidence_state,
                    "materialization_status": materialization_state,
                    "evidence_reason": evidence_reason,
                    "snapshot_status": snapshot_status,
                    "metadata_status": metadata_status,
                    "clip_status": clip_status,
                    "snapshot_path": snapshot_path,
                    "clip_path": clip_path,
                    "metadata_path": metadata_path,
                    "clip_error_message": error_message
                    if clip_status in ("failed", "not_implemented")
                    else None,
                    "error_message": error_message,
                },
            )
            event_updated = cur.rowcount is not None and cur.rowcount > 0

            if task_id or event_updated:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(materialization_status)s,
                        materialization_status = %(materialization_status)s,
                        snapshot_path = COALESCE(%(snapshot_path)s, snapshot_path),
                        clip_path = COALESCE(%(clip_path)s, clip_path),
                        metadata_path = %(metadata_path)s,
                        output_root = %(output_root)s,
                        storage_fallback_used = %(storage_fallback_used)s,
                        storage_fallback_reason = %(storage_fallback_reason)s,
                        error_message = %(evidence_reason)s,
                        updated_at = now()
                    WHERE (%(task_id)s IS NOT NULL AND task_id = %(task_id)s)
                       OR (%(task_id)s IS NULL AND event_id = %(event_id)s::uuid)
                    """,
                    {
                        "task_id": task_id,
                        "event_id": event_id,
                        "evidence_state": evidence_state,
                        "materialization_status": materialization_state,
                        "snapshot_path": snapshot_path,
                        "clip_path": clip_path,
                        "metadata_path": metadata_path,
                        "output_root": output_root,
                        "storage_fallback_used": storage_fallback_used,
                        "storage_fallback_reason": storage_fallback_reason,
                        "evidence_reason": evidence_reason,
                    },
                )
            return event_updated
    except Exception:
        logger.exception(
            "update_evidence_media_result failed event_id=%s status=%s",
            event_id,
            media_status,
        )
        return False
