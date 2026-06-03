"""BehaviorDebugProbe — integrates Phase 2A pure Python rules into Savant runtime.

Builds ``PersonPoseObservation`` objects via the official metadata adapter,
maintains per-track state in ``TrackStateStore``, and evaluates
``IntrusionRule`` against a configurable zone.

Debug-only probe — no Redis, no PostgreSQL, no event-worker.
"""

from __future__ import annotations

import os
from typing import Dict, List

import yaml

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.adapters.person_pose_adapter import build_person_pose_observations
from custom.models.camera_config import CameraConfig, RuleConfig, ZoneConfig
from custom.models.events import SecurityEvent
from custom.models.pose import is_valid_track_id
from custom.models.tracks import TrackStateStore
from custom.rules.intrusion import IntrusionRule
from custom.services.cooldown import CooldownTracker

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
            f"stage=phase2b_probe_config_warn "
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


class BehaviorDebugProbe(NvDsPyFuncPlugin):
    """Savant PyFunc that runs behavior rules and logs intrusion debug events.

    Logs a summary every ``log_every_n_frames`` frames::

        stage=phase2b_behavior_debug
        observation_count=<N>
        tracked_count=<N>
        track_count=<N>
        intrusion_event=yes|no

    And each time an intrusion event fires::

        stage=phase2b_intrusion_debug_event
        event_type=intrusion
        camera_id=<id>
        track_id=<N>
        zone=<name>
        rule=<name>
        start_ts_ms=<N>
        end_ts_ms=<N>
    """

    def __init__(
        self,
        log_every_n_frames: int = _DEFAULT_LOG_INTERVAL,
        cameras_config_path: str = "/opt/savant/src/module/config/cameras.yml",
        observation_window_s: float = 5.0,
        track_timeout_s: float = 10.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.log_every_n_frames = int(log_every_n_frames)
        self.frame_count = 0

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

        print(
            f"stage=phase2b_probe_init "
            f"camera_id={self.camera_config.camera_id} "
            f"zones={list(self.camera_config.zones.keys())} "
            f"rules={list(self.rules.keys())} "
            f"observation_window_s={observation_window_s} "
            f"track_timeout_s={track_timeout_s}",
            flush=True,
        )

    def process_frame(self, buffer, frame_meta):
        """Process a single frame — called by Savant pipeline."""
        self.frame_count += 1

        # --- Build PersonPoseObservation list ----------------------------
        result = build_person_pose_observations(
            frame_meta, camera_id=self.camera_config.camera_id
        )
        observations = result.observations

        # --- Feed observations into TrackStateStore ----------------------
        self.store.update(observations)

        # --- Evaluate rules on all active tracks -------------------------
        intrusion_triggered = False
        events: List[SecurityEvent] = []
        for track in self.store.active_tracks:
            for _rule_name, rule in self.rules.items():
                event = rule.evaluate(track)
                if event is not None:
                    intrusion_triggered = True
                    events.append(event)
                    self._log_event(event)

        # --- Periodic diagnostic summary ---------------------------------
        if self.frame_count % self.log_every_n_frames == 0:
            tracked_count = sum(
                1 for o in observations if is_valid_track_id(o.track_id)
            )
            print(
                f"stage=phase2b_behavior_debug "
                f"frame={self.frame_count} "
                f"observation_count={len(observations)} "
                f"tracked_count={tracked_count} "
                f"track_count={self.store.track_count} "
                f"intrusion_event={'yes' if intrusion_triggered else 'no'}",
                flush=True,
            )

    def _log_event(self, event: SecurityEvent) -> None:
        """Log a structured intrusion debug event line."""
        print(
            f"stage=phase2b_intrusion_debug_event "
            f"event_type={event.event_type} "
            f"camera_id={event.camera_id} "
            f"source_id={event.source_id} "
            f"track_id={event.track_id} "
            f"zone={event.zone} "
            f"rule={event.rule_name} "
            f"start_ts_ms={event.start_ts_ms} "
            f"end_ts_ms={event.end_ts_ms} "
            f"confidence={event.confidence:.4f}",
            flush=True,
        )
