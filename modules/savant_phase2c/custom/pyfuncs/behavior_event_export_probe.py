"""BehaviorEventExportProbe — Phase 2C security event schema integration.

Builds on Phase 2B's ``BehaviorDebugProbe``: ingests
``PersonPoseObservation`` via the official metadata adapter, maintains
per-track state in ``TrackStateStore``, evaluates ``IntrusionRule``,
and exports standardised ``SecurityEvent`` objects via
``DryRunEventExporter``.

Debug-only probe — no Redis, no PostgreSQL, no event-worker.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List

import yaml

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.adapters.person_pose_adapter import build_person_pose_observations
from custom.models.camera_config import CameraConfig, RuleConfig, ZoneConfig
from custom.models.events import SecurityEvent, build_source_event_id
from custom.models.pose import is_valid_track_id
from custom.models.tracks import TrackState, TrackStateStore
from custom.rules.intrusion import IntrusionRule
from custom.services.cooldown import CooldownTracker
from custom.services.event_exporter import create_event_exporter, EventExporter

_DEFAULT_LOG_INTERVAL = 15

_DEFAULT_CAMERA_CONFIG = CameraConfig(
    camera_id="default",
    zones={
        "full_frame": ZoneConfig(
            name="full_frame",
            polygon=[(-100, -100), (3000, -100), (3000, 2000), (-100, 2000)],
        ),
    },
    rules={
        "debug_intrusion": RuleConfig(
            name="debug_intrusion",
            rule_type="intrusion",
            zone="full_frame",
            enabled=True,
            min_inside_ms=1,
            cooldown_s=5,
        ),
    },
)


def _load_camera_config(path: str) -> CameraConfig:
    """Load ``CameraConfig`` from a cameras.yml file.

    Falls back to a default full-frame config if the file does not exist.
    """
    if not os.path.exists(path):
        print(
            f"stage=phase2c_probe_config_warn "
            f"path={path} not_found=true using_default=true",
            flush=True,
        )
        return _DEFAULT_CAMERA_CONFIG

    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    cameras_data = raw.get("cameras", {})
    first_cam_id, first_cam_cfg = next(iter(cameras_data.items()), ("default", {}))

    zones: Dict[str, ZoneConfig] = {}
    for zname, zdata in first_cam_cfg.get("zones", {}).items():
        zones[zname] = ZoneConfig(
            name=zdata.get("name", zname),
            polygon=[tuple(p) for p in zdata.get("polygon", [])],
        )

    rules: Dict[str, RuleConfig] = {}
    for rname, rdata in first_cam_cfg.get("rules", {}).items():
        rules[rname] = RuleConfig(
            name=rdata.get("name", rname),
            rule_type=rdata.get("rule_type", ""),
            zone=rdata.get("zone", ""),
            enabled=rdata.get("enabled", True),
            min_inside_ms=rdata.get("min_inside_ms", 1),
            cooldown_s=rdata.get("cooldown_s", 5),
        )

    return CameraConfig(
        camera_id=first_cam_cfg.get("camera_id", first_cam_id),
        zones=zones,
        rules=rules,
    )


class BehaviorEventExportProbe(NvDsPyFuncPlugin):
    """Savant PyFunc that evaluates behavior rules and exports standardised
    ``SecurityEvent`` objects via ``DryRunEventExporter``.

    Logs a summary every ``log_every_n_frames`` frames::

        stage=phase2c_behavior_debug
        observation_count=<N>
        tracked_count=<N>
        track_count=<N>
        events_exported=<N>

    And each exported security event::

        stage=phase2c_security_event_dry_run
        source_event_id=...
        event_type=intrusion
        security_event_json={...}
    """

    def __init__(
        self,
        log_every_n_frames: int = _DEFAULT_LOG_INTERVAL,
        cameras_config_path: str = "/opt/savant/src/module/config/cameras.yml",
        producer: str = "savant_phase2c",
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

        # --- Load camera configuration -----------------------------------
        self.camera_config = _load_camera_config(cameras_config_path)

        # --- Build rule engine components --------------------------------
        self.cooldown = CooldownTracker()
        self.store = TrackStateStore(
            observation_window_s=observation_window_s,
            track_timeout_s=track_timeout_s,
        )

        self.rules: Dict[str, IntrusionRule] = {}
        for rule_name, rule_cfg in self.camera_config.rules.items():
            zone = self.camera_config.get_zone(rule_cfg.zone)
            self.rules[rule_name] = IntrusionRule(rule_cfg, zone, self.cooldown)

        # --- Build exporter ----------------------------------------------
        self.exporter: EventExporter = create_event_exporter()

        print(
            f"stage=phase2c_behavior_event_export_probe_init "
            f"camera_id={self.camera_config.camera_id} "
            f"producer={self.producer} "
            f"exporter={type(self.exporter).__name__}",
            flush=True,
        )

        print(
            f"stage=phase2c_probe_init "
            f"camera_id={self.camera_config.camera_id} "
            f"zones={list(self.camera_config.zones.keys())} "
            f"rules={list(self.rules.keys())} "
            f"producer={self.producer} "
            f"gpu_id={self.gpu_id} "
            f"exporter={type(self.exporter).__name__}",
            flush=True,
        )

    def process_frame(self, buffer, frame_meta):
        """Process a single frame — called by Savant pipeline."""
        self.frame_count += 1

        try:
            self._process_frame_impl(frame_meta)
        except Exception:
            import traceback
            print(
                f"stage=phase2c_probe_error "
                f"frame={self.frame_count} "
                f"error_type={type(Exception).__name__} "
                f"traceback={traceback.format_exc().replace(chr(10), ' | ')}",
                flush=True,
            )

    def _process_frame_impl(self, frame_meta):
        """Internal frame processing with structured error visibility."""
        # --- Build PersonPoseObservation list ----------------------------
        result = build_person_pose_observations(
            frame_meta, camera_id=self.camera_config.camera_id
        )
        observations = result.observations

        # --- Feed observations into TrackStateStore ----------------------
        self.store.update(observations)

        # --- Evaluate rules and export enriched events -------------------
        events_exported = 0
        for track in self.store.active_tracks:
            for _rule_name, rule in self.rules.items():
                event = rule.evaluate(track)
                if event is not None:
                    self._enrich_and_export(event, track, frame_meta)
                    events_exported += 1

        # --- Periodic diagnostic summary ---------------------------------
        if self.frame_count % self.log_every_n_frames == 0:
            tracked_count = sum(
                1 for o in observations if is_valid_track_id(o.track_id)
            )
            print(
                f"stage=phase2c_behavior_debug "
                f"frame={self.frame_count} "
                f"observation_count={len(observations)} "
                f"tracked_count={tracked_count} "
                f"track_count={self.store.track_count} "
                f"events_exported={events_exported}",
                flush=True,
            )

    def _enrich_and_export(
        self,
        event: SecurityEvent,
        track: TrackState,
        frame_meta,
    ) -> None:
        """Fill in full schema fields on *event* and export.

        Fields that the rule engine does not set (``producer``, ``gpu_id``,
        ``frame_id``, ``event_ts_ms``, ``payload.media``, etc.) are populated
        here before handing the event to the exporter.
        """
        # --- Frame / producer metadata -----------------------------------
        frame_id = int(getattr(frame_meta, "frame_num", 0))
        event_ts_ms = track.last_seen_ms or int(time.time() * 1000)

        event.producer = self.producer
        event.gpu_id = self.gpu_id
        event.frame_id = frame_id
        event.event_ts_ms = event_ts_ms
        event.frame_uuid = None
        event.keyframe_uuid = None

        # --- Compute enrichment values -----------------------------------
        inside_ms = max(event.end_ts_ms - event.start_ts_ms, 0)
        bbox = track.current_bbox

        # --- Build payload -----------------------------------------------
        event.payload = {
            "zone_id": event.zone,
            "inside_ms": inside_ms,
            "bbox": {
                "x": bbox.x,
                "y": bbox.y,
                "width": bbox.width,
                "height": bbox.height,
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
                "frame_uuid": None,
                "keyframe_uuid": None,
            },
        }

        # --- Generate source_event_id ------------------------------------
        event.source_event_id = build_source_event_id(
            producer=event.producer,
            camera_id=event.camera_id,
            track_id=event.track_id,
            event_type=event.event_type,
            start_ts_ms=event.start_ts_ms,
        )

        # --- Export -------------------------------------------------------
        self.exporter.export(event)
