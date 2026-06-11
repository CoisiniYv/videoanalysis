"""Frame annotation exporter services.

The runtime wrapper is deliberately small: build and validate one message, then
write it to a bounded Redis Stream. Failures are counted and logged, never
raised into the Savant frame-processing path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from custom.services.frame_anchor_metadata import extract_frame_anchor_metadata
from custom.services.frame_annotation_builder import (
    FrameAnnotationBuildConfig,
    build_frame_annotation_message,
    count_frame_annotation_objects,
)
from custom.services.stream_session import stream_session_id_for_frame
from custom.services.time_utils import normalize_pts_to_ms


DEFAULT_STREAM = "security.frame_annotations"
DEFAULT_REDIS_MAXLEN = 10000
DEFAULT_WRITE_TIMEOUT_MS = 50
DEFAULT_LOG_EVERY_N = 300
DEFAULT_RUNTIME_EPOCH_STATE_PATH = (
    "/data/video-analytics/media/replay-sink-output/midterm/.current_epoch.json"
)


@dataclass(frozen=True)
class FrameAnnotationExporterConfig:
    """Runtime config for the gated frame annotation exporter."""

    enabled: bool = False
    producer: str = "savant-security"
    stream: str = DEFAULT_STREAM
    ttl_seconds: int = 120
    max_objects_per_frame: int = 100
    include_keypoints: str = "compact"
    include_landmarks: str = "compact"
    include_embedding: bool = False
    redis_maxlen: int = DEFAULT_REDIS_MAXLEN
    write_timeout_ms: int = DEFAULT_WRITE_TIMEOUT_MS
    log_every_n: int = DEFAULT_LOG_EVERY_N
    min_interval_ms: int = 0

    def build_config(self) -> FrameAnnotationBuildConfig:
        return FrameAnnotationBuildConfig(
            producer=self.producer,
            ttl_seconds=int(self.ttl_seconds),
            max_objects_per_frame=int(self.max_objects_per_frame),
            include_keypoints=str(self.include_keypoints or "compact"),
            include_landmarks=str(self.include_landmarks or "compact"),
            include_embedding=False,
        )


@dataclass
class FrameAnnotationExporterCounters:
    frames_seen: int = 0
    frames_exported: int = 0
    frames_skipped_disabled: int = 0
    frames_skipped_no_anchor: int = 0
    frames_skipped_throttled: int = 0
    validation_errors: int = 0
    redis_write_errors: int = 0
    objects_exported_person: int = 0
    objects_exported_face: int = 0
    last_validation_error: str | None = None
    last_redis_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "frames_seen": self.frames_seen,
            "frames_exported": self.frames_exported,
            "frames_skipped_disabled": self.frames_skipped_disabled,
            "frames_skipped_no_anchor": self.frames_skipped_no_anchor,
            "frames_skipped_throttled": self.frames_skipped_throttled,
            "validation_errors": self.validation_errors,
            "redis_write_errors": self.redis_write_errors,
            "objects_exported_person": self.objects_exported_person,
            "objects_exported_face": self.objects_exported_face,
            "last_validation_error": self.last_validation_error,
            "last_redis_error": self.last_redis_error,
        }


class FrameAnnotationExporter(ABC):
    """Abstract sink for validated frame annotation messages."""

    @abstractmethod
    def export(self, message: dict[str, Any]) -> bool:
        """Export a validated message. Return False for non-fatal failure."""


class DisabledFrameAnnotationExporter(FrameAnnotationExporter):
    """No-op sink used when the frame annotation producer is disabled or unavailable."""

    def __init__(self, reason: str = "disabled") -> None:
        self.reason = reason

    def export(self, message: dict[str, Any]) -> bool:
        return False


class RedisStreamFrameAnnotationExporter(FrameAnnotationExporter):
    """Write validated frame annotation messages to a bounded Redis Stream."""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        stream: str = DEFAULT_STREAM,
        maxlen: int = DEFAULT_REDIS_MAXLEN,
        write_timeout_ms: int = DEFAULT_WRITE_TIMEOUT_MS,
    ) -> None:
        try:
            import redis as _redis
        except ImportError:
            raise ImportError(
                "RedisStreamFrameAnnotationExporter requires redis-py or the "
                "local Redis shim."
            )

        self._redis_url = redis_url or os.environ.get(
            "REDIS_URL",
            "redis://redis:6379/0",
        )
        self._stream = stream or DEFAULT_STREAM
        self._maxlen = int(maxlen)
        timeout_seconds = max(float(write_timeout_ms) / 1000.0, 0.001)
        self._client: _redis.Redis = _redis.Redis.from_url(
            self._redis_url,
            socket_timeout=timeout_seconds,
        )

        print(
            "component=savant_security_frame_annotation_exporter_init "
            f"redis_url={self._redis_url} "
            f"stream={self._stream} "
            f"maxlen={self._maxlen} "
            f"write_timeout_ms={int(write_timeout_ms)}",
            flush=True,
        )

    @property
    def stream(self) -> str:
        return self._stream

    @property
    def maxlen(self) -> int:
        return self._maxlen

    def export(self, message: dict[str, Any]) -> bool:
        payload_json = json.dumps(message, separators=(",", ":"), sort_keys=True)
        counts = count_frame_annotation_objects(message)
        fields = {
            "type": "frame_annotation",
            "message_type": message.get("message_type", ""),
            "source_id": message.get("source_id", ""),
            "camera_id": message.get("camera_id", ""),
            "frame_pts": _stream_text(message.get("frame_pts")),
            "frame_uuid": _stream_text(message.get("frame_uuid")),
            "stream_session_id": _stream_text(message.get("stream_session_id")),
            "keyframe_uuid": _stream_text(message.get("keyframe_uuid")),
            "previous_keyframe_uuid": _stream_text(message.get("previous_keyframe_uuid")),
            "keyframe_pts": _stream_text(message.get("keyframe_pts")),
            "frame_dts": _stream_text(message.get("frame_dts")),
            "duration": _stream_text(message.get("duration")),
            "time_base": _stream_text(message.get("time_base")),
            "timestamp_ms": _stream_text(message.get("timestamp_ms")),
            "ttl_seconds": _stream_text(message.get("ttl_seconds")),
            "object_count": str(len(message.get("objects") or [])),
            "person_count": str(counts["person"]),
            "face_count": str(counts["face"]),
            "data": payload_json,
        }
        try:
            self._client.xadd(
                self._stream,
                fields,
                maxlen=self._maxlen,
                approximate=True,
            )
            return True
        except Exception as exc:
            print(
                "component=savant_security_frame_annotation_redis_warning "
                f"stream={self._stream} "
                f"source_id={message.get('source_id', '')} "
                f"frame_pts={message.get('frame_pts')} "
                f"error={type(exc).__name__}:{str(exc).replace(chr(10), ' | ')}",
                flush=True,
            )
            return False


class FrameAnnotationExportRuntime:
    """Pure runtime wrapper used by the Savant pyfunc and unit tests."""

    def __init__(
        self,
        *,
        config: FrameAnnotationExporterConfig | None = None,
        exporter: FrameAnnotationExporter | None = None,
        resolve_camera_id: Callable[[str], str] | None = None,
        runtime_epoch_provider: Callable[[], str] | None = None,
    ) -> None:
        self.config = config or FrameAnnotationExporterConfig()
        self.exporter = exporter or DisabledFrameAnnotationExporter()
        self.resolve_camera_id = resolve_camera_id or (lambda source_id: source_id)
        self.runtime_epoch_provider = runtime_epoch_provider or _current_runtime_epoch_id
        self.counters = FrameAnnotationExporterCounters()
        self._keyframe_pts_by_source_uuid: dict[tuple[str, str], int] = {}
        self._last_emit_pts_ns_by_source: dict[str, int] = {}

    def process_frame(self, frame_meta: Any) -> dict[str, Any] | None:
        self.counters.frames_seen += 1

        source_id = str(getattr(frame_meta, "source_id", "") or "")
        camera_id = self.resolve_camera_id(source_id) if source_id else ""

        if not self.config.enabled:
            self.counters.frames_skipped_disabled += 1
            self._log_tick(source_id, camera_id)
            return None

        frame_anchor = extract_frame_anchor_metadata(frame_meta)
        frame_pts = _int_or_none(frame_anchor.get("frame_pts"))
        frame_uuid = _text_or_none(frame_anchor.get("frame_uuid"))
        keyframe_uuid = _text_or_none(frame_anchor.get("keyframe_uuid"))
        previous_keyframe_uuid = _text_or_none(frame_anchor.get("previous_keyframe_uuid"))
        keyframe_pts = _int_or_none(frame_anchor.get("keyframe_pts"))
        frame_dts = _int_or_none(frame_anchor.get("frame_dts"))
        duration = _int_or_none(frame_anchor.get("duration"))
        time_base = _text_or_none(frame_anchor.get("time_base"))
        stream_session_id = stream_session_id_for_frame(source_id, frame_pts)
        if keyframe_uuid and keyframe_pts is not None:
            self._keyframe_pts_by_source_uuid[(source_id, keyframe_uuid)] = keyframe_pts
        elif keyframe_uuid:
            keyframe_pts = self._keyframe_pts_by_source_uuid.get(
                (source_id, keyframe_uuid)
            )
        if previous_keyframe_uuid and keyframe_pts is None:
            keyframe_pts = self._keyframe_pts_by_source_uuid.get(
                (source_id, previous_keyframe_uuid)
            )
        is_keyframe = frame_uuid is not None and keyframe_uuid == frame_uuid
        if self._throttled(source_id, frame_pts, is_keyframe=is_keyframe):
            self.counters.frames_skipped_throttled += 1
            self._log_tick(source_id, camera_id)
            return None
        if frame_pts is None and frame_uuid is None:
            self.counters.frames_skipped_no_anchor += 1
            self._log_warning(
                "missing_frame_anchor",
                source_id=source_id,
                camera_id=camera_id,
            )
            self._log_tick(source_id, camera_id)
            return None

        timestamp_ms = _timestamp_ms(frame_meta, frame_pts, self.counters.frames_seen)
        frame_num = _int_or_none(frame_anchor.get("frame_num"))
        frame_objects = list(getattr(frame_meta, "objects", []) or [])

        try:
            message = build_frame_annotation_message(
                source_id=source_id,
                camera_id=camera_id,
                frame_pts=frame_pts,
                frame_uuid=frame_uuid,
                keyframe_uuid=keyframe_uuid,
                previous_keyframe_uuid=previous_keyframe_uuid,
                keyframe_pts=keyframe_pts,
                frame_dts=frame_dts,
                duration=duration,
                time_base=time_base,
                frame_num=frame_num,
                timestamp_ms=timestamp_ms,
                runtime_epoch_id=self.runtime_epoch_provider() or None,
                stream_session_id=stream_session_id,
                frame_objects=frame_objects,
                config=self.config.build_config(),
            )
        except ValueError as exc:
            self.counters.validation_errors += 1
            self.counters.last_validation_error = str(exc)
            self._log_warning(
                "validation_error",
                source_id=source_id,
                camera_id=camera_id,
                error=str(exc),
            )
            self._log_tick(source_id, camera_id)
            return None

        try:
            exported = self.exporter.export(message)
        except Exception as exc:
            exported = False
            self.counters.last_redis_error = f"{type(exc).__name__}:{exc}"

        if not exported:
            self.counters.redis_write_errors += 1
            if self.counters.last_redis_error is None:
                self.counters.last_redis_error = "export_returned_false"
            self._log_warning(
                "redis_write_error",
                source_id=source_id,
                camera_id=camera_id,
                error=self.counters.last_redis_error,
            )
            self._log_tick(source_id, camera_id)
            return message

        counts = count_frame_annotation_objects(message)
        self.counters.frames_exported += 1
        self.counters.objects_exported_person += counts["person"]
        self.counters.objects_exported_face += counts["face"]
        self._log_tick(source_id, camera_id)
        return message

    def _throttled(
        self,
        source_id: str,
        frame_pts: int | None,
        *,
        is_keyframe: bool,
    ) -> bool:
        interval_ms = int(getattr(self.config, "min_interval_ms", 0) or 0)
        if interval_ms <= 0 or frame_pts is None:
            return False
        source_key = source_id or "__unknown_source__"
        if is_keyframe:
            self._last_emit_pts_ns_by_source[source_key] = int(frame_pts)
            return False
        interval_ns = interval_ms * 1_000_000
        last = self._last_emit_pts_ns_by_source.get(source_key)
        if last is not None:
            delta = int(frame_pts) - last
            if delta < 0:
                self._last_emit_pts_ns_by_source[source_key] = int(frame_pts)
                return False
            if delta < interval_ns:
                return True
        self._last_emit_pts_ns_by_source[source_key] = int(frame_pts)
        return False

    def _log_warning(self, reason: str, **fields: Any) -> None:
        if not self._should_log():
            return
        parts = [
            "component=savant_security_frame_annotation_warning",
            f"reason={reason}",
        ]
        for key, value in fields.items():
            parts.append(f"{key}={_safe_log_value(value)}")
        print(" ".join(parts), flush=True)

    def _log_tick(self, source_id: str, camera_id: str) -> None:
        if not self._should_log():
            return
        counters = self.counters.as_dict()
        print(
            "component=savant_security_frame_annotation_tick "
            f"enabled={self.config.enabled} "
            f"source_id={source_id} "
            f"camera_id={camera_id} "
            f"stream={self.config.stream} "
            f"maxlen={self.config.redis_maxlen} "
            f"counters={json.dumps(counters, sort_keys=True)}",
            flush=True,
        )

    def _should_log(self) -> bool:
        interval = max(int(self.config.log_every_n), 1)
        return self.counters.frames_seen == 1 or self.counters.frames_seen % interval == 0


def create_frame_annotation_exporter(
    config: FrameAnnotationExporterConfig,
) -> FrameAnnotationExporter:
    """Return a bounded Redis Stream exporter when enabled, otherwise no-op."""

    if not config.enabled:
        return DisabledFrameAnnotationExporter()

    try:
        return RedisStreamFrameAnnotationExporter(
            stream=config.stream,
            maxlen=config.redis_maxlen,
            write_timeout_ms=config.write_timeout_ms,
        )
    except Exception as exc:
        print(
            "component=savant_security_frame_annotation_exporter_init_warning "
            f"stream={config.stream} "
            f"error={type(exc).__name__}:{str(exc).replace(chr(10), ' | ')}",
            flush=True,
        )
        return DisabledFrameAnnotationExporter(reason="redis_init_failed")


def _current_runtime_epoch_id() -> str:
    env_value = os.getenv("RUNTIME_EPOCH_ID") or os.getenv("VIDEO_ANALYTICS_RUNTIME_EPOCH_ID")
    if env_value:
        return str(env_value)
    state_path = Path(
        os.getenv("RUNTIME_EPOCH_STATE_PATH", DEFAULT_RUNTIME_EPOCH_STATE_PATH)
    )
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if isinstance(data, dict):
        return str(data.get("runtime_epoch_id") or "")
    return ""


def _timestamp_ms(frame_meta: Any, frame_pts: int | None, frame_count: int) -> int | None:
    for attr_name in ("pts", "buf_pts"):
        value = getattr(frame_meta, attr_name, None)
        if value:
            normalized = normalize_pts_to_ms(value)
            if normalized:
                return int(normalized)
    if frame_pts:
        normalized = normalize_pts_to_ms(frame_pts)
        if normalized:
            return int(normalized)
    return int(frame_count)


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _stream_text(value: Any) -> str:
    return "" if value is None else str(value)


def _safe_log_value(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\n", " | ")[:500]
