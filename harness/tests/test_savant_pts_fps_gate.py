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
        keyframe_uuid=None,
        previous_keyframe_uuid=None,
        keyframe=False,
        time_base=None,
        content=SimpleNamespace(is_none=lambda: False),
    )


def test_pts_fps_gate_preserves_fractional_credit_near_target() -> None:
    _activate_module_path()
    from custom.filters.pts_fps_gate import PtsFpsGate

    gate = PtsFpsGate(enabled=True, max_fps="8/1", min_fps="2/1")

    accepted = [
        index
        for index in range(800)
        if gate(_frame("camera-a", round(index * 124_500_000)))
    ]

    assert len(accepted) >= 790


def test_pts_fps_gate_still_limits_24fps_to_8fps() -> None:
    _activate_module_path()
    from custom.filters.pts_fps_gate import PtsFpsGate

    gate = PtsFpsGate(enabled=True, max_fps="8/1", min_fps="2/1")

    accepted = [
        index
        for index in range(24)
        if gate(_frame("camera-a", index * 41_666_666))
    ]

    assert accepted == [0, 3, 6, 9, 12, 15, 18, 21]
