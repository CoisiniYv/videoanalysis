"""TrackState and TrackStateStore — per-track state management."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from custom.models.pose import BBox, PersonPoseObservation, is_valid_track_id


@dataclass
class TrackState:
    track_id: int
    observations: List[PersonPoseObservation] = field(default_factory=list)
    first_seen_ms: int = 0
    last_seen_ms: int = 0

    def add_observation(self, obs: PersonPoseObservation, window_s: float) -> None:
        self.observations.append(obs)
        if self.first_seen_ms == 0 or obs.timestamp_ms < self.first_seen_ms:
            self.first_seen_ms = obs.timestamp_ms
        if obs.timestamp_ms > self.last_seen_ms:
            self.last_seen_ms = obs.timestamp_ms
        self._trim(window_s)

    def _trim(self, window_s: float) -> None:
        if not self.observations:
            return
        cutoff = self.last_seen_ms - int(window_s * 1000)
        self.observations = [o for o in self.observations if o.timestamp_ms >= cutoff]

    @property
    def current_bbox(self) -> BBox:
        if not self.observations:
            return BBox()
        return self.observations[-1].bbox

    @property
    def avg_speed_px_s(self) -> float:
        if len(self.observations) < 2:
            return 0.0
        total_dist = 0.0
        total_time = 0.0
        for i in range(1, len(self.observations)):
            prev_fp = self.observations[i - 1].bbox.foot_point
            curr_fp = self.observations[i].bbox.foot_point
            dx = curr_fp[0] - prev_fp[0]
            dy = curr_fp[1] - prev_fp[1]
            dist = math.sqrt(dx * dx + dy * dy)
            dt = self.observations[i].timestamp_ms - self.observations[i - 1].timestamp_ms
            if dt > 0:
                total_dist += dist
                total_time += dt
        if total_time <= 0.0:
            return 0.0
        return (total_dist / total_time) * 1000.0


class TrackStateStore:
    def __init__(self, observation_window_s: float = 5.0, track_timeout_s: float = 10.0):
        self.observation_window_s = observation_window_s
        self.track_timeout_s = track_timeout_s
        self._tracks: Dict[int, TrackState] = {}

    def update(self, observations: List[PersonPoseObservation]) -> None:
        now_ms = max((o.timestamp_ms for o in observations), default=0)
        for obs in observations:
            tid = obs.track_id
            if not is_valid_track_id(tid):
                continue
            if tid not in self._tracks:
                self._tracks[tid] = TrackState(track_id=tid)
            self._tracks[tid].add_observation(obs, self.observation_window_s)
        self._evict(now_ms)

    def _evict(self, now_ms: int) -> None:
        timeout_ms = int(self.track_timeout_s * 1000)
        stale = [tid for tid, t in self._tracks.items() if now_ms - t.last_seen_ms > timeout_ms]
        for tid in stale:
            del self._tracks[tid]

    def get_track(self, track_id: int) -> Optional[TrackState]:
        return self._tracks.get(track_id)

    @property
    def active_tracks(self) -> List[TrackState]:
        return list(self._tracks.values())

    @property
    def track_count(self) -> int:
        return len(self._tracks)

    def reset(self) -> None:
        self._tracks.clear()
