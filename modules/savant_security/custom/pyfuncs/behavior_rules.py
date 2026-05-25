"""BehaviorRulesPyFunc — mainline entrypoint for evaluating behavior rules.

This is the canonical Savant PyFunc that consumes nvtracker output,
maintains per-track state via ``TrackStateStore``, evaluates rules built
from the registry, enriches and exports ``SecurityEvent`` objects via the
configured exporter.

It does not bake any specific rule logic into the pyfunc. New rules are
added by dropping a module under ``custom/rules/`` and decorating it
with ``@register_rule("<rule_type>")``. The pyfunc only invokes
``build_rules(camera_config, cooldown)`` and ``rule.evaluate(track)``.

This pyfunc is NOT wired into a module.yml in R1.1. The current E1
pipeline still runs ``custom.pyfuncs.behavior_event_export_probe``
in ``modules/savant_phase3h_zmq``. A future sub-phase will switch
``module.yml`` to point at this entrypoint after the rule set is wider.
"""

from __future__ import annotations

import os
import time
from typing import Dict

import yaml

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.adapters.person_pose_adapter import build_person_pose_observations
from custom.models.camera_config import CameraConfig, RuleConfig, ZoneConfig
from custom.models.events import SecurityEvent, build_source_event_id
from custom.models.pose import is_valid_track_id
from custom.models.tracks import TrackState, TrackStateStore
from custom.rules import build_rules
from custom.services.cooldown import CooldownTracker
from custom.services.event_exporter import EventExporter, create_event_exporter


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
        "default_intrusion": RuleConfig(
            name="default_intrusion",
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

    Falls back to a default full-frame intrusion config when the file is
    missing, so a fresh pipeline still emits events.
    """
    if not os.path.exists(path):
        print(
            f"stage=savant_security_camera_config_warn "
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


class BehaviorRulesPyFunc(NvDsPyFuncPlugin):
    """Savant PyFunc that drives the registry-built behavior rules."""

    def __init__(
        self,
        log_every_n_frames: int = _DEFAULT_LOG_INTERVAL,
        cameras_config_path: str = "/opt/savant/src/module/config/cameras.yml",
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

        self.camera_config = _load_camera_config(cameras_config_path)

        self.cooldown = CooldownTracker()
        self.store = TrackStateStore(
            observation_window_s=observation_window_s,
            track_timeout_s=track_timeout_s,
        )

        self.rules = build_rules(self.camera_config, self.cooldown)

        self.exporter: EventExporter = create_event_exporter()

        print(
            f"stage=savant_security_behavior_rules_init "
            f"camera_id={self.camera_config.camera_id} "
            f"zones={list(self.camera_config.zones.keys())} "
            f"rules={[r.config.name for r in self.rules]} "
            f"rule_types={[r.rule_type for r in self.rules]} "
            f"producer={self.producer} "
            f"gpu_id={self.gpu_id} "
            f"exporter={type(self.exporter).__name__}",
            flush=True,
        )

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
        result = build_person_pose_observations(
            frame_meta, camera_id=self.camera_config.camera_id
        )
        observations = result.observations

        self.store.update(observations)

        events_exported = 0
        for track in self.store.active_tracks:
            for rule in self.rules:
                event = rule.evaluate(track)
                if event is not None:
                    self._enrich_and_export(event, track, frame_meta)
                    events_exported += 1

        if self.frame_count % self.log_every_n_frames == 0:
            tracked_count = sum(
                1 for o in observations if is_valid_track_id(o.track_id)
            )
            print(
                f"stage=savant_security_behavior_rules_tick "
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
        """Populate fields the rule did not set, then export."""
        frame_id = int(getattr(frame_meta, "frame_num", 0))
        event_ts_ms = track.last_seen_ms or int(time.time() * 1000)

        event.producer = self.producer
        event.gpu_id = self.gpu_id
        event.frame_id = frame_id
        event.event_ts_ms = event_ts_ms
        event.frame_uuid = None
        event.keyframe_uuid = None

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
