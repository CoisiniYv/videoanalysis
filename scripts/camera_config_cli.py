#!/usr/bin/env python3
"""Operator-friendly camera config CLI.

Wraps the FastAPI camera endpoints, runtime config exporter,
the source controller, and a Pillow-based ROI preview into a
single command surface. Operators get one entry point instead of curl
+ export_runtime_configs.py + camera_source_controller.py + (today)
nothing for ROI preview.

Subcommands:

    add-camera          POST /api/v1/cameras
    add-zone            POST /api/v1/cameras/{camera_id}/zones
    add-intrusion-rule  POST /api/v1/cameras/{camera_id}/rules
    show-camera         GET  /api/v1/cameras/{camera_id}/config
    export-runtime      delegates to scripts/config/export_runtime_configs.py
    start-source        delegates to scripts/runtime/camera_source_controller.py start
    stop-source         delegates to scripts/runtime/camera_source_controller.py stop
    preview-roi         draws the polygon on a snapshot via Pillow

Conventions:
    --api-base-url     defaults to $VIDEO_ANALYTICS_API or http://localhost:8004
    rtsp_url           never appears in console output (credentials safety)
    polygon shape      "x1,y1;x2,y2;...;xN,yN", 3..10 points
    exit codes         non-zero on any API / parser / I/O failure
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_API_BASE_URL = "http://localhost:8004"
HTTP_TIMEOUT_SECONDS = 30

# Polygon ROI vertex bounds. Must match
# services/api/app/schemas/cameras.py and
# modules/savant_security/custom/services/camera_config.py.
POLYGON_MIN_POINTS = 3
POLYGON_MAX_POINTS = 10

ALLOWED_SEVERITIES = ("low", "medium", "high")


# ---------------------------------------------------------------------------
# Polygon / line parsers
# ---------------------------------------------------------------------------


def parse_polygon_points(text: str) -> List[List[float]]:
    """Parse ``"x1,y1;x2,y2;...;xN,yN"`` into ``[[x1,y1],...,[xN,yN]]``.

    Raises ``ValueError`` for any structural problem with a clear message.
    Enforces the project-wide 3..10 vertex bound.
    """
    return _parse_xy_list(
        text,
        kind="polygon",
        min_points=POLYGON_MIN_POINTS,
        max_points=POLYGON_MAX_POINTS,
    )


def parse_line_points(text: str) -> List[List[float]]:
    """Parse two ``x,y`` pairs separated by ``;``. Requires exactly 2 points."""
    return _parse_xy_list(text, kind="line", min_points=2, max_points=2)


def _parse_xy_list(
    text: str,
    *,
    kind: str,
    min_points: int,
    max_points: int,
) -> List[List[float]]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{kind} points string is empty")
    pairs = [p.strip() for p in text.split(";") if p.strip()]
    if not pairs:
        raise ValueError(f"{kind} points string contains no x,y pairs")
    if len(pairs) < min_points or len(pairs) > max_points:
        if min_points == max_points:
            raise ValueError(
                f"{kind} requires exactly {min_points} points (got {len(pairs)})"
            )
        raise ValueError(
            f"{kind} requires {min_points} to {max_points} points (got {len(pairs)})"
        )

    out: List[List[float]] = []
    for idx, pair in enumerate(pairs):
        coords = [c.strip() for c in pair.split(",")]
        if len(coords) != 2:
            raise ValueError(
                f"point #{idx + 1} must be 'x,y' (got {pair!r})"
            )
        if not coords[0] or not coords[1]:
            raise ValueError(
                f"point #{idx + 1} contains an empty coordinate (got {pair!r})"
            )
        try:
            x = float(coords[0])
            y = float(coords[1])
        except ValueError:
            raise ValueError(
                f"point #{idx + 1} has non-numeric coordinate(s): {pair!r}"
            ) from None
        out.append([x, y])
    return out


# ---------------------------------------------------------------------------
# HTTP client (injectable for tests)
# ---------------------------------------------------------------------------


HttpResult = Tuple[int, Any, str]


def default_http(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> HttpResult:
    """Issue a JSON request and return ``(status_code, parsed_body, raw_body)``.

    A non-2xx response is NOT an exception — callers decide what's fatal.
    A transport error raises ``RuntimeError``.
    """
    body: Optional[bytes] = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = (exc.read() or b"").decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"transport error from {url}: {exc.reason}") from exc

    parsed: Any = None
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
    return status, parsed, raw


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_add_camera(
    args: argparse.Namespace,
    http: Callable[..., HttpResult],
    log: Callable[[str], None],
) -> int:
    payload = {
        "id": args.camera_id,
        "source_id": args.source_id,
        "name": args.name,
        "rtsp_url": args.uri,
        "gpu_id": args.gpu_id,
        "enabled": _parse_bool(args.enabled),
    }
    if args.site_id:
        payload["site_id"] = args.site_id
    if args.location:
        payload["location"] = args.location

    url = args.api_base_url.rstrip("/") + "/api/v1/cameras"
    log(
        f"POST /api/v1/cameras "
        f"camera_id={args.camera_id} source_id={args.source_id} "
        f"name={args.name!r} scheme={_uri_scheme(args.uri)} enabled={payload['enabled']}"
    )
    return _post_and_report(url, payload, http, log)


def cmd_add_zone(
    args: argparse.Namespace,
    http: Callable[..., HttpResult],
    log: Callable[[str], None],
) -> int:
    if args.polygon and args.line:
        log("ERROR: pass only one of --polygon or --line")
        return 4
    if not args.polygon and not args.line:
        log("ERROR: must pass --polygon or --line")
        return 4

    if args.polygon is not None:
        try:
            points = parse_polygon_points(args.polygon)
        except ValueError as exc:
            log(f"ERROR: {exc}")
            return 4
        zone_type = "polygon"
    else:
        try:
            points = parse_line_points(args.line)
        except ValueError as exc:
            log(f"ERROR: {exc}")
            return 4
        zone_type = args.zone_type or "line"

    payload = {
        "zone_name": args.zone_name,
        "zone_type": zone_type,
        "points": points,
    }

    url = (
        args.api_base_url.rstrip("/")
        + f"/api/v1/cameras/{args.camera_id}/zones"
    )
    log(
        f"POST /api/v1/cameras/{args.camera_id}/zones "
        f"zone_name={args.zone_name} zone_type={zone_type} "
        f"num_points={len(points)}"
    )
    return _post_and_report(url, payload, http, log)


def cmd_add_intrusion_rule(
    args: argparse.Namespace,
    http: Callable[..., HttpResult],
    log: Callable[[str], None],
) -> int:
    if args.severity not in ALLOWED_SEVERITIES:
        log(
            f"ERROR: --severity must be one of {list(ALLOWED_SEVERITIES)} "
            f"(got {args.severity!r})"
        )
        return 4
    if args.min_inside_ms <= 0:
        log("ERROR: --min-inside-ms must be a positive integer")
        return 4
    if args.cooldown_s < 0:
        log("ERROR: --cooldown-s must be >= 0")
        return 4

    payload = {
        "rule_type": "intrusion",
        "enabled": _parse_bool(args.enabled),
        "config": {
            "zone": args.zone,
            "min_inside_ms": args.min_inside_ms,
            "cooldown_s": args.cooldown_s,
            "severity": args.severity,
            "snapshot_required": _parse_bool(args.snapshot_required),
            "clip_required": _parse_bool(args.clip_required),
        },
    }
    url = (
        args.api_base_url.rstrip("/")
        + f"/api/v1/cameras/{args.camera_id}/rules"
    )
    log(
        f"POST /api/v1/cameras/{args.camera_id}/rules "
        f"rule_type=intrusion zone={args.zone} "
        f"min_inside_ms={args.min_inside_ms} cooldown_s={args.cooldown_s} "
        f"severity={args.severity}"
    )
    return _post_and_report(url, payload, http, log)


def cmd_show_camera(
    args: argparse.Namespace,
    http: Callable[..., HttpResult],
    log: Callable[[str], None],
) -> int:
    url = (
        args.api_base_url.rstrip("/")
        + f"/api/v1/cameras/{args.camera_id}/config"
    )
    log(f"GET /api/v1/cameras/{args.camera_id}/config")
    try:
        status, parsed, raw = http("GET", url)
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1
    if status != 200:
        log(f"ERROR: API returned {status}")
        log(_extract_error(parsed) or raw)
        return 2
    log(_redact_camera_payload(parsed))
    return 0


# ---------------------------------------------------------------------------
# Delegating subcommands (reuse existing scripts)
# ---------------------------------------------------------------------------


def _import_script(name: str) -> Any:
    """Import scripts/{name}.py via importlib without polluting sys.path.

    Registers in sys.modules first so any dataclass introspection works.
    """
    candidates = {
        "export_runtime_configs": REPO_ROOT / "scripts" / "config" / "export_runtime_configs.py",
        "camera_source_controller": REPO_ROOT / "scripts" / "runtime" / "camera_source_controller.py",
    }
    path = candidates[name]
    if not path.exists():
        raise FileNotFoundError(str(path))
    mod_name = f"_camera_cli_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def cmd_export_runtime(
    args: argparse.Namespace,
    log: Callable[[str], None],
) -> int:
    mod = _import_script("export_runtime_configs")
    argv = [
        "--api-base-url", args.api_base_url,
        "--module-config-output", args.module_config_output,
        "--sources-output", args.sources_output,
    ]
    if args.include_disabled:
        argv.append("--include-disabled")
    log(f"delegate -> export_runtime_configs.main({argv!r})")
    return int(mod.main(argv, logger=log))


def cmd_start_source(
    args: argparse.Namespace,
    log: Callable[[str], None],
) -> int:
    mod = _import_script("camera_source_controller")
    argv = [
        "start",
        "--sources", args.sources,
        "--source-id", args.source_id,
        "--network", args.network,
    ]
    if args.testvideo_mount:
        argv.extend(["--testvideo-mount", args.testvideo_mount])
    log(f"delegate -> camera_source_controller.main(start {args.source_id})")
    return int(mod.main(argv, logger=log))


def cmd_stop_source(
    args: argparse.Namespace,
    log: Callable[[str], None],
) -> int:
    mod = _import_script("camera_source_controller")
    argv = ["stop", "--source-id", args.source_id]
    log(f"delegate -> camera_source_controller.main(stop {args.source_id})")
    return int(mod.main(argv, logger=log))


# ---------------------------------------------------------------------------
# preview-roi
# ---------------------------------------------------------------------------


def cmd_preview_roi(
    args: argparse.Namespace,
    log: Callable[[str], None],
) -> int:
    image_path = args.image
    if not os.path.isfile(image_path):
        log(f"ERROR: image not found: {image_path}")
        return 4

    try:
        if args.polygon:
            points = parse_polygon_points(args.polygon)
            shape_kind = "polygon"
        elif args.line:
            points = parse_line_points(args.line)
            shape_kind = "line"
        else:
            log("ERROR: must pass --polygon or --line")
            return 4
    except ValueError as exc:
        log(f"ERROR: {exc}")
        return 4

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        log(
            "ERROR: Pillow is required for preview-roi but is not installed. "
            "Install it with: pip install Pillow"
        )
        return 5

    try:
        with Image.open(image_path) as img:
            preview = img.convert("RGB").copy()
    except Exception as exc:
        log(f"ERROR: failed to open image: {exc}")
        return 4

    draw = ImageDraw.Draw(preview)
    pixel_pts = [(float(x), float(y)) for x, y in points]
    if shape_kind == "polygon":
        # Close the polygon by appending the first point at the end.
        draw.line(pixel_pts + [pixel_pts[0]], fill=(255, 64, 64), width=4)
        for i, (px, py) in enumerate(pixel_pts):
            r = 6
            draw.ellipse((px - r, py - r, px + r, py + r), outline=(255, 64, 64), width=2)
    else:
        draw.line(pixel_pts, fill=(255, 64, 64), width=4)
        for px, py in pixel_pts:
            r = 6
            draw.ellipse((px - r, py - r, px + r, py + r), outline=(255, 64, 64), width=2)

    if args.zone_name:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
        label = f"zone: {args.zone_name}"
        tx, ty = pixel_pts[0]
        draw.rectangle(
            (tx, ty - 22, tx + 8 * len(label) + 16, ty - 2),
            fill=(0, 0, 0),
        )
        draw.text((tx + 4, ty - 20), label, fill=(255, 255, 255), font=font)

    output_path = args.output
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    save_kwargs: Dict[str, Any] = {}
    if output_path.lower().endswith((".jpg", ".jpeg")):
        save_kwargs["quality"] = 92
    preview.save(output_path, **save_kwargs)

    log(
        f"wrote {output_path} ({shape_kind}, {len(points)} points, "
        f"source={image_path})"
    )
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        lo = v.strip().lower()
        if lo in ("1", "true", "yes", "on"):
            return True
        if lo in ("0", "false", "no", "off"):
            return False
    raise argparse.ArgumentTypeError(f"expected boolean, got {v!r}")


def _uri_scheme(uri: str) -> str:
    if not uri or "://" not in uri:
        return ""
    return uri.split("://", 1)[0].lower()


def _post_and_report(
    url: str,
    payload: Dict[str, Any],
    http: Callable[..., HttpResult],
    log: Callable[[str], None],
) -> int:
    try:
        status, parsed, raw = http("POST", url, payload=payload)
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1
    if status == 200:
        log(f"OK ({status})")
        return 0
    if status == 409:
        log(f"already exists ({status})")
        log(_extract_error(parsed) or raw)
        return 3
    if status in (400, 422):
        log(f"validation error ({status})")
        log(_extract_error(parsed) or raw)
        return 4
    if status == 404:
        log(f"not found ({status})")
        log(_extract_error(parsed) or raw)
        return 2
    log(f"API returned unexpected {status}")
    log(_extract_error(parsed) or raw)
    return 2


def _extract_error(parsed: Any) -> Optional[str]:
    if not isinstance(parsed, dict):
        return None
    err = parsed.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)
    if isinstance(err, list):
        return json.dumps(err, ensure_ascii=False)
    if err is not None:
        return str(err)
    detail = parsed.get("detail")
    if detail is not None:
        return json.dumps(detail, ensure_ascii=False)
    return None


def _redact_camera_payload(parsed: Any) -> str:
    """Pretty-print the camera config response with rtsp_url redacted."""
    if not isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    data = parsed.get("data", parsed)
    if isinstance(data, dict):
        cam = data.get("camera") if "camera" in data else data
        if isinstance(cam, dict) and "rtsp_url" in cam:
            scheme = _uri_scheme(str(cam["rtsp_url"]))
            cam = dict(cam)
            cam["rtsp_url"] = f"<{scheme or 'uri'}-redacted>" if scheme else "<redacted>"
            data = dict(data)
            if "camera" in data:
                data["camera"] = cam
            else:
                data = cam
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _common_api_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "--api-base-url",
        default=os.environ.get("VIDEO_ANALYTICS_API", DEFAULT_API_BASE_URL),
        help=(
            "FastAPI base URL "
            f"(default: $VIDEO_ANALYTICS_API or {DEFAULT_API_BASE_URL})"
        ),
    )
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="camera_config_cli.py",
        description="Operator CLI for cameras, zones, intrusion rules, and ROI preview.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    api_parent = _common_api_parser()

    p_add_camera = sub.add_parser("add-camera", parents=[api_parent])
    p_add_camera.add_argument("--camera-id", required=True)
    p_add_camera.add_argument("--source-id", required=True)
    p_add_camera.add_argument("--name", required=True)
    p_add_camera.add_argument("--uri", required=True, help="rtsp:// or file:// or http(s)://")
    p_add_camera.add_argument("--location", default=None)
    p_add_camera.add_argument("--site-id", default=None)
    p_add_camera.add_argument("--gpu-id", type=int, default=0)
    p_add_camera.add_argument("--enabled", default="true")

    p_add_zone = sub.add_parser("add-zone", parents=[api_parent])
    p_add_zone.add_argument("--camera-id", required=True)
    p_add_zone.add_argument("--zone-name", required=True)
    p_add_zone.add_argument(
        "--polygon",
        default=None,
        help='Polygon ROI: "x1,y1;x2,y2;...;xN,yN" with 3..10 points.',
    )
    p_add_zone.add_argument(
        "--line",
        default=None,
        help='Line ROI: "x1,y1;x2,y2" (exactly 2 points).',
    )
    p_add_zone.add_argument(
        "--zone-type",
        default=None,
        choices=["line", "direction_line"],
        help="Optional. Only used with --line; defaults to 'line'.",
    )

    p_add_rule = sub.add_parser("add-intrusion-rule", parents=[api_parent])
    p_add_rule.add_argument("--camera-id", required=True)
    p_add_rule.add_argument("--zone", required=True)
    p_add_rule.add_argument("--min-inside-ms", type=int, default=1000)
    p_add_rule.add_argument("--cooldown-s", type=int, default=30)
    p_add_rule.add_argument(
        "--severity", default="medium", choices=list(ALLOWED_SEVERITIES)
    )
    p_add_rule.add_argument("--snapshot-required", default="true")
    p_add_rule.add_argument("--clip-required", default="true")
    p_add_rule.add_argument("--enabled", default="true")

    p_show = sub.add_parser("show-camera", parents=[api_parent])
    p_show.add_argument("--camera-id", required=True)

    p_export = sub.add_parser("export-runtime", parents=[api_parent])
    p_export.add_argument(
        "--module-config-output",
        default="modules/savant_security/config/cameras.midterm.yml",
    )
    p_export.add_argument(
        "--sources-output",
        default="infra/generated/sources.generated.yml",
    )
    p_export.add_argument("--include-disabled", action="store_true")

    p_start = sub.add_parser("start-source", parents=[api_parent])
    p_start.add_argument("--source-id", required=True)
    p_start.add_argument(
        "--sources", default="infra/generated/sources.generated.yml"
    )
    p_start.add_argument(
        "--network", default="video-analytics-midterm_default"
    )
    p_start.add_argument(
        "--testvideo-mount", default=None,
        help="Optional Docker -v mount for local test video.",
    )

    p_stop = sub.add_parser("stop-source", parents=[api_parent])
    p_stop.add_argument("--source-id", required=True)

    p_preview = sub.add_parser("preview-roi")
    p_preview.add_argument("--image", required=True)
    p_preview.add_argument("--polygon", default=None)
    p_preview.add_argument("--line", default=None)
    p_preview.add_argument("--zone-name", default=None)
    p_preview.add_argument("--output", required=True)

    return parser


def main(
    argv: Optional[List[str]] = None,
    *,
    http: Optional[Callable[..., HttpResult]] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> int:
    args = build_parser().parse_args(argv)
    _http = http if http is not None else default_http
    _log = logger if logger is not None else (lambda m: print(m, file=sys.stderr, flush=True))

    if args.command == "add-camera":
        return cmd_add_camera(args, _http, _log)
    if args.command == "add-zone":
        return cmd_add_zone(args, _http, _log)
    if args.command == "add-intrusion-rule":
        return cmd_add_intrusion_rule(args, _http, _log)
    if args.command == "show-camera":
        return cmd_show_camera(args, _http, _log)
    if args.command == "export-runtime":
        return cmd_export_runtime(args, _log)
    if args.command == "start-source":
        return cmd_start_source(args, _log)
    if args.command == "stop-source":
        return cmd_stop_source(args, _log)
    if args.command == "preview-roi":
        return cmd_preview_roi(args, _log)
    _log(f"unknown command: {args.command}")
    return 99


if __name__ == "__main__":
    sys.exit(main())
