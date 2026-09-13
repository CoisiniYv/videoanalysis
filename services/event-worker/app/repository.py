"""EventRepository — idempotent PostgreSQL event insertion."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import psycopg
from psycopg.rows import dict_row

from libs.evidence_lifecycle import (
    ACTIVE_COMPATIBILITY_TASK_STATUSES,
    ACTIVE_MATERIALIZATION_STATUSES,
    MaterializationPhase,
    NormalizedReason,
    OPERATOR_EVIDENCE_STATES as CANONICAL_OPERATOR_EVIDENCE_STATES,
    materialization_status_for_operator_state,
)


_INSERT_SQL = """
INSERT INTO events (
    source_event_id,
    event_type,
    camera_id,
    source_id,
    track_id,
    person_id,
    algorithm_type,
    algorithm_version,
    severity,
    confidence,
    start_ts_ms,
    end_ts_ms,
    start_ts,
    end_ts,
    event_ts_ms,
    frame_uuid,
    keyframe_uuid,
    snapshot_path,
    clip_path,
    snapshot_required,
    clip_required,
    evidence_policy,
    recording_strategy,
    media_status,
    status,
    payload
) VALUES (
    %(source_event_id)s,
    %(event_type)s,
    %(camera_id)s,
    %(source_id)s,
    %(track_id)s,
    %(person_id)s,
    %(algorithm_type)s,
    %(algorithm_version)s,
    %(severity)s,
    %(confidence)s,
    %(start_ts_ms)s,
    %(end_ts_ms)s,
    to_timestamp(%(start_ts_ms)s::double precision / 1000.0),
    to_timestamp(%(end_ts_ms)s::double precision / 1000.0),
    %(event_ts_ms)s,
    %(frame_uuid)s,
    %(keyframe_uuid)s,
    %(snapshot_path)s,
    %(clip_path)s,
    %(snapshot_required)s,
    %(clip_required)s,
    %(evidence_policy)s::jsonb,
    %(recording_strategy)s,
    %(media_status)s,
    %(status)s,
    %(payload)s::jsonb
)
ON CONFLICT (source_event_id) DO NOTHING
RETURNING id
"""

_SELECT_BY_SID_SQL = """
SELECT id, source_event_id FROM events WHERE source_event_id = %s
"""

_COUNT_BY_SID_SQL = """
SELECT COUNT(*) FROM events WHERE source_event_id = %s
"""

_INSERT_PERSON_BBOX_OBSERVATION_SQL = """
INSERT INTO person_bbox_observations (
    source_observation_id,
    source_id,
    camera_id,
    track_id,
    timestamp_ms,
    frame_pts,
    frame_num,
    person_bbox,
    person_confidence,
    gate_status,
    payload
) VALUES (
    %(source_observation_id)s,
    %(source_id)s,
    %(camera_id)s,
    %(track_id)s,
    %(timestamp_ms)s,
    %(frame_pts)s,
    %(frame_num)s,
    %(person_bbox)s::jsonb,
    %(person_confidence)s,
    %(gate_status)s,
    %(payload)s::jsonb
)
ON CONFLICT (source_observation_id) DO NOTHING
RETURNING id
"""

_INSERT_PERSON_BBOX_OBSERVATIONS_SQL = """
WITH input_rows AS (
    SELECT *
    FROM jsonb_to_recordset(%(rows)s::jsonb) AS item (
        ordinal INTEGER,
        source_observation_id TEXT,
        source_id TEXT,
        camera_id TEXT,
        track_id TEXT,
        timestamp_ms BIGINT,
        frame_pts BIGINT,
        frame_num INTEGER,
        person_bbox JSONB,
        person_confidence DOUBLE PRECISION,
        gate_status TEXT,
        payload JSONB
    )
), ranked_rows AS (
    SELECT input_rows.*,
           row_number() OVER (
               PARTITION BY source_observation_id ORDER BY ordinal
           ) AS duplicate_rank
    FROM input_rows
), inserted AS (
    INSERT INTO person_bbox_observations (
        source_observation_id,
        source_id,
        camera_id,
        track_id,
        timestamp_ms,
        frame_pts,
        frame_num,
        person_bbox,
        person_confidence,
        gate_status,
        payload
    )
    SELECT source_observation_id,
           source_id,
           camera_id,
           track_id,
           timestamp_ms,
           frame_pts,
           frame_num,
           person_bbox,
           person_confidence,
           gate_status,
           payload
    FROM ranked_rows
    WHERE duplicate_rank = 1
    ON CONFLICT (source_observation_id) DO NOTHING
    RETURNING source_observation_id, id
)
SELECT ranked_rows.ordinal,
       CASE WHEN ranked_rows.duplicate_rank = 1 THEN inserted.id END AS id
