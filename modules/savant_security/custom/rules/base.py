"""Abstract base classes for behavior rules.

Single-track rules implement ``BehaviorRule.evaluate(track)``. Multi-track
rules implement ``FrameBehaviorRule.evaluate_frame(frame_tracks, frame_ts_ms)``.
Rules must remain pure Python — no Savant or DeepStream imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from custom.models.camera_config import RuleConfig, ZoneConfig
from custom.models.events import SecurityEvent
from custom.models.tracks import TrackState
from custom.services.cooldown import CooldownTracker


class BehaviorRule(ABC):
    """Abstract behavior rule.

    Concrete rules receive their config, the resolved zone, and the
    shared ``CooldownTracker`` at construction time. ``evaluate`` is
    called once per active track per frame and returns either a
    ``SecurityEvent`` (rule fired) or ``None``.
    """

    rule_type: str = ""

    def __init__(
        self,
        rule_config: RuleConfig,
        zone: ZoneConfig,
        cooldown: CooldownTracker,
    ) -> None:
        self.config = rule_config
        self.zone = zone
        self.cooldown = cooldown

    @abstractmethod
    def evaluate(self, track: TrackState) -> Optional[SecurityEvent]:
        """Evaluate the rule against *track* and return a SecurityEvent or None."""

    def cooldown_key(self, track: TrackState) -> str:
        """Compose a cooldown key that is unique per (camera, track, zone).

        Including ``camera_id`` is critical: nvtracker track_ids are
        per-source, so the same track_id can recur on different cameras.
        Without camera scoping, two cameras would share a single
        cooldown slot.
        """
        camera_id = track.observations[-1].camera_id if track.observations else ""
        return f"{camera_id}:{track.track_id}:{self.zone.name}"


class FrameBehaviorRule(ABC):
    """Abstract frame-level behavior rule.

    ``evaluate_frame`` is called once per source frame after the
    ``TrackStateStore`` has been updated. It receives the full active track set
    and returns zero or more events for that frame.
    """

    rule_type: str = ""

    def __init__(
        self,
        rule_config: RuleConfig,
        zone: ZoneConfig,
        cooldown: CooldownTracker,
    ) -> None:
        self.config = rule_config
        self.zone = zone
        self.cooldown = cooldown

    @abstractmethod
    def evaluate_frame(
        self,
        frame_tracks: list[TrackState],
        frame_ts_ms: int,
    ) -> list[SecurityEvent]:
        """Evaluate all active tracks for one frame."""
