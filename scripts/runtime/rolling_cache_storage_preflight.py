"""Read-only Compose rendering, bitrate probing, and pressure storage budgeting."""

from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

from scripts.runtime.pressure_storage_guard import (
    CACHE_TARGETS, StoragePreflightError, ffprobe_bytes_per_second,
    inspect_path_storage, parse_compose_cache_sources, storage_preflight,
)


def _public_uri(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path,
                       "redacted" if parsed.query else "", "redacted" if parsed.fragment else ""))


def preflight_failure_report(
    exc: Exception, *, report: dict[str, Any] | None = None, section: str = "setup",
) -> dict[str, Any]:
    """Keep every failure diagnostic serializable without recording command credentials."""
    result = dict(report or {})
    if isinstance(exc, StoragePreflightError):
        result.update(exc.report)
        message = str(exc)
    elif isinstance(exc, subprocess.SubprocessError):
        message = f"{type(exc).__name__} during {section} preflight"
        if isinstance(exc, subprocess.TimeoutExpired):
            result.setdefault(section, {})["timeout_seconds"] = exc.timeout
        elif isinstance(exc, subprocess.CalledProcessError):
            result.setdefault(section, {})["returncode"] = exc.returncode
    else:
        message = f"{type(exc).__name__} during {section} preflight: {exc}"
    message = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"']+", lambda match: _public_uri(match[0]), message)
    result.update(status="failed", reason=message, exception_type=type(exc).__name__)
    errors = result.get("errors")
    result["errors"] = errors if isinstance(errors, list) and errors else [message]
    return result