FROM ranked_rows
LEFT JOIN inserted USING (source_observation_id)
ORDER BY ranked_rows.ordinal
"""


def _person_bbox_observation_params(observation: Dict[str, Any]) -> dict[str, Any]:
    payload = observation.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return {
        "source_observation_id": observation.get("source_observation_id", ""),
        "source_id": observation.get("source_id", ""),
        "camera_id": observation.get("camera_id", ""),
        "track_id": observation.get("track_id") or None,
        "timestamp_ms": int(observation.get("timestamp_ms", 0)),
        "frame_pts": observation.get("frame_pts"),
        "frame_num": observation.get("frame_num"),
        "person_bbox": json.dumps(
            observation.get("person_bbox"),
            ensure_ascii=False,
        ),
        "person_confidence": observation.get("person_confidence"),
        "gate_status": observation.get("gate_status") or "accepted",
        "payload": json.dumps(payload, ensure_ascii=False),
    }

EVIDENCE_TASK_STATUSES = (
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
    "processing",
    "ready",
    "partial",
    "failed",
    "not_implemented",
)

OPERATOR_EVIDENCE_STATES = {
    *CANONICAL_OPERATOR_EVIDENCE_STATES,
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
    "not_implemented",
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


def _evidence_state_for_status(status: str) -> str:
    state = _STATUS_TO_EVIDENCE_STATE.get(str(status), str(status))
    return state if state in OPERATOR_EVIDENCE_STATES else "failed"


def _csv_env(name: str, default: str = "") -> tuple[str, ...]:
    value = os.getenv(name, default)
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int_map_env(name: str, default: str = "") -> dict[str, int]:
    raw = os.getenv(name, default)
    result: dict[str, int] = {}
    for part in raw.split(","):
        item = part.strip()
        if not item or ":" not in item:
            continue
        key, value = item.split(":", 1)
        key = key.strip()
        if not key:
            continue
        try:
            parsed = int(value.strip())
        except ValueError:
            continue
        if parsed > 0:
            result[key] = parsed
    return result


def _is_high_priority_event(event: Dict[str, Any]) -> bool:
    event_type = str(event.get("event_type") or "")
    priority_types = set(
        _csv_env(
            "EVIDENCE_HIGH_PRIORITY_EVENT_TYPES",
            "watchlist_hit,live_search_hit",
        )
    )
    if event_type in priority_types:
        return True
    policy = event.get("evidence_policy") if isinstance(event.get("evidence_policy"), dict) else {}
    return str(policy.get("priority") or "").strip().lower() in {"high", "critical"}


def _materialization_initial_status(event: Dict[str, Any]) -> str:
    if _bool_env("EVIDENCE_MATERIALIZATION_DEFER_LOW_PRIORITY", "false") and not _is_high_priority_event(event):
        return "manifest_ready"
    return "materialization_pending"


def _materialization_priority(event: Dict[str, Any]) -> int:
    policy = event.get("evidence_policy") if isinstance(event.get("evidence_policy"), dict) else {}
    value = policy.get("priority")
    if isinstance(value, int):
        return value
    text = str(value or "").strip().lower()
    if text in {"critical", "high"}:
        return 100
    if _is_high_priority_event(event):
        return 100
    if text == "low":
        return 10
    return 50


def _runtime_epoch_id_for_task(event: Dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    for value in (
        event.get("runtime_epoch_id"),
        payload.get("runtime_epoch_id"),
        media.get("runtime_epoch_id"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _materialization_owner_for_task(
    event: Dict[str, Any],
    materialization_status: str,
) -> str:
    if materialization_status not in ACTIVE_MATERIALIZATION_STATUSES:
        return "terminal"
    if _is_image_only_evidence(event) or _bool_env(
        "ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS",
        "false",
    ):
        return "rolling"
    return "replay"


def _payload_media(event: Dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    return media


def _is_face_match_event(event: Dict[str, Any]) -> bool:
    return event.get("algorithm_type") == "face_intelligence" or event.get(
        "event_type"
    ) in {"watchlist_hit", "live_search_hit"}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _is_image_only_evidence(event: Dict[str, Any]) -> bool:
    policy = event.get("evidence_policy") if isinstance(event.get("evidence_policy"), dict) else {}
    media = _payload_media(event)
    playback_kind = str(
        policy.get("playback_kind") or media.get("playback_kind") or ""
    ).strip().lower()
    evidence_mode = str(
        policy.get("evidence_mode") or media.get("evidence_mode") or ""
    ).strip().lower()
    clip_required = (
        _truthy(event.get("clip_required"))
        or _truthy(policy.get("clip_required"))
        or _truthy(media.get("clip_required"))
    )
    return _is_face_match_event(event) and not clip_required and (
        playback_kind == "image" or evidence_mode == "image_only"
    )


def _source_observation_id_from_event(event: Dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    match = payload.get("match") if isinstance(payload.get("match"), dict) else {}
    for value in (
        match.get("source_observation_id"),
        payload.get("source_observation_id"),
        event.get("source_observation_id"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _media_uri(path: Any) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    if text.startswith("/media/"):
        return text
    media_root = os.getenv("MEDIA_ROOT", "/data/video-analytics/media").rstrip("/")
    if media_root and text.startswith(media_root + "/"):
        return "/media/" + text[len(media_root) + 1 :].lstrip("/")
    return text


def _content_type_for_path(path: str) -> str:
    suffix = str(path or "").rsplit(".", 1)[-1].lower()
    if suffix in {"jpg", "jpeg"}:
        return "image/jpeg"
    if suffix == "png":
        return "image/png"
    if suffix == "webp":
        return "image/webp"
    return "image/jpeg"


ACTIVE_ADMISSION_STATUSES = (
    *sorted(ACTIVE_COMPATIBILITY_TASK_STATUSES),
)
# Rolling evidence V2 records outstanding work in `materialization_status`, and
# a queued task sits in `materialization_pending` -- a value the legacy
# compatibility set does not contain. Counting only the legacy column made
# every queued task invisible to admission, so a cap fired only during the
# brief window in which a task happened to be claimed.
ACTIVE_ADMISSION_MATERIALIZATION_STATUSES = (
    *sorted(ACTIVE_MATERIALIZATION_STATUSES),
)


def _coverage_merge_enabled() -> bool:
    return _bool_env("EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED", "false")


def _coverage_merge_event_types() -> set[str]:
    return set(
        _csv_env(
            "EVIDENCE_EVENT_COVERAGE_EVENT_TYPES",
            "intrusion,watchlist_hit,live_search_hit",
        )
    )


def _coverage_window_ms() -> int:
    return max(0, _int_env("EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS", 30)) * 1000


def _coverage_parent_max_duration_seconds() -> int:
    return max(0, _int_env("EVIDENCE_COVERAGE_PARENT_MAX_DURATION_SECONDS", 60))


def _source_limit_mode() -> str:
    """How `EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE` is enforced.

    `execution_concurrency` (default) keeps the task in the durable queue and
    leaves the cap to the media-worker source slots. `skip` is the legacy
    behaviour that writes `materialization_skipped` instead.
    """

    mode = os.getenv(
        "EVIDENCE_ADMISSION_SOURCE_LIMIT_MODE",
        "execution_concurrency",
    ).strip().lower()
    return "skip" if mode == "skip" else "execution_concurrency"


def _active_admission_count(
    conn: psycopg.Connection,
    *,
    source_id: str = "",
    event_type: str = "",
) -> int:
    clauses = [
        "("
        "status = ANY(%(statuses)s)"
        " OR materialization_status = ANY(%(materialization_statuses)s)"
        ")"
    ]
    params: dict[str, Any] = {
        "statuses": list(ACTIVE_ADMISSION_STATUSES),
        "materialization_statuses": list(
            ACTIVE_ADMISSION_MATERIALIZATION_STATUSES
        ),
    }
    if source_id:
        clauses.append("source_id = %(source_id)s")
        params["source_id"] = source_id
    if event_type:
        clauses.append("event_type = %(event_type)s")
        params["event_type"] = event_type
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM evidence_tasks
            WHERE {' AND '.join(clauses)}
            """,
            params,
        )
        row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _coverage_parent_event_id(
    conn: psycopg.Connection,
    *,
    event_id: str,
    source_id: str,
    event_type: str,
    event_ts_ms: int,
) -> str | None:
    if (
        not _coverage_merge_enabled()
        or not source_id
        or event_type not in _coverage_merge_event_types()
        or event_ts_ms <= 0
    ):
        return None
    window_ms = _coverage_window_ms()
    if window_ms <= 0:
        return None
    terminal_statuses = (
        "materialization_expired",
        "materialization_failed",
        "materialization_skipped",
        "failed",
        "not_implemented",
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(link.bundle_event_id, et.event_id)::text AS bundle_event_id
            FROM evidence_tasks et
            JOIN events e ON e.id = et.event_id
            LEFT JOIN evidence_event_links link
              ON link.event_id = et.event_id
             AND link.relation = 'covered_by'
            WHERE et.event_id <> %(event_id)s::uuid
              AND et.source_id = %(source_id)s
              AND et.event_type = ANY(%(event_types)s)
              AND %(event_ts_ms)s BETWEEN
                    et.event_ts_ms
                    - (GREATEST(COALESCE(et.pre_seconds, 0), 0) * 1000)
                    - %(merge_slack_ms)s
                  AND
                    et.event_ts_ms
                    + (GREATEST(COALESCE(et.post_seconds, 0), 0) * 1000)
                    + %(merge_slack_ms)s
              AND COALESCE(et.materialization_status, et.status, '') <> ALL(%(terminal_statuses)s)
            ORDER BY (et.event_type <> %(event_type)s),
                     ABS(et.event_ts_ms - %(event_ts_ms)s),
                     et.created_at ASC
            LIMIT 1
            """,
            {
                "event_id": event_id,
                "source_id": source_id,
                "event_type": event_type,
                "event_types": sorted(_coverage_merge_event_types()),
                "event_ts_ms": event_ts_ms,
                "merge_slack_ms": window_ms,
                "start_ms": event_ts_ms - window_ms,
                "end_ms": event_ts_ms + window_ms,
                "terminal_statuses": list(terminal_statuses),
            },
        )
        row = cur.fetchone()
    if not row:
        return None
    return str(row[0] or "") or None


def _upsert_evidence_event_link(
    conn: psycopg.Connection,
    *,
    event_id: str,
    bundle_event_id: str,
    reason: str,
    metadata: dict[str, Any],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO evidence_event_links (
                event_id, bundle_event_id, relation, reason, metadata
            ) VALUES (
                %(event_id)s::uuid,
                %(bundle_event_id)s::uuid,
                'covered_by',
                %(reason)s,
                %(metadata)s::jsonb
            )
            ON CONFLICT (event_id) DO UPDATE SET
                bundle_event_id = EXCLUDED.bundle_event_id,
                relation = EXCLUDED.relation,
                reason = EXCLUDED.reason,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """,
            {
                "event_id": event_id,
                "bundle_event_id": bundle_event_id,
                "reason": reason,
                "metadata": json.dumps(metadata, ensure_ascii=False),
            },
        )


