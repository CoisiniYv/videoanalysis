"""Low-overhead Savant element timing and batch occupancy metrics."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

try:
    from savant.metrics import get_or_create_counter, get_or_create_gauge
except Exception:  # pragma: no cover - available in the Savant runtime image.
    get_or_create_counter = None  # type: ignore[assignment]
    get_or_create_gauge = None  # type: ignore[assignment]


DEFAULT_STAGE_NAMES = frozenset(
    {
        "yolo26_pose",
        "yolo26_pose_preproc",
        "yolo26_pose_postproc",
        "tracker",
        "behavior_rules",
        "yolov8_face",
        "yolov8_face_preproc",
        "yolov8_face_postproc",
        "face_person_associator",
        "adaface",
        "adaface_preproc",
        "adaface_postproc",
        "face_reid_gate",
        "face_observation_exporter",
        "frame_annotation_exporter",
        "savant_perf_metrics",
    }
)
LATENCY_BUCKETS_SECONDS = (
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)


class SavantStageMetrics:
    """Record synchronous element wall time without frame-level labels."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        source = os.environ if env is None else env
        self.enabled = _boolish(source.get("SAVANT_STAGE_METRICS_ENABLED"), False)
        configured = {
            value.strip()
            for value in str(source.get("SAVANT_STAGE_METRICS_STAGES") or "").split(",")
            if value.strip()
        }
        self.stages = configured or set(DEFAULT_STAGE_NAMES)
        self._lock = threading.Lock()
        self._starts_ns: dict[tuple[str, int], int] = {}
        self._duration_counts: dict[str, int] = {}
        self._duration_sums: dict[str, float] = {}
        self._duration_buckets: dict[tuple[str, str], int] = {}
        self._duration_count = _gauge(
            "va_savant_stage_duration_seconds_count",
            "Observed GStreamer buffers completed by a measured Savant stage.",
            ["stage"],
        )
        self._duration_sum = _gauge(
            "va_savant_stage_duration_seconds_sum",
            "Cumulative synchronous wall time spent in a measured Savant stage.",
            ["stage"],
        )
        self._duration_bucket = _gauge(
            "va_savant_stage_duration_seconds_bucket",
            "Cumulative wall-time observations by upper-bound bucket.",
            ["stage", "le"],
        )
        self._duration_last = _gauge(
            "va_savant_stage_duration_seconds_last",
            "Most recent synchronous wall-time observation for a Savant stage.",
            ["stage"],
        )
        self._batch_occupancy = _counter(
            "va_savant_batch_occupancy",
            "Input buffers observed by stage and NvDs frame batch occupancy.",
            ["stage", "batch_size"],
        )
        self._unmatched_end = _counter(
            "va_savant_stage_timing_unmatched_end",
            "Stage output buffers without a corresponding input timing marker.",
            ["stage"],
        )

    def measures(self, stage: str) -> bool:
        return self.enabled and stage in self.stages

    def begin(self, stage: str, buffer: Any) -> None:
        if not self.measures(stage):
            return
        key = (stage, hash(buffer))
        with self._lock:
            self._starts_ns[key] = time.monotonic_ns()

    def observe_batch(self, stage: str, batch_size: int) -> None:
        if not self.measures(stage):
            return
        _inc(
            self._batch_occupancy,
            {"stage": stage, "batch_size": str(max(0, int(batch_size)))},
            1,
        )

    def end(self, stage: str, buffer: Any) -> None:
        if not self.measures(stage):
            return
        key = (stage, hash(buffer))
        with self._lock:
            started_ns = self._starts_ns.pop(key, None)
        if started_ns is None:
            _inc(self._unmatched_end, {"stage": stage}, 1)
            return
        duration_s = max(0.0, (time.monotonic_ns() - started_ns) / 1_000_000_000)
        labels = {"stage": stage}
        with self._lock:
            duration_count = self._duration_counts.get(stage, 0) + 1
            self._duration_counts[stage] = duration_count
            duration_sum_s = self._duration_sums.get(stage, 0.0) + duration_s
            self._duration_sums[stage] = duration_sum_s
            bucket_values: dict[str, int] = {}
            for upper_bound in LATENCY_BUCKETS_SECONDS:
                if duration_s <= upper_bound:
                    bucket = _bucket_label(upper_bound)
                    key = (stage, bucket)
                    self._duration_buckets[key] = self._duration_buckets.get(key, 0) + 1
                    bucket_values[bucket] = self._duration_buckets[key]
            infinity_key = (stage, "+Inf")
            self._duration_buckets[infinity_key] = (
                self._duration_buckets.get(infinity_key, 0) + 1
            )
            bucket_values["+Inf"] = self._duration_buckets[infinity_key]
        _set(self._duration_count, labels, duration_count)
        _set(self._duration_sum, labels, duration_sum_s)
        _set(self._duration_last, labels, duration_s)
        for bucket, count in bucket_values.items():
            _set(
                self._duration_bucket,
                {"stage": stage, "le": bucket},
                count,
            )


def _boolish(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bucket_label(value: float) -> str:
    return f"{value:g}"


def _counter(name: str, description: str, labels: list[str]) -> Any:
    return _metric(get_or_create_counter, name, description, labels)


def _gauge(name: str, description: str, labels: list[str]) -> Any:
    return _metric(get_or_create_gauge, name, description, labels)


def _metric(factory: Any, name: str, description: str, labels: list[str]) -> Any:
    if factory is None:
        return None
    for kwargs in ({"labels": labels}, {"labelnames": labels}, {"label_names": labels}, {}):
        try:
            return factory(name, description, **kwargs)
        except TypeError:
            continue
        except Exception:
            return None
    return None


def _inc(metric: Any, labels: dict[str, str], value: float) -> None:
    if metric is None:
        return
    if labels and not callable(getattr(metric, "labels", None)):
        try:
            metric.inc(value, list(labels.values()))
        except Exception:
            return
        return
    child = _child(metric, labels)
    try:
        child.inc(value)
    except TypeError:
        try:
            metric.inc(value, list(labels.values()))
        except Exception:
            return
    except Exception:
        return


def _set(metric: Any, labels: dict[str, str], value: float) -> None:
    if metric is None:
        return
    if labels and not callable(getattr(metric, "labels", None)):
        try:
            metric.set(value, list(labels.values()))
        except Exception:
            return
        return
    child = _child(metric, labels)
    try:
        child.set(value)
    except TypeError:
        try:
            metric.set(value, list(labels.values()))
        except Exception:
            return
    except Exception:
        return


def _child(metric: Any, labels: dict[str, str]) -> Any:
    if metric is None:
        return None
    if not labels:
        return metric
    try:
        return metric.labels(**labels)
    except Exception:
        return metric
