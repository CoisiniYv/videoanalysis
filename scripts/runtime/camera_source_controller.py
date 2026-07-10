#!/usr/bin/env python3
"""Camera source adapter controller.

Treats each external video source as a *Docker container* that can be
attached or detached at will. This matches Savant's official
recommendation: the Savant module waits on its ZMQ router socket and
adapters connect/disconnect independently. The module is NEVER
restarted to add or remove a camera.

Reads ``infra/generated/sources.generated.yml`` (produced by
``scripts/config/export_runtime_configs.py``) and supports four
commands::

    list                 — show every configured source (rtsp_url redacted)
    start --source-id S  — spawn a gstreamer adapter for source S
    stop  --source-id S  — kill the adapter container for source S
    status --source-id S — print the docker ps line for source S

The adapter container name is fixed at
``video-analytics-source-{source_id}`` so subsequent stop/status calls
can target it deterministically.

This is a CLI controller, not a daemon. Future runtime work can extend this
with a Retina RTSP Service driver or build an API on top of it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml


DEFAULT_SOURCES_PATH = "infra/generated/sources.generated.yml"
DEFAULT_NETWORK = "video-analytics-midterm_default"
DEFAULT_ADAPTER_IMAGE = "ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0"
CONTAINER_NAME_TEMPLATE = "video-analytics-source-{source_id}"
DEFAULT_RTSP_TRANSPORT_PARAMS = "tcp,use_wallclock_as_timestamps=1,fflags=+genpts"

ALLOWED_ADAPTER_TYPES = ("gstreamer",)


@dataclass
class SourceSpec:
    """One row from sources.generated.yml."""

    camera_id: str
    source_id: str
    uri: str
    enabled: bool
    adapter_type: str
    zmq_endpoint: str

    @property
    def container_name(self) -> str:
        return CONTAINER_NAME_TEMPLATE.format(source_id=self.source_id)

    def safe_summary(self) -> Dict[str, Any]:
        """Subset safe to print to logs. ``uri`` is intentionally omitted."""
        return {
            "camera_id": self.camera_id,
            "source_id": self.source_id,
            "scheme": _uri_scheme(self.uri),
            "enabled": self.enabled,
            "adapter_type": self.adapter_type,
        }


CommandRunner = Callable[[List[str]], "subprocess.CompletedProcess"]


def default_runner(cmd: List[str]) -> subprocess.CompletedProcess:
    """Run *cmd* via subprocess.run. ``check=False`` so callers inspect rc."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


# ---------------------------------------------------------------------------
# Source spec loading
# ---------------------------------------------------------------------------