def _extend_coverage_parent_window(
    conn: psycopg.Connection,
    *,
    parent_event_id: str,
    child_event_ts_ms: int,
    child_pre_seconds: int,
    child_post_seconds: int,
) -> dict[str, Any]:
    if not parent_event_id or child_event_ts_ms <= 0:
        return {"extended": False}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT event_ts_ms, pre_seconds, post_seconds, materialization_status,
                   COALESCE(to_jsonb(evidence_tasks)->>'materialization_phase', '')
                       AS materialization_phase,
                   COALESCE(to_jsonb(evidence_tasks)->>'materialization_lease_token', '')
                       AS materialization_lease_token
            FROM evidence_tasks
            WHERE event_id = %(parent_event_id)s::uuid
            FOR UPDATE
            """,
            {"parent_event_id": parent_event_id},
        )
        row = cur.fetchone()
        if not row:
            return {"extended": False, "reason": "parent_missing"}
        parent_status = str(row.get("materialization_status") or "")
        if parent_status not in {"manifest_ready", "materialization_pending"}:
            return {"extended": False, "reason": "parent_not_waiting"}
        if str(row.get("materialization_lease_token") or ""):
            return {"extended": False, "reason": "parent_already_leased"}
        parent_phase = str(row.get("materialization_phase") or "")
        if parent_phase and parent_phase not in {
            MaterializationPhase.WAITING_READY.value,
            MaterializationPhase.WAITING_COVERAGE.value,
        }:
            return {"extended": False, "reason": "parent_not_waiting"}

        parent_ts_ms = int(row.get("event_ts_ms") or 0)
        if parent_ts_ms <= 0:
            return {"extended": False, "reason": "parent_missing_event_ts_ms"}
        parent_pre = int(row.get("pre_seconds") or 0)
        parent_post = int(row.get("post_seconds") or 0)
        child_start_ms = child_event_ts_ms - max(0, child_pre_seconds) * 1000
        child_end_ms = child_event_ts_ms + max(0, child_post_seconds) * 1000
        required_pre = max(
            parent_pre,
            max(0, parent_ts_ms - child_start_ms + 999) // 1000,
        )
        required_post = max(
            parent_post,
            max(0, child_end_ms - parent_ts_ms + 999) // 1000,
        )
        max_duration_seconds = _coverage_parent_max_duration_seconds()
        if (
            max_duration_seconds > 0
            and required_pre + required_post > max_duration_seconds
        ):
            return {
                "extended": False,
                "reason": "parent_max_duration_exceeded",
                "parent_pre_seconds": parent_pre,
                "parent_post_seconds": parent_post,
                "required_pre_seconds": required_pre,
                "required_post_seconds": required_post,
                "max_duration_seconds": max_duration_seconds,
            }
        if required_pre == parent_pre and required_post == parent_post:
            return {
                "extended": False,
                "reason": "already_covered",
                "parent_pre_seconds": parent_pre,
                "parent_post_seconds": parent_post,
            }
        cur.execute(
            """
            UPDATE evidence_tasks
            SET pre_seconds = %(pre_seconds)s,
                post_seconds = %(post_seconds)s,
                replay_window = COALESCE(replay_window, '{}'::jsonb)
                    || jsonb_build_object(
                        'pre_seconds', %(pre_seconds)s,
                        'post_seconds', %(post_seconds)s,
                        'coverage_extended', true,
                        'coverage_extended_at', now()
                    ),
                materialization_ready_at = CASE
                    WHEN event_ts_ms BETWEEN 946684800000 AND 4102444800000
                        THEN to_timestamp(event_ts_ms::double precision / 1000.0)
                            + (
                                %(post_seconds)s::double precision
                                + %(ready_grace_s)s::double precision
                            ) * interval '1 second'
                    ELSE materialization_ready_at
                END,
                materialization_audit = COALESCE(materialization_audit, '{}'::jsonb)
                    || jsonb_build_object(
                        'coverage_extension',
                        jsonb_build_object(
                            'extended_at', now(),
                            'child_event_ts_ms', %(child_event_ts_ms)s,
                            'pre_seconds', %(pre_seconds)s,
                            'post_seconds', %(post_seconds)s
                        )
                    ),
                updated_at = now()
            WHERE event_id = %(parent_event_id)s::uuid
              AND materialization_status IN (
                  'manifest_ready', 'materialization_pending'
              )
              AND COALESCE(
                  to_jsonb(evidence_tasks)->>'materialization_lease_token',
                  ''
              ) = ''
              AND COALESCE(
                  to_jsonb(evidence_tasks)->>'materialization_phase',
                  'waiting_ready'
              ) IN ('waiting_ready', 'waiting_coverage')
            """,
            {
                "parent_event_id": parent_event_id,
                "child_event_ts_ms": child_event_ts_ms,
                "pre_seconds": required_pre,
                "post_seconds": required_post,
                "ready_grace_s": max(
                    0.0,
                    _float_env(
                        "EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS",
                        0.0,
                    ),
                ),
            },
        )
    return {
        "extended": True,
        "parent_pre_seconds": required_pre,
        "parent_post_seconds": required_post,
    }


def _evidence_admission_decision(
    conn: psycopg.Connection,
    event: Dict[str, Any],
    *,
    initial_status: str,
    source_id: str,
    event_type: str,
) -> dict[str, Any]:
    if initial_status not in {"pending", "materialization_pending"}:
        return {"allowed": True, "reason": "not_recordable_initial_status"}
    global_limit = _int_env("EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL", 0)
    source_limit = _int_env("EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE", 0)
    event_type_limits = _int_map_env("EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE")
    high_priority = _is_high_priority_event(event)
    # The per-source knob bounds how many of a camera's tasks run at once, not
    # how many may exist. Execution concurrency is enforced by the media-worker
    # SourceSlotRegistry, so a second event on a busy camera queues instead of
    # being dropped; "skip" restores the old drop-on-cap behaviour.
    source_limit_mode = _source_limit_mode()
    source_limit_observed: int | None = None
    source_limit_bypassed_for_priority = False
    try:
        if global_limit > 0:
            observed = _active_admission_count(conn)
            if observed >= global_limit:
                return {
                    "allowed": False,
                    "reason": "admission_global_active_limit_reached",
                    "scope": "global",
                    "limit": global_limit,
                    "observed": observed,
                }
        if source_limit > 0 and source_id:
            observed = _active_admission_count(conn, source_id=source_id)
            source_limit_observed = observed
            if observed >= source_limit and source_limit_mode == "skip":
                # Legacy behaviour: refuse the task outright. It is written as
                # `materialization_skipped`, so the evidence is never produced.
                if high_priority:
                    source_limit_bypassed_for_priority = True
                else:
                    return {
                        "allowed": False,
                        "reason": "admission_source_active_limit_reached",
                        "scope": "source",
                        "source_id": source_id,
                        "limit": source_limit,
                        "observed": observed,
                    }
        event_type_limit = event_type_limits.get(event_type, 0)
        if event_type_limit > 0 and event_type:
            observed = _active_admission_count(conn, event_type=event_type)
            if observed >= event_type_limit:
                return {
                    "allowed": False,
                    "reason": "admission_event_type_active_limit_reached",
                    "scope": "event_type",
                    "event_type": event_type,
                    "limit": event_type_limit,
                    "observed": observed,
                }
    except Exception as exc:
        return {
            "allowed": True,
            "reason": "admission_check_failed_open",
            "error": f"{type(exc).__name__}:{exc}",
        }
    return {
        "allowed": True,
        "reason": "admitted",
        "high_priority": high_priority,
        "source_limit_bypassed_for_priority": source_limit_bypassed_for_priority,
        "source_limit": source_limit,
        "source_limit_mode": source_limit_mode,
        "source_observed": source_limit_observed,
    }


def _event_datetime(event: Dict[str, Any]) -> datetime:
    for key in ("event_ts_ms", "start_ts_ms"):
        try:
            value = int(event.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return datetime.fromtimestamp(value / 1000.0, timezone.utc)
    return datetime.now(timezone.utc)


def _materialization_ttl_metadata(event: Dict[str, Any]) -> dict[str, Any]:
    event_at = _event_datetime(event)
    replay_ttl_s = max(0, _int_env("EVIDENCE_REPLAY_TTL_SECONDS", 300))
    annotation_ttl_s = max(0, _int_env("EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS", 120))
    replay_deadline = event_at + timedelta(seconds=replay_ttl_s)
    annotation_deadline = event_at + timedelta(seconds=annotation_ttl_s)
    materialization_deadline = min(replay_deadline, annotation_deadline)
    return {
        "replay_ttl_seconds": replay_ttl_s,
        "frame_annotation_ttl_seconds": annotation_ttl_s,
        "replay_deadline_at": replay_deadline.isoformat(),
        "annotation_deadline_at": annotation_deadline.isoformat(),
        "materialization_deadline_at": materialization_deadline.isoformat(),
    }


def _materialization_ready_at(event: Dict[str, Any], policy: Dict[str, Any]) -> datetime:
    event_at = _event_datetime(event)
    try:
        post_seconds = float(policy.get("post_seconds", 10))
    except (TypeError, ValueError):
        post_seconds = 10.0
    if _is_image_only_evidence(event):
        post_seconds = max(0.0, _float_env("ROLLING_CACHE_SEGMENT_SECONDS", 4.0))
    segment_grace_s = max(
        0.0,
        _float_env("EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS", 0.0),
    )
    return event_at + timedelta(seconds=max(0.0, post_seconds) + segment_grace_s)


def _replay_window_metadata(event: Dict[str, Any], policy: Dict[str, Any]) -> dict[str, Any]:
    return {
        "event_ts_ms": int(event.get("event_ts_ms") or event.get("start_ts_ms") or 0),
        "pre_seconds": int(policy.get("pre_seconds", 5)),
        "post_seconds": int(policy.get("post_seconds", 10)),
        "frame_uuid": event.get("frame_uuid"),
        "keyframe_uuid": event.get("keyframe_uuid"),
    }

_MIDTERM_BEHAVIOR_NOT_IMPLEMENTED_REASON = (
    "Midterm behavior evidence created the evidence task, but production "
    "snapshot/clip/metadata generation is not implemented in this deployment."
)

_MIDTERM_FACE_MATCH_NOT_IMPLEMENTED_REASON = (
    "Midterm face match evidence created the evidence task, but production "
    "snapshot/raw_clip/metadata generation is not implemented in this deployment."
)


def _not_implemented_reason(event: Dict[str, Any]) -> str:
    if event.get("algorithm_type") == "face_intelligence" or event.get(
        "event_type"
    ) in ("watchlist_hit", "live_search_hit"):
        return _MIDTERM_FACE_MATCH_NOT_IMPLEMENTED_REASON
    return _MIDTERM_BEHAVIOR_NOT_IMPLEMENTED_REASON


def _evidence_task_initial_status(event: Dict[str, Any]) -> tuple[str, str]:
    """Return (status, error_message) for a new evidence task.

    For watchlist_hit / live_search_hit and intrusion, the recording
    pipeline (record_request -> clip-worker -> media-worker) can handle
    evidence generation, so the task starts as 'pending' with no error.
    Reserved behavior events still start as 'not_implemented'.
    """
    event_type = event.get("event_type", "")
    algorithm_type = event.get("algorithm_type", "")
    if _is_image_only_evidence(event):
        return _materialization_initial_status(event), ""
    if algorithm_type == "face_intelligence" or event_type in (
        "watchlist_hit",
        "live_search_hit",
        "intrusion",
    ):
        return _materialization_initial_status(event), ""
    return "not_implemented", _MIDTERM_BEHAVIOR_NOT_IMPLEMENTED_REASON


class EventRepository:
    """Idempotent event store backed by PostgreSQL."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        self._lifecycle_v2_supported: bool | None = None

    def _supports_lifecycle_v2(self) -> bool:
        """Feature-detect Migration 029 for mixed-order deployments."""
        if self._lifecycle_v2_supported is not None:
            return self._lifecycle_v2_supported
        required = {
            "materialization_phase",
            "materialization_next_attempt_at",
            "materialization_retry_reason",
            "materialization_owner",
            "materialization_lease_token",
            "materialization_handoff",
        }
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'evidence_tasks'
                      AND column_name = ANY(%(columns)s)
                    """,
                    {"columns": sorted(required)},
                )
                rows = cur.fetchall()
            observed = {
                str(row.get("column_name") if isinstance(row, dict) else row[0])
                for row in rows
                if row
            }
            self._lifecycle_v2_supported = observed == required
        except Exception:
            # Migration 029 may intentionally be deployed after compatible
            # code.  Legacy columns remain usable until the service recreate
            # following migration application.
            self._lifecycle_v2_supported = False
        return self._lifecycle_v2_supported

    def insert_event(self, event: Dict[str, Any]) -> str | None:
        """Insert *event* dict into the events table.

        Returns the UUID of the new row if inserted, or None if a duplicate
        ``source_event_id`` was skipped.
        """
        media = event.get("payload", {}).get("media", {})
        reason = _not_implemented_reason(event)
        params = {
            "source_event_id": event.get("source_event_id", ""),
            "event_type": event.get("event_type", ""),
            "camera_id": event.get("camera_id", ""),
            "source_id": event.get("source_id", ""),
            "track_id": str(event.get("track_id", "")),
            "person_id": event.get("person_id") or None,
            "algorithm_type": event.get("algorithm_type") or event.get("event_type", ""),
            "algorithm_version": event.get("algorithm_version"),
            "severity": event.get("severity", "medium"),
            "confidence": float(event.get("confidence", 0.0)),
            "start_ts_ms": int(event.get("start_ts_ms", 0)),
            "end_ts_ms": int(event.get("end_ts_ms") or event.get("start_ts_ms", 0)),
            "event_ts_ms": int(event.get("event_ts_ms", 0)),
            "frame_uuid": event.get("frame_uuid"),
            "keyframe_uuid": event.get("keyframe_uuid"),
            "snapshot_path": media.get("snapshot_path"),
            "clip_path": media.get("clip_path"),
            "snapshot_required": bool(event.get("snapshot_required", False)),
            "clip_required": bool(event.get("clip_required", False)),
            "evidence_policy": json.dumps(
                event.get("evidence_policy", {}), ensure_ascii=False
            ),
            "recording_strategy": media.get("recording_strategy", "reserved"),
            "media_status": media.get("snapshot_status", "not_implemented"),
            "status": "new",
            "payload": json.dumps(event.get("payload", {}), ensure_ascii=False),
        }

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_INSERT_SQL, params)
            row = cur.fetchone()
            return str(row["id"]) if row else None

    def insert_person_bbox_observation(self, observation: Dict[str, Any]) -> str | None:
        """Idempotently insert one accepted person bbox observation."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _INSERT_PERSON_BBOX_OBSERVATION_SQL,
                _person_bbox_observation_params(observation),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else None

    def insert_person_bbox_observations(
        self,
        observations: list[Dict[str, Any]],
    ) -> list[str | None]:
        """Persist one trajectory batch with one PostgreSQL statement."""

        if not observations:
            return []
        rows = []
        for ordinal, observation in enumerate(observations):
            params = _person_bbox_observation_params(observation)
            rows.append(
                {
                    **params,
                    "ordinal": ordinal,
                    "person_bbox": json.loads(params["person_bbox"]),
                    "payload": json.loads(params["payload"]),
                }
            )
        with self._conn.transaction(), self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _INSERT_PERSON_BBOX_OBSERVATIONS_SQL,
                {"rows": json.dumps(rows, ensure_ascii=False)},
            )
            return [
                str(row["id"]) if row.get("id") is not None else None
                for row in cur.fetchall()
            ]

    def _face_observation_for_event(self, event: Dict[str, Any]) -> dict[str, Any]:
        source_observation_id = _source_observation_id_from_event(event)
        if not source_observation_id:
            return {}
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT source_observation_id, camera_id, source_id, track_id,
                       timestamp_ms, face_bbox, landmarks, face_confidence, quality,
                       person_bbox, snapshot_path, crop_path, payload
                FROM face_observations
                WHERE source_observation_id = %(source_observation_id)s
                LIMIT 1
                """,
                {"source_observation_id": source_observation_id},
            )
            return dict(cur.fetchone() or {})

    def _create_image_only_evidence_task(
        self,
        event: Dict[str, Any],
        event_id: str,
    ) -> str | None:
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"evidence:{event_id}"))
        media = _payload_media(event)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        matched_person = payload.get("matched_person") if isinstance(payload.get("matched_person"), dict) else {}
        match = payload.get("match") if isinstance(payload.get("match"), dict) else {}
        observation_payload = payload.get("observation") if isinstance(payload.get("observation"), dict) else {}
        observation = self._face_observation_for_event(event)
        source_observation_id = _source_observation_id_from_event(event)
        snapshot_path = (
            observation.get("snapshot_path")
            or media.get("snapshot_path")
            or media.get("full_frame_path")
        )
        crop_path = observation.get("crop_path") or media.get("crop_path")
        annotated_path = media.get("annotated_frame_path")
        face_crop_uri = _media_uri(crop_path)
        full_frame_uri = _media_uri(snapshot_path)
        annotated_frame_uri = _media_uri(annotated_path)
        image_artifact_ready = bool(face_crop_uri or full_frame_uri or annotated_frame_uri)
        image_status = "image_ready" if image_artifact_ready else "image_pending"
        task_status = "materialized" if image_artifact_ready else "materialization_pending"
        image_reason = ""
        source_id = str(event.get("source_id") or "").strip()
        runtime_epoch_id = _runtime_epoch_id_for_task(event)
        materialization_phase = (
            MaterializationPhase.TERMINAL.value
            if image_artifact_ready
            else MaterializationPhase.WAITING_READY.value
        )
        materialization_owner = "terminal" if image_artifact_ready else "rolling"
        if not image_artifact_ready and (not source_id or not runtime_epoch_id):
            image_reason = (
                NormalizedReason.MISSING_SOURCE_ID.value
                if not source_id
                else NormalizedReason.MISSING_RUNTIME_EPOCH.value
            )
            image_status = "image_failed"
            task_status = "materialization_failed"
            materialization_phase = MaterializationPhase.MANUAL_QUARANTINE.value
            materialization_owner = "terminal"
        event_ts_ms = int(event.get("event_ts_ms") or event.get("start_ts_ms", 0))
        policy = event.get("evidence_policy") if isinstance(event.get("evidence_policy"), dict) else {}
        ttl_metadata = _materialization_ttl_metadata(event)
        replay_window = _replay_window_metadata(event, policy)
        materialization_ready_at = _materialization_ready_at(event, policy)
        summary = {
            "schema_version": "face-image-evidence-v1",
            "playback_kind": "image",
            "evidence_mode": "image_only",
            "clip_required": False,
            "clip_status": "not_required",
            "image_status": image_status,
            "source_observation_id": source_observation_id,
            "person_id": event.get("person_id"),
            "matched_person": matched_person,
            "match": match,
            "similarity": match.get("similarity") or event.get("confidence"),
            "observation": observation_payload or {
                "camera_id": observation.get("camera_id"),
                "source_id": observation.get("source_id"),
                "track_id": observation.get("track_id"),
                "timestamp_ms": observation.get("timestamp_ms"),
            },
            "face_bbox": observation.get("face_bbox") or observation_payload.get("face_bbox"),
            "person_bbox": observation.get("person_bbox"),
            "face_crop_uri": face_crop_uri,
            "full_frame_uri": full_frame_uri,
            "annotated_frame_uri": annotated_frame_uri,
            "image_available": image_artifact_ready,
            "image_missing_reason": image_reason,
        }
        materialization = {
            "schema_version": "image-only-v1",
            "created_by": "event-worker",
            "materialization_status": task_status,
            "materialization_reason": image_reason,
            "materialization_phase": materialization_phase,
            "materialization_owner": materialization_owner,
            "materialization_ready_at": materialization_ready_at.isoformat(),
            "materialization_deadline_at": ttl_metadata["materialization_deadline_at"],
            "replay_window": replay_window,
        }

        lifecycle_columns = ""
        lifecycle_values = ""
        lifecycle_updates = ""
        if self._supports_lifecycle_v2():
            lifecycle_columns = """
                    materialization_phase, materialization_phase_updated_at,
                    materialization_owner, materialization_failure_reason,
            """
            lifecycle_values = """
                    %(materialization_phase)s, now(),
                    %(materialization_owner)s,
                    CASE
                        WHEN %(task_status)s = 'materialization_failed'
                            THEN NULLIF(%(image_reason)s::text, '')
                        ELSE NULL
                    END,
            """
            lifecycle_updates = """
                    materialization_phase = %(materialization_phase)s,
                    materialization_phase_updated_at = now(),
                    materialization_owner = %(materialization_owner)s,
                    materialization_failure_reason = CASE
                        WHEN %(task_status)s = 'materialization_failed'
                            THEN NULLIF(%(image_reason)s::text, '')
                        ELSE NULL
                    END,
            """

        # Task, bundle, optional artifacts, and the event compatibility
        # projection are one image-evidence state transition. Autocommit would
        # otherwise retain a partial task/bundle when the final projection
        # fails.
        with self._conn.transaction(), self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                INSERT INTO evidence_tasks (
                    task_id, event_id, source_event_id, camera_id, source_id,
                    event_type, event_ts_ms, task_type,
                    snapshot_required, clip_required,
                    pre_seconds, post_seconds, status,
                    materialization_status, materialization_policy, priority,
                    runtime_epoch_id,
                    replay_source_id, replay_window,
                    replay_deadline_at, annotation_deadline_at,
                    materialization_deadline_at, materialization_ready_at,
                    materialization_audit,
                    materialization_defer_reason,
                    {lifecycle_columns}
                    error_message
                ) VALUES (
                    %(task_id)s, %(event_id)s::uuid, %(source_event_id)s,
                    %(camera_id)s, %(source_id)s, %(event_type)s,
                    %(event_ts_ms)s, 'image_only',
                    true, false,
                    %(pre_seconds)s, %(post_seconds)s, %(task_status)s,
                    %(task_status)s, 'image_only', %(priority)s,
                    NULLIF(%(runtime_epoch_id)s::text, ''),
                    NULLIF(%(replay_source_id)s::text, ''),
                    %(replay_window)s::jsonb,
                    %(replay_deadline_at)s::timestamptz,
                    %(annotation_deadline_at)s::timestamptz,
                    %(materialization_deadline_at)s::timestamptz,
                    %(materialization_ready_at)s::timestamptz,
                    %(materialization_audit)s::jsonb,
                    NULL,
                    {lifecycle_values}
                    %(image_reason)s
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    status = %(task_status)s,
                    materialization_status = %(task_status)s,
                    task_type = 'image_only',
                    clip_required = false,
                    {lifecycle_updates}
                    error_message = %(image_reason)s,
                    updated_at = now()
                RETURNING task_id
                """,
                {
                    "task_id": task_id,
                    "event_id": event_id,
                    "source_event_id": event.get("source_event_id", ""),
                    "camera_id": event.get("camera_id", ""),
                    "source_id": source_id,
                    "event_type": event.get("event_type", ""),
                    "event_ts_ms": event_ts_ms,
                    "task_status": task_status,
                    "materialization_phase": materialization_phase,
                    "materialization_owner": materialization_owner,
                    "image_reason": image_reason,
                    "pre_seconds": int(policy.get("pre_seconds", 5)),
                    "post_seconds": int(policy.get("post_seconds", 5)),
                    "priority": _materialization_priority(event),
                    "runtime_epoch_id": runtime_epoch_id,
                    "replay_source_id": source_id,
                    "replay_window": json.dumps(replay_window, ensure_ascii=False),
                    "replay_deadline_at": ttl_metadata["replay_deadline_at"],
                    "annotation_deadline_at": ttl_metadata["annotation_deadline_at"],
                    "materialization_deadline_at": ttl_metadata[
                        "materialization_deadline_at"
                    ],
                    "materialization_ready_at": materialization_ready_at.isoformat(),
                    "materialization_audit": json.dumps(materialization, ensure_ascii=False),
                },
            )
            row = cur.fetchone()
            cur.execute(
                """
                INSERT INTO evidence_bundles (
                    event_id, source_event_id, camera_id, source_id, camera_name,
                    event_type, event_created_at, alarm_machine_time,
                    media_status, evidence_state, evidence_reason, raw_clip_uri,
                    annotation_status, annotation_count, matched_objects,
                    unknown_objects, visual_evidence_status,
                    frontend_overlay_required, summary, materialization
                ) VALUES (
                    %(event_id)s::uuid, %(source_event_id)s, %(camera_id)s,
                    %(source_id)s, %(camera_name)s, %(event_type)s,
                    now(),
                    CASE
                        WHEN %(event_ts_ms)s BETWEEN 946684800000 AND 4102444800000
                            THEN to_timestamp(%(event_ts_ms)s / 1000.0)
                        ELSE now()
                    END,
                    %(image_status)s, %(image_status)s, %(image_reason)s, NULL,
                    'not_required', 0, 1, 0, %(visual_evidence_status)s,
                    false, %(summary)s::jsonb, %(materialization)s::jsonb
                )
                ON CONFLICT (event_id) DO UPDATE SET
                    media_status = %(image_status)s::text,
                    evidence_state = %(image_status)s::text,
                    evidence_reason = %(image_reason)s::text,
                    raw_clip_uri = NULL,
                    annotation_status = 'not_required',
                    matched_objects = 1,
                    visual_evidence_status = %(visual_evidence_status)s,
                    frontend_overlay_required = false,
                    summary = EXCLUDED.summary,
                    materialization = EXCLUDED.materialization,
                    updated_at = now()
                """,
                {
                    "event_id": event_id,
                    "source_event_id": event.get("source_event_id", ""),
                    "camera_id": event.get("camera_id", ""),
                    "source_id": event.get("source_id", ""),
                    "camera_name": payload.get("camera_name") or media.get("camera_name"),
                    "event_type": event.get("event_type", ""),
                    "event_ts_ms": event_ts_ms,
                    "image_status": image_status,
                    "image_reason": image_reason,
                    "visual_evidence_status": (
                        "verified"
                        if image_artifact_ready
                        else (
                            "failed"
                            if task_status == "materialization_failed"
                            else "pending"
                        )
                    ),
                    "summary": json.dumps(summary, ensure_ascii=False),
                    "materialization": json.dumps(materialization, ensure_ascii=False),
                },
            )
            artifacts = (
                ("face_crop", crop_path),
                ("full_frame", snapshot_path),
                ("annotated_frame", annotated_path),
            )
            for artifact_type, path in artifacts:
                uri = _media_uri(path)
                if not uri:
                    continue
                cur.execute(
                    """
                    INSERT INTO evidence_artifacts (
                        event_id, artifact_type, uri, content_type, metadata
                    ) VALUES (
                        %(event_id)s::uuid, %(artifact_type)s, %(uri)s,
                        %(content_type)s, %(metadata)s::jsonb
                    )
                    ON CONFLICT (event_id, artifact_type) DO UPDATE SET
                        uri = EXCLUDED.uri,
                        content_type = EXCLUDED.content_type,
                        metadata = EXCLUDED.metadata,
                        status = 'ready',
                        updated_at = now()
                    """,
                    {
                        "event_id": event_id,
                        "artifact_type": artifact_type,
                        "uri": uri,
                        "content_type": _content_type_for_path(uri),
                        "metadata": json.dumps(
                            {
                                "source_observation_id": source_observation_id,
                                "storage_semantics": "image_only_face_evidence",
                            },
                            ensure_ascii=False,
                        ),
                    },
                )
            cur.execute(
                """
                UPDATE events
                SET snapshot_path = COALESCE(NULLIF(%(snapshot_path)s::text, ''), snapshot_path),
                    clip_required = false,
                    media_status = %(image_status)s,
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'media_status', %(image_status)s::text,
                                'snapshot_status', %(image_status)s::text,
                                'clip_status', 'not_required',
                                'metadata_status', %(image_status)s::text,
                                'evidence_state', %(image_status)s::text,
                                'evidence_reason', %(image_reason)s::text,
                                'materialization_status', %(task_status)s::text,
                                'playback_kind', 'image',
                                'evidence_mode', 'image_only',
                                'clip_required', false,
                                'snapshot_path', NULLIF(%(snapshot_path)s::text, ''),
                                'crop_path', NULLIF(%(crop_path)s::text, '')
                            )
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "event_id": event_id,
                    "snapshot_path": str(snapshot_path or ""),
                    "crop_path": str(crop_path or ""),
                    "image_status": image_status,
                    "image_reason": image_reason,
                    "task_status": task_status,
                },
            )
            return str(row["task_id"]) if row else None

    def create_evidence_task(
        self,
        event: Dict[str, Any],
        event_id: str,
    ) -> str | None:
        """Create one idempotent evidence task for an event.

        For watchlist_hit / live_search_hit and intrusion the task starts
        as 'pending' because the recording pipeline (record_request ->
        clip-worker -> media-worker) can handle evidence generation.
        """
        if _is_image_only_evidence(event):
            return self._create_image_only_evidence_task(event, event_id)

        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"evidence:{event_id}"))
        policy = event.get("evidence_policy") or {}
        if not isinstance(policy, dict):
            policy = {}
        initial_status, error_message = _evidence_task_initial_status(event)
        ttl_metadata = _materialization_ttl_metadata(event)
        replay_window = _replay_window_metadata(event, policy)
        materialization_ready_at = _materialization_ready_at(event, policy)
        materialization_policy = os.getenv("EVIDENCE_MATERIALIZATION_POLICY", "priority")
        priority = _materialization_priority(event)
        source_id = event.get("source_id", "")
        event_type = event.get("event_type", "")
        event_ts_ms = int(event.get("event_ts_ms") or event.get("start_ts_ms", 0))
        pre_seconds = int(policy.get("pre_seconds", 5))
        post_seconds = int(policy.get("post_seconds", 10))
        coverage_parent_event_id = _coverage_parent_event_id(
            self._conn,
            event_id=event_id,
            source_id=str(source_id or ""),
            event_type=str(event_type or ""),
            event_ts_ms=event_ts_ms,
        )
        coverage_decision: dict[str, Any] = {"covered": False}
        coverage_extension: dict[str, Any] = {"extended": False}
        materialization_defer_reason = ""
        if coverage_parent_event_id:
            coverage_extension = _extend_coverage_parent_window(
                self._conn,
                parent_event_id=coverage_parent_event_id,
                child_event_ts_ms=event_ts_ms,
                child_pre_seconds=pre_seconds,
                child_post_seconds=post_seconds,
            )
            if coverage_extension.get("reason") == "parent_max_duration_exceeded":
                coverage_parent_event_id = None
        if coverage_parent_event_id:
            initial_status = "materialization_deferred"
            error_message = f"covered_by_event:{coverage_parent_event_id}"
            materialization_defer_reason = "covered_by_existing_evidence"
            admission_decision = {
                "allowed": True,
                "reason": "covered_by_existing_evidence",
                "covered_by_event_id": coverage_parent_event_id,
                "coverage_extension": coverage_extension,
            }
            coverage_decision = {
                "covered": True,
                "relation": "covered_by",
                "bundle_event_id": coverage_parent_event_id,
                "reason": "same_source_event_window",
                "window_seconds": _coverage_window_ms() // 1000,
                "coverage_extension": coverage_extension,
            }
        else:
            admission_decision = _evidence_admission_decision(
                self._conn,
                event,
                initial_status=initial_status,
                source_id=str(source_id or ""),
                event_type=str(event_type or ""),
            )
            if not bool(admission_decision.get("allowed", True)):
                initial_status = "materialization_skipped"
                error_message = "evidence_admission_skipped:" + str(
                    admission_decision.get("reason") or "admission_denied"
                )

        runtime_epoch_id = _runtime_epoch_id_for_task(event)
        materialization_status = materialization_status_for_operator_state(
            initial_status
        )
        materialization_phase = (
            MaterializationPhase.WAITING_READY.value
            if materialization_status in ACTIVE_MATERIALIZATION_STATUSES
            else MaterializationPhase.TERMINAL.value
        )
        materialization_failure_reason = ""
        if materialization_status in ACTIVE_MATERIALIZATION_STATUSES and (
            not str(source_id or "").strip() or not runtime_epoch_id
        ):
            missing_reason = (
                NormalizedReason.MISSING_SOURCE_ID.value
                if not str(source_id or "").strip()
                else NormalizedReason.MISSING_RUNTIME_EPOCH.value
            )
            initial_status = "materialization_failed"
            materialization_status = "materialization_failed"
            materialization_phase = MaterializationPhase.MANUAL_QUARANTINE.value
            materialization_failure_reason = missing_reason
            error_message = f"{missing_reason}:manual_quarantine"
        materialization_owner = _materialization_owner_for_task(
            event,
            materialization_status,
        )

        params = {
            "task_id": task_id,
            "event_id": event_id,
            "source_event_id": event.get("source_event_id", ""),
            "camera_id": event.get("camera_id", ""),
            "source_id": source_id,
            "event_type": event_type,
            "event_ts_ms": event_ts_ms,
            "task_type": "snapshot_clip",
            "snapshot_required": bool(event.get("snapshot_required", False)),
            "clip_required": bool(event.get("clip_required", False)),
            "pre_seconds": pre_seconds,
            "post_seconds": post_seconds,
            "status": initial_status,
            "materialization_status": materialization_status,
            "materialization_phase": materialization_phase,
            "materialization_owner": materialization_owner,
            "materialization_failure_reason": materialization_failure_reason,
            "materialization_policy": materialization_policy,
            "priority": priority,
            "runtime_epoch_id": runtime_epoch_id,
            "replay_source_id": source_id,
            "replay_window": json.dumps(replay_window, ensure_ascii=False),
            "replay_deadline_at": ttl_metadata["replay_deadline_at"],
            "annotation_deadline_at": ttl_metadata["annotation_deadline_at"],
            "materialization_deadline_at": ttl_metadata["materialization_deadline_at"],
            "materialization_ready_at": materialization_ready_at.isoformat(),
            "materialization_audit": json.dumps(
                {
                    "schema_version": "manifest-first-v1",
                    "created_by": "event-worker",
                    "replay_ttl_seconds": ttl_metadata["replay_ttl_seconds"],
                    "frame_annotation_ttl_seconds": ttl_metadata[
                        "frame_annotation_ttl_seconds"
                    ],
                    "high_priority": _is_high_priority_event(event),
                    "materialization_ready_at": materialization_ready_at.isoformat(),
                    "admission_decision": admission_decision,
                    "coverage_decision": coverage_decision,
                },
                ensure_ascii=False,
            ),
            "materialization_defer_reason": materialization_defer_reason,
            "error_message": error_message,
        }

        lifecycle_columns = ""
        lifecycle_values = ""
        if self._supports_lifecycle_v2():
            lifecycle_columns = """
                    materialization_phase, materialization_phase_updated_at,
                    materialization_owner, materialization_failure_reason,
            """
            lifecycle_values = """
                    %(materialization_phase)s, now(),
                    %(materialization_owner)s,
                    NULLIF(%(materialization_failure_reason)s::text, ''),
            """

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                INSERT INTO evidence_tasks (
                    task_id, event_id, source_event_id, camera_id, source_id,
                    event_type, event_ts_ms, task_type,
                    snapshot_required, clip_required,
                    pre_seconds, post_seconds, status,
                    materialization_status, materialization_policy, priority,
                    runtime_epoch_id,
                    replay_source_id, replay_window,
                    replay_deadline_at, annotation_deadline_at,
                    materialization_deadline_at, materialization_ready_at,
                    materialization_audit,
                    materialization_defer_reason,
                    {lifecycle_columns}
                    error_message
                ) VALUES (
                    %(task_id)s, %(event_id)s::uuid, %(source_event_id)s,
                    %(camera_id)s, %(source_id)s, %(event_type)s,
                    %(event_ts_ms)s, %(task_type)s,
                    %(snapshot_required)s, %(clip_required)s,
                    %(pre_seconds)s, %(post_seconds)s, %(status)s,
                    %(materialization_status)s, %(materialization_policy)s,
                    %(priority)s, NULLIF(%(runtime_epoch_id)s::text, ''),
                    %(replay_source_id)s,
                    %(replay_window)s::jsonb,
                    %(replay_deadline_at)s::timestamptz,
                    %(annotation_deadline_at)s::timestamptz,
                    %(materialization_deadline_at)s::timestamptz,
                    %(materialization_ready_at)s::timestamptz,
                    %(materialization_audit)s::jsonb,
                    NULLIF(%(materialization_defer_reason)s::text, ''),
                    {lifecycle_values}
                    %(error_message)s
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    updated_at = evidence_tasks.updated_at
                RETURNING task_id
                """,
                params,
            )
            row = cur.fetchone()
            task_id_out = str(row["task_id"]) if row else None

        if coverage_parent_event_id:
            _upsert_evidence_event_link(
                self._conn,
                event_id=event_id,
                bundle_event_id=coverage_parent_event_id,
                reason="same_source_event_window",
                metadata={
                    "schema_version": "evidence-event-link-v1",
                    "created_by": "event-worker",
                    "source_id": source_id,
                    "event_type": event_type,
                    "event_ts_ms": event_ts_ms,
                    "window_seconds": _coverage_window_ms() // 1000,
                    "coverage_extension": coverage_extension,
                },
            )

        # Only mark the event's media status immediately for not_implemented.
        # For pending tasks, the media-worker will update the status after
        # generating the bundle. Setting clip_status="pending" here would
        # block the record_request gate in _handle_event.
        if initial_status in {
            "not_implemented",
            "manifest_ready",
            "materialization_skipped",
            "materialization_deferred",
            "materialization_failed",
        }:
            self.set_evidence_status(
                event_id=event_id,
                status=initial_status,
                error_message=error_message,
                materialization_metadata={
                    **ttl_metadata,
                    "materialization_ready_at": materialization_ready_at.isoformat(),
                    "materialization_policy": materialization_policy,
                    "priority": priority,
                    "replay_source_id": source_id,
                    "replay_window": replay_window,
                    "admission_decision": admission_decision,
                },
            )
        return task_id_out

    def set_evidence_status(
        self,
        *,
        event_id: str,
        status: str,
        error_message: str = "",
        snapshot_path: str | None = None,
        clip_path: str | None = None,
        metadata_path: str | None = None,
        materialization_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Update event-level media status and media payload fields."""
        if status not in EVIDENCE_TASK_STATUSES:
            raise ValueError(f"unsupported evidence status: {status}")

        evidence_state = _evidence_state_for_status(status)
        materialization_state = materialization_status_for_operator_state(
            evidence_state
        )
        materialization_metadata = materialization_metadata or {}
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET snapshot_path = COALESCE(%(snapshot_path)s, snapshot_path),
                    clip_path = COALESCE(%(clip_path)s, clip_path),
                    media_status = %(status)s,
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'snapshot_status', %(status)s::text,
                                'clip_status', %(status)s::text,
                                'metadata_status', %(status)s::text,
                                'metadata_path', %(metadata_path)s::text,
                                'evidence_state', %(evidence_state)s::text,
                                'evidence_reason', NULLIF(%(error_message)s::text, ''),
                                'evidence_state_updated_at', now(),
                                'materialization_status',
                                    %(materialization_status)s::text,
                                'materialization_reason',
                                    NULLIF(%(error_message)s::text, ''),
                                'materialization_deadline_at',
                                    NULLIF(%(materialization_deadline_at)s::text, ''),
                                'materialization_ready_at',
                                    NULLIF(%(materialization_ready_at)s::text, ''),
                                'materialization_policy',
                                    NULLIF(%(materialization_policy)s::text, ''),
                                'materialization_priority',
                                    %(materialization_priority)s::int,
                                'replay_source_id',
                                    NULLIF(%(replay_source_id)s::text, ''),
                                'replay_window',
                                    %(replay_window)s::jsonb,
                                'error_message', %(error_message)s::text
                            )
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "event_id": event_id,
                    "status": status,
                    "evidence_state": evidence_state,
                    "materialization_status": materialization_state,
                    "error_message": error_message,
                    "snapshot_path": snapshot_path,
                    "clip_path": clip_path,
                    "metadata_path": metadata_path,
                    "materialization_deadline_at": str(
                        materialization_metadata.get("materialization_deadline_at") or ""
                    ),
                    "materialization_ready_at": str(
                        materialization_metadata.get("materialization_ready_at") or ""
                    ),
                    "materialization_policy": str(
                        materialization_metadata.get("materialization_policy") or ""
                    ),
                    "materialization_priority": materialization_metadata.get(
                        "priority"
                    ),
                    "replay_source_id": str(
                        materialization_metadata.get("replay_source_id") or ""
                    ),
                    "replay_window": json.dumps(
                        materialization_metadata.get("replay_window") or {}
                    ),
                },
            )
            updated = cur.rowcount is not None and cur.rowcount > 0
            if updated:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(evidence_state)s,
                        materialization_status = %(materialization_status)s,
                        error_message = CASE
                            WHEN %(error_message)s::text != ''
                                THEN %(error_message)s::text
                            ELSE error_message
                        END,
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                    """,
                    {
                        "event_id": event_id,
                        "evidence_state": evidence_state,
                        "materialization_status": materialization_state,
                        "error_message": error_message,
                    },
                )
            return updated

    def count_by_source_event_id(self, source_event_id: str) -> int:
        """Return the number of rows with the given *source_event_id*."""
        with self._conn.cursor() as cur:
            cur.execute(_COUNT_BY_SID_SQL, (source_event_id,))
            row = cur.fetchone()
            return row[0] if row else 0

    def event_exists(self, source_event_id: str) -> bool:
        """Return True if an event with *source_event_id* exists."""
        return self.count_by_source_event_id(source_event_id) > 0

    def get_camera_alert_policy(self, camera_id: str) -> dict[str, Any]:
        """Return cameras.alert_policy for *camera_id*, or {} when absent."""
        if not camera_id:
            return {}
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT alert_policy FROM cameras WHERE id = %s",
                    (camera_id,),
                )
                row = cur.fetchone()
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            return {}
        if not row:
            return {}
        policy = row[0]
        if isinstance(policy, str):
            try:
                return json.loads(policy)
            except json.JSONDecodeError:
                return {}
        return policy if isinstance(policy, dict) else {}

    def get_last_unsuppressed_alert_ts_ms(
        self,
        camera_id: str,
        *,
        exclude_source_event_id: str,
        current_event_ts_ms: int,
        event_type: str = "",
        algorithm_type: str = "",
        cooldown_scope: str = "algorithm",
    ) -> int | None:
        """Return the most recent non-suppressed event timestamp for a scope."""
        row = self.get_last_unsuppressed_alert(
            camera_id,
            exclude_source_event_id=exclude_source_event_id,
            current_event_ts_ms=current_event_ts_ms,
            event_type=event_type,
            algorithm_type=algorithm_type,
            cooldown_scope=cooldown_scope,
        )
        if not row:
            return None
        value = row.get("event_ts_ms")
        return int(value) if value is not None else None

    def get_last_unsuppressed_alert(
        self,
        camera_id: str,
        *,
        exclude_source_event_id: str,
        current_event_ts_ms: int,
        event_type: str = "",
        algorithm_type: str = "",
        cooldown_scope: str = "algorithm",
    ) -> dict[str, Any] | None:
        """Return the most recent non-suppressed event row for a scope."""
        if not camera_id:
            return None
        scope = str(cooldown_scope or "algorithm").strip().lower()
        if scope in {"global", "camera", "camera_global"}:
            scope_filter = ""
            scope_params: dict[str, object] = {}
        elif scope in {"event", "event_type"}:
            if not event_type:
                return None
            scope_filter = "AND event_type = %(event_type)s"
            scope_params = {"event_type": event_type}
        else:
            key = algorithm_type or event_type
            if not key:
                return None
            scope_filter = (
                "AND COALESCE(NULLIF(algorithm_type, ''), event_type) = "
                "%(algorithm_key)s"
            )
            scope_params = {"algorithm_key": key}
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT event_ts_ms, event_type, algorithm_type
                FROM events
                WHERE camera_id = %(camera_id)s
                  AND source_event_id <> %(exclude_source_event_id)s
                  AND COALESCE(status, 'new') <> 'suppressed'
                  AND (%(current_event_ts_ms)s <= 0
                       OR event_ts_ms <= %(current_event_ts_ms)s)
                  {scope_filter}
                ORDER BY event_ts_ms DESC
                LIMIT 1
                """,
                {
                    "camera_id": camera_id,
                    "exclude_source_event_id": exclude_source_event_id,
                    "current_event_ts_ms": current_event_ts_ms,
                    **scope_params,
                },
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "event_ts_ms": row[0],
            "event_type": row[1],
            "algorithm_type": row[2],
        }

    def has_event_type_since_ts_ms(
        self,
        *,
        source_id: str,
        event_type: str,
        since_ts_ms: int,
    ) -> bool:
        """Return True if an unsuppressed event type was created after a timestamp."""
        if not source_id or not event_type:
            return False
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM events
                WHERE source_id = %s
                  AND event_type = %s
                  AND created_at >= to_timestamp(%s::double precision / 1000.0)
                  AND COALESCE(status, 'new') <> 'suppressed'
                LIMIT 1
                """,
                (source_id, event_type, since_ts_ms),
            )
            return cur.fetchone() is not None

    def mark_event_suppressed(
        self,
        event_id: str,
        *,
        reason: str,
        policy: dict[str, Any],
        last_alert_ts_ms: int | None,
        cooldown_scope: str = "algorithm",
        cooldown_key: str = "",
        last_alert_event_type: str | None = None,
        last_alert_algorithm_type: str | None = None,
    ) -> bool:
        """Mark an already-inserted event as suppressed by alert policy."""
        payload = {
            "decision": "suppressed",
            "reason": reason,
            "policy": policy or {},
            "last_alert_ts_ms": last_alert_ts_ms,
            "cooldown_scope": cooldown_scope,
            "cooldown_key": cooldown_key,
            "last_alert_event_type": last_alert_event_type,
            "last_alert_algorithm_type": last_alert_algorithm_type,
        }
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET status = 'suppressed',
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'alert_policy',
                            %(payload)s::jsonb
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {"event_id": event_id, "payload": json.dumps(payload)},
            )
            return cur.rowcount is not None and cur.rowcount > 0

    def get_media_clip_status(self, source_event_id: str) -> str | None:
        """Get payload->'media'->>'clip_status' for an event, or None."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT payload->'media'->>'clip_status' FROM events "
                "WHERE source_event_id = %s",
                (source_event_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_evidence_task_status(self, event_id: str) -> str | None:
        """Get the current evidence task status for an event, or None."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT status
                FROM evidence_tasks
                WHERE event_id = %s::uuid
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (event_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def mark_evidence_materialization_skipped(
        self,
        event_id: str,
        *,
        reason: str,
    ) -> bool:
        """Mark an evidence task terminal when recording policy skips generation."""
        reason_text = reason or "recording_policy_skipped"
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET media_status = 'materialization_skipped',
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'clip_status', 'materialization_skipped',
                                'metadata_status', 'materialization_skipped',
                                'evidence_state', 'materialization_skipped',
                                'evidence_reason', %(reason)s::text,
                                'evidence_state_updated_at', now(),
                                'materialization_status', 'materialization_skipped',
                                'materialization_reason', %(reason)s::text,
                                'error_message', %(reason)s::text
                            ))
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {"event_id": event_id, "reason": reason_text},
            )
            updated = cur.rowcount is not None and cur.rowcount > 0
            if updated:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = 'materialization_skipped',
                        materialization_status = 'materialization_skipped',
                        materialization_defer_reason = %(reason)s::text,
                        error_message = %(reason)s::text,
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                    """,
                    {"event_id": event_id, "reason": reason_text},
                )
            return updated

    def set_clip_status(
        self,
        event_id: str,
        status: str,
        replay_job_id: str = "",
        error_message: str = "",
    ) -> bool:
        """Set clip_status and optionally replay_job_id / error_message in payload.media.

        Returns True if a row was updated.
        """
        evidence_state = _evidence_state_for_status(status)
        materialization_state = (
            evidence_state
            if evidence_state.startswith("materialization_")
            or evidence_state == "manifest_ready"
            else status
        )
        evidence_reason = error_message or (status if status != evidence_state else "")
        with self._conn.cursor() as cur:
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
                                'materialization_status',
                                    %(materialization_status)s::text,
                                'materialization_reason',
                                    NULLIF(%(evidence_reason)s::text, ''),
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
                    "evidence_state": evidence_state,
                    "materialization_status": materialization_state,
                    "evidence_reason": evidence_reason,
                    "error_message": error_message,
                    "event_id": event_id,
                },
            )
            updated = cur.rowcount is not None and cur.rowcount > 0
            if updated:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(evidence_state)s,
                        materialization_status = %(materialization_status)s,
                        error_message = CASE
                            WHEN %(evidence_reason)s::text != ''
                                THEN %(evidence_reason)s::text
                            ELSE error_message
                        END,
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                    """,
                    {
                        "evidence_state": evidence_state,
                        "materialization_status": materialization_state,
                        "evidence_reason": evidence_reason,
                        "event_id": event_id,
                    },
                )
            return updated
