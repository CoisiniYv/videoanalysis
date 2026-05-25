"""BehaviorRulesPyFunc — mainline behavior-rule entrypoint (Phase C1.2).

Reads the C1 export schema via ``custom.services.camera_config`` and
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

This pyfunc is wired by ``modules/savant_security/module.yml``. The
legacy ``custom.pyfuncs.behavior_event_export_probe`` in
``savant_phase3h_zmq`` is untouched.
"""

from __future__ import annotations

import os
import time
from typing import Dict, Set

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.adapters.person_pose_adapter import build_person_pose_observations
from custom.models.events import SecurityEvent, build_source_event_id
from custom.models.pose import is_valid_track_id
from custom.models.tracks import TrackState
from custom.services.camera_config import load_camera_config
from custom.services.cooldown import CooldownTracker
from custom.services.event_exporter import EventExporter, create_event_exporter
from custom.services.rule_runtime import SourceRuntime, build_per_source_runtime


_DEFAULT_LOG_INTERVAL = 15
_DEFAULT_CONFIG_PATH = "/opt/savant/src/module/config/cameras.generated.yml"


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

        print(
            f"stage=savant_security_behavior_rules_init "
            f"config_path={cameras_config_path} "
            f"sources={sorted(self.runtimes.keys())} "
            f"camera_ids={[rt.camera_id for rt in self.runtimes.values()]} "
            f"producer={self.producer} "
            f"gpu_id={self.gpu_id}",
            flush=True,
        )

        self.exporter: EventExporter = create_event_exporter()
        print(
            f"stage=savant_security_behavior_rules_exporter "
            f"exporter={type(self.exporter).__name__}",
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
                f"stage=savant_security_behavior_rules_error "
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
                    f"stage=savant_security_behavior_rules_unknown_source "
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
        observations = result.observations
        runtime.store.update(observations)

        events_exported = 0
        for track in runtime.store.active_tracks:
            for rule in runtime.rules:
                event = rule.evaluate(track)
                if event is not None:
                    # Rules set last_obs.source_id (empty for adapter
                    # observations) so re-stamp from the runtime here.
                    event.source_id = source_id
                    self._enrich_and_export(event, track, frame_meta, runtime)
                    events_exported += 1

        if self.frame_count % self.log_every_n_frames == 0:
            tracked_count = sum(
                1 for o in observations if is_valid_track_id(o.track_id)
            )
            print(
                f"stage=savant_security_behavior_rules_tick "
                f"frame={self.frame_count} "
                f"source_id={source_id} "
                f"observation_count={len(observations)} "
                f"tracked_count={tracked_count} "
                f"track_count={runtime.store.track_count} "
                f"events_exported={events_exported}",
                flush=True,
            )

    # ------------------------------------------------------------------
    # Enrichment / export
    # ------------------------------------------------------------------

    def _enrich_and_export(
        self,
        event: SecurityEvent,
        track: TrackState,
        frame_meta,
        runtime: SourceRuntime,
    ) -> None:
        frame_id = int(getattr(frame_meta, "frame_num", 0))
        event_ts_ms = track.last_seen_ms or int(time.time() * 1000)

        event.producer = self.producer
        event.gpu_id = self.gpu_id
        event.frame_id = frame_id
        event.event_ts_ms = event_ts_ms
        event.frame_uuid = None
        event.keyframe_uuid = None
        event.camera_id = runtime.camera_id

        inside_ms = max(event.end_ts_ms - event.start_ts_ms, 0)
        bbox = track.current_bbox

        event.payload = {
            "zone_id": event.zone,
            "inside_ms": inside_ms,
            "bbox": {
                "x": bbox.x,
                "y": bbox.y,
                "width": bbox.width,
                "height": bbox.height,
            },
            "bbox_source": "savant_detection",
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
                "frame_uuid": None,
                "keyframe_uuid": None,
            },
        }

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
