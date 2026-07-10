"""RecordRequestPublisher — publish recording requests to Redis Stream."""

from __future__ import annotations

import json
import logging
import hashlib
import uuid
from typing import Any, Dict

from redis import Redis

logger = logging.getLogger(__name__)

DEFAULT_PRE_SECONDS = 5
DEFAULT_POST_SECONDS = 5
DEFAULT_DEDUPE_TTL_SECONDS = 24 * 60 * 60
POST_SAVANT_EVIDENCE_TOPOLOGIES = {"post_savant", "post_savant_replay"}
POST_SAVANT_REPLAY_STOP_STRATEGY = "event_anchor_pre_seconds_rewind"
PTS_TIME_BASE = 1_000_000_000


def _first_policy_value(event: Dict[str, Any], key: str) -> Any:
    """Return an evidence policy value from policy, media, payload, or event."""
    payload = event.get("payload") or {}
    payload = payload if isinstance(payload, dict) else {}
    media = payload.get("media", {})
    media = media if isinstance(media, dict) else {}
    evidence_policy = event.get("evidence_policy") or {}
    evidence_policy = evidence_policy if isinstance(evidence_policy, dict) else {}
    for source in (evidence_policy, media, payload, event):
        if key in source and source.get(key) is not None:
            return source.get(key)
    return None


def _apply_post_savant_policy(record: Dict[str, Any], event: Dict[str, Any]) -> None:
    """Attach explicit post-Savant evidence policy to a record_request."""
    replay_source_kind = _first_policy_value(event, "replay_source_kind")
    evidence_topology = _first_policy_value(event, "evidence_topology")
    metadata_source = _first_policy_value(event, "metadata_source")
    frame_pts = _first_policy_value(event, "frame_pts")
    is_post_savant = str(replay_source_kind or "").strip() == "post_savant" or (
        str(evidence_topology or "").strip() in POST_SAVANT_EVIDENCE_TOPOLOGIES
    ) or (
        str(metadata_source or "").strip() == "video_frame" and frame_pts is not None
    )
    if not is_post_savant:
        return

    record["replay_source_kind"] = "post_savant"
    record["evidence_topology"] = str(evidence_topology or "post_savant")
    record["annotation_source_policy"] = str(
        _first_policy_value(event, "annotation_source_policy")
        or "post_savant_sink_metadata_only"
    )
    for key in (
        "frame_pts",
        "frame_num",
        "metadata_domain",
        "event_frame_uuid",
        "requested_start_pts",
        "requested_end_pts",
        "event_frame_pts",
        "anchor_keyframe_uuid",
        "anchor_keyframe_pts",
        "keyframe_uuid",
        "previous_keyframe_uuid",
        "keyframe_pts",
        "previous_keyframe_pts",
        "time_base",
        "stream_session_id",
    ):
        value = _first_policy_value(event, key)
        if value is not None:
            record[key] = value
    record["replay_stop_strategy"] = POST_SAVANT_REPLAY_STOP_STRATEGY


def _normalize_anchor_keyframe_uuid(record: Dict[str, Any], event: Dict[str, Any]) -> None:
    """Normalize the Replay anchor from the alarm-frame keyframe UUID family."""
    explicit_anchor_keyframe_uuid = (
        record.get("anchor_keyframe_uuid")
        or _first_policy_value(event, "anchor_keyframe_uuid")
    )
    alarm_frame_anchor_uuid = (
        record.get("previous_keyframe_uuid")
        or _first_policy_value(event, "previous_keyframe_uuid")
        or record.get("keyframe_uuid")
        or _first_policy_value(event, "keyframe_uuid")
    )
    anchor_keyframe_uuid = explicit_anchor_keyframe_uuid or alarm_frame_anchor_uuid
    if anchor_keyframe_uuid:
        record["anchor_keyframe_uuid"] = anchor_keyframe_uuid
    if not anchor_keyframe_uuid or record.get("anchor_keyframe_pts") not in (None, ""):
        return
    if str(record.get("previous_keyframe_uuid") or "") == str(anchor_keyframe_uuid):
        previous_keyframe_pts = _first_policy_value(event, "previous_keyframe_pts")
        previous_keyframe_pts_int = _int_or_none(previous_keyframe_pts)
        if previous_keyframe_pts_int is not None:
            record["anchor_keyframe_pts"] = previous_keyframe_pts_int
            return
    if str(record.get("keyframe_uuid") or "") == str(anchor_keyframe_uuid):
        keyframe_pts = _first_policy_value(event, "keyframe_pts")
        keyframe_pts_int = _int_or_none(keyframe_pts)
        if keyframe_pts_int is not None:
            record["anchor_keyframe_pts"] = keyframe_pts_int


