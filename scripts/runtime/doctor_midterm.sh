#!/usr/bin/env bash
# Midterm deployment doctor.
#
# Read-only diagnostics. This script does not start, stop, rebuild, restart, or
# reconfigure any service.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SUMMARY_JSON="${SUMMARY_JSON:-/data/video-analytics/artifacts/midterm/runtime_doctor_summary.json}"

mkdir -p "$(dirname "$SUMMARY_JSON")"

ROOT_DIR="$ROOT_DIR" SUMMARY_JSON="$SUMMARY_JSON" python3 - <<'PY'
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - host diagnostic fallback
    yaml = None


ROOT = Path(os.environ["ROOT_DIR"])
SUMMARY_JSON = Path(os.environ["SUMMARY_JSON"])

COMPOSE = ROOT / "infra" / "docker-compose.midterm.yml"
ENV_FILE = ROOT / "infra" / "env" / "midterm.env"
REPLAY_CONFIG = ROOT / "modules" / "savant_replay" / "config.midterm.json"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.midterm.yml"
SOURCES_CONFIG = ROOT / "infra" / "generated" / "sources.generated.yml"
SMOKE = ROOT / "scripts" / "smoke" / "current" / "check_midterm_deployment.sh"

EXPECTED_ENV = {
    "MAX_FPS_CONTROL": "true",
    "MAX_FPS": "8/1",
    "MIN_FPS": "2/1",
    "POSE_INFER_INTERVAL": "1",
    "POSE_CONFIDENCE_THRESHOLD": "0.50",
    "POSE_KEYPOINT_THRESHOLD": "0.35",
    "FACE_CONFIDENCE_THRESHOLD": "0.50",
    "WATCHLIST_THRESHOLD": "0.60",
    "EVIDENCE_VERSION": "midterm",
    "EVIDENCE_SCHEMA_VERSION": "2.0-midterm",
    "EVIDENCE_INCLUDE_LEGACY_METADATA_FIELDS": "false",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], *, timeout: int = 30) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "cmd": cmd,
        }
    except Exception as exc:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc), "cmd": cmd}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_yaml_text(text: str) -> tuple[dict[str, Any], str]:
    if yaml is None:
        return {}, "pyyaml_unavailable"
    try:
        data = yaml.safe_load(text) or {}
    except Exception as exc:
        return {}, f"{type(exc).__name__}:{exc}"
    return data if isinstance(data, dict) else {}, ""


