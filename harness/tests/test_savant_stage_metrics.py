from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    ROOT
    / "modules"
    / "savant_security"
    / "custom"
    / "savant_stage_metrics.py"
)


class FakeMetric:
    def __init__(self, name: str, labels: tuple[tuple[str, str], ...] = ()) -> None:
        self.name = name
        self.labels_key = labels
        self.values: dict[tuple[tuple[str, str], ...], float] = {}

    def labels(self, **labels: str) -> "FakeMetric":
        child = FakeMetric(self.name, tuple(sorted(labels.items())))
        child.values = self.values
        return child

    def inc(self, value: float = 1) -> None:
        self.values[self.labels_key] = self.values.get(self.labels_key, 0.0) + value

    def set(self, value: float) -> None:
        self.values[self.labels_key] = value


class FakeSavantMetricFamily:
    """Match Savant's built-in metric family API (no Prometheus labels())."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, ...], float] = {}

    def inc(self, value: float, label_values: list[str]) -> None:
        key = tuple(label_values)
        self.values[key] = self.values.get(key, 0.0) + value

    def set(self, value: float, label_values: list[str]) -> None:
        self.values[tuple(label_values)] = value


def _load_module():
    spec = importlib.util.spec_from_file_location("savant_stage_metrics_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage_metrics_records_duration_buckets_and_batch_occupancy(monkeypatch) -> None:
    module = _load_module()
    metrics: dict[str, FakeMetric] = {}

    def metric(name: str, _description: str, _labels: list[str]) -> FakeMetric:
        return metrics.setdefault(name, FakeMetric(name))

    monotonic_values = iter([1_000_000_000, 1_012_000_000])
    monkeypatch.setattr(module, "_counter", metric)
    monkeypatch.setattr(module, "_gauge", metric)
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: next(monotonic_values))

    recorder = module.SavantStageMetrics(
        {
            "SAVANT_STAGE_METRICS_ENABLED": "true",
            "SAVANT_STAGE_METRICS_STAGES": "yolo26_pose",
        }
    )
    buffer = object()
    recorder.begin("yolo26_pose", buffer)
    recorder.observe_batch("yolo26_pose", 4)
    recorder.end("yolo26_pose", buffer)

    stage = (("stage", "yolo26_pose"),)
    occupancy = (("batch_size", "4"), ("stage", "yolo26_pose"))
    assert metrics["va_savant_stage_duration_seconds_count"].values[stage] == 1
    assert metrics["va_savant_stage_duration_seconds_sum"].values[stage] == 0.012
    assert metrics["va_savant_stage_duration_seconds_last"].values[stage] == 0.012
    assert metrics["va_savant_batch_occupancy"].values[occupancy] == 1
    assert metrics["va_savant_stage_duration_seconds_bucket"].values[
        (("le", "0.025"), ("stage", "yolo26_pose"))
    ] == 1
    assert metrics["va_savant_stage_duration_seconds_bucket"].values[
        (("le", "+Inf"), ("stage", "yolo26_pose"))
    ] == 1


def test_stage_metrics_is_disabled_by_default(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_counter", lambda *_args: FakeMetric("counter"))
    monkeypatch.setattr(module, "_gauge", lambda *_args: FakeMetric("gauge"))
    recorder = module.SavantStageMetrics({})

    assert recorder.enabled is False
    assert recorder.measures("yolo26_pose") is False


def test_stage_metrics_supports_savant_metric_family_api(monkeypatch) -> None:
    module = _load_module()
    metrics: dict[str, FakeSavantMetricFamily] = {}

    def metric(name: str, _description: str, _labels: list[str]) -> FakeSavantMetricFamily:
        return metrics.setdefault(name, FakeSavantMetricFamily())

    monotonic_values = iter([2_000_000_000, 2_005_000_000])
    monkeypatch.setattr(module, "_counter", metric)
    monkeypatch.setattr(module, "_gauge", metric)
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: next(monotonic_values))

    recorder = module.SavantStageMetrics(
        {
            "SAVANT_STAGE_METRICS_ENABLED": "true",
            "SAVANT_STAGE_METRICS_STAGES": "yolo26_pose",
        }
    )
    buffer = object()
    recorder.begin("yolo26_pose", buffer)
    recorder.observe_batch("yolo26_pose", 4)
    recorder.end("yolo26_pose", buffer)

    assert metrics["va_savant_stage_duration_seconds_count"].values[("yolo26_pose",)] == 1
    assert metrics["va_savant_batch_occupancy"].values[("yolo26_pose", "4")] == 1
