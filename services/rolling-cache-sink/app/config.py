"""Configuration and epoch resolution for the rolling-cache sink."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.:@+-]+$")
MAX_ROLLING_CACHE_PUBLICATION_WORKERS = 4
MAX_ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS = 4
MAX_ROLLING_CACHE_PUBLICATION_FINAL_PARENT_GROUP_LIMIT = 32


def safe_component(value: str, *, field: str) -> str:
    """Validate values used as exact directory components.

    Source IDs cannot be escaped or rewritten because Media Worker looks up the
    directory by the exact source ID. Rejecting unsafe IDs is preferable to
    publishing a segment under a path that the consumer cannot address.
    """

    value = str(value).strip()
    if not value or value in {".", ".."} or not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError(f"unsafe_{field}:{value!r}")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")
    return value


def _bounded_positive_int(name: str, default: int, *, maximum: int) -> int:
    value = _positive_int(name, default)
    if value > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {value}")
    return value


def _bounded_nonnegative_int(name: str, default: int, *, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}, got {value}")
    return value


def _choice(name: str, default: str, *, choices: tuple[str, ...]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in choices:
        rendered = ", ".join(choices)
        raise ValueError(f"{name} must be one of {rendered}, got {value!r}")
    return value


@dataclass(frozen=True)
class SinkConfig:
    zmq_endpoint: str
    cache_root: Path
    namespace: str
    segment_seconds: float
    shutdown_timeout_s: float
    pts_regression_tolerance_s: float
    http_host: str
    http_port: int
    publication_workers: int
    publication_commit_slots: int
    publication_final_parent_group_limit: int
    publication_file_sync_mode: str
    publication_metadata_layout: str
    source_id: str | None
    source_id_prefix: str | None
    explicit_epoch_id: str
    epoch_state_path: Path | None

    @classmethod
    def from_env(cls) -> "SinkConfig":
        endpoint = os.getenv("ZMQ_ENDPOINT", "").strip()
        if not endpoint:
            raise ValueError("ZMQ_ENDPOINT is required")
        namespace = safe_component(
            os.getenv("ROLLING_CACHE_NAMESPACE", "midterm"),
            field="namespace",
        )
        explicit_epoch = os.getenv("ROLLING_CACHE_RUNTIME_EPOCH_ID", "").strip()
        if explicit_epoch:
            explicit_epoch = safe_component(explicit_epoch, field="runtime_epoch_id")
        state_path_raw = os.getenv("RUNTIME_EPOCH_STATE_PATH", "").strip()
        source_id = os.getenv("SOURCE_ID", "").strip() or None
        if source_id:
            source_id = safe_component(source_id, field="source_id")
        source_id_prefix = os.getenv("SOURCE_ID_PREFIX", "").strip() or None
        return cls(
            zmq_endpoint=endpoint,
            cache_root=Path(os.getenv("ROLLING_CACHE_ROOT", "/media/rolling-cache")),
            namespace=namespace,
            segment_seconds=_positive_float("ROLLING_CACHE_SEGMENT_SECONDS", 4.0),
            shutdown_timeout_s=_positive_float(
                "ROLLING_CACHE_SINK_SHUTDOWN_TIMEOUT_SECONDS", 20.0
            ),
            pts_regression_tolerance_s=_positive_float(
                "ROLLING_CACHE_PTS_REGRESSION_TOLERANCE_SECONDS", 1.0
            ),
            http_host=os.getenv("ROLLING_CACHE_SINK_HTTP_HOST", "0.0.0.0").strip()
            or "0.0.0.0",
            http_port=_positive_int("ROLLING_CACHE_SINK_HTTP_PORT", 8080),
            publication_workers=_bounded_positive_int(
                "ROLLING_CACHE_PUBLICATION_WORKERS",
                1,
                maximum=MAX_ROLLING_CACHE_PUBLICATION_WORKERS,
            ),
            publication_commit_slots=_bounded_nonnegative_int(
                "ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS",
                0,
                maximum=MAX_ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS,
            ),
            publication_final_parent_group_limit=_bounded_positive_int(
                "ROLLING_CACHE_PUBLICATION_FINAL_PARENT_GROUP_LIMIT",
                1,
                maximum=MAX_ROLLING_CACHE_PUBLICATION_FINAL_PARENT_GROUP_LIMIT,
            ),
            publication_file_sync_mode=_choice(
                "ROLLING_CACHE_PUBLICATION_FILE_SYNC_MODE",
                "fsync",
                choices=("fsync", "fdatasync"),
            ),
            publication_metadata_layout=_choice(
                "ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT",
                "split",
                choices=("split", "single_inode", "metadata_only"),
            ),
            source_id=source_id,
            source_id_prefix=source_id_prefix,
            explicit_epoch_id=explicit_epoch,
            epoch_state_path=Path(state_path_raw) if state_path_raw else None,
        )


class EpochResolver:
    """Resolve a stable process fallback or the current runtime epoch state."""

    def __init__(
        self,
        *,
        explicit_epoch_id: str = "",
        state_path: Path | None = None,
        generated_epoch_id: str | None = None,
    ) -> None:
        self._explicit = (
            safe_component(explicit_epoch_id, field="runtime_epoch_id")
            if explicit_epoch_id
            else ""
        )
        self._state_path = state_path
        self._lock = threading.Lock()
        self._last_state_epoch = ""
        self._generated = safe_component(
            generated_epoch_id or self._new_epoch_id(),
            field="runtime_epoch_id",
        )

    @staticmethod
    def _new_epoch_id() -> str:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        return f"midterm-{stamp}-rolling-{uuid.uuid4().hex[:8]}"

    def current(self) -> str:
        if self._explicit:
            return self._explicit
        if self._state_path is not None:
            try:
                payload = json.loads(self._state_path.read_text(encoding="utf-8"))
                value = str((payload or {}).get("runtime_epoch_id") or "").strip()
                if value:
                    value = safe_component(value, field="runtime_epoch_id")
                    with self._lock:
                        self._last_state_epoch = value
                    return value
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # A concurrently replaced or temporarily absent epoch-state file
                # must not move an active stream into a random new epoch.
                with self._lock:
                    if self._last_state_epoch:
                        return self._last_state_epoch
        return self._generated
