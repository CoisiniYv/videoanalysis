"""C1M.5 YOLO26-pose coordinate-transform tests."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "modules" / "savant_security" / "custom" / "converters" / "yolo26_pose.py"
MODULE_DIR = ROOT / "modules" / "savant_security"


def test_1920x1080_to_640_letterbox_has_expected_scale_and_padding() -> None:
    mod = _load_converter()
    transform = mod.compute_pose_coordinate_transform(
        roi=(0.0, 0.0, 1920.0, 1080.0),
        model_w=640.0,
        model_h=640.0,
        mode="letterbox",
    )

    assert transform["letterbox_scale"] == 1 / 3
    assert transform["letterbox_pad_x"] == 0.0
    assert transform["letterbox_pad_y"] == 140.0


def test_letterbox_restore_unpads_y_before_scaling() -> None:
    mod = _load_converter()
    transform = mod.compute_pose_coordinate_transform(
        roi=(0.0, 0.0, 1920.0, 1080.0),
        model_w=640.0,
        model_h=640.0,
        mode="letterbox",
    )

    restored = mod.restore_model_xyxy_to_frame((100.0, 200.0, 300.0, 500.0), transform)

    assert restored == (300.0, 180.0, 900.0, 1080.0)


def test_stretch_restore_keeps_legacy_non_letterbox_y_mapping() -> None:
    mod = _load_converter()
    transform = mod.compute_pose_coordinate_transform(
        roi=(0.0, 0.0, 1920.0, 1080.0),
        model_w=640.0,
        model_h=640.0,
        mode="stretch",
    )

    restored = mod.restore_model_xyxy_to_frame((100.0, 200.0, 300.0, 500.0), transform)

    assert restored == (300.0, 337.5, 900.0, 843.75)


def test_letterbox_restore_applies_same_transform_to_keypoints() -> None:
    mod = _load_converter()
    transform = mod.compute_pose_coordinate_transform(
        roi=(0.0, 0.0, 1920.0, 1080.0),
        model_w=640.0,
        model_h=640.0,
        mode="letterbox",
    )

    points = np.array([[100.0, 200.0], [300.0, 500.0]], dtype=np.float32)
    restored = mod.restore_model_points_to_frame(points, transform)

    assert restored.tolist() == [[300.0, 180.0], [900.0, 1080.0]]


def test_converter_emits_letterbox_restored_bbox_and_keypoints(tmp_path: Path) -> None:
    mod = _load_converter()
    converter = mod.Yolo26PoseConverter(
        confidence_threshold=0.25,
        force_nms=False,
        coordinate_restore_mode="letterbox",
        debug_dump_enabled=True,
        debug_dump_dir=str(tmp_path),
        debug_dump_max_records=5,
    )
    model = _Model()
    tensor = np.zeros((1, 1, 57), dtype=np.float32)
    tensor[0, 0, 0:6] = [100.0, 200.0, 300.0, 500.0, 0.9, 0.0]
    for index in range(17):
        base = 6 + index * 3
        tensor[0, 0, base:base + 3] = [100.0 + index, 200.0 + index, 0.0]

    bbox_tensor, attrs = converter(tensor, model=model, roi=(0.0, 0.0, 1920.0, 1080.0))

    assert bbox_tensor.shape == (1, 6)
    assert bbox_tensor[0, 2:].tolist() == [600.0, 630.0, 600.0, 900.0]
    keypoints = attrs[0][0][1]
    assert keypoints[0:3] == [300.0, 180.0, 0.5]
    debug_path = tmp_path / "yolo26_pose_converter_debug.jsonl"
    assert debug_path.is_file()
    assert '"expected_pad_y": 140.0' in debug_path.read_text(encoding="utf-8")


def test_converter_stretch_mode_preserves_legacy_math() -> None:
    mod = _load_converter()
    converter = mod.Yolo26PoseConverter(
        confidence_threshold=0.25,
        force_nms=False,
        coordinate_restore_mode="stretch",
    )
    model = _Model()
    tensor = np.zeros((1, 1, 57), dtype=np.float32)
    tensor[0, 0, 0:6] = [100.0, 200.0, 300.0, 500.0, 0.9, 0.0]

    bbox_tensor, _attrs = converter(tensor, model=model, roi=(0.0, 0.0, 1920.0, 1080.0))

    assert bbox_tensor[0, 2:].tolist() == [600.0, 590.625, 600.0, 506.25]


class _Input:
    shape = [1, 3, 640, 640]


class _Model:
    input = _Input()


def _load_converter():
    _install_savant_stub()
    for name in list(sys.modules):
        if name == "custom" or name.startswith("custom."):
            del sys.modules[name]
    sys.path.insert(0, str(MODULE_DIR))
    spec = importlib.util.spec_from_file_location("c1m5_yolo26_pose", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_savant_stub() -> None:
    savant = types.ModuleType("savant")
    base = types.ModuleType("savant.base")
    converter = types.ModuleType("savant.base.converter")

    class BaseComplexModelOutputConverter:
        def __init__(self, **_kwargs):
            pass

    converter.BaseComplexModelOutputConverter = BaseComplexModelOutputConverter
    base.converter = converter
    savant.base = base
    sys.modules["savant"] = savant
    sys.modules["savant.base"] = base
    sys.modules["savant.base.converter"] = converter