def parse_yaml_file(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        return {}, "missing"
    return parse_yaml_text(path.read_text(encoding="utf-8"))


def enabled_rtsp_source_count(path: Path) -> tuple[int, str]:
    doc, error = parse_yaml_file(path)
    if error:
        return 0, error
    sources = doc.get("sources") if isinstance(doc, dict) else {}
    if not isinstance(sources, dict):
        return 0, "sources_not_mapping"
    count = 0
    for source in sources.values():
        if not isinstance(source, dict):
            continue
        uri = str(source.get("uri") or "")
        if (
            source.get("enabled") is True
            and str(source.get("adapter_type") or "") == "gstreamer"
            and uri.startswith(("rtsp://", "rtsps://"))
        ):
            count += 1
    return count, ""


def service_environment(service: dict[str, Any]) -> dict[str, str]:
    raw_env = service.get("environment") or {}
    if isinstance(raw_env, dict):
        return {str(key): str(value) for key, value in raw_env.items()}
    if isinstance(raw_env, list):
        result: dict[str, str] = {}
        for item in raw_env:
            key, sep, value = str(item).partition("=")
            if sep:
                result[key] = value
        return result
    return {}


def int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def configured_max_parallel_streams(compose_stdout: str) -> tuple[int | None, str]:
    doc, error = parse_yaml_text(compose_stdout)
    if error:
        return None, error
    services = doc.get("services") if isinstance(doc, dict) else {}
    if not isinstance(services, dict):
        return None, "services_not_mapping"
    savant = services.get("savant-security")
    if not isinstance(savant, dict):
        return None, "savant_service_missing"
    value = service_environment(savant).get("MAX_PARALLEL_STREAMS")
    parsed = int_or_none(value)
    if parsed is None:
        return None, f"invalid_max_parallel_streams:{value}"
    return parsed, ""


def docker_inspect(container: str) -> dict[str, Any]:
    res = run(["docker", "inspect", container], timeout=15)
    if not res["ok"]:
        return {"ok": False, "error": (res["stderr"] or res["stdout"]).strip()[:500]}
    try:
        data = json.loads(res["stdout"])
        state = (data[0] if data else {}).get("State", {})
        return {
            "ok": True,
            "running": bool(state.get("Running")),
            "status": state.get("Status"),
            "health": (state.get("Health") or {}).get("Status"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    env_values = parse_env(ENV_FILE)
    compose_config = run(["docker", "compose", "-f", str(COMPOSE), "config"], timeout=60)
    enabled_sources, source_count_error = enabled_rtsp_source_count(SOURCES_CONFIG)
    recommended_max_parallel_streams = max(2, enabled_sources * 2)
    configured_streams, configured_streams_error = configured_max_parallel_streams(
        compose_config["stdout"]
    ) if compose_config["ok"] else (None, "compose_config_invalid")
    root_compose_files = sorted(path.name for path in (ROOT / "infra").glob("docker-compose*.yml"))
    env_files = sorted(path.name for path in (ROOT / "infra" / "env").glob("*.env"))
    replay_configs = sorted(path.name for path in (ROOT / "modules" / "savant_replay").glob("config*.json"))
    camera_configs = sorted(
        path.name for path in (ROOT / "modules" / "savant_security" / "config").glob("cameras*.yml")
    )

    containers = {
        name: docker_inspect(f"video-analytics-midterm-{name}")
        for name in (
            "redis",
            "replay-service",
            "savant",
            "source-adapter",
            "event-worker",
            "face-worker",
            "clip-worker",
            "video-file-sink",
            "media-worker",
            "evidence-viewer",
        )
    }

    checks = {
        "compose_exists": COMPOSE.is_file(),
        "env_exists": ENV_FILE.is_file(),
        "replay_config_exists": REPLAY_CONFIG.is_file(),
        "camera_config_exists": CAMERA_CONFIG.is_file(),
        "current_smoke_exists": SMOKE.is_file(),
        "compose_config_valid": compose_config["ok"],
        "root_compose_midterm_only": root_compose_files == ["docker-compose.midterm.yml"],
        "env_midterm_only": env_files == ["midterm.env"],
        "replay_config_midterm_only": replay_configs == ["config.midterm.json"],
        "camera_config_midterm_only": camera_configs == ["cameras.midterm.yml"],
        "env_source_id_filter_absent": "SOURCE_ID" not in env_values,
        "sources_config_readable": source_count_error == "",
        "max_parallel_streams_meets_recommended": (
            configured_streams is not None
            and configured_streams >= recommended_max_parallel_streams
        ),
    }
    checks.update(
        {f"env_{key.lower()}": env_values.get(key) == expected for key, expected in EXPECTED_ENV.items()}
    )

    summary = {
        "schema_version": "midterm.runtime_doctor.v1",
        "generated_at": utc_now(),
        "files": {
            "compose": str(COMPOSE.relative_to(ROOT)),
            "env": str(ENV_FILE.relative_to(ROOT)),
            "replay_config": str(REPLAY_CONFIG.relative_to(ROOT)),
            "camera_config": str(CAMERA_CONFIG.relative_to(ROOT)),
            "sources_config": str(SOURCES_CONFIG.relative_to(ROOT)),
            "current_smoke": str(SMOKE.relative_to(ROOT)),
        },
        "stream_capacity": {
            "enabled_rtsp_source_count": enabled_sources,
            "recommended_max_parallel_streams": recommended_max_parallel_streams,
            "configured_max_parallel_streams": configured_streams,
            "source_count_error": source_count_error,
            "configured_error": configured_streams_error,
            "meets_recommended": (
                configured_streams is not None
                and configured_streams >= recommended_max_parallel_streams
            ),
        },
        "deploy_surface": {
            "root_compose_files": root_compose_files,
            "env_files": env_files,
            "replay_configs": replay_configs,
            "camera_configs": camera_configs,
        },
        "env": {
            "values": {key: env_values.get(key) for key in EXPECTED_ENV},
            "expected": EXPECTED_ENV,
        },
        "compose_config": {
            "ok": compose_config["ok"],
            "error": (compose_config["stderr"] or compose_config["stdout"]).strip()[:1200]
            if not compose_config["ok"]
            else "",
        },
        "containers_optional": containers,
        "checks": checks,
        "ok": all(checks.values()),
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"runtime_doctor_summary={SUMMARY_JSON}")
    print(f"runtime_doctor_ok={summary['ok']}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
PY