def load_sources(path: str) -> Dict[str, SourceSpec]:
    """Read sources.generated.yml and index by source_id.

    Raises ``FileNotFoundError`` when the file does not exist, and
    ``ValueError`` on structural errors.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"sources.generated.yml not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict) or "sources" not in raw:
        raise ValueError(
            f"{path} must contain a top-level 'sources' mapping"
        )
    sources_raw = raw["sources"] or {}
    if not isinstance(sources_raw, dict):
        raise ValueError("'sources' must be a mapping")

    out: Dict[str, SourceSpec] = {}
    for camera_id, entry in sources_raw.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"sources.{camera_id} must be a mapping, got "
                f"{type(entry).__name__}"
            )
        source_id = str(entry.get("source_id") or "")
        if not source_id:
            raise ValueError(f"sources.{camera_id}.source_id is required")
        if source_id in out:
            raise ValueError(
                f"duplicate source_id {source_id!r} between cameras "
                f"{out[source_id].camera_id!r} and {camera_id!r}"
            )
        adapter_type = str(entry.get("adapter_type") or "")
        if adapter_type not in ALLOWED_ADAPTER_TYPES:
            raise ValueError(
                f"sources.{camera_id}.adapter_type {adapter_type!r} not in "
                f"{list(ALLOWED_ADAPTER_TYPES)}"
            )
        out[source_id] = SourceSpec(
            camera_id=str(camera_id),
            source_id=source_id,
            uri=str(entry.get("uri") or ""),
            enabled=bool(entry.get("enabled", True)),
            adapter_type=adapter_type,
            zmq_endpoint=str(entry.get("zmq_endpoint") or ""),
        )
    return out


# ---------------------------------------------------------------------------
# URI helpers
# ---------------------------------------------------------------------------


def _uri_scheme(uri: str) -> str:
    if "://" not in uri:
        return ""
    return uri.split("://", 1)[0].lower()


def _adapter_entrypoint_for(uri: str) -> str:
    """Pick the gstreamer adapter entrypoint matching *uri*.

    The savant-adapters-gstreamer image ships several entrypoints; we
    pick the simplest that handles each common scheme. RTSP uses
    rtsp.sh, file:// (and local mp4 paths handed through as
    ``LOCATION``) use video_loop.sh.
    """
    scheme = _uri_scheme(uri)
    if scheme in ("rtsp", "rtsps"):
        return "/opt/savant/adapters/gst/sources/rtsp.sh"
    return "/opt/savant/adapters/gst/sources/video_loop.sh"


def _adapter_location_for(uri: str) -> str:
    """Translate uri → the LOCATION env var the adapter scripts read."""
    scheme = _uri_scheme(uri)
    if scheme == "file":
        return uri[len("file://"):]
    return uri


# ---------------------------------------------------------------------------
# Docker command construction
# ---------------------------------------------------------------------------


def docker_run_command(
    spec: SourceSpec,
    *,
    network: str,
    image: str,
    rtsp_transport_params: str = DEFAULT_RTSP_TRANSPORT_PARAMS,
    ffmpeg_timeout_ms: int = 20000,
    extra_volumes: Optional[List[str]] = None,
) -> List[str]:
    """Construct ``docker run -d ...`` for *spec*."""
    location = _adapter_location_for(spec.uri)
    scheme = _uri_scheme(spec.uri)
    # Replay forwards RTSP frames to Savant immediately with relative PTS.
    # An EOS-on-start reset closes the source before frames arrive, and absolute
    # timestamps make replay defer forwarding for live RTSP streams.
    eos_on_start = "false"
    cmd: List[str] = [
        "docker", "run", "-d",
        "--name", spec.container_name,
        "--restart", "unless-stopped",
        "--network", network,
        "-e", f"SOURCE_ID={spec.source_id}",
        "-e", f"LOCATION={location}",
        "-e", f"ZMQ_ENDPOINT={spec.zmq_endpoint}",
        "-e", "SYNC_OUTPUT=false",
        "-e", "BUFFER_LEN=2000",
        "-e", f"EOS_ON_START={eos_on_start}",
        "-e", f"FFMPEG_TIMEOUT_MS={max(1000, int(ffmpeg_timeout_ms or 20000))}",
        "-e", "DOWNLOAD_PATH=/tmp/video-loop-cache",
        "--entrypoint", _adapter_entrypoint_for(spec.uri),
    ]
    if scheme in ("rtsp", "rtsps"):
        cmd[cmd.index("--entrypoint"):cmd.index("--entrypoint")] = [
            "-e", f"RTSP_URI={location}",
            "-e", f"RTSP_TRANSPORT={rtsp_transport_params or DEFAULT_RTSP_TRANSPORT_PARAMS}",
        ]
    for vol in extra_volumes or []:
        cmd.extend(["-v", vol])
    cmd.append(image)
    return cmd


def docker_stop_command(spec: SourceSpec) -> List[str]:
    return ["docker", "rm", "-f", spec.container_name]


def docker_status_command(spec: SourceSpec) -> List[str]:
    return [
        "docker", "ps", "-a",
        "--filter", f"name=^/{spec.container_name}$",
        "--format", "{{.Names}}\t{{.Status}}",
    ]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace, log: Callable[[str], None]) -> int:
    try:
        sources = load_sources(args.sources)
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1
    log(f"sources_path={args.sources} count={len(sources)}")
    for spec in sources.values():
        log(json.dumps(spec.safe_summary()))
    return 0


def cmd_start(
    args: argparse.Namespace,
    log: Callable[[str], None],
    runner: CommandRunner,
) -> int:
    try:
        sources = load_sources(args.sources)
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1
    spec = sources.get(args.source_id)
    if spec is None:
        log(f"ERROR: source_id not found in {args.sources}: {args.source_id}")
        return 2
    if not spec.enabled:
        log(
            f"ERROR: source_id {spec.source_id} is disabled in "
            f"{args.sources}; enable it through the API first"
        )
        return 3

    extra_volumes: List[str] = []
    if args.testvideo_mount:
        extra_volumes.append(args.testvideo_mount)

    cmd = docker_run_command(
        spec,
        network=args.network,
        image=args.adapter_image,
        rtsp_transport_params=args.rtsp_transport_params,
        ffmpeg_timeout_ms=args.ffmpeg_timeout_ms,
        extra_volumes=extra_volumes,
    )
    log(f"START source_id={spec.source_id} container={spec.container_name}")
    log(f"  cmd={_redact_uri_in_cmd(cmd, spec.uri)}")
    result = runner(cmd)
    if result.returncode != 0:
        log(f"ERROR: docker run failed (rc={result.returncode}): {result.stderr.strip()}")
        return 4
    log(f"OK started container_id={result.stdout.strip()[:12]}")
    return 0


def cmd_stop(
    args: argparse.Namespace,
    log: Callable[[str], None],
    runner: CommandRunner,
) -> int:
    container_name = CONTAINER_NAME_TEMPLATE.format(source_id=args.source_id)
    cmd = ["docker", "rm", "-f", container_name]
    log(f"STOP source_id={args.source_id} container={container_name}")
    result = runner(cmd)
    if result.returncode != 0:
        # rm -f on a missing container produces an error — surface it.
        log(f"ERROR: docker rm -f failed (rc={result.returncode}): {result.stderr.strip()}")
        return 5
    log("OK stopped")
    return 0


def cmd_status(
    args: argparse.Namespace,
    log: Callable[[str], None],
    runner: CommandRunner,
) -> int:
    container_name = CONTAINER_NAME_TEMPLATE.format(source_id=args.source_id)
    cmd = [
        "docker", "ps", "-a",
        "--filter", f"name=^/{container_name}$",
        "--format", "{{.Names}}\t{{.Status}}",
    ]
    log(f"STATUS source_id={args.source_id} container={container_name}")
    result = runner(cmd)
    if result.returncode != 0:
        log(f"ERROR: docker ps failed (rc={result.returncode}): {result.stderr.strip()}")
        return 6
    out = result.stdout.strip()
    if not out:
        log("not_running")
        return 1
    log(out)
    return 0


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _redact_uri_in_cmd(cmd: List[str], uri: str) -> str:
    """Replace the uri in a printable command string with a redacted scheme tag."""
    safe = " ".join(shlex.quote(part) for part in cmd)
    if uri and uri in safe:
        scheme = _uri_scheme(uri) or "uri"
        safe = safe.replace(uri, f"<{scheme}-uri-redacted>")
    return safe


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(
    argv: Optional[List[str]] = None,
    *,
    runner: Optional[CommandRunner] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Manage external video source adapter containers."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--sources", default=DEFAULT_SOURCES_PATH,
        help=f"Path to sources.generated.yml (default: {DEFAULT_SOURCES_PATH})",
    )
    common.add_argument(
        "--network", default=DEFAULT_NETWORK,
        help=f"Docker network to attach (default: {DEFAULT_NETWORK})",
    )
    common.add_argument(
        "--adapter-image", default=DEFAULT_ADAPTER_IMAGE,
        help="GStreamer adapter image",
    )
    common.add_argument(
        "--rtsp-transport-params",
        default=os.environ.get("CAMERA_RUNTIME_RTSP_TRANSPORT_PARAMS", DEFAULT_RTSP_TRANSPORT_PARAMS),
        help=(
            "RTSP_TRANSPORT env passed to RTSP adapters "
            f"(default: {DEFAULT_RTSP_TRANSPORT_PARAMS})"
        ),
    )
    common.add_argument(
        "--ffmpeg-timeout-ms",
        type=int,
        default=int(os.environ.get("CAMERA_SOURCE_FFMPEG_TIMEOUT_MS", "20000")),
        help="FFMPEG_TIMEOUT_MS env passed to source adapters.",
    )
    common.add_argument(
        "--testvideo-mount",
        default=None,
        help=(
            "Optional -v mount spec for local test video, e.g. "
            "'/abs/path/to/testVideo:/testVideo:ro'."
        ),
    )

    sub.add_parser("list", parents=[common], help="List configured sources")

    p_start = sub.add_parser("start", parents=[common], help="Start source adapter")
    p_start.add_argument("--source-id", required=True)

    p_stop = sub.add_parser("stop", parents=[common], help="Stop source adapter")
    p_stop.add_argument("--source-id", required=True)

    p_status = sub.add_parser("status", parents=[common], help="Adapter status")
    p_status.add_argument("--source-id", required=True)

    args = parser.parse_args(argv)
    _runner = runner if runner is not None else default_runner
    _log = logger if logger is not None else _stderr_log

    if args.command == "list":
        return cmd_list(args, _log)
    if args.command == "start":
        return cmd_start(args, _log, _runner)
    if args.command == "stop":
        return cmd_stop(args, _log, _runner)
    if args.command == "status":
        return cmd_status(args, _log, _runner)
    _log(f"unknown command: {args.command}")
    return 99


def _stderr_log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
