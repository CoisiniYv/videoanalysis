#!/usr/bin/env python3
"""Export runtime config files from the FastAPI camera service.

Produces:

1. ``modules/savant_security/config/cameras.midterm.yml``
   The Savant module reads this through ``custom.services.camera_config``.
   It is byte-equivalent to the FastAPI ``/api/v1/cameras/config/export``
   response.

2. ``infra/generated/sources.generated.yml``
   The adapter-side source manifest read by
   ``scripts/runtime/camera_source_controller.py``. One entry per camera,
   keyed by ``camera_id``, with the source_id / uri / zmq endpoint the
   controller needs to spawn a gstreamer adapter container.

The console log never includes ``rtsp_url`` — operators copy logs into
tickets, and RTSP URLs frequently carry credentials. Only the safe
fields (camera_id / source_id / name / enabled) are printed.

Usage::

    python scripts/config/export_runtime_configs.py \\
        --api-base-url http://localhost:8001 \\
        --module-config-output modules/savant_security/config/cameras.midterm.yml \\
        --sources-output infra/generated/sources.generated.yml
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

import yaml


DEFAULT_API_BASE_URL = "http://localhost:8001"
DEFAULT_MODULE_OUTPUT = "modules/savant_security/config/cameras.midterm.yml"
DEFAULT_SOURCES_OUTPUT = "infra/generated/sources.generated.yml"
EXPORT_PATH = "/api/v1/cameras/config/export"
HTTP_TIMEOUT_SECONDS = 30

# Default ZMQ endpoint the gstreamer source adapter will connect to.
# Format matches what the midterm compose's source adapter path expects.
DEFAULT_ZMQ_ENDPOINT = "dealer+connect:tcp://savant-security:5555"

ADAPTER_TYPE_GSTREAMER = "gstreamer"


def default_fetcher(url: str) -> str:
    """Synchronous GET via stdlib urllib. Raises ``RuntimeError`` on failure."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"transport error from {url}: {exc.reason}") from exc
    if not 200 <= status < 300:
        raise RuntimeError(f"HTTP {status} from {url}")
    return body