def _int_or_none(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _apply_event_frame_timeline(record: Dict[str, Any], event: Dict[str, Any]) -> None:
    """Preserve the event frame PTS window used for Replay/sidecar alignment."""
    for key in ("frame_pts", "frame_num", "metadata_domain"):
        value = _first_policy_value(event, key)
        if value is not None and record.get(key) in (None, ""):
            record[key] = value

    event_frame_pts = _int_or_none(record.get("event_frame_pts"))
    if event_frame_pts is None:
        event_frame_pts = _int_or_none(_first_policy_value(event, "event_frame_pts"))
    if event_frame_pts is None:
        event_frame_pts = _int_or_none(record.get("frame_pts"))
    if event_frame_pts is None:
        return

    record.setdefault("event_frame_pts", event_frame_pts)
    pre_seconds = _int_or_none(record.get("pre_seconds")) or DEFAULT_PRE_SECONDS
    post_seconds = _int_or_none(record.get("post_seconds")) or DEFAULT_POST_SECONDS
    record.setdefault(
        "requested_start_pts",
        max(0, int(event_frame_pts) - int(pre_seconds) * PTS_TIME_BASE),
    )
    record.setdefault(
        "requested_end_pts",
        int(event_frame_pts) + int(post_seconds) * PTS_TIME_BASE,
    )


def build_record_request(
    event: Dict[str, Any],
    event_id: str,
    *,
    request_id: str | None = None,
    default_replay_source_id: str = "",
    default_pre_seconds: int = DEFAULT_PRE_SECONDS,
    default_post_seconds: int = DEFAULT_POST_SECONDS,
) -> Dict[str, Any] | None:
    """Build the record_request payload published to Redis."""
    source_event_id = event.get("source_event_id", "")
    source_id = _resolve_source_id(event, default_replay_source_id)
    payload = event.get("payload") or {}
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    evidence_policy = event.get("evidence_policy") or {}
    if not isinstance(evidence_policy, dict):
        evidence_policy = {}

    if not source_id:
        logger.error(
            "record_request_skipped: no source_id could be resolved "
            "for source_event_id=%s",
            source_event_id,
        )
        return None

    record = {
        "request_id": request_id or str(uuid.uuid4()),
        "event_id": event_id,
        "source_event_id": source_event_id,
        "event_type": event.get("event_type", ""),
        "camera_id": event.get("camera_id", ""),
        "source_id": source_id,
        "event_ts_ms": int(event.get("event_ts_ms", 0)),
        "frame_uuid": event.get("frame_uuid"),
        "event_frame_uuid": event.get("event_frame_uuid") or event.get("frame_uuid"),
        "keyframe_uuid": (
            event.get("keyframe_uuid")
            or (media.get("keyframe_uuid") if isinstance(media, dict) else None)
        ),
        "anchor_keyframe_uuid": event.get("anchor_keyframe_uuid"),
        "previous_keyframe_uuid": (
            event.get("previous_keyframe_uuid")
            or (media.get("previous_keyframe_uuid") if isinstance(media, dict) else None)
        ),
        "pre_seconds": int(
            evidence_policy.get("pre_seconds", default_pre_seconds)
        ),
        "post_seconds": int(
            evidence_policy.get("post_seconds", default_post_seconds)
        ),
        "strategy": "savant_replay",
        "status": "pending",
    }
    runtime_epoch_id = _first_policy_value(event, "runtime_epoch_id")
    if runtime_epoch_id is not None and str(runtime_epoch_id).strip():
        record["runtime_epoch_id"] = str(runtime_epoch_id)
    stream_session_id = _first_policy_value(event, "stream_session_id")
    if stream_session_id is not None and str(stream_session_id).strip():
        record["stream_session_id"] = str(stream_session_id)
    for key in (
        "record_request_shard_id",
        "replay_shard_id",
        "shard_mapping_version",
        "replay_shard_mapping_version",
    ):
        value = _first_policy_value(event, key)
        if value is not None and str(value).strip():
            record[key] = str(value)
    _apply_post_savant_policy(record, event)
    _normalize_anchor_keyframe_uuid(record, event)
    _apply_event_frame_timeline(record, event)
    return record


def _resolve_source_id(event: Dict[str, Any], default_source_id: str = "") -> str:
    """Resolve the replay source_id from an event.

    Priority:
    1. payload.media.source_id (if present and not null/empty)
    2. event.source_id (if not "0" or empty)
    3. *default_source_id* (from Config.default_replay_source_id)
    4. fallback "" (no hardcoded default)
    """
    payload = event.get("payload") or {}
    media = payload.get("media", {}) if isinstance(payload, dict) else {}
    media_sid = media.get("source_id", "") if isinstance(media, dict) else ""
    if media_sid and str(media_sid) not in ("", "0", "None", "null"):
        logger.debug("source_id resolved from payload.media.source_id=%s", media_sid)
        return str(media_sid)

    event_sid = str(event.get("source_id", ""))
    if event_sid and event_sid not in ("0", ""):
        logger.debug("source_id resolved from event.source_id=%s", event_sid)
        return event_sid

    if default_source_id:
        logger.info(
            "source_id resolved from Config.default_replay_source_id=%s "
            "(original_source_id=%s media_source_id=%s)",
            default_source_id,
            event_sid,
            media_sid,
        )
        return default_source_id

    logger.warning(
        "source_id could not be resolved — no media.source_id, "
        "event.source_id=%s, and no DEFAULT_REPLAY_SOURCE_ID configured",
        event_sid,
    )
    return ""


class RecordRequestPublisher:
    """Publishes recording requests to a Redis Stream.

    Called only after successful DB insert, when RECORDING_ENABLED=true.
    Does not block, does not wait for clip completion.
    """

    def __init__(
        self,
        client: Redis,
        stream: str,
        default_replay_source_id: str = "",
        default_pre_seconds: int = DEFAULT_PRE_SECONDS,
        default_post_seconds: int = DEFAULT_POST_SECONDS,
        dedupe_ttl_seconds: int = DEFAULT_DEDUPE_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._stream = stream
        self._default_replay_source_id = default_replay_source_id
        self._default_pre_seconds = int(default_pre_seconds)
        self._default_post_seconds = int(default_post_seconds)
        self._dedupe_ttl_seconds = max(1, int(dedupe_ttl_seconds))

    def _dedupe_key(self, source_event_id: str, recording_strategy: str) -> str:
        token = json.dumps(
            {
                "stream": self._stream,
                "source_event_id": str(source_event_id or ""),
                "strategy": str(recording_strategy or ""),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"security:record_request:dedupe:{digest}"

    def _reserve_request(
        self,
        source_event_id: str,
        recording_strategy: str,
        request_id: str,
    ) -> bool:
        key = self._dedupe_key(source_event_id, recording_strategy)
        try:
            reserved = self._client.set(
                key,
                request_id,
                ex=self._dedupe_ttl_seconds,
                nx=True,
            )
        except Exception:
            logger.exception(
                "record_request_dedupe_reserve_failed source_event_id=%s strategy=%s",
                source_event_id,
                recording_strategy,
            )
            return False
        if not reserved:
            logger.info(
                "record_request_dedupe_duplicate source_event_id=%s strategy=%s",
                source_event_id,
                recording_strategy,
            )
            return False
        logger.debug(
            "record_request_dedupe_reserved source_event_id=%s strategy=%s ttl_s=%s",
            source_event_id,
            recording_strategy,
            self._dedupe_ttl_seconds,
        )
        return True

    def _release_request(
        self,
        source_event_id: str,
        recording_strategy: str,
        request_id: str,
    ) -> None:
        key = self._dedupe_key(source_event_id, recording_strategy)
        try:
            value = self._client.get(key)
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if value == request_id:
                self._client.delete(key)
        except Exception:
            logger.exception(
                "record_request_dedupe_release_failed source_event_id=%s strategy=%s",
                source_event_id,
                recording_strategy,
            )

    def publish(self, event: Dict[str, Any], event_id: str) -> str | None:
        """Publish a record_request for *event*.

        Args:
            event: The full SecurityEvent dict.
            event_id: The PostgreSQL UUID of the inserted row.

        Returns:
            The Redis message id, or None on failure.
        """
        source_event_id = event.get("source_event_id", "")
        record = build_record_request(
            event,
            event_id,
            default_replay_source_id=self._default_replay_source_id,
            default_pre_seconds=self._default_pre_seconds,
            default_post_seconds=self._default_post_seconds,
        )
        if record is None:
            return None
        request_id = str(record["request_id"])
        source_id = str(record["source_id"])
        strategy = str(record.get("strategy") or "savant_replay")

        if not self._reserve_request(source_event_id, strategy, request_id):
            return None

        fields = {
            "request_id": request_id,
            "event_id": event_id,
            "source_event_id": source_event_id,
            "status": "pending",
            "data": json.dumps(record, ensure_ascii=False),
        }

        try:
            msg_id = self._client.xadd(
                self._stream, fields, maxlen=10000, approximate=True
            )
            logger.info(
                "record_request_published request_id=%s event_id=%s source_event_id=%s "
                "source_id=%s stream=%s",
                request_id,
                event_id,
                source_event_id,
                source_id,
                self._stream,
            )
            return msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        except Exception:
            self._release_request(source_event_id, strategy, request_id)
            logger.exception(
                "record_request publish failed for source_event_id=%s",
                source_event_id,
            )
            return None

    def has_request(self, source_event_id: str, recording_strategy: str) -> bool:
        """Return True when a record_request already exists for this event."""
        key = self._dedupe_key(source_event_id, recording_strategy)
        try:
            return bool(self._client.exists(key))
        except Exception:
            logger.exception(
                "record_request lookup failed for source_event_id=%s", source_event_id
            )
            return False
