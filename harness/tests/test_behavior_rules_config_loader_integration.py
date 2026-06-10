"""Integration tests for BehaviorRulesPyFunc's rule-runtime adapter (midterm).

The Savant pyfunc itself imports ``savant.deepstream.pyfunc`` which is
only available inside the GPU container. These tests target the pure
adapter that the pyfunc uses — ``custom.services.rule_runtime`` — and
verify the full path:

    cameras.midterm.yml
        -> load_camera_config (midterm)
            -> build_per_source_runtime (midterm)
                -> rule.evaluate -> SecurityEvent

That exercises the same code path the runtime uses, without touching
Savant. The bbox→event flow is identical to what
``BehaviorRulesPyFunc._process_frame_impl`` does, minus the
adapter-call to ``build_person_pose_observations``.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
MODULES_ROOT = str(Path(__file__).resolve().parents[2] / "modules")


def _isolate_savant_security_modules():
    """Drop sibling runtime paths from sys.path and clear cached custom.* imports."""
    sys.path[:] = [
        p for p in sys.path
        if not (p.startswith(MODULES_ROOT) and p != MODULE_DIR)
    ]
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    for name in [m for m in list(sys.modules) if m == "custom" or m.startswith("custom.")]:
        sys.modules.pop(name, None)


@pytest.fixture()
def rt_mod():
    _isolate_savant_security_modules()
    return importlib.import_module("custom.services.rule_runtime")


@pytest.fixture()
def loader_mod():
    _isolate_savant_security_modules()
    return importlib.import_module("custom.services.camera_config")


def _write_yaml(tmp_path, text):
    p = tmp_path / "cameras.midterm.yml"
    p.write_text(textwrap.dedent(text).strip() + "\n")
    return str(p)


VALID_YAML = """
    cameras:
      cam_001:
        enabled: true
        source_id: primary_rtsp
        name: Test
        input:
          type: rtsp
          rtsp_url: rtsp://example.local/stream
          rtsp_transport: tcp
        gpu_id: 0
        zones:
          perimeter:
            zone_id: perimeter
            zone_type: polygon
            points:
              - [100, 100]
              - [400, 100]
              - [400, 400]
              - [100, 400]
        rules:
          rule_intrusion:
            rule_id: rule_intrusion
            algorithm_id: behavior.intrusion
            rule_type: intrusion
            enabled: true
            config:
              zone_id: perimeter
              min_inside_ms: 500
              cooldown_s: 60
              severity: high
              snapshot_required: true
              clip_required: false
            evidence_policy:
              snapshot_required: true
              clip_required: true
              pre_seconds: 6
              post_seconds: 12
          rule_watchlist:
            rule_id: rule_watchlist
            algorithm_id: face.watchlist
            rule_type: face.watchlist
            enabled: true
            config:
              threshold: 0.75
              cooldown_s: 60
