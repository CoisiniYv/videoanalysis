#!/usr/bin/env bash
# C1H.0 official runtime doctor.
#
# Read-only diagnostics. This script does not start, stop, rebuild, restart, or
# reconfigure any service.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SUMMARY_JSON="${SUMMARY_JSON:-/data/video-analytics/artifacts/c1h0/runtime_doctor_summary.json}"

mkdir -p "$(dirname "$SUMMARY_JSON")"

ROOT_DIR="$ROOT_DIR" SUMMARY_JSON="$SUMMARY_JSON" python3 - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(os.environ["ROOT_DIR"])
SUMMARY_JSON = Path(os.environ["SUMMARY_JSON"])

OFFICIAL_COMPOSE = ROOT / "infra" / "docker-compose.c1-official-replay-dev.yml"
OFFICIAL_ENV = ROOT / "infra" / "env" / "c1-official-replay-dev.env"
OFFICIAL_MODULE = ROOT / "modules" / "savant_security" / "module.yml"
EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")

EXPECTED_ENV = {
    "FACE_OBSERVATION_EXPORT_ENABLED": "true",
    "FACE_REID_MIN_INTERVAL_MS": "1000",
    "RECORDING_EVENT_TYPES": "watchlist_hit,intrusion",
    "RECORDING_MAX_REQUESTS_PER_RUN": "100",
    "RECORDING_COOLDOWN_SECONDS": "30",
    "CLIP_WORKER_MAX_JOBS_PER_RUN": "100",
    "CLIP_WORKER_RUN_ONCE": "false",
    "CLIP_WORKER_MAX_CONCURRENT_JOBS": "1",
    "CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS": "30",
    "WATCHLIST_MATCH_ENABLED": "true",
    "EVIDENCE_MAX_DURATION_SLACK_SEC": "10",
    "RTSP_TRANSPORT": "tcp",
    "SAVANT_MODULE_FILE": "module.yml",
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


def preview(text: str, limit: int = 1200) -> str:
    text = text.strip()
    return text[:limit]


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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


def http_check(url: str) -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, method="GET")
    try:
        with opener.open(req, timeout=5) as resp:
            body = resp.read(500).decode("utf-8", errors="replace")
            return {"ok": 200 <= resp.status < 400, "status": resp.status, "body_preview": body}
    except urllib.error.HTTPError as exc:
        body = exc.read(500).decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "body_preview": body}
    except Exception as exc:
        return {"ok": False, "status": None, "error": str(exc)}


def docker_inspect(container: str) -> dict[str, Any]:
    res = run(["docker", "inspect", container], timeout=20)
    if not res["ok"]:
        return {"ok": False, "error": preview(res["stderr"] or res["stdout"])}
    try:
        data = json.loads(res["stdout"])
        return {"ok": True, "data": data[0] if data else {}}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "raw": preview(res["stdout"])}


