"""ONNX Runtime session loading utilities for offline face inference."""

from __future__ import annotations

import logging
import os
from typing import Sequence

import numpy as np

logger = logging.getLogger(__name__)

_DEFAULT_PROVIDER_ORDER = ("CUDAExecutionProvider", "CPUExecutionProvider")


def resolve_providers(provider_spec: str | None = None) -> list[str]:
    """Parse provider spec string into ordered provider list.

    Falls back to CPUExecutionProvider if requested providers are unavailable.
    """
    if provider_spec:
        requested = [p.strip() for p in provider_spec.split(",") if p.strip()]
    else:
        requested = list(_DEFAULT_PROVIDER_ORDER)

    try:
        import onnxruntime as ort
        available = set(ort.get_available_providers())
    except ImportError:
        logger.warning("onnxruntime not installed, using CPUExecutionProvider only")
        return ["CPUExecutionProvider"]

    resolved = [p for p in requested if p in available]
    if not resolved:
        resolved = ["CPUExecutionProvider"]

    if resolved != requested:
        logger.info(
            "ONNX provider resolution: requested=%s resolved=%s",
            requested,
            resolved,
        )
    return resolved


def load_session(
    model_path: str,
    provider_spec: str | None = None,
) -> "onnxruntime.InferenceSession":
    """Load an ONNX model with provider fallback.

    Returns an InferenceSession with the best available provider.
    Logs the actual provider used.
    """
    import onnxruntime as ort

    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"ONNX model not found: {model_path}")

    providers = resolve_providers(provider_spec)
    logger.info("Loading ONNX model: %s with providers=%s", model_path, providers)

    sess_opts = ort.SessionOptions()
    sess_opts.log_severity_level = 3  # ERROR only
    session = ort.InferenceSession(model_path, sess_opts, providers=providers)

    actual_providers = session.get_providers()
    logger.info("ONNX model loaded: %s providers=%s", model_path, actual_providers)

    input_info = [(inp.name, inp.shape, inp.type) for inp in session.get_inputs()]
    output_info = [(out.name, out.shape, out.type) for out in session.get_outputs()]
    logger.info("  inputs:  %s", input_info)
    logger.info("  outputs: %s", output_info)

    return session


def get_session_providers(session: "onnxruntime.InferenceSession") -> list[str]:
    """Return the actual providers an InferenceSession is using."""
    return list(session.get_providers())
