#!/usr/bin/env python3
"""Analyze a midterm pressure artifact for evidence pipeline bottlenecks."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


TIMING_LINE_RE = re.compile(r"\bclip_worker_phase_timing\b")
KEY_VALUE_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S+)")
COUNT_MARKERS = (
    "max_concurrent_reached",
    "max_concurrent_per_source_reached",
    "max_concurrent_per_shard_reached",
    "clip_worker_queued",
    "replay_slot_terminal_state",
)
LATENCY_FIELDS = (
    "record_request_pending_ms",
    "proof_wait_ms",
    "replay_job_create_ms",
    "replay_slot_hold_ms",
    "replay_duration_seconds_effective",
)
MEDIA_LATENCY_FIELDS = (
    "queue_wait_ms",
    "replay_to_sink_metadata_ms",
    "sink_video_to_stable_ms",
    "finalizer_pool_wait_ms",
    "sink_ffprobe_ready_to_finalizer_start_ms",
)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


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


def _numeric_distribution(values: list[float] | list[int]) -> dict[str, Any]:
    sorted_values = sorted(float(value) for value in values if value is not None)
    if not sorted_values:
        return {"status": "not_enough_data", "count": 0}
    return {
        "status": "measured",
        "count": len(sorted_values),
        "min": sorted_values[0],
        "p50": _percentile(sorted_values, 0.50),
        "p95": _percentile(sorted_values, 0.95),
        "p99": _percentile(sorted_values, 0.99),
        "max": sorted_values[-1],
    }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _safe_float(value: Any) -> float | None:
    if _is_number(value):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _nested_get(item: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = item
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _metric_from_evidence(evidence: dict[str, Any], field: str) -> float | None:
    paths = (
        ("payload", "evidence_diagnostics", "phase_latency_ms", field),
        ("payload", "evidence_diagnostics", field),
        ("payload", "materialization_metrics", "phase_latency_ms", field),
        ("payload", "materialization_metrics", field),
        ("payload", "media", "evidence_diagnostics", "phase_latency_ms", field),
        ("payload", "media", "evidence_diagnostics", field),
    )
    for path in paths:
        value = _safe_float(_nested_get(evidence, path))
        if value is not None:
            return value
    return None


def _source_id_from_evidence(evidence: dict[str, Any]) -> str:
    for path in (
        ("source_id",),
        ("payload", "media", "source_id"),
        ("payload", "source_id"),
    ):
        value = _nested_get(evidence, path)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _shard_id_from_evidence(evidence: dict[str, Any]) -> str:
    for path in (
        ("payload", "media", "replay_shard_id"),
        ("payload", "evidence_diagnostics", "replay_shard", "shard_id"),
        ("payload", "media", "evidence_diagnostics", "replay_shard", "shard_id"),
    ):
        value = _nested_get(evidence, path)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _distribution_by_key(
    items: list[dict[str, Any]],
    field: str,
    key_fn,
) -> dict[str, dict[str, Any]]:
    values: dict[str, list[float]] = defaultdict(list)
    for item in items:
        value = _metric_from_evidence(item, field)
        if value is not None:
            values[key_fn(item)].append(value)
    return {key: _numeric_distribution(item_values) for key, item_values in sorted(values.items())}


def _extract_log_metrics(text: str) -> dict[str, Any]:
    counts = Counter()
    latency_values: dict[str, list[float]] = {field: [] for field in LATENCY_FIELDS}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for marker in COUNT_MARKERS:
            if marker in line:
                counts[marker] += 1
        if not TIMING_LINE_RE.search(line):
            continue
        for match in KEY_VALUE_RE.finditer(line):
            key = match.group("key")
            if key not in latency_values:
                continue
            value = _safe_float(match.group("value"))
            if value is not None:
                latency_values[key].append(value)

    return {
        "counts": {marker: counts.get(marker, 0) for marker in COUNT_MARKERS},
        "phase_timing": {
            field: _numeric_distribution(values)
            for field, values in latency_values.items()
        },
    }


def _measured_count(distribution: dict[str, Any]) -> int:
    return int(distribution.get("count") or 0)


def _distribution_value(distribution: dict[str, Any], key: str) -> float | None:
    value = distribution.get(key)
    return float(value) if _is_number(value) else None


def _source_tail_summary(
    by_source: dict[str, dict[str, Any]],
    *,
    threshold_ms: float,
) -> dict[str, Any]:
    measured = {
        source_id: metrics
        for source_id, metrics in by_source.items()
        if _measured_count(metrics) > 0
    }
    tail_sources = {
        source_id: metrics
        for source_id, metrics in measured.items()
        if (_distribution_value(metrics, "max") or 0.0) >= threshold_ms
    }
    top_sources = sorted(
        measured.items(),
        key=lambda item: (
            _distribution_value(item[1], "max") or 0.0,
            _distribution_value(item[1], "p95") or 0.0,
        ),
        reverse=True,
    )[:10]
    return {
        "threshold_ms": threshold_ms,
        "measured_source_count": len(measured),
        "tail_source_count": len(tail_sources),
        "tail_source_fraction": (
            len(tail_sources) / len(measured) if measured else 0.0
        ),
        "top_sources": [
            {"source_id": source_id, "metrics": metrics}
            for source_id, metrics in top_sources
        ],
    }


def _classify(summary: dict[str, Any]) -> dict[str, Any]:
    record_overall = summary["clip_worker"]["record_request_pending_ms"]["overall"]
    media_queue = summary["media_worker"]["queue_wait_ms"]["overall"]
    media_tail = summary["media_worker"]["queue_wait_ms"]["source_tail"]
    shard_metrics = summary["media_worker"]["queue_wait_ms"]["by_shard"]

    record_p95 = _distribution_value(record_overall, "p95") or 0.0
    media_queue_p95 = _distribution_value(media_queue, "p95") or 0.0
    high_media_shards = [
        shard_id
        for shard_id, metrics in shard_metrics.items()
        if (_distribution_value(metrics, "p95") or 0.0) >= 60_000
    ]
    tail_source_count = int(media_tail.get("tail_source_count") or 0)
    tail_source_fraction = float(media_tail.get("tail_source_fraction") or 0.0)

    reasons: list[str] = []
    if record_p95 >= 60_000:
        reasons.append("record_request_pending_ms_p95_over_60s")
    if media_queue_p95 >= 60_000:
        reasons.append("media_queue_wait_ms_p95_over_60s")
    if len(high_media_shards) >= 2:
        reasons.append("tail_present_on_multiple_replay_shards")
    if tail_source_count >= 10 or tail_source_fraction >= 0.35:
        reasons.append("tail_spread_across_many_sources")

    if (
        media_queue_p95 >= 60_000
        and len(high_media_shards) >= 2
        and (tail_source_count >= 10 or tail_source_fraction >= 0.35)
    ):
        diagnosis = "global_or_shard_outlet_bottleneck_likely"
    elif (
        record_p95 >= 60_000
        and media_queue_p95 < 60_000
    ):
        diagnosis = "clip_or_admission_queue_bottleneck_likely"
    elif tail_source_count and tail_source_count <= max(2, int(0.1 * max(1, media_tail["measured_source_count"]))):
        diagnosis = "hot_source_self_queue_likely"
    else:
        diagnosis = "mixed_or_inconclusive"

    return {
        "diagnosis": diagnosis,
        "reasons": reasons,
        "record_request_pending_ms_p95": record_p95,
        "media_queue_wait_ms_p95": media_queue_p95,
        "high_media_queue_shards": high_media_shards,
    }


def analyze_artifact(artifact_dir: Path) -> dict[str, Any]:
    report = _read_json(artifact_dir / "report.json", {})
    downstream = _read_json(
        artifact_dir / "downstream_observability_summary.json",
        report.get("downstream_observability") or {},
    )
    clip_log_text = (artifact_dir / "clip_worker_logs_since_start.txt").read_text(
        encoding="utf-8",
        errors="replace",
    ) if (artifact_dir / "clip_worker_logs_since_start.txt").exists() else ""
    clip_log_metrics = _extract_log_metrics(clip_log_text)

    kept_evidence = report.get("kept_evidence") or []
    if not isinstance(kept_evidence, list):
        kept_evidence = []

    clip_phase = (downstream.get("phase_latency_ms") or {}).get("clip_worker") or {}
    media_worker = downstream.get("media_worker") or {}
    media_phase = (downstream.get("phase_latency_ms") or {}).get("media_worker") or {}

    evidence_counts = Counter(_source_id_from_evidence(item) for item in kept_evidence)
    source_to_shard: dict[str, str] = {}
    for item in kept_evidence:
        source_to_shard.setdefault(_source_id_from_evidence(item), _shard_id_from_evidence(item))

    record_by_source = _distribution_by_key(
        kept_evidence,
        "record_request_pending_ms",
        _source_id_from_evidence,
    )
    record_by_shard = _distribution_by_key(
        kept_evidence,
        "record_request_pending_ms",
        _shard_id_from_evidence,
    )

    summary = {
        "artifact_dir": str(artifact_dir),
        "run_id": report.get("run_id") or artifact_dir.name,
        "status": report.get("status"),
        "kept_evidence_count": report.get("kept_evidence_count", len(kept_evidence)),
        "clip_worker": {
            "record_request_pending_ms": {
                "overall": (
                    clip_phase.get("record_request_pending_ms")
                    or clip_log_metrics["phase_timing"]["record_request_pending_ms"]
                ),
                "by_source_from_kept_evidence": record_by_source,
                "by_shard_from_kept_evidence": record_by_shard,
                "source_tail_from_kept_evidence": _source_tail_summary(
                    record_by_source,
                    threshold_ms=60_000,
                ),
            },
            "proof_wait_ms": {
                "overall": (
                    clip_phase.get("proof_wait_ms")
                    or clip_log_metrics["phase_timing"]["proof_wait_ms"]
                ),
            },
            "replay_job_create_ms": {
                "overall": (
                    clip_phase.get("replay_job_create_ms")
                    or clip_log_metrics["phase_timing"]["replay_job_create_ms"]
                ),
            },
            "log_counts": clip_log_metrics["counts"],
        },
        "media_worker": {
            field: {
                "overall": media_worker.get(field) or media_phase.get(field) or {},
            }
            for field in MEDIA_LATENCY_FIELDS
        },
        "source_evidence_counts": dict(sorted(evidence_counts.items())),
        "source_to_shard_from_kept_evidence": dict(sorted(source_to_shard.items())),
    }

    media_queue_by_source = media_worker.get("queue_wait_ms_by_source") or {}
    media_queue_by_shard = media_worker.get("queue_wait_ms_by_shard") or {}
    summary["media_worker"]["queue_wait_ms"]["by_source"] = media_queue_by_source
    summary["media_worker"]["queue_wait_ms"]["by_shard"] = media_queue_by_shard
    summary["media_worker"]["queue_wait_ms"]["source_tail"] = _source_tail_summary(
        media_queue_by_source,
        threshold_ms=60_000,
    )
    summary["diagnosis"] = _classify(summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args(argv)

    summary = analyze_artifact(args.artifact_dir)
    indent = 2 if args.pretty else None
    print(json.dumps(summary, ensure_ascii=False, indent=indent, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
