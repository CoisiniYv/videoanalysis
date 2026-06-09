"""BehaviorRulesPyFunc — mainline behavior-rule entrypoint.

Reads the camera runtime config via ``custom.services.camera_config`` and
routes each frame through the per-source state + rules built by
``custom.services.rule_runtime``. The rule layer itself is unchanged —
this pyfunc is the Savant ↔ pure-Python adapter only.

Lifecycle / failure modes:

- At ``__init__`` time the YAML is loaded and validated. A missing or
  invalid config raises ``CameraConfigError`` — the module fails fast
  instead of running with stale or zero configuration.
- A frame whose ``source_id`` is not configured logs a warning ONCE per
  source and otherwise is silently dropped (no events). The pipeline
  keeps running for other configured sources.
- Disabled cameras and disabled rules never reach ``evaluate``.
- ``snapshot_required`` / ``clip_required`` / ``severity`` flow from
  YAML → ``RuleConfig`` → ``IntrusionRule`` → ``SecurityEvent``.

This pyfunc is wired by ``modules/savant_security/module.yml``.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Mapping, Set

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.adapters.person_pose_adapter import build_person_pose_observations
from custom.models.events import SecurityEvent, build_source_event_id
from custom.models.person_events import (
    PersonBBoxObservationEventDraft,
    build_person_source_observation_id,
)
from custom.models.pose import (
    PersonQualityGateConfig,
    PersonPoseObservation,
    filter_person_pose_observations,
    is_valid_track_id,
    visible_keypoint_count,
)
from custom.models.tracks import TrackState
from custom.services.camera_config import load_camera_config
from custom.services.cooldown import CooldownTracker
from custom.services.event_exporter import EventExporter, create_event_exporter
from custom.services.frame_anchor_metadata import (
    FrameAnchorTraceWriter,
    FrameUuidRuntimeProbe,
    extract_frame_anchor_metadata,
)
from custom.services.person_observation_exporter import (
    PersonObservationExporter,
    PersonObservationThrottleMap,
    create_person_observation_exporter,
)
from custom.services.rule_runtime import (
    SourceRuntime,
    build_per_source_runtime,
    evaluate_runtime_frame,
)


_DEFAULT_LOG_INTERVAL = 15
_DEFAULT_CONFIG_PATH = "/opt/savant/src/module/config/cameras.generated.yml"


def _env_float(name: str, default: float) -> float:
    try:
        value = os.getenv(name)
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        value = os.getenv(name)
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _person_bbox_observation_min_interval_ms(default: int = 333) -> int:
    value = os.getenv("PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS")
    if value is not None and str(value).strip() != "":
        return _env_int("PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS", default)
    return _env_int("PERSON_OBSERVATION_MIN_INTERVAL_MS", default)


def _config_float(config: Mapping[str, Any], key: str, default: float) -> float:
    try:
        value = config.get(key)
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except Exception:
        return default


def _config_int(config: Mapping[str, Any], key: str, default: int) -> int:
    try:
        value = config.get(key)
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _intrusion_gate_config(rule_config: Mapping[str, Any]) -> PersonQualityGateConfig:
    min_conf = _config_float(rule_config, "min_person_confidence", 0.25)
    min_width = _config_float(rule_config, "min_person_width", 20.0)
    min_height = _config_float(rule_config, "min_person_height", 40.0)
    return PersonQualityGateConfig(
        min_confidence=_env_float("INTRUSION_MIN_PERSON_CONFIDENCE", min_conf),
        min_width=_env_float("INTRUSION_MIN_PERSON_WIDTH", min_width),
        min_height=_env_float("INTRUSION_MIN_PERSON_HEIGHT", min_height),
        min_visible_keypoints=_env_int(
            "INTRUSION_MIN_VISIBLE_KEYPOINTS",
            _config_int(rule_config, "min_visible_keypoints", 0),
        ),
        keypoint_threshold=_env_float("POSE_KEYPOINT_THRESHOLD", 0.25),
        max_bbox_area_ratio=_env_float(
            "INTRUSION_MAX_BBOX_AREA_RATIO",
            _config_float(rule_config, "max_bbox_area_ratio", 0.9),
        ),
        min_aspect_ratio=_env_float(
            "INTRUSION_MIN_BBOX_ASPECT_RATIO",
            _config_float(rule_config, "min_bbox_aspect_ratio", 0.1),
        ),
        max_aspect_ratio=_env_float(
            "INTRUSION_MAX_BBOX_ASPECT_RATIO",
            _config_float(rule_config, "max_bbox_aspect_ratio", 4.0),
        ),
    )


def _runtime_intrusion_config(runtime: SourceRuntime) -> Mapping[str, Any]:
    for rule_entry in runtime.camera_entry.rules.values():
        if rule_entry.algorithm_id == "behavior.intrusion" and rule_entry.enabled:
            return rule_entry.config
    return {}


def _frame_dimensions(frame_meta, runtime: SourceRuntime) -> tuple[float | None, float | None]:
    width = None
    height = None
    for attr in ("width", "frame_width", "source_frame_width"):
        try:
            value = getattr(frame_meta, attr, None)
            if value:
                width = float(value)
                break
        except Exception:
            pass
    for attr in ("height", "frame_height", "source_frame_height"):
        try:
            value = getattr(frame_meta, attr, None)
            if value:
                height = float(value)
                break
        except Exception:
            pass
    if width and height:
        return width, height

    points = []
    for zone in runtime.camera_entry.zones.values():
        points.extend(zone.points)
    if points:
        try:
            inferred_width = max(float(pt[0]) for pt in points)
            inferred_height = max(float(pt[1]) for pt in points)
            width = width or inferred_width
            height = height or inferred_height
        except Exception:
            pass
    return width, height


class BehaviorRulesPyFunc(NvDsPyFuncPlugin):
    """Per-source registry-driven behavior rule pyfunc."""

    def __init__(
        self,
        log_every_n_frames: int = _DEFAULT_LOG_INTERVAL,
        cameras_config_path: str = _DEFAULT_CONFIG_PATH,
        producer: str = "savant_security",
        gpu_id: int = 0,
        observation_window_s: float = 5.0,
        track_timeout_s: float = 10.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.log_every_n_frames = int(log_every_n_frames)
        self.frame_count = 0
        self.producer = producer
        self.gpu_id = int(gpu_id)

        # Fail fast: invalid config raises CameraConfigError up to the
        # pipeline init. Better than running with a stale on-disk file.
        self.bundle = load_camera_config(cameras_config_path)

        self.cooldown = CooldownTracker()
        self.runtimes: Dict[str, SourceRuntime] = build_per_source_runtime(
            self.bundle,
            self.cooldown,
            observation_window_s=observation_window_s,
            track_timeout_s=track_timeout_s,
        )
        self._unknown_source_warned: Set[str] = set()
        self._frame_uuid_probe = FrameUuidRuntimeProbe()
        self._frame_anchor_trace = FrameAnchorTraceWriter()

        print(
            f"component=savant_security_behavior_rules_init "
            f"config_path={cameras_config_path} "
            f"sources={sorted(self.runtimes.keys())} "
            f"camera_ids={[rt.camera_id for rt in self.runtimes.values()]} "
            f"producer={self.producer} "
            f"gpu_id={self.gpu_id}",
            flush=True,
        )

        self.exporter: EventExporter = create_event_exporter()
        print(
            f"component=savant_security_behavior_rules_exporter "
            f"exporter={type(self.exporter).__name__}",
            flush=True,
        )
        self.person_observation_exporter: PersonObservationExporter = (
            create_person_observation_exporter()
        )
        person_observation_min_interval_ms = _person_bbox_observation_min_interval_ms()
        self.person_observation_throttle = PersonObservationThrottleMap(
            min_interval_ms=person_observation_min_interval_ms
        )
        self._person_observation_export_count = 0
        self._person_observation_skip_count = 0
        print(
            f"component=savant_security_person_observation_exporter "
            f"exporter={type(self.person_observation_exporter).__name__} "
            f"stream={os.getenv('PERSON_OBSERVATION_STREAM', 'security.person_observations')} "
            f"min_interval_ms={person_observation_min_interval_ms}",
            flush=True,
        )

    # ------------------------------------------------------------------
    # Frame processing
    # ------------------------------------------------------------------

    def process_frame(self, buffer, frame_meta):
        self.frame_count += 1
        try:
            self._process_frame_impl(frame_meta)
        except Exception:
            import traceback
            print(
                f"component=savant_security_behavior_rules_error "
                f"frame={self.frame_count} "
                f"traceback={traceback.format_exc().replace(chr(10), ' | ')}",
                flush=True,
            )

    def _process_frame_impl(self, frame_meta):
        source_id = str(getattr(frame_meta, "source_id", ""))
        runtime = self.runtimes.get(source_id)

        if runtime is None:
            if source_id and source_id not in self._unknown_source_warned:
                self._unknown_source_warned.add(source_id)
                print(
                    f"component=savant_security_behavior_rules_unknown_source "
                    f"source_id={source_id} "
                    f"known_sources={sorted(self.runtimes.keys())}",
                    flush=True,
                )
            return

        # Build observations stamped with the configured camera_id, not
        # the raw source_id — downstream consumers expect camera_id to
        # match the API/DB record.
        result = build_person_pose_observations(
            frame_meta, camera_id=runtime.camera_id
        )
        raw_observations = result.observations
        frame_width, frame_height = _frame_dimensions(frame_meta, runtime)
        gate_config = _intrusion_gate_config(_runtime_intrusion_config(runtime))
        gate_result = filter_person_pose_observations(
            raw_observations,
            gate_config,
            frame_width=frame_width,
            frame_height=frame_height,
        )
        observations = gate_result.accepted_observations
        timestamp_ms_used_by_event = (
            max((obs.timestamp_ms for obs in raw_observations), default=None)
            if raw_observations
            else None
        )
        frame_anchor = extract_frame_anchor_metadata(frame_meta)
        self._frame_uuid_probe.probe(
            frame_meta,
            timestamp_ms_used_by_event=timestamp_ms_used_by_event,
            notes="BehaviorRulesPyFunc event-decision frame_meta runtime object",
        )
        self._frame_anchor_trace.write(
            frame_meta,
            timestamp_ms_used_by_event=timestamp_ms_used_by_event,
            notes="BehaviorRulesPyFunc source-frame identity trace",
        )
        person_observations_exported, person_observations_skipped = (
            self._export_person_observations(
                observations=observations,
                source_id=source_id,
                camera_id=runtime.camera_id,
                frame_meta=frame_meta,
                frame_anchor=frame_anchor,
            )
        )
        events_exported = 0
        for evaluation in evaluate_runtime_frame(runtime, observations):
            # Rules set last_obs.source_id (empty for adapter observations) so
            # re-stamp from the runtime here.
            evaluation.event.source_id = source_id
            self._enrich_and_export(evaluation.event, evaluation.track, frame_meta, runtime)
            events_exported += 1

        if self.frame_count % self.log_every_n_frames == 0:
            tracked_count = sum(
                1 for o in observations if is_valid_track_id(o.track_id)
            )
            gate_stats = gate_result.stats.as_dict()
            print(
                f"component=savant_security_behavior_rules_tick "
                f"frame={self.frame_count} "
                f"source_id={source_id} "
                f"raw_observation_count={len(raw_observations)} "
                f"observation_count={len(observations)} "
                f"tracked_count={tracked_count} "
                f"track_count={runtime.store.track_count} "
                f"events_exported={events_exported} "
                f"person_observations_exported={person_observations_exported} "
                f"person_observations_skipped={person_observations_skipped} "
                f"total_person_observations_exported={self._person_observation_export_count} "
                f"person_gate={gate_stats}",
                flush=True,
            )

    def _export_person_observations(
        self,
        *,
        observations: list[PersonPoseObservation],
        source_id: str,
        camera_id: str,
        frame_meta,
        frame_anchor: Mapping[str, Any],
    ) -> tuple[int, int]:
        exported = 0
        skipped = 0
        frame_num = getattr(frame_meta, "frame_num", None)
        frame_pts = self._int_or_none(frame_anchor.get("frame_pts"))

        for index, obs in enumerate(observations):
            track_id = int(obs.track_id) if is_valid_track_id(obs.track_id) else 0
            track_part = str(track_id) if track_id > 0 else f"no_track:{index}"
            throttle_key = f"{camera_id}:{track_part}"
            if not self.person_observation_throttle.is_allowed(
                throttle_key, obs.timestamp_ms
            ):
                skipped += 1
                continue

            draft = self._build_person_observation(
                obs=obs,
                source_id=source_id,
                camera_id=camera_id,
                frame_num=frame_num,
                frame_pts=frame_pts,
                index=index,
                frame_anchor=frame_anchor,
            )
            self.person_observation_exporter.export(draft)
            self.person_observation_throttle.record(throttle_key, obs.timestamp_ms)
            exported += 1

        self._person_observation_export_count += exported
        self._person_observation_skip_count += skipped
        return exported, skipped

    def _build_person_observation(
        self,
        *,
        obs: PersonPoseObservation,
        source_id: str,
        camera_id: str,
        frame_num: Any,
        frame_pts: int | None,
        index: int,
        frame_anchor: Mapping[str, Any],
    ) -> PersonBBoxObservationEventDraft:
        track_id = int(obs.track_id) if is_valid_track_id(obs.track_id) else 0
        xyxy = [float(value) for value in obs.bbox.xyxy]
        payload = {
            "media": {
                "frame_uuid": frame_anchor.get("frame_uuid"),
                "keyframe_uuid": frame_anchor.get("keyframe_uuid"),
                "previous_keyframe_uuid": frame_anchor.get("previous_keyframe_uuid"),
                "frame_pts": frame_anchor.get("frame_pts"),
                "frame_dts": frame_anchor.get("frame_dts"),
                "duration": frame_anchor.get("duration"),
                "frame_num": frame_anchor.get("frame_num"),
                "ntp_timestamp": frame_anchor.get("ntp_timestamp"),
                "time_base": frame_anchor.get("time_base"),
                "source_id": frame_anchor.get("source_id") or source_id,
                "metadata_source": frame_anchor.get("metadata_source"),
            }
        }
        return PersonBBoxObservationEventDraft(
            source_observation_id=build_person_source_observation_id(
                source_id=source_id,
                track_id=track_id,
                timestamp_ms=obs.timestamp_ms,
                index=index,
            ),
            producer=self.producer,
            source_id=source_id,
            camera_id=camera_id,
            track_id=str(track_id) if track_id > 0 else None,
            timestamp_ms=int(obs.timestamp_ms),
            frame_pts=frame_pts,
            frame_num=self._int_or_none(frame_num),
            person_bbox=xyxy,
            person_confidence=float(obs.confidence),
            gate_status="accepted",
            payload=payload,
        )

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        try:
            if value is None or str(value).strip() == "":
                return None
            return int(value)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Enrichment / export
    # ------------------------------------------------------------------

    def _enrich_and_export(
        self,
        event: SecurityEvent,
        track: TrackState | None,
        frame_meta,
        runtime: SourceRuntime,
    ) -> None:
        frame_id = int(getattr(frame_meta, "frame_num", 0))
        event_ts_ms = (
            track.last_seen_ms
            if track is not None and track.last_seen_ms
            else event.end_ts_ms or event.start_ts_ms or int(time.time() * 1000)
        )
        frame_anchor = extract_frame_anchor_metadata(frame_meta)

        event.producer = self.producer
        event.gpu_id = self.gpu_id
        event.frame_id = frame_id
        event.event_ts_ms = event_ts_ms
        event.frame_uuid = frame_anchor.get("frame_uuid")
        event.keyframe_uuid = frame_anchor.get("keyframe_uuid")
        event.camera_id = runtime.camera_id

        gate_config = _intrusion_gate_config(_runtime_intrusion_config(runtime))
        last_obs = (
            track.observations[-1]
            if track is not None and track.observations
            else None
        )

        enrichment = {
            "zone_id": event.zone,
            "person_quality_gate": {
                "status": "accepted",
                "min_person_confidence": gate_config.min_confidence,
                "min_person_width": gate_config.min_width,
                "min_person_height": gate_config.min_height,
            },
            "rule": event.rule_name,
            "media": {
                "snapshot_required": event.snapshot_required,
                "clip_required": event.clip_required,
                "snapshot_status": "not_implemented",
                "clip_status": "not_implemented",
                "recording_strategy": "reserved",
                "pre_seconds": 5,
                "post_seconds": 5,
                "source_id": event.source_id,
                "event_ts_ms": event_ts_ms,
                "frame_uuid": frame_anchor.get("frame_uuid"),
                "keyframe_uuid": frame_anchor.get("keyframe_uuid"),
                "previous_keyframe_uuid": frame_anchor.get("previous_keyframe_uuid"),
                "frame_pts": frame_anchor.get("frame_pts"),
                "frame_dts": frame_anchor.get("frame_dts"),
                "duration": frame_anchor.get("duration"),
                "frame_num": frame_anchor.get("frame_num"),
                "ntp_timestamp": frame_anchor.get("ntp_timestamp"),
                "time_base": frame_anchor.get("time_base"),
                "metadata_source": frame_anchor.get("metadata_source"),
            },
        }
        if last_obs is not None:
            bbox = track.current_bbox
            person_bbox = {
                "x": bbox.x,
                "y": bbox.y,
                "width": bbox.width,
                "height": bbox.height,
            }
            enrichment.update(
                {
                    "person_bbox": person_bbox,
                    "bbox": dict(person_bbox),
                    "bbox_source": "savant_detection",
                    "person_confidence": last_obs.confidence,
                    "visible_keypoint_count": visible_keypoint_count(
                        last_obs.keypoints,
                        gate_config.keypoint_threshold,
                    ),
                }
            )

        if event.event_type == "intrusion" and "inside_ms" not in event.payload:
            end_ts_ms = event.end_ts_ms if event.end_ts_ms is not None else event.start_ts_ms
            enrichment["inside_ms"] = max(end_ts_ms - event.start_ts_ms, 0)

        merged_payload = dict(event.payload or {})
        for key, value in enrichment.items():
            if key == "media" and isinstance(merged_payload.get("media"), dict):
                media = dict(value)
                media.update(merged_payload["media"])
                merged_payload["media"] = media
            else:
                merged_payload.setdefault(key, value)
        event.payload = merged_payload

        if os.getenv("SAVANT_EVENT_MEDIA_REQUIRED", "").lower() in ("1", "true", "yes"):
            event.snapshot_required = True
            event.clip_required = True
            event.payload["media"]["snapshot_required"] = True
            event.payload["media"]["clip_required"] = True
            event.payload["media"]["recording_strategy"] = "savant_replay"

        event.source_event_id = build_source_event_id(
            producer=event.producer,
            camera_id=event.camera_id,
            track_id=event.track_id,
            event_type=event.event_type,
            start_ts_ms=event.start_ts_ms,
        )

        self.exporter.export(event)
