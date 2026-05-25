"""IntrusionRule — pure Python intrusion detection registered as ``intrusion``."""

from __future__ import annotations

from typing import Optional

from custom.geometry.polygon import point_in_polygon
from custom.models.events import SecurityEvent
from custom.models.tracks import TrackState
from custom.rules.base import BehaviorRule
from custom.rules.registry import register_rule


@register_rule("intrusion")
class IntrusionRule(BehaviorRule):
    """Fires when a track's foot_point stays inside *zone.polygon* for at
    least ``rule_config.min_inside_ms``. Rate-limited via the shared
    ``CooldownTracker`` keyed on ``camera_id:track_id:zone``."""

    def evaluate(self, track: TrackState) -> Optional[SecurityEvent]:
        if not self.config.enabled:
            return None
        if not track.observations:
            return None

        now_ms = track.last_seen_ms
        inside_ms, first_inside_ts = self._continuous_inside(track)
        if inside_ms < self.config.min_inside_ms:
            return None

        key = self.cooldown_key(track)
        if not self.cooldown.can_emit(key, now_ms):
            return None

        self.cooldown.record_emit(key, self.config.cooldown_s, now_ms)

        last_obs = track.observations[-1]
        return SecurityEvent(
            event_type="intrusion",
            camera_id=last_obs.camera_id,
            source_id=last_obs.source_id,
            track_id=track.track_id,
            start_ts_ms=first_inside_ts,
            end_ts_ms=now_ms,
            confidence=last_obs.confidence,
            severity=self.config.severity,
            zone=self.zone.name,
            rule_name=self.config.name,
            description=f"Track {track.track_id} intruded zone '{self.zone.name}'",
            snapshot_required=self.config.snapshot_required,
            clip_required=self.config.clip_required,
        )

    def _continuous_inside(self, track: TrackState):
        obs_list = track.observations
        if not obs_list:
            return 0, 0
        if not point_in_polygon(obs_list[-1].bbox.foot_point, self.zone.polygon):
            return 0, 0
        first_inside = len(obs_list) - 1
        for i in range(len(obs_list) - 2, -1, -1):
            if point_in_polygon(obs_list[i].bbox.foot_point, self.zone.polygon):
                first_inside = i
            else:
                break
        duration = obs_list[-1].timestamp_ms - obs_list[first_inside].timestamp_ms
        return max(duration, 0), obs_list[first_inside].timestamp_ms
