"""Tests for the registry-built intrusion rule (mainline entrypoint, R1.1).

These tests exercise the rule **through** ``custom.rules.build_rules`` to
prove the unified entrypoint produces a working IntrusionRule. They do
not duplicate the deep coverage in ``test_phase2a_intrusion_rule.py`` —
that test still guards the historical phase2a copy.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
MODULES_ROOT = str(Path(__file__).resolve().parents[2] / "modules")


def _isolate_savant_security_modules():
    """See test_rule_registry._isolate_savant_security_modules."""
    sys.path[:] = [
        p for p in sys.path
        if not (p.startswith(MODULES_ROOT) and p != MODULE_DIR)
    ]
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    for name in [m for m in list(sys.modules) if m == "custom" or m.startswith("custom.")]:
        sys.modules.pop(name, None)


def _fresh_rules():
    _isolate_savant_security_modules()
    return importlib.import_module("custom.rules")


# ---------------------------------------------------------------------------
# Fixtures: a square zone and a continuous-inside track
# ---------------------------------------------------------------------------

ZONE_POLYGON = [(100, 100), (400, 100), (400, 400), (100, 400)]


@pytest.fixture()
def rules_pkg():
    return _fresh_rules()


@pytest.fixture()
def camera_config(rules_pkg):
    """Depends on rules_pkg so module isolation has already happened."""
    from custom.models.camera_config import CameraConfig, RuleConfig, ZoneConfig

    return CameraConfig(
        camera_id="cam_01",
        zones={
            "perimeter": ZoneConfig(name="perimeter", polygon=ZONE_POLYGON),
        },
        rules={
            "intrusion_perimeter": RuleConfig(
                name="intrusion_perimeter",
                rule_type="intrusion",
                zone="perimeter",
                enabled=True,
                min_inside_ms=500,
                cooldown_s=60,
            ),
        },
    )


def _make_inside_track(track_id: int, camera_id: str, ts_start: int, ts_end: int, count: int = 5):
    """Track with foot_point at (270, 350) — inside the square zone."""
    from custom.models.pose import BBox, PersonPoseObservation
    from custom.models.tracks import TrackState

    track = TrackState(track_id=track_id)
    step = (ts_end - ts_start) // max(count - 1, 1)
    for i in range(count):
        ts = ts_start + step * i
        track.add_observation(
            PersonPoseObservation(
                source_id=camera_id,
                camera_id=camera_id,
                frame_id=0,
                timestamp_ms=ts,
                bbox=BBox(x=250.0, y=250.0, width=40.0, height=100.0),
                confidence=0.9,
                track_id=track_id,
            ),
            window_s=10.0,
        )
    return track


# ---------------------------------------------------------------------------
# Entrypoint behavior
# ---------------------------------------------------------------------------

def test_registry_built_rule_fires(rules_pkg, camera_config):
    from custom.services.cooldown import CooldownTracker

    cooldown = CooldownTracker()
    rule = rules_pkg.build_rules(camera_config, cooldown)[0]

    track = _make_inside_track(track_id=1, camera_id="cam_01", ts_start=0, ts_end=1000)
    event = rule.evaluate(track)

    assert event is not None
    assert event.event_type == "intrusion"
    assert event.camera_id == "cam_01"
    assert event.track_id == 1
    assert event.zone == "perimeter"
    assert event.rule_name == "intrusion_perimeter"
    assert event.start_ts_ms <= event.end_ts_ms
    assert event.end_ts_ms - event.start_ts_ms >= 500


def test_registry_built_rule_returns_security_event_dataclass(rules_pkg, camera_config):
    from custom.models.events import SecurityEvent
    from custom.services.cooldown import CooldownTracker

    cooldown = CooldownTracker()
    rule = rules_pkg.build_rules(camera_config, cooldown)[0]

    track = _make_inside_track(track_id=1, camera_id="cam_01", ts_start=0, ts_end=1000)
    event = rule.evaluate(track)
    assert isinstance(event, SecurityEvent)

    # to_dict / to_json contract still works through the registry entrypoint.
    as_dict = event.to_dict()
    assert as_dict["event_type"] == "intrusion"
    assert as_dict["camera_id"] == "cam_01"
    assert as_dict["schema_version"] == "1.0"


def test_registry_built_rule_respects_cooldown(rules_pkg, camera_config):
    from custom.services.cooldown import CooldownTracker

    cooldown = CooldownTracker()
    rule = rules_pkg.build_rules(camera_config, cooldown)[0]

    track = _make_inside_track(track_id=1, camera_id="cam_01", ts_start=0, ts_end=1000)
    first = rule.evaluate(track)
    second = rule.evaluate(track)
    assert first is not None
    assert second is None


def test_multi_camera_same_track_id_does_not_share_cooldown(rules_pkg, camera_config):
    """Two cameras emit on the same track_id: both rules must fire."""
    from custom.services.cooldown import CooldownTracker

    cooldown = CooldownTracker()
    rule = rules_pkg.build_rules(camera_config, cooldown)[0]

    track_cam_a = _make_inside_track(track_id=42, camera_id="cam_01", ts_start=0, ts_end=1000)
    track_cam_b = _make_inside_track(track_id=42, camera_id="cam_02", ts_start=0, ts_end=1000)

    event_a = rule.evaluate(track_cam_a)
    event_b = rule.evaluate(track_cam_b)
    assert event_a is not None and event_a.camera_id == "cam_01"
    assert event_b is not None and event_b.camera_id == "cam_02"


def test_entrypoint_yields_rule_per_enabled_rule_config(rules_pkg):
    """Two enabled intrusion rules → two BehaviorRule instances."""
    from custom.models.camera_config import CameraConfig, RuleConfig, ZoneConfig
    from custom.services.cooldown import CooldownTracker

    cfg = CameraConfig(
        camera_id="cam_01",
        zones={
            "perimeter_a": ZoneConfig(name="perimeter_a", polygon=ZONE_POLYGON),
            "perimeter_b": ZoneConfig(
                name="perimeter_b",
                polygon=[(500, 500), (800, 500), (800, 800), (500, 800)],
            ),
        },
        rules={
            "rule_a": RuleConfig(
                name="rule_a", rule_type="intrusion", zone="perimeter_a", enabled=True
            ),
            "rule_b": RuleConfig(
                name="rule_b", rule_type="intrusion", zone="perimeter_b", enabled=True
            ),
        },
    )
    rules = rules_pkg.build_rules(cfg, CooldownTracker())
    assert {r.config.name for r in rules} == {"rule_a", "rule_b"}
    assert {r.zone.name for r in rules} == {"perimeter_a", "perimeter_b"}