def main(
    argv: Optional[List[str]] = None,
    *,
    fetcher: Optional[Callable[[str], str]] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> int:
    args = _parse_args(argv)
    _fetch = fetcher if fetcher is not None else default_fetcher
    _log = logger if logger is not None else _stderr_log

    url = args.api_base_url.rstrip("/") + EXPORT_PATH
    if args.include_disabled:
        url += "?include_disabled=true"
    _log(f"GET {url}")

    try:
        body = _fetch(url)
    except Exception as exc:
        _log(f"ERROR: fetch failed: {exc}")
        return 1

    try:
        doc = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        _log(f"ERROR: response is not valid YAML: {exc}")
        return 2

    if not isinstance(doc, dict) or "cameras" not in doc:
        _log("ERROR: response missing top-level 'cameras' key")
        return 3

    cameras = doc.get("cameras") or {}
    if not isinstance(cameras, dict):
        _log("ERROR: 'cameras' must be a mapping")
        return 3

    _log(f"received {len(cameras)} camera entries")
    for cam_id, cam_doc in cameras.items():
        _log_camera_summary(_log, cam_id, cam_doc)

    # ------------------------------------------------------------------
    # File 1: module-side cameras.midterm.yml (write the body verbatim)
    # ------------------------------------------------------------------
    module_out = os.path.abspath(args.module_config_output)
    try:
        _atomic_write_text(module_out, body, _log)
    except OSError as exc:
        _log(f"ERROR: module config write failed: {exc}")
        return 4
    _log(f"wrote {module_out}")

    # ------------------------------------------------------------------
    # File 2: adapter-side sources.generated.yml
    # ------------------------------------------------------------------
    sources_doc = _build_sources_doc(
        cameras, zmq_endpoint=args.zmq_endpoint,
        adapter_type=args.adapter_type,
    )
    sources_text = yaml.safe_dump(sources_doc, sort_keys=False, allow_unicode=True)

    # Validate round-trip before writing — guards against odd unicode
    # camera ids or non-string keys we did not anticipate.
    try:
        round_tripped = yaml.safe_load(sources_text)
    except yaml.YAMLError as exc:
        _log(f"ERROR: generated sources YAML failed validation: {exc}")
        return 5
    if not isinstance(round_tripped, dict) or "sources" not in round_tripped:
        _log("ERROR: generated sources YAML missing 'sources' key")
        return 5

    sources_out = os.path.abspath(args.sources_output)
    try:
        _atomic_write_text(sources_out, sources_text, _log)
    except OSError as exc:
        _log(f"ERROR: sources config write failed: {exc}")
        return 4
    _log(
        f"wrote {sources_out} "
        f"(active={sum(1 for s in sources_doc['sources'].values() if s['enabled'])}, "
        f"total={len(sources_doc['sources'])})"
    )

    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export module + adapter runtime configs from the FastAPI service.",
    )
    parser.add_argument(
        "--api-base-url",
        default=os.environ.get("VIDEO_ANALYTICS_API", DEFAULT_API_BASE_URL),
    )
    parser.add_argument(
        "--module-config-output",
        default=DEFAULT_MODULE_OUTPUT,
        help=f"Path for cameras.midterm.yml (default: {DEFAULT_MODULE_OUTPUT})",
    )
    parser.add_argument(
        "--sources-output",
        default=DEFAULT_SOURCES_OUTPUT,
        help=f"Path for sources.generated.yml (default: {DEFAULT_SOURCES_OUTPUT})",
    )
    parser.add_argument(
        "--zmq-endpoint",
        default=os.environ.get("SAVANT_ZMQ_INPUT", DEFAULT_ZMQ_ENDPOINT),
        help=(
            "ZMQ endpoint the source adapter will dial. "
            f"Default: {DEFAULT_ZMQ_ENDPOINT}"
        ),
    )
    parser.add_argument(
        "--adapter-type",
        default=ADAPTER_TYPE_GSTREAMER,
        choices=[ADAPTER_TYPE_GSTREAMER],
        help="Adapter family written into sources.generated.yml.",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Pass ?include_disabled=true to the API.",
    )
    return parser.parse_args(argv)


def _stderr_log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _log_camera_summary(
    log: Callable[[str], None], cam_id: str, cam_doc: Any
) -> None:
    """Log only the fields safe to print. Never log ``rtsp_url``."""
    if not isinstance(cam_doc, dict):
        log(f"  camera_id={cam_id} (non-mapping entry, skipped)")
        return
    log(
        f"  camera_id={cam_id} "
        f"source_id={cam_doc.get('source_id', '')} "
        f"name={cam_doc.get('name', '')!r} "
        f"enabled={cam_doc.get('enabled', True)}"
    )


def _build_sources_doc(
    cameras: Dict[str, Any],
    *,
    zmq_endpoint: str,
    adapter_type: str,
) -> Dict[str, Any]:
    """Build the adapter-side sources.generated.yml document.

    Disabled cameras are included but flagged ``enabled: false`` so the
    operator can inspect them; the controller will refuse to start them.
    """
    sources: Dict[str, Any] = {}
    for cam_id, cam_doc in cameras.items():
        if not isinstance(cam_doc, dict):
            continue
        sources[cam_id] = {
            "camera_id": cam_id,
            "source_id": str(cam_doc.get("source_id", "")),
            "uri": str(cam_doc.get("rtsp_url", "")),
            "enabled": bool(cam_doc.get("enabled", True)),
            "adapter_type": adapter_type,
            "zmq_endpoint": zmq_endpoint,
        }
    return {"sources": sources}


def _atomic_write_text(
    path: str, text: str, log: Callable[[str], None]
) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
        log(f"created parent directory: {parent}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
