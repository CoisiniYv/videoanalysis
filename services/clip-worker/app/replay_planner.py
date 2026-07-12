"""Pure Clip request, gate, anchor, label, and Replay payload planning."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping
from uuid import UUID

from app.contracts import (
    ClipGateContext,
    ClipGateDecision,
    ClipGatePolicy,
    NormalizedRecordRequest,
    ReplayFrameDomainProofs,
    ReplayPlan,
)


PTS_TIME_BASE = 1_000_000_000
MISSING_KEYFRAME_ERROR = "missing_keyframe_uuid_and_anchored_lookup_unavailable"
MISSING_ANCHOR_KEYFRAME_PTS_ERROR = "missing_anchor_keyframe_pts"
REPLAY_ANCHOR_STRATEGY_EVENT_START = "event_start_keyframe"
REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME = "event_keyframe"
FRAME_DOMAIN_SESSION_POLICY_STRICT = "strict_single_session"
FRAME_DOMAIN_SESSION_POLICY_CROSS_POST = "post_window_cross_session_pts_verified"
PRE_WINDOW_POLICY_FULL = "full_requested_window"
PRE_WINDOW_POLICY_TRUNCATED = "truncated_to_current_session"
POST_SAVANT_EVIDENCE_TOPOLOGIES = frozenset(
    {"post_savant", "post_savant_replay"}
)
PRIORITY_EVENT_TYPES = frozenset({"watchlist_hit", "live_search_hit"})
MIN_DELIVERY_DURATION_S = 30
DELIVERY_DURATION_EXTRA_SLACK_S = 10
RELIABLE_SINK_OPTIONS: dict[str, Any] = {
    "send_timeout": {"secs": 5, "nanos": 0},
    "send_retries": 5,
    "receive_timeout": {"secs": 5, "nanos": 0},
    "receive_retries": 5,
    "send_hwm": 10000,
    "receive_hwm": 10000,
    "inflight_ops": 100,
}
REPLAY_LABEL_FIELDS = (
    "request_id",
    "source_event_id",
    "replay_source_kind",
    "evidence_topology",
    "annotation_source_policy",
    "frame_pts",
    "frame_num",
    "metadata_domain",
    "requested_start_pts",
    "original_requested_start_pts",
    "effective_start_pts",
    "requested_end_pts",
    "requested_pre_window_seconds",
    "effective_pre_window_seconds",
    "pre_window_truncated_seconds",
    "event_frame_uuid",
    "event_frame_pts",
    "anchor_keyframe_uuid",
    "anchor_keyframe_pts",
    "anchor_keyframe_source",
    "evidence_anchor_strategy",
    "start_window_frame_uuid",
    "start_window_frame_pts",
    "start_window_stream_session_id",
    "start_window_coverage_used",
    "start_window_frame_annotation_stream_id",
    "post_window_frame_uuid",
    "post_window_frame_pts",
    "post_window_stream_session_id",
    "post_window_proof_used",
    "post_window_frame_annotation_stream_id",
    "post_window_cross_session_proof_used",
    "pre_window_truncated",
    "pre_window_policy",
    "frame_domain_session_policy",
    "frame_domain_proof_method",
    "replay_stop_strategy",
    "replay_duration_base_seconds",
    "replay_duration_anchor_before_start_guard_used",
    "replay_duration_anchor_before_start_guard_s",
    "replay_duration_before_slack_s",
    "replay_duration_extra_slack_s",
    "replay_duration_seconds",
    "runtime_epoch_id",
    "record_request_shard_id",
    "consumer_resolved_shard_id",
    "shard_mapping_version",
    "stream_session_id",
)


class ReplayPlanParityError(RuntimeError):
    """Raised when the pure planner diverges from the legacy payload oracle."""


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _int_or_none(value: object) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record_request_event_type(req: Mapping[str, Any]) -> str:
    event_type = str(req.get("event_type") or "").strip()
    if event_type:
        return event_type
    source_event_id = str(req.get("source_event_id") or "").strip()
    source_event_type = source_event_id.split(":", 1)[0]
    return source_event_type if source_event_type in PRIORITY_EVENT_TYPES else ""


def keyframe_from_request(req: Mapping[str, Any]) -> tuple[str | None, str]:
    for field in (
        "anchor_keyframe_uuid",
        "previous_keyframe_uuid",
        "keyframe_uuid",
    ):
        value = req.get(field)
        if value:
            return str(value), field
    return None, MISSING_KEYFRAME_ERROR


def request_identity(req: Mapping[str, Any]) -> str:
    return f"{req.get('source_event_id', '')}:{req.get('strategy', '')}"


def is_post_savant_media_request(req: Mapping[str, Any]) -> bool:
    replay_source_kind = str(req.get("replay_source_kind") or "").strip()
    evidence_topology = str(req.get("evidence_topology") or "").strip()
    annotation_policy = str(req.get("annotation_source_policy") or "").strip()
    metadata_domain = str(req.get("metadata_domain") or "").strip()
    return (
        replay_source_kind == "post_savant"
        or evidence_topology in POST_SAVANT_EVIDENCE_TOPOLOGIES
        or annotation_policy == "post_savant_sink_metadata_only"
        or (
            str(req.get("strategy") or "").strip() == "savant_replay"
            and metadata_domain == "video_frame"
        )
    )


def normalize_record_request(
    req: Mapping[str, Any],
    *,
    default_pre_seconds: int,
    default_post_seconds: int,
) -> NormalizedRecordRequest:
    raw = dict(req)
    source_event_id = str(raw.get("source_event_id") or "")
    strategy = str(raw.get("strategy") or "")
    keyframe_uuid, keyframe_source = keyframe_from_request(raw)
    return NormalizedRecordRequest(
        request_id=request_identity(raw),
        event_id=str(raw.get("event_id", "")),
        source_event_id=source_event_id,
        source_id=str(raw.get("source_id") or ""),
        camera_id=str(raw.get("camera_id") or ""),
        event_ts_ms=int(raw.get("event_ts_ms", 0)),
        event_type=record_request_event_type(raw),
        strategy=strategy,
        pre_seconds=int(raw.get("pre_seconds", default_pre_seconds)),
        post_seconds=int(raw.get("post_seconds", default_post_seconds)),
        keyframe_uuid=keyframe_uuid,
        keyframe_source=keyframe_source,
        post_savant_media_request=is_post_savant_media_request(raw),
        canonical_json=_canonical_json(raw),
    )


def requested_pts_window(
    req: Mapping[str, Any],
    *,
    pre_seconds: int,
    post_seconds: int,
) -> tuple[int | None, int | None]:
    event_frame_pts = _int_or_none(req.get("event_frame_pts") or req.get("frame_pts"))
    requested_start_pts = _int_or_none(req.get("requested_start_pts"))
    requested_end_pts = _int_or_none(req.get("requested_end_pts"))
    if event_frame_pts is not None:
        if requested_start_pts is None:
            requested_start_pts = max(
                0,
                int(event_frame_pts) - int(pre_seconds) * PTS_TIME_BASE,
            )
        if requested_end_pts is None:
            requested_end_pts = int(event_frame_pts) + int(post_seconds) * PTS_TIME_BASE
    return requested_start_pts, requested_end_pts


def _uuid7_timestamp_ms(value: str) -> int | None:
    try:
        return int((UUID(value).int >> 80) & ((1 << 48) - 1))
    except (TypeError, ValueError, AttributeError):
        return None


def replay_anchor_lookup_ts_ms(
    req: Mapping[str, Any],
    *,
    pre_seconds: int,
    post_seconds: int = 0,
    anchor_strategy: str,
) -> int:
    del pre_seconds
    event_ts_ms = int(req.get("event_ts_ms", 0) or 0)
    if anchor_strategy in {
        REPLAY_ANCHOR_STRATEGY_EVENT_START,
        REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME,
    }:
        frame_uuid_ms = _uuid7_timestamp_ms(str(req.get("frame_uuid") or ""))
        if frame_uuid_ms is not None:
            return frame_uuid_ms + int(max(post_seconds, 0) * 1000)
        if event_ts_ms > 0:
            return event_ts_ms + int(max(post_seconds, 0) * 1000)
    return event_ts_ms


def should_lookup_replay_anchor(anchor_strategy: str) -> bool:
    return anchor_strategy in {
        REPLAY_ANCHOR_STRATEGY_EVENT_START,
        REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME,
    }


def replay_anchor_selection(anchor_strategy: str) -> str:
    return "strict_at_or_after" if should_lookup_replay_anchor(anchor_strategy) else "nearest"


def apply_replay_anchor_to_request(
    req: Mapping[str, Any],
    *,
    proofs: ReplayFrameDomainProofs,
    anchor_keyframe_uuid: str,
    anchor_keyframe_pts: int,
    anchor_keyframe_source: str,
    pre_seconds: int,
    post_seconds: int,
    replay_duration_extra_slack_s: float = 0.0,
) -> dict[str, Any]:
    updated = dict(req)
    start_window_frame = proofs.start_window_frame
    post_window_frame = proofs.post_window_frame
    if not anchor_keyframe_uuid or anchor_keyframe_pts is None:
        raise ValueError(MISSING_ANCHOR_KEYFRAME_PTS_ERROR)
    event_frame_uuid = (
        str(updated.get("event_frame_uuid") or updated.get("frame_uuid") or "") or None
    )
    event_frame_pts = _int_or_none(
        updated.get("event_frame_pts") or updated.get("frame_pts")
    )
    if event_frame_uuid:
        updated["event_frame_uuid"] = event_frame_uuid
    if event_frame_pts is not None:
        updated.setdefault("event_frame_pts", event_frame_pts)
        original_requested_start_pts = _int_or_none(updated.get("requested_start_pts"))
        updated.setdefault(
            "requested_start_pts",
            max(0, int(event_frame_pts) - int(pre_seconds) * PTS_TIME_BASE),
        )
        updated.setdefault(
            "requested_end_pts",
            int(event_frame_pts) + int(post_seconds) * PTS_TIME_BASE,
        )
        requested_start_pts = _int_or_none(updated.get("requested_start_pts"))
        requested_end_pts = _int_or_none(updated.get("requested_end_pts"))
        effective_start_pts = (
            int(proofs.effective_start_pts)
            if proofs.effective_start_pts
            else requested_start_pts
        )
        if (
            requested_start_pts is not None
            and effective_start_pts is not None
            and int(effective_start_pts) > int(requested_start_pts)
        ):
            updated["original_requested_start_pts"] = int(requested_start_pts)
            updated["effective_start_pts"] = int(effective_start_pts)
            updated["requested_start_pts"] = int(effective_start_pts)
            updated["pre_window_truncated"] = True
            updated["pre_window_policy"] = PRE_WINDOW_POLICY_TRUNCATED
            updated["requested_pre_window_seconds"] = max(
                (
                    int(event_frame_pts)
                    - int(original_requested_start_pts or requested_start_pts)
                )
                / PTS_TIME_BASE,
                0.0,
            )
            updated["effective_pre_window_seconds"] = max(
                (int(event_frame_pts) - int(effective_start_pts)) / PTS_TIME_BASE,
                0.0,
            )
            updated["pre_window_truncated_seconds"] = max(
                (
                    int(effective_start_pts)
                    - int(original_requested_start_pts or requested_start_pts)
                )
                / PTS_TIME_BASE,
                0.0,
            )
            requested_start_pts = int(effective_start_pts)
        else:
            updated["pre_window_truncated"] = False
            updated["pre_window_policy"] = PRE_WINDOW_POLICY_FULL
            if requested_start_pts is not None:
                updated["effective_start_pts"] = int(requested_start_pts)
        if requested_start_pts is not None:
            updated["replay_offset_seconds"] = max(
                (int(anchor_keyframe_pts) - int(requested_start_pts))
                / PTS_TIME_BASE,
                0.0,
            )
        else:
            updated["replay_offset_seconds"] = 0.0
        if (
            requested_start_pts is not None
            and requested_end_pts is not None
            and requested_end_pts > requested_start_pts
        ):
            decodable_start_pts = (
                start_window_frame.keyframe_pts
                if start_window_frame.keyframe_pts is not None
                else start_window_frame.frame_pts
            )
            job_start_pts = min(
                int(anchor_keyframe_pts),
                int(requested_start_pts),
                int(decodable_start_pts),
            )
            replay_duration_base_seconds = max(
                (int(requested_end_pts) - job_start_pts) / PTS_TIME_BASE,
                (int(requested_end_pts) - int(requested_start_pts))
                / PTS_TIME_BASE,
            )
            replay_duration_seconds = replay_duration_base_seconds
            anchor_before_start_guard_s = 0.0
            if int(anchor_keyframe_pts) <= int(requested_start_pts):
                anchor_before_start_guard_s = max(
                    float(pre_seconds + post_seconds),
                    1.0,
                ) + 1.0
                replay_duration_seconds += anchor_before_start_guard_s
            extra_slack_s = max(float(replay_duration_extra_slack_s), 0.0)
            updated["replay_duration_base_seconds"] = replay_duration_base_seconds
            updated["replay_duration_anchor_before_start_guard_used"] = (
                anchor_before_start_guard_s > 0
            )
            updated["replay_duration_anchor_before_start_guard_s"] = (
                anchor_before_start_guard_s
            )
            updated["replay_duration_before_slack_s"] = replay_duration_seconds
            if extra_slack_s:
                replay_duration_seconds += extra_slack_s
                updated["replay_duration_extra_slack_s"] = extra_slack_s
            updated["replay_duration_seconds"] = replay_duration_seconds
        else:
            extra_slack_s = max(float(replay_duration_extra_slack_s), 0.0)
            updated["replay_duration_base_seconds"] = float(pre_seconds + post_seconds)
            updated["replay_duration_anchor_before_start_guard_used"] = False
            updated["replay_duration_anchor_before_start_guard_s"] = 0.0
            updated["replay_duration_before_slack_s"] = float(pre_seconds + post_seconds)
            if extra_slack_s:
                updated["replay_duration_extra_slack_s"] = extra_slack_s
            updated["replay_duration_seconds"] = (
                float(pre_seconds + post_seconds) + extra_slack_s
            )
    updated["anchor_keyframe_uuid"] = anchor_keyframe_uuid
    updated["anchor_keyframe_pts"] = int(anchor_keyframe_pts)
    updated["anchor_keyframe_source"] = anchor_keyframe_source
    updated["evidence_anchor_strategy"] = "uuid_first_pts_verified"
    updated["start_window_frame_uuid"] = start_window_frame.frame_uuid
    updated["start_window_frame_pts"] = start_window_frame.frame_pts
    updated["start_window_stream_session_id"] = start_window_frame.stream_session_id
    updated["start_window_frame_annotation_stream_id"] = start_window_frame.stream_id
    updated["post_window_frame_uuid"] = post_window_frame.frame_uuid
    updated["post_window_frame_pts"] = post_window_frame.frame_pts
    updated["post_window_stream_session_id"] = post_window_frame.stream_session_id
    updated["post_window_frame_annotation_stream_id"] = post_window_frame.stream_id
    updated["post_window_proof_used"] = True
    updated["start_window_coverage_used"] = True
    requested_stream_session_id = str(updated.get("stream_session_id") or "")
    post_window_cross_session = bool(
        requested_stream_session_id
        and post_window_frame.stream_session_id
        and post_window_frame.stream_session_id != requested_stream_session_id
    )
    updated["post_window_cross_session_proof_used"] = post_window_cross_session
    updated["pre_window_truncated"] = bool(updated.get("pre_window_truncated", False))
    updated["pre_window_policy"] = str(
        updated.get("pre_window_policy") or PRE_WINDOW_POLICY_FULL
    )
    updated["frame_domain_session_policy"] = (
        FRAME_DOMAIN_SESSION_POLICY_CROSS_POST
        if post_window_cross_session
        else FRAME_DOMAIN_SESSION_POLICY_STRICT
    )
    if bool(updated.get("pre_window_truncated")) and post_window_cross_session:
        updated["frame_domain_proof_method"] = (
            "frame_cache_truncated_start_window_and_cross_session_post_window_pts"
        )
    elif bool(updated.get("pre_window_truncated")):
        updated["frame_domain_proof_method"] = (
            "frame_cache_truncated_start_window_keyframe_reference_and_post_window_pts"
        )
    elif post_window_cross_session:
        updated["frame_domain_proof_method"] = (
            "frame_cache_start_window_keyframe_and_cross_session_post_window_pts"
        )
    else:
        updated["frame_domain_proof_method"] = (
            "frame_cache_start_window_keyframe_and_post_window_pts"
            if start_window_frame.anchor_method == "frame_annotation"
            else "frame_cache_start_window_keyframe_reference_and_post_window_pts"
        )
    return updated


def replay_job_labels(
    event_id: str,
    req: Mapping[str, Any],
    *,
    replay_offset_seconds: float | None = None,
    replay_duration_seconds: float | None = None,
) -> dict[str, str]:
    labels = {"event_id": event_id}
    for key in REPLAY_LABEL_FIELDS:
        value = req.get(key)
        if value is not None and value != "":
            labels[key] = str(value).lower() if isinstance(value, bool) else str(value)
    if replay_offset_seconds is not None:
        labels["replay_offset_seconds"] = f"{float(replay_offset_seconds):.6f}"
    if replay_duration_seconds is not None:
        labels["replay_duration_seconds"] = f"{float(replay_duration_seconds):.6f}"
    return labels


def clip_gate_decision(
    policy: ClipGatePolicy,
    context: ClipGateContext,
) -> ClipGateDecision:
    high_priority_types = set(policy.high_priority_event_types) or set(
        PRIORITY_EVENT_TYPES
    )
    is_priority_event = context.event_type in high_priority_types
    pressure_level = str(policy.pressure_level or "normal").lower()
    if pressure_level == "hard":
        return ClipGateDecision(
            allowed=False,
            reason="materialization_pressure_hard_limit",
            error_message="EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL=hard",
            terminal_defer=True,
            degrade_decision={
                "level": pressure_level,
                "action": "keep_manifest_stop_media_materialization",
            },
        )
    if pressure_level == "critical" and not is_priority_event:
        return ClipGateDecision(
            allowed=False,
            reason="materialization_pressure_critical_low_priority_deferred",
            error_message="critical pressure allows only high-priority materialization",
            terminal_defer=True,
            degrade_decision={"level": pressure_level, "action": "defer_low_priority"},
        )
    if pressure_level == "warning" and not is_priority_event:
        return ClipGateDecision(
            allowed=False,
            reason="materialization_pressure_warning_low_priority_deferred",
            error_message="warning pressure defers low-priority materialization",
            terminal_defer=True,
            degrade_decision={"level": pressure_level, "action": "defer_low_priority"},
        )
    if policy.run_once and policy.max_jobs_per_run > 0 and (
        context.jobs_created >= policy.max_jobs_per_run
    ):
        return ClipGateDecision(
            allowed=False,
            reason="max_jobs_reached",
            error_message="CLIP_WORKER_MAX_JOBS_PER_RUN reached",
            terminal_defer=True,
            quota_decision={
                "scope": "run",
                "limit": policy.max_jobs_per_run,
                "observed": context.jobs_created,
            },
        )
    event_type_limit = policy.quota_for(context.event_type)
    if event_type_limit > 0 and context.event_type_count >= event_type_limit:
        return ClipGateDecision(
            allowed=False,
            reason="event_type_quota_reached",
            error_message=(
                "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS reached for "
                f"{context.event_type}"
            ),
            terminal_defer=True,
            quota_decision={
                "scope": "event_type",
                "event_type": context.event_type,
                "limit": event_type_limit,
                "observed": context.event_type_count,
            },
        )
    if policy.max_concurrency > 0 and context.active_global >= policy.max_concurrency:
        return ClipGateDecision(
            allowed=False,
            reason="max_concurrent_reached",
            error_message="EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY reached",
            quota_decision={
                "scope": "global_concurrency",
                "limit": policy.max_concurrency,
                "observed": context.active_global,
            },
        )
    if (
        policy.max_concurrency_per_shard > 0
        and context.active_shard >= policy.max_concurrency_per_shard
    ):
        return ClipGateDecision(
            allowed=False,
            reason="max_concurrent_per_shard_reached",
            error_message="EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD reached",
            quota_decision={
                "scope": "shard_concurrency",
                "shard_id": context.shard_id,
                "limit": policy.max_concurrency_per_shard,
                "observed": context.active_shard,
            },
        )
    if (
        policy.max_concurrency_per_source > 0
        and context.active_source >= policy.max_concurrency_per_source
    ):
        return ClipGateDecision(
            allowed=False,
            reason="max_concurrent_per_source_reached",
            error_message="EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE reached",
            quota_decision={
                "scope": "source_concurrency",
                "source_id": context.source_id,
                "limit": policy.max_concurrency_per_source,
                "observed": context.active_source,
            },
        )
    if (
        policy.per_camera_cooldown_seconds > 0
        and context.camera_id
        and context.cooldown_gate_ts_ms - context.last_camera_job_ts_ms
        < policy.per_camera_cooldown_seconds * 1000
        and not is_priority_event
    ):
        return ClipGateDecision(
            allowed=False,
            reason="cooldown",
            error_message="CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS reached",
        )
    return ClipGateDecision(allowed=True)


def build_replay_payload(
    *,
    source_id: str,
    keyframe_uuid: str,
    pre_seconds: float,
    post_seconds: float,
    sink_endpoint: str,
    labels: Mapping[str, str] | None = None,
    stop_condition_mode: str = "frame_count",
    fallback_reason: str | None = None,
    fps: int = 30,
    force_constant_cadence: bool = True,
    offset_seconds_override: float | None = None,
    duration_seconds_override: float | None = None,
    ts_sync: bool = False,
) -> dict[str, Any]:
    normalized_labels = dict(labels or {})
    event_id = normalized_labels.get("event_id", "unknown")
    runtime_epoch_id = str(normalized_labels.get("runtime_epoch_id") or "").strip()
    resulting_stream_id = (
        f"replay-{runtime_epoch_id}-event-{event_id}"
        if runtime_epoch_id
        else f"replay-event-{event_id}"
    )
    effective_fps = fps if fps > 0 else 30
    frame_duration_nanos = int(PTS_TIME_BASE // effective_fps)
    expected_seconds = (
        float(duration_seconds_override)
        if duration_seconds_override is not None
        else float(pre_seconds) + float(post_seconds)
    )
    total_frames = int(round(expected_seconds * effective_fps))
    stop_condition: dict[str, Any]
    if stop_condition_mode == "ts_delta_sec":
        stop_condition = {"ts_delta_sec": {"max_delta_sec": expected_seconds}}
    else:
        stop_condition = {"frame_count": total_frames}
    frame_duration = {"secs": 0, "nanos": frame_duration_nanos}
    max_delivery_duration_s = max(
        MIN_DELIVERY_DURATION_S,
        int(
            math.ceil(
                max(float(expected_seconds), 0.0)
                + DELIVERY_DURATION_EXTRA_SLACK_S
            )
        ),
    )
    configuration: dict[str, Any] = {
        "ts_sync": bool(ts_sync),
        "skip_intermediary_eos": False,
        "send_eos": True,
        "stop_on_incorrect_ts": False,
        "stored_stream_id": source_id,
        "resulting_stream_id": resulting_stream_id,
        "routing_labels": "bypass",
        "max_idle_duration": {"secs": 10, "nanos": 0},
        "max_delivery_duration": {"secs": max_delivery_duration_s, "nanos": 0},
        "send_metadata_only": False,
        "labels": normalized_labels,
    }
    if force_constant_cadence:
        configuration.update(
            {
                "ts_discrepancy_fix_duration": frame_duration,
                "min_duration": frame_duration,
                "max_duration": frame_duration,
            }
        )
    payload: dict[str, Any] = {
        "sink": {"url": sink_endpoint, "options": dict(RELIABLE_SINK_OPTIONS)},
        "configuration": configuration,
        "stop_condition": stop_condition,
        "anchor_keyframe": keyframe_uuid,
        "anchor_wait_duration": {"secs": 1, "nanos": 0},
        "offset": {
            "seconds": (
                float(pre_seconds)
                if offset_seconds_override is None
                else float(offset_seconds_override)
            )
        },
        "attributes": [],
    }
    if fallback_reason is not None:
        payload["fallback_reason"] = fallback_reason
    return payload


def build_replay_plan(
    *,
    source_id: str,
    keyframe_uuid: str,
    pre_seconds: float,
    post_seconds: float,
    sink_endpoint: str,
    labels: Mapping[str, str] | None = None,
    stop_condition_mode: str = "frame_count",
    fallback_reason: str | None = None,
    fps: int = 30,
    force_constant_cadence: bool = True,
    offset_seconds_override: float | None = None,
    duration_seconds_override: float | None = None,
    ts_sync: bool = False,
) -> ReplayPlan:
    payload = build_replay_payload(
        source_id=source_id,
        keyframe_uuid=keyframe_uuid,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
        sink_endpoint=sink_endpoint,
        labels=labels,
        stop_condition_mode=stop_condition_mode,
        fallback_reason=fallback_reason,
        fps=fps,
        force_constant_cadence=force_constant_cadence,
        offset_seconds_override=offset_seconds_override,
        duration_seconds_override=duration_seconds_override,
        ts_sync=ts_sync,
    )
    canonical_payload_json = _canonical_json(payload)
    return ReplayPlan(
        schema_version="clip-replay-plan-v1",
        source_id=source_id,
        keyframe_uuid=keyframe_uuid,
        pre_seconds=float(pre_seconds),
        post_seconds=float(post_seconds),
        sink_endpoint=sink_endpoint,
        stop_condition_mode=stop_condition_mode,
        fps=int(fps),
        offset_seconds_override=offset_seconds_override,
        duration_seconds_override=duration_seconds_override,
        labels=tuple(sorted((str(key), str(value)) for key, value in (labels or {}).items())),
        canonical_payload_json=canonical_payload_json,
        plan_hash=hashlib.sha256(canonical_payload_json.encode("utf-8")).hexdigest(),
    )


def assert_replay_plan_parity(
    plan: ReplayPlan,
    legacy_payload: Mapping[str, Any],
) -> None:
    if plan.payload() != dict(legacy_payload):
        raise ReplayPlanParityError(
            f"clip_replay_plan_shadow_mismatch:{plan.plan_hash}"
        )
