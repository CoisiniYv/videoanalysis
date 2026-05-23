"""Inspect an ONNX model and print input/output names, shapes, and dtypes.

Usage:
    python scripts/model_inspect/inspect_onnx.py /path/to/model.onnx
"""

import sys
from pathlib import Path


def inspect_onnx(model_path: str) -> None:
    p = Path(model_path)
    if not p.is_file():
        print(f"[FAIL] Model not found: {model_path}")
        sys.exit(1)

    try:
        import onnx
    except ImportError:
        try:
            import onnxruntime as ort
        except ImportError:
            print("[FAIL] Neither 'onnx' nor 'onnxruntime' is installed.")
            print("       Install one of: pip install onnx  or  pip install onnxruntime")
            sys.exit(1)
        _inspect_ort(p)
    else:
        _inspect_onnx(p, onnx)


def _inspect_onnx(p: Path, onnx_module):
    model = onnx_module.load(str(p))
    print(f"=== ONNX Model: {p.name} ===")
    print(f"IR version: {model.ir_version}")
    print(f"Opset: {model.opset_import[0].version if model.opset_import else 'N/A'}")
    print(f"Producer: {model.producer_name} {model.producer_version}")

    print(f"\n--- Inputs ({len(model.graph.input)}) ---")
    for inp in model.graph.input:
        shape = [d.dim_value for d in inp.type.tensor_type.shape.dim]
        dtype = inp.type.tensor_type.elem_type
        print(f"  {inp.name}: shape={shape} dtype={dtype}")

    print(f"\n--- Outputs ({len(model.graph.output)}) ---")
    for out in model.graph.output:
        shape = [d.dim_value for d in out.type.tensor_type.shape.dim]
        dtype = out.type.tensor_type.elem_type
        print(f"  {out.name}: shape={shape} dtype={dtype}")

    # Estimate total parameter count
    total_params = 0
    for init in model.graph.initializer:
        n = 1
        for d in init.dims:
            n *= d
        total_params += n
    print(f"\nTotal initializer params (approx): {total_params:,}")


def _inspect_ort(p: Path):
    import numpy as np

    session = ort.InferenceSession(str(p))
    print(f"=== ONNX Model: {p.name} (via onnxruntime) ===")

    print(f"\n--- Inputs ({len(session.get_inputs())}) ---")
    for inp in session.get_inputs():
        print(f"  {inp.name}: shape={inp.shape} dtype={inp.type}")

    print(f"\n--- Outputs ({len(session.get_outputs())}) ---")
    for out in session.get_outputs():
        print(f"  {out.name}: shape={out.shape} dtype={out.type}")

    # Run a dummy forward pass
    input_meta = session.get_inputs()[0]
    dummy_shape = [d if isinstance(d, int) and d > 0 else 1 for d in input_meta.shape]
    dummy_input = np.random.randn(*dummy_shape).astype(np.float32)
    outputs = session.run(None, {input_meta.name: dummy_input})
    print(f"\n--- Dummy forward pass ---")
    for i, (out_meta, out_tensor) in enumerate(zip(session.get_outputs(), outputs)):
        print(f"  {out_meta.name}: actual_shape={out_tensor.shape} dtype={out_tensor.dtype}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/model_inspect/inspect_onnx.py <model.onnx>")
        sys.exit(1)
    inspect_onnx(sys.argv[1])
