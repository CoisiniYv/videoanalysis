"""Event worker — consume SecurityEvent from Redis Stream, insert into PostgreSQL, publish alerts."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Dict

import psycopg
from redis import Redis

from app.alert_policy import AlertPolicyDecision, AlertPolicyService
from app.alert_publisher import AlertPublisher
from app.config import Config, load_config
from app.record_request import RecordRequestPublisher, _resolve_source_id
from app.redis_consumer import RedisStreamConsumer
from app.repository import EventRepository, _evidence_task_initial_status

logger = logging.getLogger(__name__)

shutdown_requested = False

MIDTERM_BEHAVIOR_EVIDENCE_EVENT_TYPES = {"intrusion"}
MIDTERM_DEFAULT_EVIDENCE_POLICY = {
    "snapshot_required": True,
    "clip_required": True,
    "pre_seconds": 5,
    "post_seconds": 5,
}
MATERIALIZATION_RECORDABLE_TASK_STATUSES = {
    "pending",
    "materialization_pending",
}
RECORDING_POLICY_TERMINAL_SKIP_REASONS = {
    "cooldown",
    "event_type_mismatch",
    "max_requests_reached",
    "source_id_mismatch",
    "duplicate_record_request",
}
_MIN_EPOCH_MS = 946684800000  # 2000-01-01T00:00:00Z
_MAX_FUTURE_SKEW_MS = 24 * 60 * 60 * 1000
DEFAULT_RUNTIME_EPOCH_REDIS_KEY = "video_analytics:midterm:runtime_epoch"


@dataclass
class RecordingPolicyState:
    published_requests: int = 0
    last_recorded_at_ms: dict[str, int] = field(default_factory=dict)
    last_recorded_event_type: dict[str, str] = field(default_factory=dict)


def request_shutdown(signum: int, _frame: object) -> None:
    global shutdown_requested
    logger.info("shutdown requested by signal=%s", signum)
    shutdown_requested = True


def _parse_event(fields: Dict[bytes, bytes]) -> dict | None:
    """Parse a SecurityEvent dict from Redis stream fields.

    The ``data`` field contains the full JSON.  Top-level stream fields
    (source_event_id, event_type, camera_id, track_id) are used as
    supplementary validation.
    """
    data_raw = fields.get(b"data")
    if not data_raw:
        logger.warning("stream entry missing data field, skipping")
        return None

    try:
        event = json.loads(data_raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("failed to parse event JSON: %s", exc)
        return None

    required = ["source_event_id", "event_type", "camera_id"]
    for field in required:
        if not event.get(field):
            logger.warning("event missing required field=%s, skipping", field)
            return None

    return event


def _parse_person_observation(fields: Dict[bytes, bytes]) -> dict | None:
    """Parse one accepted person bbox observation from Redis stream fields."""

    data_raw = fields.get(b"data")
    if not data_raw:
        logger.warning("person observation stream entry missing data field, skipping")
        return None
    try:
        observation = json.loads(data_raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("failed to parse person observation JSON: %s", exc)
        return None

    required = [
        "source_observation_id",
        "source_id",
        "camera_id",
        "timestamp_ms",
        "person_bbox",
    ]
    for field_name in required:
        if observation.get(field_name) in (None, ""):
            logger.warning(
                "person observation missing required field=%s, skipping",
                field_name,
            )
            return None
    if observation.get("gate_status") != "accepted":
        logger.warning(
            "person observation gate_status=%s is not accepted, skipping",
            observation.get("gate_status"),
        )
        return None
    bbox = observation.get("person_bbox")
    if not isinstance(bbox, list) or len(bbox) < 4:
        logger.warning("person observation person_bbox is not xyxy list, skipping")
        return None
    try:
        observation["timestamp_ms"] = int(observation["timestamp_ms"])
        observation["person_bbox"] = [float(value) for value in bbox[:4]]
    except (TypeError, ValueError):
        logger.warning("person observation numeric fields invalid, skipping")
        return None
    return observation


def _get_evidence_task_status(
    repo: EventRepository,
    event: dict,
    event_id: str,
) -> str | None:
    """Return the task status created for this event.

    The real repository reads the persisted status. Tests and lightweight
    repositories may not implement that lookup, so fall back to the same initial
    status policy used by EventRepository.create_evidence_task().
    """
    if hasattr(repo, "get_evidence_task_status"):
        try:
            status = repo.get_evidence_task_status(event_id)
        except Exception:
            logger.exception("evidence_task status lookup failed event_id=%s", event_id)
            status = None
        if status:
            return str(status)

    status, _reason = _evidence_task_initial_status(event)
    return status


def _mark_recording_policy_skipped(
    repo: EventRepository,
    *,
    event_id: str | None,
    skip_reason: str,
) -> None:
    if not event_id or skip_reason not in RECORDING_POLICY_TERMINAL_SKIP_REASONS:
        return
    reason = f"recording_policy_skipped:{skip_reason}"
    try:
        if hasattr(repo, "mark_evidence_materialization_skipped"):
            repo.mark_evidence_materialization_skipped(event_id, reason=reason)
        elif hasattr(repo, "set_evidence_status"):
            repo.set_evidence_status(
                event_id=event_id,
                status="materialization_skipped",
                error_message=reason,
            )
    except Exception:
        logger.exception(
            "evidence_task skip status update failed event_id=%s reason=%s",
            event_id,
            reason,
        )


def _recording_gate_ts_ms(event: dict) -> int:
    """Return a comparable timestamp for recording cooldown decisions.

    Behavior events carry epoch millisecond timestamps. Some face observations
    carry stream PTS-relative milliseconds; those are valid for frame identity
    but not for wall-clock cooldown comparisons, so use current wall-clock for
    the recording gate only.
    """
    now_ms = int(time.time() * 1000)
    try:
        ts_ms = int(event.get("event_ts_ms", 0) or 0)
    except (TypeError, ValueError):
        ts_ms = 0
    if ts_ms < _MIN_EPOCH_MS or ts_ms > now_ms + _MAX_FUTURE_SKEW_MS:
        return now_ms
    return ts_ms


def _recording_cooldown_key(source_id: str, event_type: str) -> str:
    event_part = str(event_type or "_unknown_event")
    return f"{source_id}:{event_part}" if source_id else event_part


def _current_runtime_epoch_id(redis_client: Redis) -> str:
    key = os.getenv("RUNTIME_EPOCH_REDIS_KEY", DEFAULT_RUNTIME_EPOCH_REDIS_KEY)
    try:
        value = redis_client.get(key)
    except Exception:
        logger.exception("runtime epoch redis lookup failed key=%s", key)
        return ""
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = text
    if isinstance(parsed, dict):
        return str(parsed.get("runtime_epoch_id") or "")
    return str(parsed or "")


def _apply_runtime_epoch(event: dict, runtime_epoch_id: str) -> None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        event["payload"] = payload
    media = payload.get("media")
    if not isinstance(media, dict):
        media = {}
        payload["media"] = media
    existing_epoch = str(
        event.get("runtime_epoch_id")
        or payload.get("runtime_epoch_id")
        or media.get("runtime_epoch_id")
        or ""
    )
    event_epoch = str(runtime_epoch_id or existing_epoch or "")
    if not event_epoch:
        return
    if runtime_epoch_id and existing_epoch and existing_epoch != runtime_epoch_id:
        logger.warning(
            "runtime_epoch_overridden source_event_id=%s old_runtime_epoch_id=%s "
            "current_runtime_epoch_id=%s",
            event.get("source_event_id", ""),
            existing_epoch,
            runtime_epoch_id,
        )
    event["runtime_epoch_id"] = event_epoch
    payload["runtime_epoch_id"] = event_epoch
    media["runtime_epoch_id"] = event_epoch


def _handle_event(
    event: dict,
    msg_id: str,
    repo: EventRepository,
    consumer: RedisStreamConsumer,
    alert_publisher: AlertPublisher | None = None,
    record_publisher: RecordRequestPublisher | None = None,
    *,
    alert_policy_service: AlertPolicyService | None = None,
    recording_state: RecordingPolicyState | None = None,
    recording_event_types: tuple[str, ...] = (),
    recording_source_id: str = "",
    recording_max_requests_per_run: int = 0,
    recording_cooldown_seconds: int = 0,
    recording_cooldown_grace_ms: int = 1000,
    recording_pre_seconds: int = MIDTERM_DEFAULT_EVIDENCE_POLICY["pre_seconds"],
    recording_post_seconds: int = MIDTERM_DEFAULT_EVIDENCE_POLICY["post_seconds"],
    runtime_epoch_id: str = "",
) -> tuple[bool, str | None]:
    """Process a single event: insert into DB, publish alert + record request, then ACK.

    Returns ``(newly_inserted, event_id)``.
    Alert is published only for new inserts.
    Record request is published only when clip_status is unset (idempotent).
    Failures in alert/record publishing do not block ACK.
    """
    _apply_default_evidence_policy(event)
    _apply_recording_window(event, recording_pre_seconds, recording_post_seconds)
    _apply_runtime_epoch(event, runtime_epoch_id)
    event_id = None
    try:
        event_id = repo.insert_event(event)
    except Exception:
        logger.exception(
            "db insert failed for source_event_id=%s msg_id=%s",
            event.get("source_event_id"),
            msg_id,
        )
        return False, None

    newly_inserted = event_id is not None
    source_event_id = event.get("source_event_id", "")

    alert_policy_decision = AlertPolicyDecision("emit")
    if newly_inserted and event_id and alert_policy_service is not None:
        try:
            alert_policy_decision = alert_policy_service.apply(event, event_id)
        except Exception:
            logger.exception(
                "alert policy check failed for source_event_id=%s",
                source_event_id,
            )
            alert_policy_decision = AlertPolicyDecision("emit")
        if alert_policy_decision.suppressed:
            logger.info(
                "event_suppressed source_event_id=%s camera_id=%s reason=%s",
                source_event_id,
                event.get("camera_id", ""),
                alert_policy_decision.reason,
            )

    if (
        newly_inserted
        and not alert_policy_decision.suppressed
        and alert_publisher is not None
    ):
        try:
            alert_publisher.publish(event, event_id=event_id)
        except Exception:
            logger.exception(
                "alert publish failed for source_event_id=%s",
                source_event_id,
            )

    evidence_task_status: str | None = None
    if (
        newly_inserted
        and not alert_policy_decision.suppressed
        and event_id
        and _requires_evidence(event)
    ):
        if hasattr(repo, "create_evidence_task"):
            try:
                repo.create_evidence_task(event, event_id)
                evidence_task_status = _get_evidence_task_status(repo, event, event_id)
            except Exception:
                logger.exception(
                    "evidence_task creation failed for source_event_id=%s",
                    source_event_id,
                )
        else:
            logger.debug(
                "evidence_task skipped: repository has no create_evidence_task"
            )

    # Record request — idempotent: check DB clip_status before publishing
    if record_publisher is not None and not alert_policy_decision.suppressed:
        clip_required = event.get("clip_required", False)
        payload_media = (event.get("payload") or {}).get("media", {})
        payload_clip = (
            payload_media.get("clip_required", False)
            if isinstance(payload_media, dict)
            else False
        )
        source_id = _resolve_source_id(event, "")
        event_type = event.get("event_type", "")
        if clip_required or payload_clip:
            existing_status = repo.get_media_clip_status(source_event_id)
            allowed = True
            skip_reason = ""
            recording_gate_ts_ms = _recording_gate_ts_ms(event)
            cooldown_key = _recording_cooldown_key(source_id, event_type)

            if recording_event_types and event_type not in recording_event_types:
                allowed = False
                skip_reason = "event_type_mismatch"
            elif evidence_task_status not in MATERIALIZATION_RECORDABLE_TASK_STATUSES:
                allowed = False
                skip_reason = f"evidence_task_status={evidence_task_status or 'missing'}"
            elif recording_source_id and source_id != recording_source_id:
                allowed = False
                skip_reason = "source_id_mismatch"
            elif recording_state is not None:
                if (
                    recording_max_requests_per_run > 0
                    and recording_state.published_requests
                    >= recording_max_requests_per_run
                ):
                    allowed = False
                    skip_reason = "max_requests_reached"
                elif recording_cooldown_seconds > 0:
                    last_recorded_at = recording_state.last_recorded_at_ms.get(
                        cooldown_key
                    )
                    last_recorded_event_type = (
                        recording_state.last_recorded_event_type.get(cooldown_key)
                    )
                    cooldown_threshold_ms = max(
                        0,
                        recording_cooldown_seconds * 1000
                        - max(0, recording_cooldown_grace_ms),
                    )
                    cooldown_active = (
                        last_recorded_at is not None
                        and recording_gate_ts_ms - last_recorded_at
                        < cooldown_threshold_ms
                    )
                    if cooldown_active:
                        allowed = False
                        skip_reason = "cooldown"

            if allowed and source_event_id and record_publisher.has_request(
                source_event_id, "savant_replay"
            ):
                allowed = False
                skip_reason = "duplicate_record_request"
            elif allowed and existing_status and existing_status not in (
                "", "not_implemented", "not_required"
            ):
                allowed = False
                skip_reason = f"already_has_clip_status={existing_status}"

            if not allowed:
                logger.info(
                    "record_request_skipped source_event_id=%s source_id=%s "
                    "event_type=%s reason=%s",
                    source_event_id,
                    source_id,
                    event_type,
                    skip_reason,
                )
                if (
                    newly_inserted
                    and evidence_task_status in MATERIALIZATION_RECORDABLE_TASK_STATUSES
                ):
                    _mark_recording_policy_skipped(
                        repo,
                        event_id=event_id,
                        skip_reason=skip_reason,
                    )
            else:
                logger.info(
                    "record_request_check source_event_id=%s top_clip_required=%s "
                    "payload_clip_required=%s media_strategy=%s",
                    source_event_id,
                    clip_required,
                    payload_clip,
                    payload_media.get("recording_strategy", "")
                    if isinstance(payload_media, dict)
                    else "",
                )
                try:
                    msg = record_publisher.publish(event, event_id=event_id or "")
                    if msg:
                        if event_id:
                            repo.set_clip_status(event_id, "pending")
                        if recording_state is not None:
                            recording_state.published_requests += 1
                            if source_id:
                                recording_state.last_recorded_at_ms[cooldown_key] = (
                                    recording_gate_ts_ms
                                )
                                recording_state.last_recorded_event_type[
                                    cooldown_key
                                ] = event_type
                except Exception:
                    logger.exception(
                        "record_request publish failed for source_event_id=%s",
                        source_event_id,
                    )

    if not consumer.ack(msg_id):
        logger.error("ack failed for msg_id=%s", msg_id)
    else:
        logger.debug(
            "acked msg_id=%s source_event_id=%s inserted=%s",
            msg_id,
            source_event_id,
            newly_inserted,
        )

    return newly_inserted, event_id


def _requires_evidence(event: dict) -> bool:
    policy = event.get("evidence_policy") or {}
    policy_snapshot = (
        policy.get("snapshot_required", False) if isinstance(policy, dict) else False
    )
    policy_clip = (
        policy.get("clip_required", False) if isinstance(policy, dict) else False
    )
    return bool(
        event.get("snapshot_required", False)
        or event.get("clip_required", False)
        or policy_snapshot
        or policy_clip
    )


def _apply_default_evidence_policy(event: dict) -> None:
    """Enable default intrusion evidence for legacy behavior events."""
    event_type = event.get("event_type", "")
    if event_type not in MIDTERM_BEHAVIOR_EVIDENCE_EVENT_TYPES:
        return

    if _requires_evidence(event):
        return

    event["snapshot_required"] = True
    event["clip_required"] = True

    policy = event.get("evidence_policy")
    if not isinstance(policy, dict):
        policy = {}
    event["evidence_policy"] = {**MIDTERM_DEFAULT_EVIDENCE_POLICY, **policy}

    payload = event.setdefault("payload", {})
    if not isinstance(payload, dict):
        payload = {}
        event["payload"] = payload
    media = payload.setdefault("media", {})
    if not isinstance(media, dict):
        media = {}
        payload["media"] = media
    media.setdefault("snapshot_status", "not_implemented")
    media.setdefault("clip_status", "not_implemented")
    media.setdefault("recording_strategy", "reserved")
    media["snapshot_required"] = True
    media["clip_required"] = True
    media.setdefault("pre_seconds", MIDTERM_DEFAULT_EVIDENCE_POLICY["pre_seconds"])
    media.setdefault("post_seconds", MIDTERM_DEFAULT_EVIDENCE_POLICY["post_seconds"])
    media.setdefault("source_id", event.get("source_id", ""))
    media.setdefault("event_ts_ms", event.get("event_ts_ms", 0))
    media.setdefault("frame_uuid", event.get("frame_uuid"))
    media.setdefault("keyframe_uuid", event.get("keyframe_uuid"))


def _apply_recording_window(
    event: dict,
    pre_seconds: int,
    post_seconds: int,
) -> None:
    """Fill missing recording-window fields on clip-required events."""
    payload = event.get("payload")
    payload_media = payload.get("media", {}) if isinstance(payload, dict) else {}
    clip_required = bool(
        event.get("clip_required", False)
        or (
            isinstance(payload_media, dict)
            and payload_media.get("clip_required", False)
        )
    )
    if not clip_required:
        return

    policy = event.get("evidence_policy")
    if not isinstance(policy, dict):
        policy = {}
    event["evidence_policy"] = policy

    if not isinstance(payload, dict):
        payload = {}
        event["payload"] = payload
    media = payload.setdefault("media", {})
    if not isinstance(media, dict):
        media = {}
        payload["media"] = media

    def _has_value(value: object) -> bool:
        return value is not None and str(value).strip() != ""

    def _int_value(value: object) -> int | None:
        try:
            return max(int(float(value)), 0)
        except (TypeError, ValueError):
            return None

    if not _has_value(policy.get("pre_seconds")):
        if isinstance(payload_media, dict) and _has_value(payload_media.get("pre_seconds")):
            media_pre_seconds = _int_value(payload_media["pre_seconds"])
            if media_pre_seconds is not None:
                policy["pre_seconds"] = media_pre_seconds
        elif pre_seconds >= 0:
            policy["pre_seconds"] = int(pre_seconds)
    if not _has_value(policy.get("post_seconds")):
        if isinstance(payload_media, dict) and _has_value(payload_media.get("post_seconds")):
            media_post_seconds = _int_value(payload_media["post_seconds"])
            if media_post_seconds is not None:
                policy["post_seconds"] = media_post_seconds
        elif post_seconds >= 0:
            policy["post_seconds"] = int(post_seconds)

    if not _has_value(media.get("pre_seconds")) and _has_value(policy.get("pre_seconds")):
        policy_pre_seconds = _int_value(policy["pre_seconds"])
        if policy_pre_seconds is not None:
            media["pre_seconds"] = policy_pre_seconds
    if not _has_value(media.get("post_seconds")) and _has_value(policy.get("post_seconds")):
        policy_post_seconds = _int_value(policy["post_seconds"])
        if policy_post_seconds is not None:
            media["post_seconds"] = policy_post_seconds


def _process_batch(
    messages: list[tuple[str, dict[bytes, bytes]]],
    repo: EventRepository,
    consumer: RedisStreamConsumer,
    alert_publisher: AlertPublisher | None = None,
    record_publisher: RecordRequestPublisher | None = None,
    *,
    alert_policy_service: AlertPolicyService | None = None,
    recording_state: RecordingPolicyState | None = None,
    recording_event_types: tuple[str, ...] = (),
    recording_source_id: str = "",
    recording_max_requests_per_run: int = 0,
    recording_cooldown_seconds: int = 0,
    recording_cooldown_grace_ms: int = 1000,
    recording_pre_seconds: int = MIDTERM_DEFAULT_EVIDENCE_POLICY["pre_seconds"],
    recording_post_seconds: int = MIDTERM_DEFAULT_EVIDENCE_POLICY["post_seconds"],
    runtime_epoch_id: str = "",
) -> tuple[int, int]:
    inserted = 0
    duplicates = 0
    for msg_id, fields in messages:
        event = _parse_event(fields)
        if event is None:
            consumer.ack(msg_id)
            continue

        new, _ = _handle_event(
            event,
            msg_id,
            repo,
            consumer,
            alert_publisher,
            record_publisher,
            alert_policy_service=alert_policy_service,
            recording_state=recording_state,
            recording_event_types=recording_event_types,
            recording_source_id=recording_source_id,
            recording_max_requests_per_run=recording_max_requests_per_run,
            recording_cooldown_seconds=recording_cooldown_seconds,
            recording_cooldown_grace_ms=recording_cooldown_grace_ms,
            recording_pre_seconds=recording_pre_seconds,
            recording_post_seconds=recording_post_seconds,
            runtime_epoch_id=runtime_epoch_id,
        )
        if new:
            inserted += 1
        else:
            duplicates += 1
    return inserted, duplicates


def _handle_person_observation(
    observation: dict,
    msg_id: str,
    repo: EventRepository,
    consumer: RedisStreamConsumer,
) -> str:
    """Insert one person bbox observation and ACK on success/duplicate."""

    try:
        obs_id = repo.insert_person_bbox_observation(observation)
    except Exception:
        logger.exception(
            "person observation db insert failed source_observation_id=%s msg_id=%s",
            observation.get("source_observation_id"),
            msg_id,
        )
        return "failed"

    if not consumer.ack(msg_id):
        logger.error("person observation ack failed msg_id=%s", msg_id)
    return "inserted" if obs_id is not None else "duplicate"


def _process_person_observation_batch(
    messages: list[tuple[str, dict[bytes, bytes]]],
    repo: EventRepository,
    consumer: RedisStreamConsumer,
) -> tuple[int, int, int, int]:
    inserted = 0
    duplicates = 0
    skipped = 0
    failed = 0
    for msg_id, fields in messages:
        observation = _parse_person_observation(fields)
        if observation is None:
            skipped += 1
            consumer.ack(msg_id)
            continue
        outcome = _handle_person_observation(observation, msg_id, repo, consumer)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "duplicate":
            duplicates += 1
        else:
            failed += 1
    return inserted, duplicates, skipped, failed


def connect_redis(cfg: Config) -> Redis:
    client = Redis.from_url(cfg.redis_url, decode_responses=False)
    client.ping()
    logger.info("connected to redis url=%s", cfg.redis_url)
    return client


def connect_postgres(cfg: Config) -> psycopg.Connection:
    conn = psycopg.connect(cfg.database_url, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    logger.info("connected to postgres url=%s", cfg.database_url)
    return conn


def run_worker(
    cfg: Config,
    redis_client: Redis,
    pg_conn: psycopg.Connection,
) -> None:
    consumer = RedisStreamConsumer(
        redis_client, cfg.event_stream, cfg.consumer_group, cfg.consumer_name
    )
    consumer.ensure_group()
    person_consumer: RedisStreamConsumer | None = None
    if cfg.person_observation_enabled:
        person_consumer = RedisStreamConsumer(
            redis_client,
            cfg.person_observation_stream,
            cfg.person_observation_consumer_group,
            cfg.person_observation_consumer_name,
            start_id=cfg.person_observation_consumer_start_id,
        )
        person_consumer.ensure_group()
    repo = EventRepository(pg_conn)
    alert_policy_service = AlertPolicyService(repo)
    alert_publisher = AlertPublisher(redis_client, cfg.alert_stream)
    record_publisher = (
        RecordRequestPublisher(
            redis_client,
            cfg.record_request_stream,
            default_replay_source_id=cfg.default_replay_source_id,
            default_pre_seconds=cfg.recording_pre_seconds,
            default_post_seconds=cfg.recording_post_seconds,
            dedupe_ttl_seconds=cfg.record_request_dedupe_ttl_seconds,
        )
        if cfg.recording_enabled
        else None
    )
    recording_state = RecordingPolicyState()

    logger.info(
        "worker started stream=%s group=%s consumer=%s alert_stream=%s "
        "recording_enabled=%s record_request_stream=%s recording_event_types=%s "
        "recording_source_id=%s recording_max_requests_per_run=%s "
        "recording_cooldown_seconds=%s recording_cooldown_grace_ms=%s "
        "recording_pre_seconds=%s "
        "recording_post_seconds=%s record_request_dedupe_ttl_seconds=%s "
        "person_observation_enabled=%s "
        "person_observation_stream=%s person_observation_group=%s "
        "person_observation_start_id=%s person_observation_batch_size=%s",
        cfg.event_stream,
        cfg.consumer_group,
        cfg.consumer_name,
        cfg.alert_stream,
        cfg.recording_enabled,
        cfg.record_request_stream,
        cfg.recording_event_types,
        cfg.recording_source_id,
        cfg.recording_max_requests_per_run,
        cfg.recording_cooldown_seconds,
        cfg.recording_cooldown_grace_ms,
        cfg.recording_pre_seconds,
        cfg.recording_post_seconds,
        cfg.record_request_dedupe_ttl_seconds,
        cfg.person_observation_enabled,
        cfg.person_observation_stream,
        cfg.person_observation_consumer_group,
        cfg.person_observation_consumer_start_id,
        cfg.person_observation_batch_size,
    )

    total_inserted = 0
    total_duplicates = 0
    total_person_inserted = 0
    total_person_duplicates = 0
    total_person_skipped = 0
    total_person_failed = 0
    last_report = time.monotonic()

    while not shutdown_requested:
        try:
            if person_consumer is not None:
                person_pending = person_consumer.read_pending(
                    count=cfg.person_observation_batch_size
                )
                if person_pending:
                    pins, pdup, pskip, pfail = _process_person_observation_batch(
                        person_pending,
                        repo,
                        person_consumer,
                    )
                    total_person_inserted += pins
                    total_person_duplicates += pdup
                    total_person_skipped += pskip
                    total_person_failed += pfail
                    if pins or pdup or pskip or pfail:
                        logger.info(
                            "person observation pending batch: inserted=%d "
                            "duplicates=%d skipped=%d failed=%d",
                            pins,
                            pdup,
                            pskip,
                            pfail,
                        )

                person_new = person_consumer.read_new(
                    count=cfg.person_observation_batch_size, block_ms=1
                )
                if person_new:
                    pins, pdup, pskip, pfail = _process_person_observation_batch(
                        person_new,
                        repo,
                        person_consumer,
                    )
                    total_person_inserted += pins
                    total_person_duplicates += pdup
                    total_person_skipped += pskip
                    total_person_failed += pfail
                    logger.info(
                        "person observation new batch: inserted=%d duplicates=%d "
                        "skipped=%d failed=%d",
                        pins,
                        pdup,
                        pskip,
                        pfail,
                    )

            # 1. Process pending messages (recovery)
            pending = consumer.read_pending(count=cfg.batch_size)
            if pending:
                runtime_epoch_id = _current_runtime_epoch_id(redis_client)
                ins, dup = _process_batch(
                    pending,
                    repo,
                    consumer,
                    alert_publisher,
                    record_publisher,
                    alert_policy_service=alert_policy_service,
                    recording_state=recording_state,
                    recording_event_types=cfg.recording_event_types,
                    recording_source_id=cfg.recording_source_id,
                    recording_max_requests_per_run=cfg.recording_max_requests_per_run,
                    recording_cooldown_seconds=cfg.recording_cooldown_seconds,
                    recording_cooldown_grace_ms=cfg.recording_cooldown_grace_ms,
                    recording_pre_seconds=cfg.recording_pre_seconds,
                    recording_post_seconds=cfg.recording_post_seconds,
                    runtime_epoch_id=runtime_epoch_id,
                )
                total_inserted += ins
                total_duplicates += dup
                if ins or dup:
                    logger.info(
                        "pending batch: inserted=%d duplicates=%d", ins, dup
                    )

            # 2. Read new messages
            new_msgs = consumer.read_new(
                count=cfg.batch_size, block_ms=cfg.poll_timeout_ms
            )
            if new_msgs:
                runtime_epoch_id = _current_runtime_epoch_id(redis_client)
                ins, dup = _process_batch(
                    new_msgs,
                    repo,
                    consumer,
                    alert_publisher,
                    record_publisher,
                    alert_policy_service=alert_policy_service,
                    recording_state=recording_state,
                    recording_event_types=cfg.recording_event_types,
                    recording_source_id=cfg.recording_source_id,
                    recording_max_requests_per_run=cfg.recording_max_requests_per_run,
                    recording_cooldown_seconds=cfg.recording_cooldown_seconds,
                    recording_cooldown_grace_ms=cfg.recording_cooldown_grace_ms,
                    recording_pre_seconds=cfg.recording_pre_seconds,
                    recording_post_seconds=cfg.recording_post_seconds,
                    runtime_epoch_id=runtime_epoch_id,
                )
                total_inserted += ins
                total_duplicates += dup

            # 3. Periodic summary
            now = time.monotonic()
            if now - last_report >= 60:
                logger.info(
                    "worker summary: total_inserted=%d total_duplicates=%d "
                    "total_person_inserted=%d total_person_duplicates=%d "
                    "total_person_skipped=%d total_person_failed=%d",
                    total_inserted,
                    total_duplicates,
                    total_person_inserted,
                    total_person_duplicates,
                    total_person_skipped,
                    total_person_failed,
                )
                last_report = now

        except Exception:
            logger.exception("worker loop error, sleeping 1s")
            time.sleep(1)

    logger.info(
        "worker stopped: total_inserted=%d total_duplicates=%d "
        "total_person_inserted=%d total_person_duplicates=%d "
        "total_person_skipped=%d total_person_failed=%d",
        total_inserted,
        total_duplicates,
        total_person_inserted,
        total_person_duplicates,
        total_person_skipped,
        total_person_failed,
    )
