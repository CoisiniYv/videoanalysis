"""C2.15 Savant ingress FPS gate contract tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "modules" / "savant_security"


def _activate_module_path() -> None:
    for name in list(sys.modules):
        if name == "custom" or name.startswith("custom."):
            del sys.modules[name]
    module_root = str(MODULE_ROOT)
    if module_root in sys.path:
        sys.path.remove(module_root)
    sys.path.insert(0, module_root)


def _frame(source_id: str, pts_ns: int):
    return SimpleNamespace(
        source_id=source_id,
        pts=pts_ns,
        uuid=f"frame-{pts_ns}",
        keyframe_uuid="frame-0" if pts_ns else None,
        previous_keyframe_uuid="frame-0" if pts_ns else None,
        keyframe=False,
        time_base=None,
        content=SimpleNamespace(is_none=lambda: False),
    )


def test_pts_fps_gate_limits_24fps_to_about_8fps_per_source() -> None:
    _activate_module_path()
    from custom.filters.pts_fps_gate import PtsFpsGate

    gate = PtsFpsGate(enabled=True, max_fps="8/1", min_fps="2/1")
    frame_step_ns = 1_000_000_000 // 24

    accepted = [
        index
        for index in range(12)
        if gate(_frame("camera-a", index * frame_step_ns))
    ]

    assert accepted == [0, 3, 6, 9]


def test_pts_fps_gate_keeps_sources_independent() -> None:
    _activate_module_path()
    from custom.filters.pts_fps_gate import PtsFpsGate

    gate = PtsFpsGate(enabled=True, max_fps="8/1", min_fps="2/1")

    assert gate(_frame("camera-a", 0)) is True
    assert gate(_frame("camera-b", 0)) is True
    assert gate(_frame("camera-a", 41_666_666)) is False
    assert gate(_frame("camera-b", 41_666_666)) is False
    assert gate(_frame("camera-a", 166_666_666)) is True
    assert gate(_frame("camera-b", 166_666_666)) is True


def test_pts_fps_gate_passes_missing_pts_for_diagnostics() -> None:
    _activate_module_path()
    from custom.filters.pts_fps_gate import PtsFpsGate

    gate = PtsFpsGate(enabled=True, max_fps="8/1", min_fps="2/1")

    assert gate(_frame("camera-a", 0)) is True
    assert gate(SimpleNamespace(source_id="camera-a", pts=None, content=SimpleNamespace(is_none=lambda: False))) is True


def test_pts_fps_gate_always_passes_keyframes() -> None:
    _activate_module_path()
    from custom.filters.pts_fps_gate import PtsFpsGate

    gate = PtsFpsGate(enabled=True, max_fps="8/1", min_fps="2/1")

    assert gate(_frame("camera-a", 0)) is True
    assert gate(
        SimpleNamespace(
            source_id="camera-a",
            pts=41_666_666,
            uuid="keyframe-1",
            keyframe_uuid="keyframe-1",
            previous_keyframe_uuid="keyframe-1",
            keyframe=True,
            time_base=None,
            content=SimpleNamespace(is_none=lambda: False),
        )
    ) is True


def test_pts_fps_gate_does_not_treat_unknown_keyframe_state_as_keyframe() -> None:
    _activate_module_path()
    from custom.filters.pts_fps_gate import PtsFpsGate

    gate = PtsFpsGate(enabled=True, max_fps="8/1", min_fps="2/1")

    assert gate(_frame("camera-a", 0)) is True
    assert gate(
        SimpleNamespace(
            source_id="camera-a",
            pts=41_666_666,
            uuid="frame-without-keyframe-state",
            keyframe_uuid=None,
            previous_keyframe_uuid=None,
            time_base=None,
            content=SimpleNamespace(is_none=lambda: False),
        )
    ) is False


def test_pts_fps_gate_drops_empty_content() -> None:
    _activate_module_path()
    from custom.filters.pts_fps_gate import PtsFpsGate

    gate = PtsFpsGate(enabled=True, max_fps="8/1", min_fps="2/1")
    empty = SimpleNamespace(
        source_id="camera-a",
        pts=0,
        content=SimpleNamespace(is_none=lambda: True),
    )

    assert gate(empty) is False


def test_env_pts_fps_gate_defaults_off_for_replay_first_compressed_streams(
    monkeypatch,
) -> None:
    _activate_module_path()
    from custom.filters.pts_fps_gate import env_pts_fps_gate

    monkeypatch.delenv("MAX_FPS_CONTROL", raising=False)

    gate = env_pts_fps_gate()

    assert gate.enabled is False