def _json_result(completed: Any, label: str) -> dict[str, Any]:
    if completed.returncode != 0:
        raise StoragePreflightError(f"{label} failed: rc={completed.returncode}")
    try:
        result = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise StoragePreflightError(f"{label} returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise StoragePreflightError(f"{label} returned a non-object JSON result")
    return result


def pressure_storage_override(cfg: Any, sink_services: Sequence[str]) -> Path | None:
    cache = str(cfg.pressure_rolling_cache_host_root or "").strip()
    materialized = str(cfg.pressure_rolling_cache_materialized_host_root or "").strip()
    if not cache and not materialized:
        return None
    if not cache or not materialized:
        raise StoragePreflightError("pressure rolling-cache and materialized host roots must be set together")
    token = re.sub(r"[^A-Za-z0-9_-]+", "_", str(cfg.run_id or "pressure"))[:120] or "pressure"
    expected = (Path("/dev/shm/video-analytics-pressure") / token).resolve(strict=False)
    roots = [Path(cache).resolve(strict=False), Path(materialized).resolve(strict=False)]
    for root in roots:
        try:
            root.relative_to(expected)
        except ValueError as exc:
            raise StoragePreflightError(f"pressure cache host roots must be run-scoped under {expected}") from exc
    if roots[0] == roots[1]:
        raise StoragePreflightError("pressure cache and materialized roots must be distinct")
    # Compose accepts JSON as YAML; this file contains no environment values.
    overlay = {"services": {
        service: {"volumes": [
            {"type": "bind", "source": str(root), "target": target,
             "bind": {"create_host_path": False}}
            for root, target in zip(roots, CACHE_TARGETS)
        ]}
        for service in (*sink_services, "media-worker")
    }}
    artifact_dir = Path(cfg.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "compose.rolling-cache-pressure-storage.override.json"
    path.write_text(json.dumps(overlay, indent=2) + "\n", encoding="utf-8")
    return path.resolve()


def _config_summary(config: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    summary = {service: {"cache_sources": paths} for service, paths in sources.items()}
    for service, value in (config.get("services") or {}).items():
        if service.startswith(("analysis-forwarder", "replay-raw-fanout")):
            keys = ("FORWARDER_MAX_QUEUE_AGE_MS", "FORWARDER_STRICT_FPS_BUDGET", "FORWARDER_QUEUE_MAX_SIZE")
        elif service in {"savant", "savant-a", "savant-b"}:
            keys = ("FACE_INFER_INTERVAL", "FACE_EMBEDDING_INFER_INTERVAL")
        else:
            continue
        environment = value.get("environment") or {}
        summary.setdefault(service, {})["environment"] = {key: environment[key] for key in keys if key in environment}
    return summary


def rolling_cache_storage_preflight(
    cfg: Any, *, compose_base_args: list[str], run_command: Callable[[list[str]], Any],
    retention_s: int | None = None, inspector: Callable = inspect_path_storage,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a safe-to-persist report and an in-memory full Compose rendering."""
    report: dict[str, Any] = {"status": "checking", "compose": {}, "input": {}, "space_estimates": {}}
    active_section = "compose"
    try:
        if compose_base_args[:2] != ["docker", "compose"]:
            raise StoragePreflightError("compose preflight requires a docker compose base command")
        sink_services = ["rolling-cache-sink-a", "rolling-cache-sink-b"] if cfg.dual_shard_same_gpu else ["rolling-cache-sink"]
        command = [str(value) for value in compose_base_args]
        overlay = next((command[index + 1] for index, value in enumerate(command[:-1])
                        if value == "-f" and command[index + 1].endswith(
                            "compose.rolling-cache-pressure-storage.override.json")), None)
        command.extend(("config", "--format", "json"))
        report["compose"] = {
            "command": command,
            "files": [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-f"],
            "pressure_tmpfs_override": overlay,
        }
        completed = run_command(command)
        report["compose"]["returncode"] = completed.returncode
        rendered = _json_result(completed, "docker compose config")
        sources = parse_compose_cache_sources(rendered, sink_services)
        report["compose"]["services"] = _config_summary(rendered, sources)
        input_uri = str(cfg.rtsp_republish_input_uri or cfg.rtsp_uri or "").strip()
        if not input_uri:
            raise StoragePreflightError("input bitrate source is unavailable")
        probe_command = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", input_uri]
        report["input"] = {"uri": _public_uri(input_uri), "command": [*probe_command[:-1], _public_uri(input_uri)]}
        active_section = "input"
        completed = run_command(probe_command)
        report["input"]["returncode"] = completed.returncode
        probe = _json_result(completed, "ffprobe bitrate probe")
        byte_rate = ffprobe_bytes_per_second(probe)
        report["input"]["bytes_per_second_per_camera"] = byte_rate
        try:
            format_rate = float((probe.get("format") or {}).get("bit_rate"))
        except (TypeError, ValueError):
            format_rate = 0.0
        report["input"]["bitrate_source"] = "format.bit_rate" if math.isfinite(format_rate) and format_rate > 0 else "sum(streams[].bit_rate)"
        retention = retention_s if retention_s is not None else (
            int(cfg.pressure_rolling_cache_retention_s) if cfg.pressure_rolling_cache_retention_s > 0
            else max(900, int(cfg.duration_s) + int(cfg.drain_s) + 120)
        )
        max_window = max(int(pre) + int(post) for pre, post in cfg.evidence_policy_groups)
        concurrency = int(cfg.media_worker_materialization_max_active) + int(cfg.media_worker_finalizer_queue_capacity)
        if concurrency <= 0:
            raise StoragePreflightError("bounded materialization concurrency is required for storage space projection")
        cooldown = int(cfg.pressure_algorithm_cooldown_s)
        if cooldown <= 0:
            raise StoragePreflightError("positive algorithm cooldown is required for storage space projection")
        behavior_rules = getattr(cfg, "behavior_rules", ()) or ()
        if isinstance(behavior_rules, str):
            behavior_rules = [rule.strip() for rule in behavior_rules.split(",") if rule.strip()]
        event_type_count = 2 + len(set(behavior_rules))
        materialized = math.ceil(max_window * byte_rate * concurrency * 1.3)
        evidence = math.ceil(int(cfg.duration_s) * int(cfg.stream_count) * event_type_count / cooldown * max_window * byte_rate * 1.3)
        report["space_estimates"] = {
            "materialized": {
                "formula": "max_pre_plus_post_seconds * bytes_per_second_per_camera * (max_active + finalizer_queue_capacity) * 1.3",
                "max_pre_plus_post_seconds": max_window, "concurrency": concurrency,
                "estimated_bytes": materialized,
            },
            "evidence": {
                "formula": "duration_seconds * camera_count * (2 + behavior_rule_count) / cooldown_seconds * max_pre_plus_post_seconds * bytes_per_second_per_camera * 1.3",
                "duration_seconds": int(cfg.duration_s), "event_type_count": event_type_count,
                "cooldown_seconds": cooldown, "estimated_bytes": evidence,
            },
        }
        storage = storage_preflight(
            sources, retention_s=retention, camera_count=int(cfg.stream_count),
            bytes_per_second_per_camera=byte_rate, materialized_estimate_bytes=materialized,
            evidence_root=cfg.evidence_root, evidence_estimate_bytes=evidence,
            allow_rotational=bool(getattr(cfg, "allow_rotational_rolling_cache", False)),
            inspector=inspector,
        )
        report.update(storage)
        report.update(reason="passed", errors=[])
        return report, rendered
    except Exception as exc:
        report = preflight_failure_report(exc, report=report, section=active_section)
        raise StoragePreflightError(report["reason"], report) from None
