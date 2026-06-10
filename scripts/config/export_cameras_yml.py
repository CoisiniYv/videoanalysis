#!/usr/bin/env python3
"""Fetch the FastAPI cameras.yml export and write it to disk.

Usage:
    python scripts/config/export_cameras_yml.py \\
      --api-base-url http://localhost:8001 \\
      --output modules/savant_security/config/cameras.midterm.yml

The script GETs ``/api/v1/cameras/config/export`` from the API, validates
the response with ``yaml.safe_load``, creates the output's parent
directory if needed, then writes the body verbatim.

The console log never includes ``rtsp_url``. Operators get
``camera_id``, ``source_id``, ``name``, ``enabled`` only.

Exit code is non-zero on any HTTP / YAML / I/O failure. The script is
designed to fit in CI / deploy pipelines and Make targets.
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
DEFAULT_OUTPUT = "modules/savant_security/config/cameras.midterm.yml"
EXPORT_PATH = "/api/v1/cameras/config/export"
HTTP_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# HTTP fetcher (kept as a thin shim so tests can inject a fake)
# ---------------------------------------------------------------------------


def default_fetcher(url: str) -> str:
    """Issue a GET against *url* and return the response body as text.

    Raises ``RuntimeError`` on non-2xx status or transport failure.
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"HTTP {exc.code} from {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"transport error from {url}: {exc.reason}") from exc

    if not 200 <= status < 300:
        raise RuntimeError(f"HTTP {status} from {url}")
    return body


# ---------------------------------------------------------------------------
# Main entrypoint (test-friendly)
# ---------------------------------------------------------------------------


def main(
    argv: Optional[List[str]] = None,
    *,
    fetcher: Optional[Callable[[str], str]] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> int:
    """Run the export. Returns a process exit code (0 on success)."""
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

    output_path = os.path.abspath(args.output)
    parent = os.path.dirname(output_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
        _log(f"created parent directory: {parent}")

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError as exc:
        _log(f"ERROR: write failed: {exc}")
        return 4

    _log(f"wrote {output_path}")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch /api/v1/cameras/config/export and write to disk.",
    )
    parser.add_argument(
        "--api-base-url",
        default=os.environ.get("VIDEO_ANALYTICS_API", DEFAULT_API_BASE_URL),
        help=f"FastAPI base URL (default: {DEFAULT_API_BASE_URL})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output file path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Pass ?include_disabled=true to the API",
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
    source_id = cam_doc.get("source_id", "")
    name = cam_doc.get("name", "")
    enabled = cam_doc.get("enabled", True)
    log(
        f"  camera_id={cam_id} source_id={source_id} "
        f"name={name!r} enabled={enabled}"
    )


if __name__ == "__main__":
    sys.exit(main())
