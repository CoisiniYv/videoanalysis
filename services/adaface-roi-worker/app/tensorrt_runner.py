"""TensorRT 10 AdaFace runner backed by CuPy device allocations."""

from __future__ import annotations

from pathlib import Path

import numpy as np


class TensorRTAdaFaceRunner:
    def __init__(self, engine_path: str, max_batch_size: int = 16) -> None:
        import cupy as cp
        import tensorrt as trt

        self._cp = cp
        self._trt = trt
        self._logger = trt.Logger(trt.Logger.WARNING)
        engine_bytes = Path(engine_path).read_bytes()
        self._runtime = trt.Runtime(self._logger)
        self._engine = self._runtime.deserialize_cuda_engine(engine_bytes)
        if self._engine is None:
            raise RuntimeError(f"failed_to_deserialize_engine:{engine_path}")
        self._context = self._engine.create_execution_context()
        if self._context is None:
            raise RuntimeError("failed_to_create_execution_context")
        self._stream = cp.cuda.Stream(non_blocking=True)
        self._max_batch_size = max(int(max_batch_size), 1)
        self._input_name = self._find_tensor(trt.TensorIOMode.INPUT, "input")
        self._feature_name = self._find_tensor(trt.TensorIOMode.OUTPUT, "feature")
        self._output_names = [
            self._engine.get_tensor_name(index)
            for index in range(self._engine.num_io_tensors)
            if self._engine.get_tensor_mode(self._engine.get_tensor_name(index))
            == trt.TensorIOMode.OUTPUT
        ]

    def _find_tensor(self, mode, preferred: str) -> str:
        names = [
            self._engine.get_tensor_name(index)
            for index in range(self._engine.num_io_tensors)
            if self._engine.get_tensor_mode(self._engine.get_tensor_name(index)) == mode
        ]
        if preferred in names:
            return preferred
        if len(names) != 1:
            raise RuntimeError(f"ambiguous_tensor mode={mode} names={names}")
        return names[0]

    def infer(self, aligned_bgr_images: list[np.ndarray]) -> np.ndarray:
        batch = len(aligned_bgr_images)
        if batch < 1 or batch > self._max_batch_size:
            raise ValueError(f"invalid_batch_size:{batch}")
        host_input = preprocess_bgr_batch(aligned_bgr_images)
        self._context.set_input_shape(self._input_name, host_input.shape)

        cp = self._cp
        with self._stream:
            device_input = cp.asarray(host_input)
            self._context.set_tensor_address(self._input_name, device_input.data.ptr)
            outputs = {}
            for name in self._output_names:
                shape = tuple(self._context.get_tensor_shape(name))
                dtype = np.dtype(self._trt.nptype(self._engine.get_tensor_dtype(name)))
                output = cp.empty(shape, dtype=dtype)
                outputs[name] = output
                self._context.set_tensor_address(name, output.data.ptr)
            if not self._context.execute_async_v3(self._stream.ptr):
                raise RuntimeError("tensorrt_execute_async_v3_failed")
        self._stream.synchronize()
        return cp.asnumpy(outputs[self._feature_name]).astype(np.float32, copy=False)


def preprocess_bgr_batch(images: list[np.ndarray]) -> np.ndarray:
    """Match Savant AdaFace input: BGR, (x-127.5)/127.5, NCHW."""
    if not images:
        raise ValueError("empty_image_batch")
    out = np.empty((len(images), 3, 112, 112), dtype=np.float32)
    for index, image in enumerate(images):
        if image.shape != (112, 112, 3):
            raise ValueError(f"invalid_aligned_image_shape:{image.shape}")
        out[index] = (
            image.astype(np.float32).transpose(2, 0, 1) - np.float32(127.5)
        ) * np.float32(1.0 / 127.5)
    return np.ascontiguousarray(out)
