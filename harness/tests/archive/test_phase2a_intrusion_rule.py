"""Tests for IntrusionRule."""

import sys
from pathlib import Path

# Make the phase2a module importable
MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_phase2a")
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from custom.geometry.polygon import point_in_polygon
from custom.models.camera_config import RuleConfig, ZoneConfig
from custom.models.pose import BBox, PersonPoseObservation
from custom.models.tracks import TrackState, TrackStateStore
from custom.rules.intrusion import IntrusionRule
from custom.services.cooldown import CooldownTracker


# ---------------------------------------------------------------------------
# A 500×500 square zone
# ---------------------------------------------------------------------------
ZONE = ZoneConfig(
    name="perimeter",
    polygon=[(100, 100), (400, 100), (400, 400), (100, 400)],
)

RULE = RuleConfig(
    name="intrusion_perimeter",
    rule_type="intrusion",
    zone="perimeter",
    enabled=True,
    min_inside_ms=500,
    cooldown_s=60,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_obs(
    track_id: int,
    ts_ms: int,
    x: float = 250.0,
    y: float = 250.0,
    w: float = 40.0,
    h: float = 100.0,
    camera_id: str = "cam_01",
) -> PersonPoseObservation:
    """Create an observation whose foot_point depends on (x, y, h).

    foot_point = (x + w/2, y + h)
    """
    return PersonPoseObservation(
        source_id="test_source",
        camera_id=camera_id,
        frame_id=0,
        timestamp_ms=ts_ms,
        bbox=BBox(x=x, y=y, width=w, height=h),
        confidence=0.9,
        track_id=track_id,
    )


def make_track_inside(ts_start: int, ts_end: int, count: int = 5):
    """Build a TrackState whose foot_point is inside the zone for *count*
    observations spanning *ts_start* .. *ts_end*."""
    track = TrackState(track_id=1)
    step = (ts_end - ts_start) // max(count - 1, 1)
    for i in range(count):
        ts = ts_start + step * i
        # foot at (250+20, 250+100) = (270, 350) → inside the zone
        track.add_observation(_make_obs(1, ts, x=250, y=250), window_s=10.0)
    return track


def make_track_outside(ts_start: int, ts_end: int):
    """Build a TrackState whose foot_point is outside the zone."""
    track = TrackState(track_id=2)
    # foot at (450+20, 450+100) = (470, 550) → outside (y > 400)
    track.add_observation(_make_obs(2, ts_start, x=450, y=450), window_s=10.0)
    track.add_observation(_make_obs(2, ts_end, x=450, y=450), window_s=10.0)
    return track


# ---------------------------------------------------------------------------
# Polygon utility
# ---------------------------------------------------------------------------

def test_point_in_polygon_inside():
    assert point_in_polygon((250, 250), ZONE.polygon) is True


def test_point_in_polygon_outside():
    assert point_in_polygon((50, 50), ZONE.polygon) is False


def test_point_in_polygon_on_edge():
    assert point_in_polygon((100, 200), ZONE.polygon) is True


# ===========================================================================
# 6. Positive case: inside for >= min_inside_ms → event
# ===========================================================================

def test_intrusion_positive():
    cooldown = CooldownTracker()
    rule = IntrusionRule(RULE, ZONE, cooldown)
    track = make_track_inside(0, 1000, count=5)  # span = 1000 ms ≥ 500

    event = rule.evaluate(track)

    assert event is not None
    assert event.event_type == "intrusion"
    assert event.track_id == 1
    assert event.zone == "perimeter"
    assert event.start_ts_ms <= event.end_ts_ms
    # The event must span at least min_inside_ms
    assert event.end_ts_ms - event.start_ts_ms >= 500


# ===========================================================================
# 7. Negative case: inside duration too short → no event
# ===========================================================================

def test_intrusion_duration_too_short():
    cooldown = CooldownTracker()
    rule = IntrusionRule(RULE, ZONE, cooldown)
    # Only 200 ms span < 500 ms min_inside_ms
    track = make_track_inside(0, 200, count=3)

    event = rule.evaluate(track)
    assert event is None


# ===========================================================================
# 8. Negative case: outside ROI → no event
# ===========================================================================

def test_intrusion_outside_roi():
    cooldown = CooldownTracker()
    rule = IntrusionRule(RULE, ZONE, cooldown)
    track = make_track_outside(0, 1000)

    event = rule.evaluate(track)
    assert event is None


# ===========================================================================
# 9. Cooldown prevents duplicate
# ===========================================================================

def test_intrusion_cooldown():
    cooldown = CooldownTracker()
    rule = IntrusionRule(RULE, ZONE, cooldown)

    track = make_track_inside(0, 1000, count=5)

    # First evaluation → event
    event1 = rule.evaluate(track)
    assert event1 is not None

    # Second evaluation with same state → should be blocked by cooldown
    event2 = rule.evaluate(track)
    assert event2 is None

    # After cooldown expires, a new update should allow emission again
    cooldown.clear(f"{track.track_id}:{ZONE.name}")
    event3 = rule.evaluate(track)
    assert event3 is not None


# ===========================================================================
# 10. Multiple tracks isolated
# ===========================================================================

def test_multiple_tracks_isolated():
    cooldown = CooldownTracker()
    rule = IntrusionRule(RULE, ZONE, cooldown)

    inside_track = make_track_inside(0, 1000, count=5)
    outside_track = make_track_outside(0, 1000)

    event_inside = rule.evaluate(inside_track)
    event_outside = rule.evaluate(outside_track)

    assert event_inside is not None
    assert event_outside is None


# ===========================================================================
# Disabled rule → no event
# ===========================================================================

def test_disabled_rule_no_event():
    disabled_rule_cfg = RuleConfig(
        name="intrusion_perimeter",
        rule_type="intrusion",
        zone="perimeter",
        enabled=False,
        min_inside_ms=500,
        cooldown_s=60,
    )
    cooldown = CooldownTracker()
    rule = IntrusionRule(disabled_rule_cfg, ZONE, cooldown)
    track = make_track_inside(0, 1000, count=5)

    event = rule.evaluate(track)
    assert event is None


# ===========================================================================
# End-to-end: store + rule
# ===========================================================================

def test_store_and_rule_integration():
    """Simulate the per-frame flow: update store, evaluate rule on active tracks."""
    cooldown = CooldownTracker()
    rule = IntrusionRule(RULE, ZONE, cooldown)
    store = TrackStateStore(observation_window_s=10.0)

    # Frame sequence: 5 frames over 1 second, foot_point inside zone
    for frame in range(5):
        ts = frame * 250  # 0, 250, 500, 750, 1000 ms
        obs = _make_obs(1, ts, x=250, y=250)
        store.update([obs])

    track = store.get_track(1)
    assert track is not None

    # Continuous inside from t=0 to t=1000 = 1000ms >= 500ms → event
    event = rule.evaluate(track)
    assert event is not None
    assert event.event_type == "intrusion"
    assert event.track_id == 1

    # Evaluate again → cooldown blocks
    assert rule.evaluate(track) is None
