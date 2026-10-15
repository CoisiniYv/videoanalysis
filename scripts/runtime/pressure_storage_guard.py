"""Read-only, fail-closed storage checks for the midterm pressure runner."""

from __future__ import annotations

import math
import os
import posixpath
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


CACHE_TARGETS = ("/media/rolling-cache", "/media/rolling-cache-materialized")


class StoragePreflightError(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any] | None = None):
        super().__init__(message)
        self.report = report if report is not None else {"status": "failed", "errors": [message]}


def _absolute_source(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise StoragePreflightError("cache source must be an absolute host bind path")
    return posixpath.normpath(value)


def _required_cache_targets(service: str) -> tuple[str, ...]:
    return CACHE_TARGETS if service == "media-worker" or service.endswith("-media-worker") else CACHE_TARGETS[:1]


def parse_compose_cache_sources(
    config: Mapping[str, Any], sink_services: Sequence[str]
) -> dict[str, dict[str, str]]:
    """Resolve effective child paths from `compose config --format json`."""
    services = config.get("services") or {}
    result: dict[str, dict[str, str]] = {}
    for service in dict.fromkeys((*sink_services, "media-worker")):
        if service not in services:
            raise StoragePreflightError(f"missing compose service: {service}")
        mounts = services[service].get("volumes") or []
        result[service] = {}
        for target in _required_cache_targets(service):
            candidates: list[tuple[int, Mapping[str, Any]]] = []
            for mount in mounts:
                if not isinstance(mount, dict):
                    raise StoragePreflightError("compose config JSON must contain normalized volume objects")
                destination = str(mount.get("target") or "").rstrip("/") or "/"
                if target == destination or target.startswith(destination.rstrip("/") + "/"):
                    candidates.append((len(destination), mount))
            if not candidates:
                raise StoragePreflightError(f"missing cache mount: {service}:{target}")
            mount = max(candidates, key=lambda item: item[0])[1]
            if mount.get("type") != "bind":
                raise StoragePreflightError(f"cache mount is not a host bind: {service}:{target}")
            destination = str(mount["target"]).rstrip("/") or "/"
            suffix = target[len(destination):].lstrip("/")
            result[service][target] = posixpath.join(_absolute_source(mount.get("source")), suffix).rstrip("/") or "/"
    validate_cache_sources(result)
    return result


def validate_cache_sources(
    sources: Mapping[str, Mapping[str, str]],
    *, expected: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    """Require shared input cache and the materialization reader's output root."""
    if len(sources) < 2 or not any(len(_required_cache_targets(service)) == 2 for service in sources):
        raise StoragePreflightError("missing cache services: require sinks and media-worker")
    normalized: dict[str, dict[str, str]] = {}
    for service, mounts in sources.items():
        normalized[service] = {}
        for target in _required_cache_targets(service):
            if target not in mounts or not mounts[target]:
                raise StoragePreflightError(f"missing cache mount: {service}:{target}")
            normalized[service][target] = _absolute_source(mounts[target])
    for target in CACHE_TARGETS:
        if len({mounts[target] for mounts in normalized.values() if target in mounts}) != 1:
            raise StoragePreflightError(f"inconsistent cache sources for {target}: {normalized}")
    if expected is not None:
        expected_normalized = validate_cache_sources(expected)
        if normalized != expected_normalized:
            raise StoragePreflightError(f"cache sources differ from expected preflight: expected={expected_normalized}, actual={normalized}")
    return normalized


def _unescape_mountinfo(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _filesystem_type(path: str, device: str, mountinfo_text: str) -> str:
    matches = []
    for line in mountinfo_text.splitlines():
        fields = line.split()
        if len(fields) < 10 or fields[2] != device or "-" not in fields:
            continue
        mountpoint = _unescape_mountinfo(fields[4])
        if path == mountpoint or path.startswith(mountpoint.rstrip("/") + "/"):
            separator = fields.index("-")
            if separator + 1 < len(fields):
                matches.append((len(mountpoint), fields[separator + 1]))
    if not matches:
        raise StoragePreflightError(f"unresolved filesystem type for {path} on {device}")
    return max(matches)[1]


def _trace_block_device(node: Path, seen: frozenset[str] = frozenset()) -> dict[str, Any]:
    resolved = node.resolve(strict=True)
    identity = str(resolved)
    if identity in seen:
        raise StoragePreflightError(f"unresolved cyclic block-device chain: {identity}")
    seen = seen | {identity}
    record: dict[str, Any] = {"name": resolved.name, "sysfs_path": identity}
    if (resolved / "partition").exists():
        record["partition"] = True
        record["parents"] = [_trace_block_device(resolved.parent, seen)]
    else:
        slaves = sorted((resolved / "slaves").iterdir()) if (resolved / "slaves").is_dir() else []
        if slaves:
            record["slaves"] = [_trace_block_device(slave, seen) for slave in slaves]
        else:
            if resolved.name.startswith(("dm-", "md")):
                raise StoragePreflightError(f"unresolved backing devices for {resolved.name}")
            rotational = (resolved / "queue/rotational").read_text(encoding="ascii").strip()
            if rotational not in {"0", "1"}:
                raise StoragePreflightError(f"unresolved rotational value for {resolved.name}")
            record["rotational"] = rotational == "1"
            record["transport"] = "nvme" if resolved.name.startswith("nvme") else None
    return record


def _leaf_devices(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    children = node.get("parents") or node.get("slaves")
    if not children:
        return [node]
    return [leaf for child in children for leaf in _leaf_devices(child)]


def inspect_path_storage(
    path: str | Path, *, sys_root: str | Path = "/sys",
    mountinfo_text: str | None = None,
    stat_fn: Callable = os.stat, statvfs_fn: Callable = os.statvfs,
    realpath_fn: Callable = os.path.realpath,
) -> dict[str, Any]:
    """Trace st_dev through sysfs in the helper's namespace; never create paths."""
    requested = _absolute_source(str(path))
    existing = requested
    try:
        while True:
            try:
                stat = stat_fn(existing)
                break
            except FileNotFoundError:
                parent = posixpath.dirname(existing)
                if parent == existing:
                    raise
                existing = parent
        resolved = str(realpath_fn(existing))
        device = f"{os.major(stat.st_dev)}:{os.minor(stat.st_dev)}"
        if mountinfo_text is None:
            mountinfo_text = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        filesystem_type = _filesystem_type(resolved, device, mountinfo_text)
        capacity = statvfs_fn(existing)
        result: dict[str, Any] = {
            "path": requested, "existing_path": existing, "realpath": resolved,
            "device": device, "filesystem_type": filesystem_type,
            "is_tmpfs": filesystem_type == "tmpfs",
            "total_bytes": int(capacity.f_blocks * capacity.f_frsize),
            "available_bytes": int(capacity.f_bavail * capacity.f_frsize),
            "rotational": None, "device_chain": [], "leaf_devices": [],
        }
        if filesystem_type == "tmpfs":
            return result
        chain = _trace_block_device(Path(sys_root) / "dev/block" / device)
        leaves = _leaf_devices(chain)
        result.update(
            rotational=any(leaf["rotational"] for leaf in leaves),
            device_chain=[chain], leaf_devices=sorted({leaf["name"] for leaf in leaves}),
        )
        return result
    except StoragePreflightError:
        raise
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        raise StoragePreflightError(f"unresolved storage for {requested}: {exc}") from exc


def ffprobe_bytes_per_second(probe: Mapping[str, Any]) -> float:
    def positive(value: Any) -> float | None:
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) and parsed > 0 else None
        except (TypeError, ValueError):
            return None
    value = positive((probe.get("format") or {}).get("bit_rate"))
    if value is not None:
        return value / 8.0
    streams = probe.get("streams") or []
    rates = [positive(stream.get("bit_rate")) for stream in streams]
    if rates and all(rate is not None for rate in rates):
        return sum(rates) / 8.0
    raise StoragePreflightError("input bitrate is unavailable in ffprobe output")


def storage_preflight(
    sources: Mapping[str, Mapping[str, str]], *, retention_s: int,
    camera_count: int, bytes_per_second_per_camera: float,
    materialized_estimate_bytes: int, evidence_root: str | Path,
    evidence_estimate_bytes: int, data_root: str | Path = "/data",
    shm_root: str | Path = "/dev/shm", shm_estimate_bytes: int = 0,
    allow_rotational: bool = False, inspector: Callable = inspect_path_storage,
) -> dict[str, Any]:
    """Inspect planned roots and budget concurrent allocations per filesystem."""
    report: dict[str, Any] = {
        "status": "checking", "services": {}, "paths": [], "filesystems": [],
        "errors": [], "warnings": [], "allow_rotational": allow_rotational,
        "retention_seconds": retention_s, "camera_count": camera_count,
        "bytes_per_second_per_camera": None,
        "bitrate_safety_factor": 1.3,
    }
    try:
        normalized = validate_cache_sources(sources)
        report["services"] = normalized
        try:
            bitrate = float(bytes_per_second_per_camera)
        except (TypeError, ValueError) as exc:
            raise StoragePreflightError("input bitrate must be known and positive") from exc
        if not math.isfinite(bitrate) or bitrate <= 0:
            raise StoragePreflightError("input bitrate must be known and positive")
        report["bytes_per_second_per_camera"] = bitrate
        if retention_s <= 0 or camera_count <= 0:
            raise StoragePreflightError("retention and camera count must be positive")
        estimates = (materialized_estimate_bytes, evidence_estimate_bytes, shm_estimate_bytes)
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in estimates):
            raise StoragePreflightError("storage space estimates must be nonnegative integers")
        cache_bytes = math.ceil(retention_s * camera_count * bitrate * 1.3)
        report["estimated_cache_bytes"] = cache_bytes
        report["estimated_materialized_bytes"] = materialized_estimate_bytes
        report["estimated_evidence_bytes"] = evidence_estimate_bytes
        roots = next(mounts for mounts in normalized.values() if CACHE_TARGETS[1] in mounts)
        plans = (
            (roots[CACHE_TARGETS[0]], "rolling_cache", cache_bytes),
            (roots[CACHE_TARGETS[1]], "materialized", materialized_estimate_bytes),
            (str(evidence_root), "evidence", evidence_estimate_bytes),
            (str(data_root), "data", 0), (str(shm_root), "shm", shm_estimate_bytes),
        )
        paths: dict[str, dict[str, Any]] = {}
        filesystems: dict[str, dict[str, Any]] = {}
        for path, role, estimated in plans:
            path = _absolute_source(path)
            if path not in paths:
                record = dict(inspector(path))
                record.update(path=path, roles=[], estimated_additional_bytes=0)
                paths[path] = record
                report["paths"].append(record)
            record = paths[path]
            record["roles"].append(role)
            record["estimated_additional_bytes"] += estimated
            is_tmpfs = record.get("filesystem_type") == "tmpfs"
            if not is_tmpfs and type(record.get("rotational")) is not bool:
                raise StoragePreflightError(f"unresolved rotational storage for {path}")
            if role in {"rolling_cache", "materialized"} and record.get("rotational"):
                message = f"rotational rolling-cache storage: {path}"
                if not allow_rotational:
                    raise StoragePreflightError(message)
                report["warnings"].append(message + " (explicit override)")
            if record.get("total_bytes", 0) <= 0 or record.get("available_bytes", -1) < 0:
                raise StoragePreflightError(f"unresolved storage space for {path}")
            device = str(record.get("device") or "")
            if not device:
                raise StoragePreflightError(f"unresolved filesystem identity for {path}")
            fs = filesystems.setdefault(device, {
                "device": device, "filesystem_type": record["filesystem_type"],
                "total_bytes": record["total_bytes"], "available_bytes": record["available_bytes"],
                "paths": [], "roles": [], "estimated_additional_bytes": 0,
                "minimum_remaining_fraction": 0.0,
            })
            fs["available_bytes"] = min(fs["available_bytes"], record["available_bytes"])
            fs["total_bytes"] = max(fs["total_bytes"], record["total_bytes"])
            if path not in fs["paths"]:
                fs["paths"].append(path)
            fs["roles"].append(role)
            fs["estimated_additional_bytes"] += estimated
            if role in {"rolling_cache", "materialized"} and not is_tmpfs:
                fs["minimum_remaining_fraction"] = 0.2
        report["filesystems"] = list(filesystems.values())
        for fs in filesystems.values():
            remaining = fs["available_bytes"] - fs["estimated_additional_bytes"]
            fs["estimated_remaining_bytes"] = remaining
            fs["estimated_remaining_fraction"] = remaining / fs["total_bytes"]
            if remaining < math.ceil(fs["total_bytes"] * fs["minimum_remaining_fraction"]):
                raise StoragePreflightError(f"insufficient storage space on {fs['device']}: {fs}")
        report["status"] = "passed"
        return report
    except StoragePreflightError as exc:
        report["status"] = "failed"
        report["errors"].append(str(exc))
        raise StoragePreflightError(str(exc), report) from exc
