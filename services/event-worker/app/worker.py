"""Event worker — consume SecurityEvent from Redis Stream, insert into PostgreSQL, publish alerts."""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Dict

import psycopg
from redis import Redis

from app.alert_publisher import AlertPublisher
from app.config import Config, load_config
from app.record_request import RecordRequestPublisher, _resolve_source_id
from app.redis_consumer import RedisStreamConsumer
from app.repository import EventRepository

logger = logging.getLogger(__name__)

shutdown_requested = False

R3_1A_BEHAVIOR_EVIDENCE_EVENT_TYPES = {"intrusion"}
R3_1A_DEFAULT_EVIDENCE_POLICY = {
    "snapshot_required": True,
    "clip_required": True,
    "pre_seconds": 5,
    "post_seconds": 10,
}


@dataclass
class RecordingPolicyState:
    published_requests: int = 0
    last_recorded_at_ms: dict[str, int] = field(default_factory=dict)


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


def _handle_event(
    event: dict,
    msg_id: str,
    repo: EventRepository,
    consumer: RedisStreamConsumer,
    alert_publisher: AlertPublisher | None = None,
    record_publisher: RecordRequestPublisher | None = None,
    *,
    recording_state: RecordingPolicyState | None = None,
    recording_event_types: tuple[str, ...] = (),
    recording_source_id: str = "",
    recording_max_requests_per_run: int = 0,
    recording_cooldown_seconds: int = 0,
) -> tuple[bool, str | None]:
    """Process a single event: insert into DB, publish alert + record request, then ACK.

    Returns ``(newly_inserted, event_id)``.
    Alert is published only for new inserts.
    Record request is published only when clip_status is unset (idempotent).
    Failures in alert/record publishing do not block ACK.
    """
    _apply_default_evidence_policy(event)
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

    if newly_inserted and alert_publisher is not None:
        try:
            alert_publisher.publish(event, event_id=event_id)
        except Exception:
            logger.exception(
                "alert publish failed for source_event_id=%s",
                source_event_id,
            )

    if newly_inserted and event_id and _requires_evidence(event):
        if hasattr(repo, "create_evidence_task"):
            try:
                repo.create_evidence_task(event, event_id)
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
    if record_publisher is not None:
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

            if recording_event_types and event_type not in recording_event_types:
                allowed = False
                skip_reason = "event_type_mismatch"
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
                    last_recorded_at = recording_state.last_recorded_at_ms.get(source_id)
                    event_ts_ms = int(event.get("event_ts_ms", 0))
                    if (
                        last_recorded_at is not None
                        and event_ts_ms - last_recorded_at
                        < recording_cooldown_seconds * 1000
                    ):
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
                                recording_state.last_recorded_at_ms[source_id] = int(
                                    event.get("event_ts_ms", 0)
                                )
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
    """Enable the R3.1A intrusion evidence MVP for legacy behavior events."""
    event_type = event.get("event_type", "")
    if event_type not in R3_1A_BEHAVIOR_EVIDENCE_EVENT_TYPES:
        return

    if _requires_evidence(event):
        return

    event["snapshot_required"] = True
    event["clip_required"] = True

    policy = event.get("evidence_policy")
    if not isinstance(policy, dict):
        policy = {}
    event["evidence_policy"] = {**R3_1A_DEFAULT_EVIDENCE_POLICY, **policy}

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
    media.setdefault("pre_seconds", R3_1A_DEFAULT_EVIDENCE_POLICY["pre_seconds"])
    media.setdefault("post_seconds", R3_1A_DEFAULT_EVIDENCE_POLICY["post_seconds"])
    media.setdefault("source_id", event.get("source_id", ""))
    media.setdefault("event_ts_ms", event.get("event_ts_ms", 0))
    media.setdefault("frame_uuid", event.get("frame_uuid"))
    media.setdefault("keyframe_uuid", event.get("keyframe_uuid"))


def _process_batch(
    messages: list[tuple[str, dict[bytes, bytes]]],
    repo: EventRepository,
    consumer: RedisStreamConsumer,
    alert_publisher: AlertPublisher | None = None,
    record_publisher: RecordRequestPublisher | None = None,
    *,
    recording_state: RecordingPolicyState | None = None,
    recording_event_types: tuple[str, ...] = (),
    recording_source_id: str = "",
    recording_max_requests_per_run: int = 0,
    recording_cooldown_seconds: int = 0,
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
            recording_state=recording_state,
            recording_event_types=recording_event_types,
            recording_source_id=recording_source_id,
            recording_max_requests_per_run=recording_max_requests_per_run,
            recording_cooldown_seconds=recording_cooldown_seconds,
        )
        if new:
            inserted += 1
        else:
            duplicates += 1
    return inserted, duplicates


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
    repo = EventRepository(pg_conn)
    alert_publisher = AlertPublisher(redis_client, cfg.alert_stream)
    record_publisher = (
        RecordRequestPublisher(
            redis_client,
            cfg.record_request_stream,
            default_replay_source_id=cfg.default_replay_source_id,
        )
        if cfg.recording_enabled
        else None
    )
    recording_state = RecordingPolicyState()

    logger.info(
        "worker started stream=%s group=%s consumer=%s alert_stream=%s "
        "recording_enabled=%s record_request_stream=%s recording_event_types=%s "
        "recording_source_id=%s recording_max_requests_per_run=%s "
        "recording_cooldown_seconds=%s",
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
    )

    total_inserted = 0
    total_duplicates = 0
    last_report = time.monotonic()

    while not shutdown_requested:
        try:
            # 1. Process pending messages (recovery)
            pending = consumer.read_pending(count=cfg.batch_size)
            if pending:
                ins, dup = _process_batch(
                    pending,
                    repo,
                    consumer,
                    alert_publisher,
                    record_publisher,
                    recording_state=recording_state,
                    recording_event_types=cfg.recording_event_types,
                    recording_source_id=cfg.recording_source_id,
                    recording_max_requests_per_run=cfg.recording_max_requests_per_run,
                    recording_cooldown_seconds=cfg.recording_cooldown_seconds,
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
                ins, dup = _process_batch(
                    new_msgs,
                    repo,
                    consumer,
                    alert_publisher,
                    record_publisher,
                    recording_state=recording_state,
                    recording_event_types=cfg.recording_event_types,
                    recording_source_id=cfg.recording_source_id,
                    recording_max_requests_per_run=cfg.recording_max_requests_per_run,
                    recording_cooldown_seconds=cfg.recording_cooldown_seconds,
                )
                total_inserted += ins
                total_duplicates += dup

            # 3. Periodic summary
            now = time.monotonic()
            if now - last_report >= 60:
                logger.info(
                    "worker summary: total_inserted=%d total_duplicates=%d",
                    total_inserted,
                    total_duplicates,
                )
                last_report = now

        except Exception:
            logger.exception("worker loop error, sleeping 1s")
            time.sleep(1)

    logger.info(
        "worker stopped: total_inserted=%d total_duplicates=%d",
        total_inserted,
        total_duplicates,
    )
