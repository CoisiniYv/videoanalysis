#!/usr/bin/env python3
"""Read-only ONNX model inspector.

Prints model-level metadata (ir_version, producer, opset), graph
inputs / outputs with their tensor shapes (including dynamic
dimensions), and node / initializer counts. Optionally runs
``onnx.shape_inference`` to fill in intermediate tensor shapes.

The script is intentionally read-only:

- It does NOT modify the model.
- It does NOT simplify the graph.
- It does NOT build a TensorRT engine.
- It does NOT download anything.

Usage::

    python scripts/models/inspect_onnx_model.py PATH [--json] [--no-shape-inference]

Exit codes:
    0  ok
    1  file missing
    2  ``onnx`` package not importable
    3  ONNX parse / load error
    4  unexpected error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional


def _import_onnx():
    try:
        import onnx  # noqa: F401
        from onnx import shape_inference  # noqa: F401
        return None
    except ImportError as exc:
        print(
            "ERROR: the `onnx` Python package is not importable in this "
            "environment. Install it manually (`pip install onnx` or `conda "
            "install -c conda-forge onnx`) or re-run this script from an "
            "interpreter that already has it. Auto-installation is not done "
            f"by this script.\n  ImportError: {exc}",
            file=sys.stderr,
        )
        return 2


def _proto_dim_to_str(dim) -> str:
    """Render an onnx TensorProto.Dimension as a printable token."""
    # Each dim has either dim_value (int >= 0) or dim_param (symbolic name).
    if dim.HasField("dim_value"):
        return str(dim.dim_value)
    if dim.HasField("dim_param") and dim.dim_param:
        return dim.dim_param
    return "?"


def _describe_value_info(value_info) -> Dict[str, Any]:
    """Convert an onnx ValueInfoProto to a serialisable dict."""
    from onnx import TensorProto

    ttype = value_info.type.tensor_type
    elem = ttype.elem_type
    elem_name = TensorProto.DataType.Name(elem) if elem else "UNKNOWN"
    dims: List[str] = []
    if ttype.shape and ttype.shape.dim:
        dims = [_proto_dim_to_str(d) for d in ttype.shape.dim]
    has_dynamic = any(d in {"?"} or not d.lstrip("-").isdigit() for d in dims) if dims else True
    return {
        "name": value_info.name,
        "elem_type": elem_name,
        "shape": dims,
        "rank": len(dims),
        "dynamic": has_dynamic,
    }


def _human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024 or unit == "TB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def inspect(
    path: str,
    *,
    run_shape_inference: bool = True,
) -> Dict[str, Any]:
    """Return a JSON-serialisable inspection report for *path*."""
    import onnx
    from onnx import shape_inference

    file_size = os.path.getsize(path)
    model = onnx.load(path, load_external_data=False)

    report: Dict[str, Any] = {
        "model_path": os.path.abspath(path),
        "file_size_bytes": file_size,
        "file_size_human": _human_size(file_size),
        "ir_version": int(model.ir_version),
        "producer_name": model.producer_name or "",
        "producer_version": model.producer_version or "",
        "model_version": int(model.model_version) if model.model_version else 0,
        "domain": model.domain or "",
        "opset_imports": [
            {"domain": op.domain or "ai.onnx", "version": int(op.version)}
            for op in model.opset_import
        ],
    }

    graph = model.graph
    report["graph_name"] = graph.name or ""
    report["initializer_count"] = len(graph.initializer)
    report["node_count"] = len(graph.node)

    # Some graphs declare initializers as inputs; filter those out.
    initializer_names = {init.name for init in graph.initializer}
    real_inputs = [
        v for v in graph.input if v.name not in initializer_names
    ]

    report["inputs"] = [_describe_value_info(v) for v in real_inputs]
    report["outputs"] = [_describe_value_info(v) for v in graph.output]

    # Op kind histogram (top 10 by count) — useful for sanity-checking what
    # kind of network this is.
    op_counts: Dict[str, int] = {}
    for node in graph.node:
        op_counts[node.op_type] = op_counts.get(node.op_type, 0) + 1
    report["op_kinds_top"] = sorted(
        op_counts.items(), key=lambda kv: kv[1], reverse=True
    )[:10]

    # Shape inference adds inferred shapes to value_info for intermediate
    # tensors and (more importantly) to outputs whose shape is left
    # symbolic at export time.
    if run_shape_inference:
        try:
            inferred = shape_inference.infer_shapes(model)
            report["outputs_inferred"] = [
                _describe_value_info(v) for v in inferred.graph.output
            ]
        except Exception as exc:
            report["shape_inference_error"] = str(exc)
    else:
        report["shape_inference_error"] = "skipped via --no-shape-inference"

    # Dynamic-dimension summary across inputs and outputs.
    dyn_inputs = [i["name"] for i in report["inputs"] if i["dynamic"]]
    dyn_outputs = [o["name"] for o in report["outputs"] if o["dynamic"]]
    report["dynamic_input_names"] = dyn_inputs
    report["dynamic_output_names"] = dyn_outputs
    report["any_dynamic"] = bool(dyn_inputs or dyn_outputs)

    return report


def _render_text(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"model_path        : {report['model_path']}")
    lines.append(f"file_size         : {report['file_size_human']} ({report['file_size_bytes']} B)")
    lines.append(f"ir_version        : {report['ir_version']}")
    lines.append(f"producer_name     : {report['producer_name'] or '(unspecified)'}")
    lines.append(f"producer_version  : {report['producer_version'] or '(unspecified)'}")
    lines.append(f"model_version     : {report['model_version']}")
    lines.append(f"domain            : {report['domain'] or '(unspecified)'}")
    lines.append("opset_imports     :")
    for op in report["opset_imports"]:
        lines.append(f"  - {op['domain']} v{op['version']}")
    lines.append(f"graph_name        : {report['graph_name'] or '(unspecified)'}")
    lines.append(f"initializer_count : {report['initializer_count']}")
    lines.append(f"node_count        : {report['node_count']}")
    lines.append("op_kinds_top      :")
    for op_type, count in report["op_kinds_top"]:
        lines.append(f"  - {op_type:<24s} {count}")
    lines.append("inputs            :")
    for v in report["inputs"]:
        dyn = " (dynamic)" if v["dynamic"] else ""
        lines.append(f"  - {v['name']}{dyn}")
        lines.append(f"      shape     : {v['shape']}")
        lines.append(f"      elem_type : {v['elem_type']}")
    lines.append("outputs           :")
    for v in report["outputs"]:
        dyn = " (dynamic)" if v["dynamic"] else ""
        lines.append(f"  - {v['name']}{dyn}")
        lines.append(f"      shape     : {v['shape']}")
        lines.append(f"      elem_type : {v['elem_type']}")
    if "outputs_inferred" in report:
        lines.append("outputs (post shape-inference):")
        for v in report["outputs_inferred"]:
            lines.append(f"  - {v['name']}")
            lines.append(f"      shape     : {v['shape']}")
            lines.append(f"      elem_type : {v['elem_type']}")
    elif "shape_inference_error" in report:
        lines.append(f"shape_inference   : SKIPPED ({report['shape_inference_error']})")
    lines.append(f"any_dynamic       : {report['any_dynamic']}")
    if report["dynamic_input_names"]:
        lines.append(f"dynamic_inputs    : {report['dynamic_input_names']}")
    if report["dynamic_output_names"]:
        lines.append(f"dynamic_outputs   : {report['dynamic_output_names']}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only ONNX model inspector."
    )
    parser.add_argument("model_path", help="Path to the .onnx file.")
    parser.add_argument(
        "--json", action="store_true",
        help="Print machine-readable JSON instead of text.",
    )
    parser.add_argument(
        "--no-shape-inference", action="store_true",
        help="Skip onnx.shape_inference (faster, less info).",
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.model_path):
        print(
            f"ERROR: not a file: {args.model_path}",
            file=sys.stderr,
        )
        return 1

    rc = _import_onnx()
    if rc:
        return rc

    try:
        report = inspect(
            args.model_path,
            run_shape_inference=not args.no_shape_inference,
        )
    except Exception as exc:
        # Distinguish onnx.checker / parse failures from anything else.
        msg = f"{type(exc).__name__}: {exc}"
        if "Decode" in msg or "Protobuf" in msg or "parse" in msg.lower():
            print(f"ERROR: failed to parse ONNX file: {msg}", file=sys.stderr)
            return 3
        print(f"ERROR: unexpected failure: {msg}", file=sys.stderr)
        return 4

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(_render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
