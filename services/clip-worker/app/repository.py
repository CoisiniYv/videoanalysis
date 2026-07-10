"""Clip-worker repository — update media status in events table."""

from __future__ import annotations

import json
import logging
import uuid

import psycopg

logger = logging.getLogger(__name__)

EVIDENCE_STATES = {
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
    if state in {
        "manifest_ready",
        "materialization_pending",
        "materializing",
        "materialized",
        "materialization_deferred",
        "materialization_failed",
        "materialization_expired",
        "materialization_skipped",
    }:
        return state
    if state in {"pending", "queued", "waiting_proof", "replaying", "finalizing"}:
        return "materialization_pending"
    if state == "ready":
        return "materialized"
    if state == "failed":
        return "materialization_failed"
    return "manifest_ready"


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
    """Mark deferred or pending manifest-first tasks expired after TTL deadline."""
    if not callable(getattr(pg_conn, "cursor", None)):
        return 0
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                WITH expired AS (
                    UPDATE evidence_tasks
                    SET status = 'materialization_expired',
                        materialization_status = 'materialization_expired',
                        materialization_expired_reason = concat(
                            'materialization_deadline_expired:',
                            COALESCE(materialization_deadline_at::text, '')
                        ),
                        error_message = concat(
                            'materialization_deadline_expired:',
                            COALESCE(materialization_deadline_at::text, '')
                        ),
                        updated_at = now()
                    WHERE materialization_status IN (
                        'manifest_ready',
                        'materialization_pending',
                        'materialization_deferred',
                        'waiting_proof',
                        'queued',
                        'replay_job_created',
                        'replaying',
                        'materializing',
                        'finalizing'
                    )
                      AND materialization_deadline_at IS NOT NULL
                      AND materialization_deadline_at <= now()
                      AND NOT (
                          materialization_status = 'materializing'
                          AND COALESCE(
                              materialization_audit->'rolling_cache'->>'status',
                              ''
                          ) = 'materializing'
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
                    payload = COALESCE(e.payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(e.payload->'media', '{}'::jsonb)
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
                """
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
    """Set clip status and synchronize operator-visible evidence state."""
    if not event_id:
        return False
    state = evidence_state_for_status(status, evidence_state)
    materialization_state = materialization_status_for_state(state)
    reason = _reason_for_state(status, state, evidence_reason, error_message)
    try:
        with pg_conn.cursor() as cur:
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
                                'replay_job_id', NULLIF(%(replay_job_id)s::text, ''),
                                'evidence_state', %(evidence_state)s::text,
                                'evidence_reason', NULLIF(%(evidence_reason)s::text, ''),
                                'evidence_state_updated_at', now(),
                                'materialization_status', %(materialization_status)s::text,
                                'materialization_reason', NULLIF(%(evidence_reason)s::text, ''),
                                'materialization_deadline_at',
                                    NULLIF(%(materialization_deadline_at)s::text, ''),
                                'quota_decision', %(quota_decision)s::jsonb,
                                'degrade_decision', %(degrade_decision)s::jsonb,
                                'evidence_request_id', NULLIF(%(request_id)s::text, ''),
                                'evidence_attempt_count', %(attempt_count)s::int,
                                'evidence_diagnostics', %(diagnostics)s::jsonb,
                                'replay_shard_id', NULLIF(%(replay_shard_id)s::text, ''),
                                'replay_api_url', NULLIF(%(replay_api_url)s::text, ''),
                                'replay_job_sink_url', NULLIF(%(replay_job_sink_url)s::text, ''),
                                'error_message', NULLIF(%(error_message)s::text, '')
                            ))
                        ),
                    media_status = %(evidence_state)s,
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "status_text": status,
                    "replay_job_id": replay_job_id,
                    "evidence_state": state,
                    "materialization_status": materialization_state,
                    "evidence_reason": reason,
                    "request_id": request_id,
                    "attempt_count": attempt_count,
                    "diagnostics": _json_or_null(diagnostics),
                    "quota_decision": _json_or_null(quota_decision or {}),
                    "degrade_decision": _json_or_null(degrade_decision or {}),
                    "materialization_deadline_at": materialization_deadline_at,
                    "replay_shard_id": _replay_shard_value(replay_shard, "shard_id"),
                    "replay_api_url": _replay_shard_value(replay_shard, "replay_api_url"),
                    "replay_job_sink_url": _replay_shard_value(
                        replay_shard,
                        "replay_job_sink_url",
                    ),
                    "error_message": error_message,
                    "event_id": event_id,
                },
            )
            event_updated = cur.rowcount is not None and cur.rowcount > 0
            if replay_job_request is not None:
                cur.execute(
                    """
                    UPDATE events
                    SET payload = jsonb_set(
                            COALESCE(payload, '{}'::jsonb),
                            '{media,replay_job_request}',
                            %(request)s::jsonb
                        ),
                        updated_at = now()
                    WHERE id = %(event_id)s::uuid
                    """,
                    {
                        "request": json.dumps(replay_job_request),
                        "event_id": event_id,
                    },
                )
            if event_updated:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(materialization_status)s,
                        materialization_status = %(materialization_status)s,
                        error_message = CASE
                            WHEN %(evidence_reason)s::text != ''
                                THEN %(evidence_reason)s::text
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
                            ELSE materialization_defer_reason
                        END,
                        materialization_failure_reason = CASE
                            WHEN %(materialization_status)s::text = 'materialization_failed'
                                THEN %(evidence_reason)s::text
                            ELSE materialization_failure_reason
                        END,
                        materialization_expired_reason = CASE
                            WHEN %(materialization_status)s::text = 'materialization_expired'
                                THEN %(evidence_reason)s::text
                            ELSE materialization_expired_reason
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
                    """,
                    {
                        "event_id": event_id,
                        "evidence_state": state,
                        "materialization_status": materialization_state,
                        "evidence_reason": reason,
                        "attempt_count": attempt_count,
                        "replay_shard_id": _replay_shard_value(
                            replay_shard,
                            "shard_id",
                        ),
                        "replay_api_url": _replay_shard_value(
                            replay_shard,
                            "replay_api_url",
                        ),
                        "replay_job_sink_url": _replay_shard_value(
                            replay_shard,
                            "replay_job_sink_url",
                        ),
                        "materialization_deadline_at": materialization_deadline_at,
                        "quota_decision": _json_or_null(quota_decision or {}),
                        "degrade_decision": _json_or_null(degrade_decision or {}),
                    },
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