def env_list_to_dict(items: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            out[key] = value
    return out


def docker_exec(container: str, cmd: str, *, timeout: int = 20) -> dict[str, Any]:
    return run(["docker", "exec", container, "sh", "-lc", cmd], timeout=timeout)


def docker_logs(container: str, *, since: str = "24h", timeout: int = 20) -> dict[str, Any]:
    return run(["docker", "logs", "--since", since, container], timeout=timeout)


def redis_xlen(stream: str) -> dict[str, Any]:
    if shutil.which("redis-cli") is not None:
        res = run(["redis-cli", "-h", "127.0.0.1", "-p", "6385", "XLEN", stream], timeout=10)
        if res["ok"]:
            raw = res["stdout"].strip()
            try:
                return {"ok": True, "length": int(raw), "method": "localhost:6385"}
            except ValueError:
                return {"ok": False, "raw": raw, "method": "localhost:6385"}
    res = docker_exec("c1-official-redis", f"redis-cli XLEN {stream}", timeout=10)
    if not res["ok"]:
        return {"ok": False, "error": preview(res["stderr"] or res["stdout"])}
    raw = res["stdout"].strip()
    try:
        return {"ok": True, "length": int(raw), "method": "docker_exec"}
    except ValueError:
        return {"ok": False, "raw": raw}


def psql_json(query: str) -> dict[str, Any]:
    if shutil.which("psql") is not None:
        res = run(
            [
                "psql",
                "postgresql://video:video@127.0.0.1:5438/video_analytics",
                "-t",
                "-A",
                "-c",
                query,
            ],
            timeout=20,
        )
        if res["ok"]:
            raw = res["stdout"].strip()
            if not raw:
                return {"ok": True, "data": None, "method": "localhost:5438"}
            try:
                return {"ok": True, "data": json.loads(raw), "method": "localhost:5438"}
            except json.JSONDecodeError:
                return {"ok": False, "raw": raw, "method": "localhost:5438"}
    res = run(
        [
            "docker",
            "exec",
            "c1-official-postgres",
            "psql",
            "-U",
            "video",
            "-d",
            "video_analytics",
            "-t",
            "-A",
            "-c",
            query,
        ],
        timeout=20,
    )
    if not res["ok"]:
        return {"ok": False, "error": preview(res["stderr"] or res["stdout"])}
    raw = res["stdout"].strip()
    if not raw:
        return {"ok": True, "data": None, "method": "docker_exec"}
    try:
        return {"ok": True, "data": json.loads(raw), "method": "docker_exec"}
    except json.JSONDecodeError:
        return {"ok": False, "raw": raw}


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        return {"_error": str(exc)}


def first_jsonl(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if raw:
                    data = json.loads(raw)
                    return data if isinstance(data, dict) else {}
    except Exception as exc:
        return {"_error": str(exc)}
    return {}


def raw_clip(bundle: Path, metadata: dict[str, Any]) -> Path | None:
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    raw_path = media.get("raw_clip_path") if isinstance(media, dict) else None
    if isinstance(raw_path, str) and raw_path:
        candidate = bundle / Path(raw_path).name
        if candidate.is_file():
            return candidate
    for suffix in ("mp4", "mov", "mkv", "webm"):
        candidate = bundle / f"raw_clip.{suffix}"
        if candidate.is_file():
            return candidate
    for candidate in sorted(bundle.glob("raw_clip.*")):
        if candidate.is_file():
            return candidate
    return None


def ffprobe_duration(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"ok": False, "reason": "missing_raw_clip"}
    if shutil.which("ffprobe") is None:
        return {"ok": False, "reason": "ffprobe_not_available", "raw_clip": str(path)}
    res = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=20,
    )
    if not res["ok"]:
        return {"ok": False, "reason": "ffprobe_failed", "raw_clip": str(path), "error": preview(res["stderr"] or res["stdout"])}
    try:
        return {"ok": True, "raw_clip": str(path), "duration_seconds": float(res["stdout"].strip())}
    except ValueError:
        return {"ok": False, "reason": "ffprobe_unparseable", "raw_clip": str(path), "raw": res["stdout"].strip()}


def evidence_bundle_status() -> list[dict[str, Any]]:
    if not EVIDENCE_ROOT.is_dir():
        return []
    bundles = [p for p in EVIDENCE_ROOT.iterdir() if p.is_dir()]
    bundles.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for bundle in bundles[:5]:
        metadata = load_json(bundle / "metadata.json")
        summary = load_json(bundle / "summary.json")
        annotation = first_jsonl(bundle / "annotations.jsonl")
        annotations_file = bundle / "annotations.jsonl"
        event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
        annotations_meta = metadata.get("annotations") if isinstance(metadata.get("annotations"), dict) else {}
        media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
        clip_validation = media.get("clip_validation") if isinstance(media.get("clip_validation"), dict) else {}
        clip = raw_clip(bundle, metadata)
        duration = ffprobe_duration(clip)
        out.append(
            {
                "bundle_id": bundle.name,
                "mtime": datetime.fromtimestamp(bundle.stat().st_mtime, timezone.utc).isoformat(),
                "event_type": summary.get("event_type") or event.get("event_type") or annotation.get("event_type"),
                "annotation_status": annotation.get("annotation_status") or annotations_meta.get("annotation_status") or summary.get("annotation_status"),
                "overlay_available": annotation.get("overlay_available") or annotations_meta.get("overlay_available") or summary.get("overlay_available"),
                "duration_guard_failed": clip_validation.get("duration_guard_failed") or summary.get("duration_guard_failed"),
                "annotations_jsonl_size": annotations_file.stat().st_size if annotations_file.is_file() else None,
                "raw_clip_duration": duration,
            }
        )
    return out


def main() -> int:
    env_values = parse_env(OFFICIAL_ENV)
    module_text = OFFICIAL_MODULE.read_text(encoding="utf-8") if OFFICIAL_MODULE.is_file() else ""
    root_compose_files = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "infra").glob("docker-compose*.yml"))

    compose_config = run(["docker", "compose", "-f", str(OFFICIAL_COMPOSE), "config"], timeout=60)
    compose_data: dict[str, Any] = {}
    if compose_config["ok"]:
        try:
            loaded = yaml.safe_load(compose_config["stdout"])
            compose_data = loaded if isinstance(loaded, dict) else {}
        except Exception:
            compose_data = {}
    compose_savant = (
        compose_data.get("services", {}).get("savant-security", {})
        if isinstance(compose_data.get("services"), dict)
        else {}
    )
    compose_clip = (
        compose_data.get("services", {}).get("clip-worker", {})
        if isinstance(compose_data.get("services"), dict)
        else {}
    )
    savant_inspect = docker_inspect("c1-official-savant")
    savant_env = {}
    if savant_inspect.get("ok"):
        savant_env = env_list_to_dict(savant_inspect["data"].get("Config", {}).get("Env"))
    clip_inspect = docker_inspect("c1-official-clip-worker")
    clip_env = {}
    clip_started_at = None
    if clip_inspect.get("ok"):
        clip_env = env_list_to_dict(clip_inspect["data"].get("Config", {}).get("Env"))
        state = clip_inspect["data"].get("State", {})
        clip_started_at = state.get("StartedAt") if isinstance(state, dict) else None
    clip_logs = docker_logs("c1-official-clip-worker", since="24h", timeout=20)
    clip_log_text = (clip_logs.get("stdout") or "") + "\n" + (clip_logs.get("stderr") or "")
    clip_max_jobs_reached_count = clip_log_text.count("max_jobs_reached")
    clip_replay_job_created_count = clip_log_text.count("replay_job_created")
    container_module_sha = docker_exec(
        "c1-official-savant",
        "sha256sum /opt/savant/src/module/module.yml 2>/dev/null | awk '{print $1}'",
        timeout=20,
    )

    db = {
        "active_cameras": psql_json(
            "SELECT COALESCE(jsonb_agg(jsonb_build_object('id', id, 'source_id', source_id, 'enabled', enabled) ORDER BY id), '[]'::jsonb) FROM cameras WHERE enabled = true;"
        ),
        "active_camera_rules": psql_json(
            "SELECT COALESCE(jsonb_agg(jsonb_build_object('camera_id', camera_id, 'rule_id', rule_id, 'algorithm_id', algorithm_id, 'enabled', enabled) ORDER BY camera_id, rule_id), '[]'::jsonb) FROM camera_rules WHERE enabled = true;"
        ),
    }

    summary: dict[str, Any] = {
        "schema_version": "c1h0.runtime_doctor.v1",
        "generated_at": utc_now(),
        "git": {
            "head": preview(run(["git", "rev-parse", "HEAD"], timeout=10)["stdout"]),
            "status_short": run(["git", "status", "--short"], timeout=20)["stdout"].splitlines(),
        },
        "mainline": {
            "official_compose": str(OFFICIAL_COMPOSE.relative_to(ROOT)),
            "official_compose_exists": OFFICIAL_COMPOSE.is_file(),
            "official_compose_config_valid": compose_config["ok"],
            "official_compose_config_error": preview(compose_config["stderr"] or compose_config["stdout"]) if not compose_config["ok"] else None,
            "root_compose_files": root_compose_files,
            "only_one_active_root_compose": root_compose_files == ["infra/docker-compose.c1-official-replay-dev.yml"],
            "official_module": str(OFFICIAL_MODULE.relative_to(ROOT)),
            "official_module_exists": OFFICIAL_MODULE.is_file(),
        },
        "env_file": {
            "path": str(OFFICIAL_ENV.relative_to(ROOT)),
            "exists": OFFICIAL_ENV.is_file(),
            "values": {key: env_values.get(key) for key in EXPECTED_ENV},
            "expected": EXPECTED_ENV,
            "matches_expected": {key: env_values.get(key) == value for key, value in EXPECTED_ENV.items()},
        },
        "endpoints": {
            "api_health": http_check("http://localhost:8000/health"),
            "operator": http_check("http://localhost:8000/operator"),
            "evidence_viewer_health": http_check("http://localhost:8090/health"),
        },
        "savant_security": {
            "container_inspect_ok": savant_inspect.get("ok", False),
            "inspect_error": savant_inspect.get("error"),
            "env": {
                key: savant_env.get(key)
                for key in (
                    "SAVANT_MODULE_FILE",
                    "FACE_OBSERVATION_EXPORT_ENABLED",
                    "CAMERAS_CONFIG_PATH",
                    "SOURCE_ID",
                    "REDIS_URL",
                    "EVENT_STREAM",
                )
            },
            "compose_env": {
                key: compose_savant.get("environment", {}).get(key)
                for key in (
                    "SAVANT_MODULE_FILE",
                    "FACE_OBSERVATION_EXPORT_ENABLED",
                    "CAMERAS_CONFIG_PATH",
                    "SOURCE_ID",
                    "REDIS_URL",
                    "EVENT_STREAM",
                )
            },
            "module_yml_host_sha256": file_sha256(OFFICIAL_MODULE),
            "module_yml_container_sha256": preview(container_module_sha["stdout"]) if container_module_sha["ok"] else None,
            "module_yml_container_sha_error": preview(container_module_sha["stderr"] or container_module_sha["stdout"]) if not container_module_sha["ok"] else None,
            "module_contains": {
                "yolov8_face": "yolov8_face" in module_text,
                "adaface": "adaface" in module_text,
                "face_observation_exporter": "face_observation_exporter" in module_text,
                "behavior_rules": "behavior_rules" in module_text,
            },
        },
        "clip_worker": {
            "container_inspect_ok": clip_inspect.get("ok", False),
            "inspect_error": clip_inspect.get("error"),
            "started_at": clip_started_at,
            "env": {
                key: clip_env.get(key)
                for key in (
                    "CLIP_WORKER_MAX_JOBS_PER_RUN",
                    "CLIP_WORKER_RUN_ONCE",
                    "CLIP_WORKER_MAX_CONCURRENT_JOBS",
                    "CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS",
                    "RECORD_REQUEST_STREAM",
                )
            },
            "compose_env": {
                key: compose_clip.get("environment", {}).get(key)
                for key in (
                    "CLIP_WORKER_MAX_JOBS_PER_RUN",
                    "CLIP_WORKER_RUN_ONCE",
                    "CLIP_WORKER_MAX_CONCURRENT_JOBS",
                    "CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS",
                    "RECORD_REQUEST_STREAM",
                )
            },
            "diagnostics": {
                "logs_since": "24h",
                "logs_ok": clip_logs.get("ok", False),
                "max_jobs_reached_log_count": clip_max_jobs_reached_count,
                "replay_job_created_log_count": clip_replay_job_created_count,
                "max_jobs_exhausted_suspected": (
                    clip_max_jobs_reached_count > 0
                    and clip_replay_job_created_count == 0
                    and clip_env.get("CLIP_WORKER_RUN_ONCE", "false").lower() != "true"
                ),
            },
        },
        "redis": {
            stream: redis_xlen(stream)
            for stream in (
                "security.face_observations",
                "security.person_observations",
                "security.events",
                "security.alerts",
                "security.record_requests",
            )
        },
        "db": db,
        "evidence": {
            "root": str(EVIDENCE_ROOT),
            "latest_5_bundle_status": evidence_bundle_status(),
        },
    }

    critical_failures = []
    if not summary["mainline"]["official_compose_exists"]:
        critical_failures.append("official_compose_missing")
    if not summary["mainline"]["official_compose_config_valid"]:
        critical_failures.append("official_compose_config_invalid")
    if not summary["mainline"]["only_one_active_root_compose"]:
        critical_failures.append("multiple_root_compose_files")
    if not summary["env_file"]["exists"]:
        critical_failures.append("env_file_missing")
    if not summary["env_file"]["matches_expected"].get("FACE_OBSERVATION_EXPORT_ENABLED"):
        critical_failures.append("face_observation_export_not_enabled_by_env_file")
    for key, ok in summary["savant_security"]["module_contains"].items():
        if not ok:
            critical_failures.append(f"module_missing_{key}")
    summary["overall"] = {
        "status": "fail" if critical_failures else "ok",
        "critical_failures": critical_failures,
        "note": "Endpoint, Redis, DB, and evidence checks are diagnostic and may be unavailable if the runtime is stopped.",
    }

    SUMMARY_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"runtime_doctor_summary={SUMMARY_JSON}")
    print(f"overall_status={summary['overall']['status']}")
    if critical_failures:
        print("critical_failures=" + ",".join(critical_failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY
