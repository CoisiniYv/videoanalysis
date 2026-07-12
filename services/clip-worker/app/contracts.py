"""Dependency-free domain contracts for Clip planning and proof resolution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import json
from typing import Any, Mapping


class ProofResolutionKind(str, Enum):
    READY = "ready"
    NOT_READY = "not_ready"
    INVALID = "invalid"


class AckDisposition(str, Enum):
    ACK = "ack"
    HOLD = "hold"


class ProcessingCode(str, Enum):
    MALFORMED = "malformed"
    DUPLICATE_TERMINAL = "duplicate_terminal"
    STALE_TARGET = "stale_target"
    RETRY_PENDING = "retry_pending"
    CAPACITY_PENDING = "capacity_pending"
    REPLAY_CREATED = "replay_created"
    PERMANENT_FAILURE = "permanent_failure"
    TRANSIENT_FAILURE = "transient_failure"
    UNEXPECTED_FAILURE = "unexpected_failure"


class CrashPoint(str, Enum):
    BEFORE_REPLAY_CREATE = "before_replay_create"
    AFTER_REPLAY_RESPONSE = "after_replay_response"
    BEFORE_DURABLE_COMMIT = "before_durable_commit"
    AFTER_DURABLE_COMMIT = "after_durable_commit"
    BEFORE_ACK = "before_ack"
    AFTER_ACK = "after_ack"


@dataclass(frozen=True)
class DeliveryEnvelope:
    stream: str
    group: str
    consumer: str
    message_id: str
    fields: tuple[tuple[object, object], ...]
    delivery_count: int = 1
    reclaimed: bool = False

    @property
    def retry_count(self) -> int:
        return max(0, int(self.delivery_count) - 1)

    def field_map(self) -> dict[object, object]:
        return dict(self.fields)


@dataclass(frozen=True)
class ProcessingOutcome:
    code: ProcessingCode
    ack_disposition: AckDisposition
    durable: bool
    event_id: str = ""
    request_id: str = ""
    reason: str = ""
    replay_job_id: str = ""
    slot_token: str = ""
    diagnostics: tuple[tuple[str, object], ...] = ()
    ack_performed: bool = False

    def with_ack_performed(self, value: bool) -> "ProcessingOutcome":
        return replace(self, ack_performed=bool(value))


@dataclass(frozen=True)
class ClipGateDecision:
    allowed: bool
    reason: str = ""
    error_message: str = ""
    terminal_defer: bool = False
    quota_decision: Mapping[str, object] | None = None
    degrade_decision: Mapping[str, object] | None = None


@dataclass(frozen=True)
class ActiveReplayJob:
    until_monotonic: float
    shard_id: str
    source_id: str
    event_type: str = ""


@dataclass(frozen=True)
class ReplaySlotTiming:
    replay_duration_seconds_effective: float
    replay_duration_effective_reason: str
    timeout_budget_s: float


@dataclass(frozen=True)
class FrameAnnotationAnchor:
    frame_uuid: str
    frame_pts: int
    stream_id: str
    source_id: str
    camera_id: str
    stream_session_id: str = ""
    keyframe_uuid: str | None = None
    previous_keyframe_uuid: str | None = None
    keyframe_pts: int | None = None
    anchor_method: str = "frame_annotation"


@dataclass(frozen=True)
class ReplayFrameDomainProofs:
    start_window_frame: FrameAnnotationAnchor
    post_window_frame: FrameAnnotationAnchor
    requested_start_pts: int = 0
    effective_start_pts: int = 0
    pre_window_truncated: bool = False
    pre_window_policy: str = "full_requested_window"


@dataclass(frozen=True)
class FrameAnnotationRangeCacheEntry:
    cached_at_monotonic: float
    entries: list[tuple[str, dict[str, object]]]


@dataclass(frozen=True)
class NormalizedRecordRequest:
    request_id: str
    event_id: str
    source_event_id: str
    source_id: str
    camera_id: str
    event_ts_ms: int
    event_type: str
    strategy: str
    pre_seconds: int
    post_seconds: int
    keyframe_uuid: str | None
    keyframe_source: str
    post_savant_media_request: bool
    canonical_json: str

    def as_dict(self) -> dict[str, Any]:
        value = json.loads(self.canonical_json)
        if not isinstance(value, dict):
            raise ValueError("normalized request payload is not an object")
        return value


@dataclass(frozen=True)
class ClipGatePolicy:
    high_priority_event_types: tuple[str, ...]
    pressure_level: str
    run_once: bool
    max_jobs_per_run: int
    event_type_quotas: tuple[tuple[str, int], ...]
    max_concurrency: int
    max_concurrency_per_shard: int
    max_concurrency_per_source: int
    per_camera_cooldown_seconds: int

    def quota_for(self, event_type: str) -> int:
        return dict(self.event_type_quotas).get(event_type, 0)


@dataclass(frozen=True)
class ClipGateContext:
    jobs_created: int
    active_global: int
    active_shard: int
    active_source: int
    shard_id: str
    source_id: str
    camera_id: str
    cooldown_gate_ts_ms: int
    last_camera_job_ts_ms: int
    event_type: str
    event_type_count: int


@dataclass(frozen=True)
class ReplayPlan:
    schema_version: str
    source_id: str
    keyframe_uuid: str
    pre_seconds: float
    post_seconds: float
    sink_endpoint: str
    stop_condition_mode: str
    fps: int
    offset_seconds_override: float | None
    duration_seconds_override: float | None
    labels: tuple[tuple[str, str], ...]
    canonical_payload_json: str
    plan_hash: str

    def payload(self) -> dict[str, Any]:
        value = json.loads(self.canonical_payload_json)
        if not isinstance(value, dict):
            raise ValueError("Replay plan payload is not an object")
        return value


@dataclass(frozen=True)
class ReadyProof:
    request_json: str
    keyframe_uuid: str
    keyframe_source: str
    kind: ProofResolutionKind = ProofResolutionKind.READY

    def request(self) -> dict[str, Any]:
        value = json.loads(self.request_json)
        if not isinstance(value, dict):
            raise ValueError("proof request is not an object")
        return value


@dataclass(frozen=True)
class NotReadyProof:
    error: str
    keyframe_uuid: str | None
    keyframe_source: str
    kind: ProofResolutionKind = ProofResolutionKind.NOT_READY


@dataclass(frozen=True)
class InvalidProof:
    error: str
    keyframe_uuid: str | None
    keyframe_source: str
    kind: ProofResolutionKind = ProofResolutionKind.INVALID


ProofResolution = ReadyProof | NotReadyProof | InvalidProof
