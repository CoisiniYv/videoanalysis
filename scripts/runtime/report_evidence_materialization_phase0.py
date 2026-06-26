#!/usr/bin/env python3
"""Build a Phase 0 evidence materialization cost report."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DATABASE_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_ENV_FILE = Path("infra/env/midterm.env")
PENDING_TASK_STATUSES = {
    "pending",
    "replaying",
    "finalizing",
    "claimed",
    "processing",
    "manifest_ready",
    "materialization_pending",
    "materialization_deferred",
    "materializing",
}


def build_phase0_report(
    event_rows: list[dict[str, Any]],
    task_rows: list[dict[str, Any]],
    *,
    observed_at: datetime | None = None,
    data_source: str = "database",
    phase_label: str = "phase0",
    baseline_report: dict[str, Any] | None = None,
    reference_reports: dict[str, dict[str, Any]] | None = None,
    replay_ttl_seconds: int = 300,
    frame_annotation_ttl_seconds: int = 120,
    proposed_deferred_windows_seconds: list[int] | None = None,
    warning: str | None = None,
) -> dict[str, Any]:
    observed_at = observed_at or datetime.now(timezone.utc)
    media_rows = [_as_dict(row.get("media")) for row in event_rows]
    metrics_rows = [
        _as_dict(media.get("materialization_metrics"))
        for media in media_rows
        if isinstance(media.get("materialization_metrics"), dict)
    ]
    replay_shards = Counter(
        str(media.get("replay_shard_id") or "unknown") for media in media_rows
    )
    modes = Counter(
        str(metrics.get("materialization_mode") or "unknown")
        for metrics in metrics_rows
    )
    guardrail_modes = Counter(
        str(metrics.get("guardrail_mode") or "unknown")
        for metrics in metrics_rows
    )
    materialization_statuses = Counter(
        _materialization_status_for_report(media)
        for media in media_rows
    )
    quota_levels = Counter(
        _decision_level(media.get("quota_decision")) for media in media_rows
    )
    degrade_levels = Counter(
        _decision_level(media.get("degrade_decision")) for media in media_rows
    )
    storage_levels = Counter(
        _storage_level_from_media(media) for media in media_rows
    )
    task_statuses = Counter(str(row.get("status") or "unknown") for row in task_rows)
    task_materialization_statuses = Counter(
        str(row.get("materialization_status") or row.get("status") or "unknown")
        for row in task_rows
    )

    ffmpeg_elapsed_s = [
        value / 1000.0 for value in _numbers(metrics_rows, "ffmpeg_elapsed_ms")
    ]
    materialization_elapsed_s = [
        value / 1000.0 for value in _numbers(metrics_rows, "finalization_elapsed_ms")
    ]
    finalization_cpu_s = _numbers(metrics_rows, "finalization_process_cpu_seconds")
    queue_wait_s = [
        value / 1000.0 for value in _numbers(metrics_rows, "queue_wait_ms")
    ]
    ffmpeg_cpu_s = _numbers(metrics_rows, "ffmpeg_child_cpu_seconds")
    input_bytes = _numbers(metrics_rows, "input_bytes")
    output_bytes = _numbers(metrics_rows, "output_bytes")
    source_durations = _numbers(metrics_rows, "source_metadata_duration_seconds")
    cleanup_bytes = _numbers(media_rows, "sink_output_cleanup_deleted_bytes")
    task_latencies_s = [
        latency
        for latency in (
            _row_latency_seconds(row.get("created_at"), row.get("updated_at"))
            for row in task_rows
        )
        if latency is not None
    ]

    dominant, reason = infer_dominant_cost(
        metrics_rows=metrics_rows,
        ffmpeg_elapsed_s=ffmpeg_elapsed_s,
        ffmpeg_cpu_s=ffmpeg_cpu_s,
        queue_wait_s=queue_wait_s,
        input_bytes=input_bytes,
    )
    active_dominant, active_reason = infer_active_materialization_cost(
        metrics_rows=metrics_rows,
        ffmpeg_elapsed_s=ffmpeg_elapsed_s,
        ffmpeg_cpu_s=ffmpeg_cpu_s,
        input_bytes=input_bytes,
    )

    status = "ok" if metrics_rows else "insufficient_runtime_samples"
    if warning and status == "ok":
        status = "warning"
    sample_quality = (
        "low"
        if 0 < len(metrics_rows) < 3
        else "none"
        if not metrics_rows
        else "ok"
    )

    report = {
        "schema_version": "phase0-evidence-materialization-report-v1",
        "phase_label": phase_label,
        "status": status,
        "warning": warning or "",
        "data_source": data_source,
        "observed_at": observed_at.isoformat(),
        "event_sample_count": len(event_rows),
        "instrumented_materialization_sample_count": len(metrics_rows),
        "sample_quality": sample_quality,
        "replay_job_counts_by_shard": dict(sorted(replay_shards.items())),
        "replay_sink": {
            "input_bytes_total": int(sum(input_bytes)),
            "input_bytes": _stats(input_bytes),
            "source_metadata_duration_seconds": _stats(source_durations),
        },
        "materialization": {
            "mode_counts": dict(sorted(modes.items())),
            "guardrail_mode_counts": dict(sorted(guardrail_modes.items())),
            "ffmpeg_transcode_elapsed_seconds": _stats(ffmpeg_elapsed_s),
            "ffmpeg_child_cpu_seconds": _stats(ffmpeg_cpu_s),
            "media_worker_finalization_process_cpu_seconds": _stats(finalization_cpu_s),
            "finalization_elapsed_seconds": _stats(materialization_elapsed_s),
            "output_bytes_total": int(sum(output_bytes)),
            "output_bytes": _stats(output_bytes),
        },
        "cleanup": {
            "deleted_bytes_total": int(sum(cleanup_bytes)),
            "deleted_event_count": len(cleanup_bytes),
            "deleted_bytes": _stats(cleanup_bytes),
        },
        "queue": {
            "task_status_counts": dict(sorted(task_statuses.items())),
            "task_materialization_status_counts": dict(
                sorted(task_materialization_statuses.items())
            ),
            "materialization_status_counts": dict(sorted(materialization_statuses.items())),
            "current_depth": sum(
                count
                for status_name, count in task_statuses.items()
                if status_name in PENDING_TASK_STATUSES
            ),
            "materialization_queue_wait_seconds": _stats(queue_wait_s),
            "evidence_task_lifecycle_seconds": _stats(task_latencies_s),
        },
        "quota_and_degrade": {
            "quota_decision_levels": dict(sorted(quota_levels.items())),
            "degrade_decision_levels": dict(sorted(degrade_levels.items())),
            "storage_quota_levels": dict(sorted(storage_levels.items())),
        },
        "ttl_sizing": build_ttl_sizing(
            metrics_rows,
            replay_ttl_seconds=replay_ttl_seconds,
            frame_annotation_ttl_seconds=frame_annotation_ttl_seconds,
            proposed_deferred_windows_seconds=proposed_deferred_windows_seconds,
        ),
        "pressure_test_acceptance": {
            "two_source_token": (
                "PASS_REPLAY_EVIDENCE_IO_OPTIMIZED_TWO_SOURCE"
                if phase_label in {"phase5_two_source", "two_source"}
                and len(replay_shards) >= 2
                and metrics_rows
                else "not_run"
            ),
            "sixty_stream_token": "not_run_runtime_limited",
            "sixty_stream_reason": (
                "real 60-stream pressure input was not available to this report"
            ),
        },
        "dominant_current_cost": dominant,
        "dominant_cost_reason": reason,
        "dominant_active_materialization_cost": active_dominant,
        "dominant_active_materialization_reason": active_reason,
    }
    if baseline_report:
        report["baseline_comparison"] = build_baseline_comparison(
            current_report=report,
            baseline_report=baseline_report,
        )
    if reference_reports:
        report["reference_report_comparisons"] = {
            label: build_baseline_comparison(
                current_report=report,
                baseline_report=reference_report,
            )
            for label, reference_report in sorted(reference_reports.items())
        }
    return report


def build_baseline_comparison(
    *,
    current_report: dict[str, Any],
    baseline_report: dict[str, Any],
) -> dict[str, Any]:
    comparisons = {
        "queue_current_depth": _compare_scalar(
            _nested_number(current_report, ("queue", "current_depth")),
            _nested_number(baseline_report, ("queue", "current_depth")),
        ),
        "queue_wait_avg_seconds": _compare_scalar(
            _nested_number(
                current_report,
                ("queue", "materialization_queue_wait_seconds", "avg"),
            ),
            _nested_number(
                baseline_report,
                ("queue", "materialization_queue_wait_seconds", "avg"),
            ),
        ),
        "queue_wait_p95_seconds": _compare_scalar(
            _nested_number(
                current_report,
                ("queue", "materialization_queue_wait_seconds", "p95"),
            ),
            _nested_number(
                baseline_report,
                ("queue", "materialization_queue_wait_seconds", "p95"),
            ),
        ),
        "ffmpeg_elapsed_avg_seconds": _compare_scalar(
            _nested_number(
                current_report,
                ("materialization", "ffmpeg_transcode_elapsed_seconds", "avg"),
            ),
            _nested_number(
                baseline_report,
                ("materialization", "ffmpeg_transcode_elapsed_seconds", "avg"),
            ),
        ),
        "ffmpeg_elapsed_p95_seconds": _compare_scalar(
            _nested_number(
                current_report,
                ("materialization", "ffmpeg_transcode_elapsed_seconds", "p95"),
            ),
            _nested_number(
                baseline_report,
                ("materialization", "ffmpeg_transcode_elapsed_seconds", "p95"),
            ),
        ),
        "ffmpeg_child_cpu_avg_seconds": _compare_scalar(
            _nested_number(
                current_report,
                ("materialization", "ffmpeg_child_cpu_seconds", "avg"),
            ),
            _nested_number(
                baseline_report,
                ("materialization", "ffmpeg_child_cpu_seconds", "avg"),
            ),
        ),
        "ffmpeg_child_cpu_p95_seconds": _compare_scalar(
            _nested_number(
                current_report,
                ("materialization", "ffmpeg_child_cpu_seconds", "p95"),
            ),
            _nested_number(
                baseline_report,
                ("materialization", "ffmpeg_child_cpu_seconds", "p95"),
            ),
        ),
    }
    return {
        "baseline_phase_label": baseline_report.get("phase_label") or "phase0",
        "baseline_observed_at": baseline_report.get("observed_at", ""),
        "current_phase_label": current_report.get("phase_label", ""),
        "current_observed_at": current_report.get("observed_at", ""),
        "dominant_current_cost_before": baseline_report.get("dominant_current_cost"),
        "dominant_current_cost_after": current_report.get("dominant_current_cost"),
        "dominant_active_materialization_cost_before": baseline_report.get(
            "dominant_active_materialization_cost"
        ),
        "dominant_active_materialization_cost_after": current_report.get(
            "dominant_active_materialization_cost"
        ),
        "metrics": comparisons,
    }


def infer_dominant_cost(
    *,
    metrics_rows: list[dict[str, Any]],
    ffmpeg_elapsed_s: list[float],
    ffmpeg_cpu_s: list[float],
    queue_wait_s: list[float],
    input_bytes: list[float],
) -> tuple[str, str]:
    if not metrics_rows:
        return (
            "insufficient_runtime_samples",
            "no events with media.materialization_metrics were found",
        )
    avg_ffmpeg = _average(ffmpeg_elapsed_s)
    avg_cpu = _average(ffmpeg_cpu_s)
    avg_queue = _average(queue_wait_s)
    if avg_queue is not None and avg_ffmpeg is not None and avg_queue > avg_ffmpeg * 1.2:
        return (
            "queue_wait",
            f"avg_queue_wait_s={avg_queue:.3f} exceeds avg_ffmpeg_elapsed_s={avg_ffmpeg:.3f}",
        )
    if avg_cpu is not None and avg_ffmpeg is not None and avg_cpu >= avg_ffmpeg * 0.5:
        return (
            "transcode_cpu",
            f"avg_ffmpeg_child_cpu_s={avg_cpu:.3f} tracks avg_ffmpeg_elapsed_s={avg_ffmpeg:.3f}",
        )
    if avg_ffmpeg is not None:
        return (
            "transcode_elapsed",
            f"avg_ffmpeg_elapsed_s={avg_ffmpeg:.3f}; child CPU samples are unavailable or low",
        )
    if input_bytes:
        return (
            "sink_io_or_copy",
            "materialization samples have input bytes but no ffmpeg timing",
        )
    return (
        "something_else",
        "instrumented samples exist but lack queue, ffmpeg, and byte metrics",
    )


def infer_active_materialization_cost(
    *,
    metrics_rows: list[dict[str, Any]],
    ffmpeg_elapsed_s: list[float],
    ffmpeg_cpu_s: list[float],
    input_bytes: list[float],
) -> tuple[str, str]:
    if not metrics_rows:
        return (
            "insufficient_runtime_samples",
            "no events with media.materialization_metrics were found",
        )
    avg_ffmpeg = _average(ffmpeg_elapsed_s)
    avg_cpu = _average(ffmpeg_cpu_s)
    if avg_cpu is not None and avg_ffmpeg is not None and avg_cpu >= avg_ffmpeg * 0.5:
        return (
            "transcode_cpu",
            f"avg_ffmpeg_child_cpu_s={avg_cpu:.3f} tracks avg_ffmpeg_elapsed_s={avg_ffmpeg:.3f}",
        )
    if avg_ffmpeg is not None:
        return (
            "transcode_elapsed",
            f"avg_ffmpeg_elapsed_s={avg_ffmpeg:.3f}; child CPU samples are unavailable or low",
        )
    if input_bytes:
        return (
            "sink_io_or_copy",
            "materialization samples have input bytes but no ffmpeg timing",
        )
    return (
        "something_else",
        "instrumented samples exist but lack active materialization cost metrics",
    )


def fetch_event_rows(
    database_url: str,
    *,
    limit: int,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    query = """
        SELECT
            id::text AS event_id,
            source_id,
            media_status,
            created_at,
            updated_at,
            COALESCE(payload->'media', '{}'::jsonb) AS media
        FROM events
        WHERE (
            COALESCE(payload->'media'->>'recording_strategy', '') = 'savant_replay'
            OR COALESCE(payload->'media'->>'evidence_topology', '') = 'post_savant_replay'
            OR COALESCE(payload->'media'->>'replay_job_id', '') <> ''
        )
          AND (%(since)s::timestamptz IS NULL OR updated_at >= %(since)s::timestamptz)
        ORDER BY updated_at DESC
        LIMIT %(limit)s
    """
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, {"limit": int(limit), "since": since})
            return [dict(row) for row in cur.fetchall()]


def fetch_task_rows(
    database_url: str,
    *,
    limit: int,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    query = """
        SELECT
            task_id,
            event_id::text AS event_id,
            source_id,
            status,
            materialization_status,
            replay_shard_id,
            replay_source_id,
            materialization_deadline_at,
            quota_decision,
            degrade_decision,
            created_at,
            updated_at
        FROM evidence_tasks
        WHERE (%(since)s::timestamptz IS NULL OR updated_at >= %(since)s::timestamptz)
        ORDER BY updated_at DESC
        LIMIT %(limit)s
    """
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, {"limit": int(limit), "since": since})
            return [dict(row) for row in cur.fetchall()]


def _stats(values: list[float]) -> dict[str, Any]:
    finite = [float(value) for value in values if value is not None]
    finite.sort()
    if not finite:
        return {"count": 0}
    return {
        "count": len(finite),
        "sum": round(sum(finite), 6),
        "avg": round(sum(finite) / len(finite), 6),
        "p50": round(_percentile(finite, 0.50), 6),
        "p95": round(_percentile(finite, 0.95), 6),
        "p99": round(_percentile(finite, 0.99), 6),
        "min": round(finite[0], 6),
        "max": round(finite[-1], 6),
    }


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = percentile * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def _numbers(rows: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = _number_or_none(row.get(key))
        if value is not None:
            values.append(value)
    return values


def _number_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _materialization_status_for_report(media: dict[str, Any]) -> str:
    status = str(media.get("materialization_status") or "").strip()
    if status:
        return status
    if isinstance(media.get("materialization_metrics"), dict):
        return "materialized"
    return "unknown"


def _decision_level(value: object) -> str:
    doc = _as_dict(value)
    if not doc:
        return "none"
    return str(doc.get("level") or doc.get("overall_level") or "recorded")


def _storage_level_from_media(media: dict[str, Any]) -> str:
    guardrails = _as_dict(media.get("materialization_guardrails"))
    storage = _as_dict(guardrails.get("storage_quota_decision"))
    if not storage:
        return "none"
    return str(storage.get("overall_level") or "recorded")


def build_ttl_sizing(
    metrics_rows: list[dict[str, Any]],
    *,
    replay_ttl_seconds: int = 300,
    frame_annotation_ttl_seconds: int = 120,
    proposed_deferred_windows_seconds: list[int] | None = None,
) -> dict[str, Any]:
    input_bytes = 0.0
    input_seconds = 0.0
    for metrics in metrics_rows:
        bytes_value = _number_or_none(metrics.get("input_bytes"))
        duration_value = _number_or_none(
            metrics.get("source_metadata_duration_seconds")
            or metrics.get("input_duration_seconds")
        )
        if bytes_value is None or duration_value is None or duration_value <= 0:
            continue
        input_bytes += bytes_value
        input_seconds += duration_value
    bytes_per_second_per_stream = (
        input_bytes / input_seconds if input_seconds > 0 else None
    )
    ttl_seconds = tuple(
        sorted({300, 600, 900, max(0, int(replay_ttl_seconds))})
    )
    proposed_windows = tuple(
        sorted({max(0, int(value)) for value in (proposed_deferred_windows_seconds or [])})
    )
    evaluated_windows = tuple(sorted(set(ttl_seconds).union(proposed_windows)))
    stream_counts = (30, 60)
    estimates: dict[str, Any] = {}
    for stream_count in stream_counts:
        estimates[f"{stream_count}_streams"] = {
            str(ttl): (
                int(bytes_per_second_per_stream * stream_count * ttl)
                if bytes_per_second_per_stream is not None
                else None
            )
            for ttl in evaluated_windows
        }
    annotation_extension_multipliers = {
        str(window): (
            round(window / frame_annotation_ttl_seconds, 6)
            if frame_annotation_ttl_seconds > 0
            else None
        )
        for window in evaluated_windows
        if window > frame_annotation_ttl_seconds
    }
    return {
        "schema_version": "phase4-ttl-sizing-v1",
        "bytes_per_second_per_stream": (
            round(bytes_per_second_per_stream, 6)
            if bytes_per_second_per_stream is not None
            else None
        ),
        "replay_ttl_seconds_current": int(replay_ttl_seconds),
        "replay_ttl_seconds_evaluated": list(evaluated_windows),
        "frame_annotation_ttl_seconds_current": int(frame_annotation_ttl_seconds),
        "proposed_deferred_windows_seconds": list(proposed_windows),
        "effective_deferred_window_seconds_current": min(
            int(replay_ttl_seconds), int(frame_annotation_ttl_seconds)
        ),
        "frame_annotation_extension_multipliers": annotation_extension_multipliers,
        "replay_rocksdb_estimated_bytes": estimates,
        "deferred_window_note": (
            "deferred materialization is bounded by the earlier of Replay TTL "
            "and frame-annotation TTL unless those configs are increased"
        ),
    }


def _nested_number(document: dict[str, Any], path: tuple[str, ...]) -> float | None:
    value: object = document
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _number_or_none(value)


def _compare_scalar(current: float | None, baseline: float | None) -> dict[str, Any]:
    if current is None or baseline is None:
        return {
            "current": current,
            "baseline": baseline,
            "delta": None,
            "ratio": None,
        }
    return {
        "current": round(float(current), 6),
        "baseline": round(float(baseline), 6),
        "delta": round(float(current) - float(baseline), 6),
        "ratio": (
            round(float(current) / float(baseline), 6)
            if float(baseline) != 0.0
            else None
        ),
    }


def _as_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _datetime_or_none(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_latency_seconds(start: object, end: object) -> float | None:
    started = _datetime_or_none(start)
    ended = _datetime_or_none(end)
    if started is None or ended is None:
        return None
    return max(0.0, (ended - started).total_seconds())


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _database_url(args: argparse.Namespace) -> str:
    if args.database_url:
        return args.database_url
    env_file_values = _load_env_file(args.env_file)
    return (
        os.getenv("VIDEO_ANALYTICS_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or env_file_values.get("VIDEO_ANALYTICS_DATABASE_URL")
        or env_file_values.get("DATABASE_URL")
        or DEFAULT_DATABASE_URL
    )


def _int_from_sources(
    key: str,
    *,
    env_file_values: dict[str, str],
    default: int,
) -> int:
    value = os.getenv(key) or env_file_values.get(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _parse_proposed_windows(value: str) -> list[int]:
    windows: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            windows.append(int(item))
        except ValueError:
            continue
    return windows


def _load_reference_reports(values: list[str]) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    for value in values:
        if "=" in value:
            label, path_text = value.split("=", 1)
            label = label.strip()
        else:
            path_text = value
            label = Path(path_text).stem
        if not label:
            label = Path(path_text).stem
        path = Path(path_text)
        reports[label] = json.loads(path.read_text(encoding="utf-8"))
    return reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--phase-label", default="phase0")
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument(
        "--reference-report",
        action="append",
        default=[],
        help="additional reference report as label=/path/report.json",
    )
    parser.add_argument("--replay-ttl-seconds", type=int)
    parser.add_argument("--frame-annotation-ttl-seconds", type=int)
    parser.add_argument(
        "--proposed-deferred-windows-seconds",
        default="",
        help="comma-separated deferred windows to include in TTL sizing",
    )
    parser.add_argument("--since-iso", default="")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return non-zero when the database cannot be queried",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = _database_url(args)
    env_file_values = _load_env_file(args.env_file)
    since = _datetime_or_none(args.since_iso)
    baseline_report = None
    if args.baseline_report:
        baseline_report = json.loads(args.baseline_report.read_text(encoding="utf-8"))
    reference_reports = _load_reference_reports(args.reference_report)
    replay_ttl_seconds = (
        args.replay_ttl_seconds
        if args.replay_ttl_seconds is not None
        else _int_from_sources(
            "EVIDENCE_REPLAY_TTL_SECONDS",
            env_file_values=env_file_values,
            default=300,
        )
    )
    frame_annotation_ttl_seconds = (
        args.frame_annotation_ttl_seconds
        if args.frame_annotation_ttl_seconds is not None
        else _int_from_sources(
            "EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS",
            env_file_values=env_file_values,
            default=_int_from_sources(
                "FRAME_ANNOTATION_TTL_SECONDS",
                env_file_values=env_file_values,
                default=120,
            ),
        )
    )
    proposed_deferred_windows_seconds = _parse_proposed_windows(
        args.proposed_deferred_windows_seconds
    )
    warning = ""
    try:
        event_rows = fetch_event_rows(database_url, limit=args.limit, since=since)
        task_rows = fetch_task_rows(database_url, limit=args.limit, since=since)
        data_source = "database"
    except Exception as exc:
        event_rows = []
        task_rows = []
        data_source = "database_unavailable"
        warning = f"{type(exc).__name__}:{exc}"
        if args.strict:
            print(json.dumps({"status": "database_unavailable", "warning": warning}, indent=2))
            return 2

    report = build_phase0_report(
        event_rows,
        task_rows,
        data_source=data_source,
        phase_label=args.phase_label,
        baseline_report=baseline_report,
        reference_reports=reference_reports,
        replay_ttl_seconds=replay_ttl_seconds,
        frame_annotation_ttl_seconds=frame_annotation_ttl_seconds,
        proposed_deferred_windows_seconds=proposed_deferred_windows_seconds,
        warning=warning,
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.report_path:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
