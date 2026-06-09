"""Frame-level behavior rule runtime contract tests."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

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


@pytest.fixture()
def modules():
    _isolate_savant_security_modules()
    return {
        "base": importlib.import_module("custom.rules.base"),
        "camera_config": importlib.import_module("custom.models.camera_config"),
        "events": importlib.import_module("custom.models.events"),
        "pose": importlib.import_module("custom.models.pose"),
        "runtime": importlib.import_module("custom.services.rule_runtime"),
        "tracks": importlib.import_module("custom.models.tracks"),
        "cooldown": importlib.import_module("custom.services.cooldown"),
        "loader": importlib.import_module("custom.services.camera_config"),
    }


def _obs(modules, track_id: int, ts_ms: int, camera_id: str = "cam_001"):
    pose = modules["pose"]
    return pose.PersonPoseObservation(
        source_id=f"src_{camera_id}",
        camera_id=camera_id,
        frame_id=ts_ms // 100,
        timestamp_ms=ts_ms,
        bbox=pose.BBox(x=track_id * 10.0, y=100.0, width=20.0, height=80.0),
        confidence=0.9,
        track_id=track_id,
    )


class _CountingFrameRule:
    rule_type = "counting_frame"

    def __init__(self, modules, *, camera_id="cam_001") -> None:
        self.config = modules["camera_config"].RuleConfig(
            name="counting_frame",
            rule_type="counting_frame",
            zone="lobby",
            algorithm_id="behavior.crowd_gathering",
        )
        self.zone = modules["camera_config"].ZoneConfig(
            name="lobby",
            polygon=[(0, 0), (1000, 0), (1000, 1000), (0, 1000)],
        )
        self.cooldown = modules["cooldown"].CooldownTracker()
        self.calls = []
        self._events = modules["events"]
        self.camera_id = camera_id

    def evaluate_frame(self, frame_tracks, frame_ts_ms):
        self.calls.append((frame_ts_ms, [track.track_id for track in frame_tracks]))
        if not frame_tracks:
            return []
        return [
            self._events.SecurityEvent(
                event_type="crowd_gathering",
                camera_id=self.camera_id,
                source_id=f"src_{self.camera_id}",
                track_id=0,
                start_ts_ms=frame_ts_ms,
                end_ts_ms=frame_ts_ms,
                confidence=1.0,
                zone="lobby",
                rule_name="counting_frame",
                payload={"cluster_id": 1},
            ),
            self._events.SecurityEvent(
                event_type="crowd_gathering",
                camera_id=self.camera_id,
                source_id=f"src_{self.camera_id}",
                track_id=0,
                start_ts_ms=frame_ts_ms,
                end_ts_ms=frame_ts_ms,
                confidence=1.0,
                zone="lobby",
                rule_name="counting_frame",
                payload={"cluster_id": 2},
            ),
        ]


def _runtime(modules, rule, source_id="src_cam_001", camera_id="cam_001"):
    loader = modules["loader"]
    runtime_mod = modules["runtime"]
    cam = loader.CameraEntry(
        camera_id=camera_id,
        source_id=source_id,
        name="Camera",
        rtsp_url="rtsp://example",
    )
    return runtime_mod.SourceRuntime(
        source_id=source_id,
        camera_id=camera_id,
        camera_entry=cam,
        rules=[rule],
        single_track_rules=[],
        frame_rules=[rule],
        store=modules["tracks"].TrackStateStore(),
    )


def test_frame_level_rule_runs_once_per_frame_not_once_per_track(modules) -> None:
    rule = _CountingFrameRule(modules)
    runtime = _runtime(modules, rule)

    evaluations = modules["runtime"].evaluate_runtime_frame(
        runtime,
        [_obs(modules, 1, 1000), _obs(modules, 2, 1000), _obs(modules, 3, 1000)],
    )

    assert rule.calls == [(1000, [1, 2, 3])]
    assert len(evaluations) == 2


def test_frame_level_rule_can_return_multiple_events(modules) -> None:
    rule = _CountingFrameRule(modules)
    runtime = _runtime(modules, rule)

    evaluations = modules["runtime"].evaluate_runtime_frame(
        runtime,
        [_obs(modules, 1, 1000), _obs(modules, 2, 1000)],
    )
    assert [ev.event.payload["cluster_id"] for ev in evaluations] == [1, 2]


def test_no_active_tracks_produces_no_events(modules) -> None:
    rule = _CountingFrameRule(modules)
    runtime = _runtime(modules, rule)

    evaluations = modules["runtime"].evaluate_runtime_frame(runtime, [])
    assert evaluations == []
    assert rule.calls == [(0, [])]


def test_source_frame_state_is_isolated(modules) -> None:
    rule_a = _CountingFrameRule(modules, camera_id="cam_a")
    rule_b = _CountingFrameRule(modules, camera_id="cam_b")
    runtime_a = _runtime(modules, rule_a, source_id="src_cam_a", camera_id="cam_a")
    runtime_b = _runtime(modules, rule_b, source_id="src_cam_b", camera_id="cam_b")

    modules["runtime"].evaluate_runtime_frame(
        runtime_a,
        [_obs(modules, 1, 1000, camera_id="cam_a")],
    )
    modules["runtime"].evaluate_runtime_frame(
        runtime_b,
        [_obs(modules, 1, 2000, camera_id="cam_b")],
    )

    assert rule_a.calls == [(1000, [1])]
    assert rule_b.calls == [(2000, [1])]


def test_last_frame_does_not_need_next_frame_to_flush(modules) -> None:
    rule = _CountingFrameRule(modules)
    runtime = _runtime(modules, rule)

    evaluations = modules["runtime"].evaluate_runtime_frame(
        runtime,
        [_obs(modules, 1, 1000)],
    )
    assert len(evaluations) == 2
    assert rule.calls == [(1000, [1])]
