"""Non-blocking provenance warnings for an already contaminated runtime."""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence


def is_pressure_source(value: str) -> bool:
    return value.startswith((
        "pressure60",
        "pressure40",
        "pressure_",
        "forwarder60_null_",
        "rc",
    ))


def is_pressure_camera(camera: Mapping[str, Any]) -> bool:
    return camera.get("site_id") == "pressure" or bool(
        re.fullmatch(r"pressure-\d+", str(camera.get("location") or ""))
    )


def baseline_pollution(
    *, environments: Mapping[str, Mapping[str, str]], retention_limits: Mapping[str, int],
    cameras: Sequence[Mapping[str, Any]] | None,
    rules: Sequence[Mapping[str, Any]] | None, expected_source_count: int | None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    unverified: list[str] = []
    pressure_sources = {
        str(row["source_id"]) for rows in (cameras, rules) for row in rows or []
        if row.get("source_id") and is_pressure_camera(row)
    }
    for service, environment in environments.items():
        if not environment:
            unverified.append(f"{service}.environment")
            continue
        raw = environment.get("ROLLING_CACHE_SOURCES", "")
        try:
            sources = json.loads(raw) if raw.lstrip().startswith("[") else raw.split(",")
        except (ValueError, TypeError):
            sources = raw.split(",")
        historical = sorted(
            str(value).strip() for value in sources
            if is_pressure_source(str(value).strip()) or str(value).strip() in pressure_sources
        )
        if historical:
            findings.append({"code": "historical_pressure_sources", "service": service,
                             "sources": historical})
        value = environment.get("ROLLING_CACHE_RETENTION_SECONDS")
        if value is not None:
            try:
                actual = int(value)
                if service not in retention_limits:
                    unverified.append(f"{service}.formal_retention")
                elif actual > retention_limits[service]:
                    findings.append({"code": "retention_above_formal", "service": service,
                                     "actual_seconds": actual,
                                     "formal_seconds": retention_limits[service]})
            except (ValueError, TypeError):
                unverified.append(f"{service}.retention")
    actual_count = None if cameras is None else sum(bool(x.get("enabled")) for x in cameras)
    if cameras is None:
        unverified.append("camera_population")
    if rules is None:
        unverified.append("camera_rules")
    else:
        residual = [dict(row) for row in rules if row.get("enabled") and (
            is_pressure_camera(row) or str(row.get("source_id") or "") in pressure_sources
            or is_pressure_source(str(row.get("source_id") or ""))
        )]
        if residual:
            findings.append({"code": "enabled_pressure_rules", "rules": residual})
    if expected_source_count is None:
        unverified.append("runtime_source_count")
    elif actual_count is not None and actual_count != expected_source_count:
        findings.append({"code": "runtime_camera_count_mismatch",
                         "expected": expected_source_count, "actual": actual_count})
    return {"severity": "red" if findings else "none", "blocking": False,
            "findings": findings, "unverified": unverified,
            "expected_runtime_source_count": expected_source_count,
            "actual_enabled_source_count": actual_count}