"""


def _make_track_inside(track_id, camera_id, ts_start, ts_end, count=5):
    from custom.models.pose import BBox, PersonPoseObservation
    from custom.models.tracks import TrackState

    track = TrackState(track_id=track_id)
    step = (ts_end - ts_start) // max(count - 1, 1)
    for i in range(count):
        ts = ts_start + step * i
        track.add_observation(
            PersonPoseObservation(
                source_id="",   # set on event by pyfunc
                camera_id=camera_id,
                frame_id=0,
                timestamp_ms=ts,
                bbox=BBox(x=240.0, y=240.0, width=40.0, height=100.0),
                confidence=0.9,
                track_id=track_id,
            ),
            window_s=10.0,
        )
    return track


# ===========================================================================
# 1. source_id → camera_id mapping
# ===========================================================================


def test_source_id_to_camera_id_mapping(loader_mod, rt_mod, tmp_path):
    cfg = _write_yaml(tmp_path, VALID_YAML)
    bundle = loader_mod.load_camera_config(cfg)
    from custom.services.cooldown import CooldownTracker
    runtimes = rt_mod.build_per_source_runtime(bundle, CooldownTracker())
    assert "primary_rtsp" in runtimes
    assert runtimes["primary_rtsp"].camera_id == "cam_001"


# ===========================================================================
# 2. intrusion rule reads YAML perimeter polygon
# ===========================================================================


def test_intrusion_uses_yaml_perimeter_points(loader_mod, rt_mod, tmp_path):
    cfg = _write_yaml(tmp_path, VALID_YAML)
    bundle = loader_mod.load_camera_config(cfg)
    from custom.services.cooldown import CooldownTracker
    runtimes = rt_mod.build_per_source_runtime(bundle, CooldownTracker())
    rule = runtimes["primary_rtsp"].rules[0]
    polygon = rule.zone.polygon
    assert polygon == [(100.0, 100.0), (400.0, 100.0), (400.0, 400.0), (100.0, 400.0)]
    assert [rule.config.rule_type for rule in runtimes["primary_rtsp"].rules] == ["intrusion"]


# ===========================================================================
# 3. unknown source_id is not in the runtimes map
# ===========================================================================


def test_unknown_source_id_not_in_runtimes(loader_mod, rt_mod, tmp_path):
    cfg = _write_yaml(tmp_path, VALID_YAML)
    bundle = loader_mod.load_camera_config(cfg)
    from custom.services.cooldown import CooldownTracker
    runtimes = rt_mod.build_per_source_runtime(bundle, CooldownTracker())
    assert runtimes.get("unconfigured_source") is None


def test_behavior_enablement_is_camera_scoped(loader_mod, rt_mod, tmp_path):
    cfg = _write_yaml(tmp_path, """
        cameras:
          cam_a:
            enabled: true
            source_id: src_a
            name: A
            rtsp_url: rtsp://a
            zones:
              perimeter:
                type: polygon
                points: [[0,0],[10,0],[10,10],[0,10]]
            rules:
              a_intrusion:
                rule_id: a_intrusion
                algorithm_id: behavior.intrusion
                enabled: true
                config:
                  zone_id: perimeter
                  min_inside_ms: 100
                  cooldown_s: 1
          cam_b:
            enabled: true
            source_id: src_b
            name: B
            rtsp_url: rtsp://b
            zones:
              perimeter:
                type: polygon
                points: [[0,0],[10,0],[10,10],[0,10]]
            rules:
              b_watchlist:
                rule_id: b_watchlist
                algorithm_id: face.watchlist
                enabled: true
                config:
                  threshold: 0.75
    """)
    bundle = loader_mod.load_camera_config(cfg)
    from custom.services.cooldown import CooldownTracker
    runtimes = rt_mod.build_per_source_runtime(bundle, CooldownTracker())
    assert set(runtimes) == {"src_a"}
    assert runtimes["src_a"].camera_id == "cam_a"


# ===========================================================================
# 4. disabled camera does not appear in runtimes
# ===========================================================================


def test_disabled_camera_omitted(loader_mod, rt_mod, tmp_path):
    cfg = _write_yaml(tmp_path, """
        cameras:
          cam_off:
            enabled: false
            source_id: off_src
            name: "Off Camera"
            rtsp_url: rtsp://x
            zones:
              z:
                type: polygon
                points: [[0,0],[10,0],[10,10]]
            rules:
              intrusion:
                enabled: true
                zone: z
                min_inside_ms: 100
                cooldown_s: 1
    """)
    bundle = loader_mod.load_camera_config(cfg)
    from custom.services.cooldown import CooldownTracker
    runtimes = rt_mod.build_per_source_runtime(bundle, CooldownTracker())
    assert runtimes == {}


# ===========================================================================
# 5. disabled rule does not produce a runtime rule
# ===========================================================================


def test_disabled_rule_omitted(loader_mod, rt_mod, tmp_path):
    cfg = _write_yaml(tmp_path, """
        cameras:
          cam_001:
            enabled: true
            source_id: primary_rtsp
            name: Test
            rtsp_url: rtsp://x
            zones:
              perimeter:
                type: polygon
                points: [[0,0],[10,0],[10,10],[0,10]]
            rules:
              intrusion:
                enabled: false
                zone: perimeter
                min_inside_ms: 100
                cooldown_s: 1
    """)
    bundle = loader_mod.load_camera_config(cfg)
    from custom.services.cooldown import CooldownTracker
    runtimes = rt_mod.build_per_source_runtime(bundle, CooldownTracker())
    # No enabled rules → camera does not produce a runtime entry.
    assert runtimes == {}


# ===========================================================================
# 6. severity / snapshot_required / clip_required flow to SecurityEvent
# ===========================================================================


def test_severity_and_flags_flow_to_event(loader_mod, rt_mod, tmp_path):
    cfg = _write_yaml(tmp_path, VALID_YAML)
    bundle = loader_mod.load_camera_config(cfg)
    from custom.services.cooldown import CooldownTracker
    runtimes = rt_mod.build_per_source_runtime(bundle, CooldownTracker())
    rule = runtimes["primary_rtsp"].rules[0]

    track = _make_track_inside(track_id=5, camera_id="cam_001", ts_start=0, ts_end=1000)
    event = rule.evaluate(track)
    assert event is not None
    assert event.severity == "high"
    assert event.snapshot_required is True
    assert event.clip_required is True
    assert event.algorithm_type == "behavior.intrusion"
    assert event.rule_name == "rule_intrusion"
    assert event.zone == "perimeter"
    assert event.payload["algorithm_id"] == "behavior.intrusion"
    assert event.payload["rule_id"] == "rule_intrusion"
    assert event.payload["zone_id"] == "perimeter"


# ===========================================================================
# 7. ROI is loaded from YAML, NOT from a hardcoded default
# ===========================================================================


def test_roi_comes_from_yaml_not_hardcode(loader_mod, rt_mod, tmp_path):
    """Different YAML → different polygon → different in/out decision.

    With a tiny zone in the corner the same track foot_point falls
    outside and no event fires. This proves the polygon is read from
    YAML, not from a stale default.
    """
    cfg = _write_yaml(tmp_path, """
        cameras:
          cam_001:
            enabled: true
            source_id: primary_rtsp
            name: Test
            rtsp_url: rtsp://x
            zones:
              perimeter:
                type: polygon
                points: [[0,0],[10,0],[10,10],[0,10]]   # 10x10 corner zone
            rules:
              intrusion:
                enabled: true
                zone: perimeter
                min_inside_ms: 100
                cooldown_s: 1
                severity: low
    """)
    bundle = loader_mod.load_camera_config(cfg)
    from custom.services.cooldown import CooldownTracker
    runtimes = rt_mod.build_per_source_runtime(bundle, CooldownTracker())
    rule = runtimes["primary_rtsp"].rules[0]

    # Track foot is at ~(260, 340) — outside the (0,0)-(10,10) zone.
    track = _make_track_inside(track_id=5, camera_id="cam_001", ts_start=0, ts_end=1000)
    event = rule.evaluate(track)
    assert event is None


# ===========================================================================
# 8. invalid YAML at construction → fail fast
# ===========================================================================


def test_invalid_yaml_raises_at_load_time(loader_mod, tmp_path):
    cfg = _write_yaml(tmp_path, """
        cameras:
          cam_001:
            enabled: true
            source_id: primary_rtsp
            name: Test
            rtsp_url: rtsp://x
            zones:
              perimeter:
                type: polygon
                points: [[0,0],[10,0],[10,10]]
            rules:
              intrusion:
                enabled: true
                zone: does_not_exist  # bad reference
                min_inside_ms: 100
                cooldown_s: 1
    """)
    with pytest.raises(loader_mod.CameraConfigError):
        loader_mod.load_camera_config(cfg)


# ===========================================================================
# 9. enrichment populates the camera_id from runtime, not from raw source_id
# ===========================================================================


def test_camera_id_comes_from_config(loader_mod, rt_mod, tmp_path):
    cfg = _write_yaml(tmp_path, VALID_YAML)
    bundle = loader_mod.load_camera_config(cfg)
    from custom.services.cooldown import CooldownTracker
    runtimes = rt_mod.build_per_source_runtime(bundle, CooldownTracker())
    rt = runtimes["primary_rtsp"]
    # The map source_id → CameraEntry must give back the API-side camera_id.
    assert rt.source_id == "primary_rtsp"
    assert rt.camera_id == "cam_001"


def test_camera_entry_to_legacy_config_preserves_fields(loader_mod, rt_mod, tmp_path):
    cfg = _write_yaml(tmp_path, VALID_YAML)
    bundle = loader_mod.load_camera_config(cfg)
    cam = bundle.get_camera("cam_001")
    legacy = rt_mod.camera_entry_to_legacy_config(cam)
    assert legacy.camera_id == "cam_001"
    rule_cfg = legacy.rules["rule_intrusion"]
    assert rule_cfg.min_inside_ms == 500
    assert rule_cfg.cooldown_s == 60
    assert rule_cfg.severity == "high"
    assert rule_cfg.snapshot_required is True
    assert rule_cfg.clip_required is True
    assert rule_cfg.config["evidence_policy"]["pre_seconds"] == 6
    assert rule_cfg.config["evidence_policy"]["post_seconds"] == 12
