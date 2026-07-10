#!/usr/bin/env python3
"""Create dynamic-batch ONNX variants for the midterm YOLO models.

The checked-in model assets under /data were exported with fixed batch-1 graph
constants.  Simply building a TensorRT engine with batch_size > 1 then accepts a
larger input batch but still emits only a batch-1 output.  This script patches
the safe shape constants that pin the exported graph to batch 1:

* Reshape shape tensors with leading 1 become leading 0, so ONNX copies the
  runtime input batch dimension.
* Nearest-neighbor Resize nodes that used static sizes [1, C, H, W] are
  rewritten to scale factors [1, 1, 2, 2], removing the fixed batch dimension.
* YOLO26-pose is switched to the pre-TopK decoded raw output
  [B, 8400, 56].  The exported TopK path flattens across the batch and is only
  correct for batch 1.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import onnx
from onnx import TensorProto, numpy_helper
import numpy as np


DEFAULT_MODELS = (
    (
        Path("/data/video-analytics/models/yolo26_pose/yolo26_pose.onnx"),
        Path("/data/video-analytics/models/yolo26_pose/yolo26_pose.dynamic.raw56.onnx"),
    ),
    (
        Path("/data/video-analytics/models/yolov8_face/yolov8n-face.onnx"),
        Path("/data/video-analytics/models/yolov8_face/yolov8n-face.dynamic.onnx"),
    ),
)


def _set_graph_batch_dynamic(model: onnx.ModelProto) -> None:
    for value_info in list(model.graph.input) + list(model.graph.output) + list(model.graph.value_info):
        shape = value_info.type.tensor_type.shape
        if not shape.dim:
            continue
        if shape.dim[0].dim_value == 1 or shape.dim[0].dim_param:
            shape.dim[0].ClearField("dim_value")
            shape.dim[0].dim_param = "batch"


def _patch_reshape_shape_initializers(model: onnx.ModelProto) -> int:
    initializers = {initializer.name: initializer for initializer in model.graph.initializer}
    reshape_shape_names = {
        node.input[1]
        for node in model.graph.node
        if node.op_type == "Reshape" and len(node.input) > 1
    }
    patched = 0
    for name in reshape_shape_names:
        initializer = initializers.get(name)
        if initializer is None or initializer.data_type != TensorProto.INT64:
            continue
        values = numpy_helper.to_array(initializer).copy()
        if values.ndim != 1 or values.size < 1 or values[0] != 1:
            continue
        values[0] = 0
        initializer.CopyFrom(numpy_helper.from_array(values.astype(np.int64), name))
        patched += 1
    return patched


def _patch_resize_static_sizes(model: onnx.ModelProto) -> int:
    initializers = {initializer.name: initializer for initializer in model.graph.initializer}
    patched = 0
    for node in model.graph.node:
        if node.op_type != "Resize" or len(node.input) < 4:
            continue
        x_name, roi_name, _old_scales_name, sizes_name = list(node.input[:4])
        initializer = initializers.get(sizes_name)
        if initializer is None or initializer.data_type != TensorProto.INT64:
            continue
        sizes = numpy_helper.to_array(initializer)
        if sizes.ndim != 1 or sizes.size != 4 or sizes[0] != 1:
            continue
        scale_h = float(sizes[2]) / float(max(1, sizes[2] // 2))
        scale_w = float(sizes[3]) / float(max(1, sizes[3] // 2))
        if abs(scale_h - 2.0) > 1e-6 or abs(scale_w - 2.0) > 1e-6:
            raise ValueError(
                f"Unsupported Resize static size {sizes.tolist()} in node {node.name!r}; "
                "only YOLO nearest x2 resize is patched automatically."
            )
        scales_name = f"__dynamic_batch_resize_scales_{patched}"
        scales = np.array([1.0, 1.0, scale_h, scale_w], dtype=np.float32)
        model.graph.initializer.append(numpy_helper.from_array(scales, scales_name))
        del node.input[:]
        node.input.extend([x_name, roi_name, scales_name])
        patched += 1
    return patched


def _switch_pose_to_decoded_raw56_output(model: onnx.ModelProto) -> bool:
    tensor_name = "/model.23/Transpose_output_0"
    if not any(tensor_name in node.output for node in model.graph.node):
        return False
    output_name = "output0"
    for node in model.graph.node:
        for index, name in enumerate(node.output):
            if name == output_name:
                node.output[index] = "__fixed_batch_post_topk_output0"
    model.graph.node.append(
        onnx.helper.make_node(
            "Identity",
            inputs=[tensor_name],
            outputs=[output_name],
            name="/dynamic_batch_raw56_output",
        )
    )
    del model.graph.output[:]
    model.graph.output.extend(
        [
            onnx.helper.make_tensor_value_info(
                output_name,
                TensorProto.FLOAT,
                ["batch", "num_predictions", 56],
            )
        ]
    )
    return True


def patch_model(src: Path, dst: Path) -> tuple[int, int, int]:
    model = onnx.load(src)
    reshape_count = _patch_reshape_shape_initializers(model)
    resize_count = _patch_resize_static_sizes(model)
    raw56_output = _switch_pose_to_decoded_raw56_output(model) if "yolo26_pose" in src.name else False
    _set_graph_batch_dynamic(model)
    dst.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, dst)
    onnx.checker.check_model(str(dst))
    return reshape_count, resize_count, int(raw56_output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        action="append",
        nargs=2,
        metavar=("SRC", "DST"),
        help="Patch one source ONNX to one destination ONNX. Can be repeated.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pairs = (
        [(Path(src), Path(dst)) for src, dst in args.model]
        if args.model
        else list(DEFAULT_MODELS)
    )
    for src, dst in pairs:
        reshape_count, resize_count, raw56_output = patch_model(src, dst)
        print(
            f"patched src={src} dst={dst} "
            f"reshape_shapes={reshape_count} resize_nodes={resize_count} "
            f"raw56_output={raw56_output}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
