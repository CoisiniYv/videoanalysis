#!/usr/bin/env python3
"""Parse Savant video-file-sink pressure logs."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


NEW_WRITER_RE = re.compile(
    r"New writer for source=.*?amount of resident writers is\s+(?P<count>\d+)",
    re.IGNORECASE,
)
RESIDENT_WRITER_RE = re.compile(
    r"\b(?:resident[_\s-]*writers?|resident_writer_count)\s*[=:]\s*(?P<count>\d+)",
    re.IGNORECASE,
)
PENDING_RECLAIM_RE = re.compile(
    r"\b(?:pending[_\s-]*(?:reclaim|writers?)|pending_reclaim_count)\s*[=:]\s*(?P<count>\d+)",
    re.IGNORECASE,
)
PIPELINE_OPERATION_RE = re.compile(
    r"Operation took\s+(?P<duration>[0-9:.]+)",
    re.IGNORECASE,
)


def _percentile(sorted_values: list[float], quantile: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _duration_to_ms(value: str) -> float | None:
    text = value.strip().rstrip(".")
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return ((hours * 3600) + (minutes * 60) + seconds) * 1000.0
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return ((minutes * 60) + seconds) * 1000.0
        return float(parts[0]) * 1000.0
    except ValueError:
        return None


def _duration_distribution(values: list[float]) -> dict[str, Any]:
    sorted_values = sorted(values)
    return {
        "count": len(sorted_values),
        "p50": _percentile(sorted_values, 0.50),
        "p95": _percentile(sorted_values, 0.95),
        "p99": _percentile(sorted_values, 0.99),
        "max": sorted_values[-1] if sorted_values else None,
    }


def _is_gst_error(line: str) -> bool:
    upper = line.upper()
    return (
        "GSTREAMER-ERROR" in upper
        or "GSTREAMER-CRITICAL" in upper
        or "GST-ERROR" in upper
        or "GST-CRITICAL" in upper
        or " ERROR " in f" {upper} "
        or upper.startswith("ERROR ")
        or "RUNTIMEERROR" in upper
    )


def _is_gst_warning(line: str) -> bool:
    upper = line.upper()
    if _is_gst_error(line):
        return False
    return (
        "GSTREAMER-WARNING" in upper
        or "GST-WARNING" in upper
        or " WARNING " in f" {upper} "
        or upper.startswith("WARNING ")
        or " WARN " in f" {upper} "
        or upper.startswith("WARN ")
    )


def parse_video_file_sink_log(text: str, *, sink_instance: str) -> dict[str, Any]:
    """Return pressure metrics from one video-file-sink log stream."""
    new_writer_count = 0
    resident_writer_max = 0
    pending_reclaim_max = 0
    operation_durations_ms: list[float] = []
    eos_count = 0
    gst_error_count = 0
    gst_warning_count = 0

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        writer_match = NEW_WRITER_RE.search(line)
        if writer_match:
            new_writer_count += 1
            resident_writer_max = max(
                resident_writer_max,
                int(writer_match.group("count")),
            )
        else:
            resident_match = RESIDENT_WRITER_RE.search(line)
            if resident_match:
                resident_writer_max = max(
                    resident_writer_max,
                    int(resident_match.group("count")),
                )

        pending_match = PENDING_RECLAIM_RE.search(line)
        if pending_match:
            pending_reclaim_max = max(
                pending_reclaim_max,
                int(pending_match.group("count")),
            )

        operation_match = PIPELINE_OPERATION_RE.search(line)
        if operation_match:
            duration_ms = _duration_to_ms(operation_match.group("duration"))
            if duration_ms is not None:
                operation_durations_ms.append(duration_ms)

        if "Received EOS from source" in line or "EOS from source" in line:
            eos_count += 1
        if _is_gst_error(line):
            gst_error_count += 1
        elif _is_gst_warning(line):
            gst_warning_count += 1

    distribution = _duration_distribution(operation_durations_ms)
    return {
        "sink_instance": sink_instance,
        "new_writer_count": new_writer_count,
        "resident_writer_max": resident_writer_max,
        "pending_reclaim_max": pending_reclaim_max,
        "pipeline_operation_count": distribution["count"],
        "pipeline_operation_duration_ms_p50": distribution["p50"],
        "pipeline_operation_duration_ms_p95": distribution["p95"],
        "pipeline_operation_duration_ms_p99": distribution["p99"],
        "pipeline_operation_duration_ms_max": distribution["max"],
        "eos_count": eos_count,
        "gst_error_count": gst_error_count,
        "gst_warning_count": gst_warning_count,
    }


def aggregate_video_file_sink_metrics(
    instances: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate already parsed per-sink metrics."""
    operation_counts = [
        int(item.get("pipeline_operation_count") or 0)
        for item in instances.values()
    ]
    return {
        "sink_instance_count": len(instances),
        "new_writer_count": sum(
            int(item.get("new_writer_count") or 0) for item in instances.values()
        ),
        "resident_writer_max": max(
            [int(item.get("resident_writer_max") or 0) for item in instances.values()]
            or [0]
        ),
        "pending_reclaim_max": max(
            [int(item.get("pending_reclaim_max") or 0) for item in instances.values()]
            or [0]
        ),
        "pipeline_operation_count": sum(operation_counts),
        "eos_count": sum(int(item.get("eos_count") or 0) for item in instances.values()),
        "gst_error_count": sum(
            int(item.get("gst_error_count") or 0) for item in instances.values()
        ),
        "gst_warning_count": sum(
            int(item.get("gst_warning_count") or 0) for item in instances.values()
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_path", type=Path)
    parser.add_argument("--sink-instance", default="")
    args = parser.parse_args(argv)
    sink_instance = args.sink_instance or args.log_path.stem
    metrics = parse_video_file_sink_log(
        args.log_path.read_text(encoding="utf-8", errors="replace"),
        sink_instance=sink_instance,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
