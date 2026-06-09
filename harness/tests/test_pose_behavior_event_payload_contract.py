"""Behavior event payload enrichment contract for pose rules."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType

import pytest

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
MODULES_ROOT = str(Path(__file__).resolve().parents[2] / "modules")


def _isolate_savant_security_modules() -> None:
    sys.path[:] = [
        p for p in sys.path
        if not (p.startswith(MODULES_ROOT) and p != MODULE_DIR)
    ]
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    for name in [m for m in list(sys.modules) if m == "custom" or m.startswith("custom.")]:
        sys.modules.pop(name, None)


def _install_savant_pyfunc_stub() -> None:
    savant = ModuleType("savant")
    deepstream = ModuleType("savant.deepstream")
    pyfunc = ModuleType("savant.deepstream.pyfunc")

    class NvDsPyFuncPlugin:
        def __init__(self, **kwargs) -> None:
            pass

    pyfunc.NvDsPyFuncPlugin = NvDsPyFuncPlugin
    savant.deepstream = deepstream
    deepstream.pyfunc = pyfunc
    sys.modules["savant"] = savant
    sys.modules["savant.deepstream"] = deepstream
    sys.modules["savant.deepstream.pyfunc"] = pyfunc


@pytest.fixture()
def modules():
    _isolate_savant_security_modules()
    _install_savant_pyfunc_stub()
    return {
        "behavior_rules": importlib.import_module("custom.pyfuncs.behavior_rules"),
        "events": importlib.import_module("custom.models.events"),
        "camera_config": importlib.import_module("custom.services.camera_config"),
        "rule_runtime": importlib.import_module("custom.services.rule_runtime"),
        "pose": importlib.import_module("custom.models.pose"),
        "tracks": importlib.import_module("custom.models.tracks"),
    }


class _Exporter:
    def __init__(self) -> None:
        self.events = []

    def export(self, event) -> None:
        self.events.append(event)


def _plugin(modules):
    cls = modules["behavior_rules"].BehaviorRulesPyFunc
    plugin = cls.__new__(cls)
    plugin.producer = "savant_security"
    plugin.gpu_id = 0
    plugin.exporter = _Exporter()
    return plugin


def _runtime(modules):
    camera_config = modules["camera_config"]
    rule_runtime = modules["rule_runtime"]
    cam = camera_config.CameraEntry(
        camera_id="cam_001",
        source_id="src_001",
        name="Lobby",
        rtsp_url="rtsp://example.local/lobby",
        zones={
            "lobby": camera_config.ZoneEntry(
                name="lobby",
                type="polygon",
                points=[[0, 0], [800, 0], [800, 600], [0, 600]],
            )
        },
        rules={
            "intrusion_lobby": camera_config.RuleEntry(
                rule_id="intrusion_lobby",
                algorithm_id="behavior.intrusion",
                rule_type="intrusion",
                enabled=True,
                config={"zone_id": "lobby", "min_inside_ms": 1000, "cooldown_s": 30},
            )
        },
    )
    return rule_runtime.SourceRuntime(
        source_id="src_001",
        camera_id="cam_001",
        camera_entry=cam,
    )


def _track(modules):
    pose = modules["pose"]
    tracks = modules["tracks"]
    track = tracks.TrackState(track_id=7)
    track.add_observation(
        pose.PersonPoseObservation(
            source_id="src_001",
            camera_id="cam_001",
            frame_id=1,
            timestamp_ms=1000,
            bbox=pose.BBox(x=100.0, y=200.0, width=80.0, height=160.0),
            confidence=0.91,
            track_id=7,
            keypoints=[
                pose.Keypoint(x=120.0, y=220.0, confidence=0.9, name="nose"),
                pose.Keypoint(x=125.0, y=260.0, confidence=0.8, name="left_shoulder"),
            ],
        ),
        window_s=10.0,
    )
    return track


def _frame_meta():
    return SimpleNamespace(
        frame_num=12,
        source_id="src_001",
        frame_uuid="frame-uuid",
        keyframe_uuid="keyframe-uuid",
    )


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "fall",
            {
                "algorithm_id": "behavior.fall",
                "rule_id": "fall_lobby",
                "camera_id": "cam_001",
                "zone_id": "lobby",
                "track_id": 7,
                "posture": {"is_lying": True, "available_votes": 3},
            },
        ),
        (
            "crowd_gathering",
            {
                "algorithm_id": "behavior.crowd_gathering",
                "rule_id": "crowd_lobby",
                "camera_id": "cam_001",
                "zone_id": "lobby",
                "cluster_id": 3,
                "member_track_ids": [1, 2, 3, 4, 5],
            },
        ),
        (
            "chasing",
            {
                "algorithm_id": "behavior.chasing",
                "rule_id": "chasing_lobby",
                "camera_id": "cam_001",
                "zone_id": "lobby",
                "leader_track_id": 11,
                "follower_track_id": 12,
            },
        ),
    ],
)
def test_enrichment_preserves_rule_payload_fields(modules, event_type, payload) -> None:
    events = modules["events"]
    plugin = _plugin(modules)
    event = events.SecurityEvent(
        event_type=event_type,
        camera_id="cam_001",
        source_id="src_001",
        track_id=7 if event_type == "fall" else 0,
        start_ts_ms=1000,
        end_ts_ms=2500,
        confidence=0.9,
        severity="medium",
        zone="lobby",
        rule_name=payload["rule_id"],
        snapshot_required=True,
        clip_required=True,
        payload=dict(payload),
    )

    plugin._enrich_and_export(event, _track(modules), _frame_meta(), _runtime(modules))
    exported = plugin.exporter.events[0]

    for key, value in payload.items():
        assert exported.payload[key] == value
    assert "media" in exported.payload
    assert exported.payload["person_bbox"] == {
        "x": 100.0,
        "y": 200.0,
        "width": 80.0,
        "height": 160.0,
    }
    assert exported.payload["person_quality_gate"]["status"] == "accepted"
    assert "inside_ms" not in exported.payload


def test_frame_level_event_enrichment_does_not_require_representative_track(modules) -> None:
    events = modules["events"]
    plugin = _plugin(modules)
    event = events.SecurityEvent(
        event_type="crowd_gathering",
        camera_id="cam_001",
        source_id="src_001",
        track_id=0,
        start_ts_ms=1000,
        end_ts_ms=3100,
        confidence=1.0,
        severity="medium",
        zone="lobby",
        rule_name="crowd_lobby",
        snapshot_required=True,
        clip_required=True,
        payload={
            "algorithm_id": "behavior.crowd_gathering",
            "rule_id": "crowd_lobby",
            "camera_id": "cam_001",
            "zone_id": "lobby",
            "cluster_id": 3,
            "member_track_ids": [1, 2, 3, 4, 5],
        },
    )

    plugin._enrich_and_export(event, None, _frame_meta(), _runtime(modules))
    exported = plugin.exporter.events[0]

    assert exported.payload["cluster_id"] == 3
    assert exported.payload["member_track_ids"] == [1, 2, 3, 4, 5]
    assert "media" in exported.payload
    assert "person_quality_gate" in exported.payload
    assert "person_bbox" not in exported.payload
    assert "inside_ms" not in exported.payload
