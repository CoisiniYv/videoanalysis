"""Person pose adapter contracts."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace


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


class _Obj:
    label = "person"
    element_name = "yolo26_pose"
    track_id = 18446744073709551615
    object_id = 0
    confidence = 0.91

    def __init__(self) -> None:
        self.bbox = SimpleNamespace(xc=100.0, yc=200.0, width=40.0, height=120.0)

    def get_attr_meta(self, element_name: str, attr_name: str):
        if element_name == "tracker" and attr_name == "track_id":
            return SimpleNamespace(value=42)
        return None


def test_adapter_reads_tracker_attr_when_direct_track_id_is_invalid() -> None:
    _isolate_savant_security_modules()
    adapter = importlib.import_module("custom.adapters.person_pose_adapter")
    frame_meta = SimpleNamespace(
        source_id="source_lab",
        frame_num=7,
        buf_pts=1_000_000_000,
        objects=[_Obj()],
    )

    result = adapter.build_person_pose_observations(frame_meta, camera_id="cam_lab")

    assert result.skipped_untracked_person_count == 0
    assert len(result.observations) == 1
    assert result.observations[0].track_id == 42
    assert result.observations[0].camera_id == "cam_lab"


def test_adapter_prefers_epoch_pts_over_stale_ntp_timestamp() -> None:
    _isolate_savant_security_modules()
    adapter = importlib.import_module("custom.adapters.person_pose_adapter")
    frame_meta = SimpleNamespace(
        source_id="source_lab",
        frame_num=7,
        pts=1_783_333_931_920_500_000,
        ntp_timestamp=1_783_334_137_267,
        objects=[_Obj()],
    )

    result = adapter.build_person_pose_observations(frame_meta, camera_id="cam_lab")

    assert len(result.observations) == 1
    assert result.observations[0].timestamp_ms == 1_783_333_931_920


def test_adapter_keeps_ntp_timestamp_when_pts_is_relative() -> None:
    _isolate_savant_security_modules()
    adapter = importlib.import_module("custom.adapters.person_pose_adapter")
    frame_meta = SimpleNamespace(
        source_id="source_lab",
        frame_num=7,
        pts=1_000_000_000,
        ntp_timestamp=1_783_334_137_267,
        objects=[_Obj()],
    )

    result = adapter.build_person_pose_observations(frame_meta, camera_id="cam_lab")

    assert len(result.observations) == 1
    assert result.observations[0].timestamp_ms == 1_783_334_137_267
