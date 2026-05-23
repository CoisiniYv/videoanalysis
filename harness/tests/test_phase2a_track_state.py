"""Tests for TrackState and TrackStateStore."""

import sys
import math
from pathlib import Path

# Make the phase2a module importable
MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_phase2a")
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from custom.models.pose import BBox, PersonPoseObservation, is_valid_track_id
from custom.models.tracks import TrackState, TrackStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_obs(
    track_id: int,
    ts_ms: int,
    x: float = 100.0,
    y: float = 200.0,
    w: float = 50.0,
    h: float = 120.0,
    frame_id: int = 0,
) -> PersonPoseObservation:
    return PersonPoseObservation(
        source_id="test",
        camera_id="cam_01",
        frame_id=frame_id,
        timestamp_ms=ts_ms,
        bbox=BBox(x=x, y=y, width=w, height=h),
        confidence=0.9,
        track_id=track_id,
    )


# ===========================================================================
# 1. TrackState accumulates observations
# ===========================================================================

def test_accumulates_observations():
    track = TrackState(track_id=1)
    assert len(track.observations) == 0

    track.add_observation(_make_obs(1, 1000), window_s=10.0)
    assert len(track.observations) == 1

    track.add_observation(_make_obs(1, 1100), window_s=10.0)
    assert len(track.observations) == 2


# ===========================================================================
# 2. first_seen_ms and last_seen_ms
# ===========================================================================

def test_first_and_last_seen():
    track = TrackState(track_id=1)
    track.add_observation(_make_obs(1, 3000), window_s=10.0)
    track.add_observation(_make_obs(1, 1000), window_s=10.0)  # earlier
    track.add_observation(_make_obs(1, 2000), window_s=10.0)

    assert track.first_seen_ms == 1000
    assert track.last_seen_ms == 3000


# ===========================================================================
# 3. current_bbox returns the latest bbox
# ===========================================================================

def test_current_bbox_is_latest():
    track = TrackState(track_id=1)
    track.add_observation(_make_obs(1, 1000, x=10, y=20), window_s=10.0)
    track.add_observation(_make_obs(1, 2000, x=50, y=80), window_s=10.0)

    bbox = track.current_bbox
    assert bbox.x == 50.0
    assert bbox.y == 80.0


# ===========================================================================
# 4. avg_speed_px_s
# ===========================================================================

def test_avg_speed_zero_for_single_observation():
    track = TrackState(track_id=1)
    track.add_observation(_make_obs(1, 1000), window_s=10.0)
    assert track.avg_speed_px_s == 0.0


def test_avg_speed_horizontal():
    track = TrackState(track_id=1)
    # foot_point at (10 + 25, 120) = (35, 120)
    track.add_observation(_make_obs(1, 1000, x=10, y=0, w=50, h=120),
                          window_s=10.0)
    # foot_point at (60 + 25, 120) = (85, 120), moved 50 px right in 1000 ms
    track.add_observation(_make_obs(1, 2000, x=60, y=0, w=50, h=120),
                          window_s=10.0)

    speed = track.avg_speed_px_s
    # 50 px in 1 s = 50 px/s
    assert math.isclose(speed, 50.0, rel_tol=1e-6), f"expected 50, got {speed}"


def test_avg_speed_multiple_segments():
    track = TrackState(track_id=1)
    # t=0:    foot = (25, 120)
    track.add_observation(_make_obs(1, 0, x=0, y=0, w=50, h=120),
                          window_s=10.0)
    # t=1000: foot = (125, 120), moved 100 right
    track.add_observation(_make_obs(1, 1000, x=100, y=0, w=50, h=120),
                          window_s=10.0)
    # t=2000: foot = (225, 120), moved 100 right
    track.add_observation(_make_obs(1, 2000, x=200, y=0, w=50, h=120),
                          window_s=10.0)

    speed = track.avg_speed_px_s
    # total 200 px in 2 s = 100 px/s
    assert math.isclose(speed, 100.0, rel_tol=1e-6), f"expected 100, got {speed}"


# ===========================================================================
# 5. Observation trimming outside window
# ===========================================================================

def test_removes_old_observations():
    track = TrackState(track_id=1)
    # timeline: 0, 2000, 4000, 6000, 8000, 10000
    for t in range(0, 11000, 2000):
        track.add_observation(_make_obs(1, t), window_s=3.0)

    # After the last add, window is last_seen=10000, window=3s → cutoff=7000
    # So only observations with ts >= 7000 survive: 8000, 10000
    assert len(track.observations) == 2
    assert track.observations[0].timestamp_ms == 8000
    assert track.observations[1].timestamp_ms == 10000


# ===========================================================================
# TrackStateStore
# ===========================================================================

def test_store_creates_tracks():
    store = TrackStateStore()
    store.update([_make_obs(1, 1000), _make_obs(2, 1000)])

    assert store.track_count == 2
    assert store.get_track(1) is not None
    assert store.get_track(2) is not None


def test_store_skips_untracked():
    store = TrackStateStore()
    store.update([_make_obs(0, 1000)])  # track_id=0 → filtered
    assert store.track_count == 0


def test_store_evicts_stale_tracks():
    store = TrackStateStore(track_timeout_s=1.0)
    store.update([_make_obs(1, 0), _make_obs(2, 0)])
    assert store.track_count == 2

    # Update with only track 1 at t=5000ms (5s later, > 1s timeout)
    store.update([_make_obs(1, 5000)])
    assert store.track_count == 1
    assert store.get_track(1) is not None
    assert store.get_track(2) is None


def test_store_reset():
    store = TrackStateStore()
    store.update([_make_obs(1, 1000), _make_obs(2, 1000)])
    assert store.track_count == 2
    store.reset()
    assert store.track_count == 0


def test_store_active_tracks():
    store = TrackStateStore()
    store.update([_make_obs(1, 1000), _make_obs(2, 2000)])
    tids = sorted(t.track_id for t in store.active_tracks)
    assert tids == [1, 2]
