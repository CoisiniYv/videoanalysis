#!/usr/bin/env python3
"""Phase 2 single-T4 30-stream pressure-run acceptance checker."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


PASS_TOKEN = "PASS_PHASE2_SINGLE_T4_30"
DEFAULT_RUNTIME_OVERVIEW_URL = "http://127.0.0.1:8090/api/v1/runtime/overview"
DEFAULT_DURATION_S = 1800
DEFAULT_SAMPLE_INTERVAL_S = 30
DEFAULT_TARGET_FPS = 8.0
DEFAULT_FPS_TOLERANCE = 0.10
DEFAULT_MIN_SOURCES = 30


readiness = None


@dataclass(frozen=True)
class GpuSample:
    index: str
    name: str
    gpu_util_pct: float | None
    decoder_util_pct: float | None
    memory_used_mib: float | None
    memory_total_mib: float | None


@dataclass(frozen=True)
class PressureSample:
    observed_at_epoch_s: float
    runtime_overview: dict[str, Any]
    gpu_samples: list[GpuSample]


@dataclass(frozen=True)
class AcceptanceCheck:
    name: str
    ok: bool
    detail: str


def evaluate_pressure_run(
    samples: list[PressureSample],
    *,
    target_fps: float = DEFAULT_TARGET_FPS,
    fps_tolerance: float = DEFAULT_FPS_TOLERANCE,
    min_sources: int = DEFAULT_MIN_SOURCES,
    max_gpu_util_pct: float = 95.0,
    max_decoder_util_pct: float = 90.0,
    min_gpu_memory_headroom_pct: float = 10.0,
    max_forwarder_queue_depth: int = 256,
    evidence_count_delta: int | None = None,
    min_evidence_events: int = 1,
) -> list[AcceptanceCheck]:
    if len(samples) < 2:
        return [
            AcceptanceCheck(
                "sample_count",
                False,
                f"actual={len(samples)} required>=2",
            )
        ]
    first = samples[0]
    last = samples[-1]
    checks = [
        AcceptanceCheck("sample_count", True, f"actual={len(samples)}"),
        _check_runtime_health(samples),
        _check_source_count(samples, min_sources=min_sources),
        _check_fps_sustained(samples, target_fps=target_fps, tolerance=fps_tolerance),
        _check_annotation_counters(first, last, min_sources=min_sources),
        _check_restart_counts_flat(first, last),
        _check_forwarder_send_failures_flat(first, last),
        _check_forwarder_queue(samples, max_queue_depth=max_forwarder_queue_depth),
        _check_gpu_budget(
            samples,
            max_gpu_util_pct=max_gpu_util_pct,
            max_decoder_util_pct=max_decoder_util_pct,
            min_memory_headroom_pct=min_gpu_memory_headroom_pct,
        ),
    ]
    if evidence_count_delta is not None:
        checks.append(
            AcceptanceCheck(
                "evidence_generated",
                evidence_count_delta >= min_evidence_events,
                f"actual_delta={evidence_count_delta} required>={min_evidence_events}",
            )
        )
    return checks


def build_pressure_sample(
    runtime_overview: dict[str, Any],
    gpu_samples: list[GpuSample],
    *,
    observed_at_epoch_s: float | None = None,
) -> PressureSample:
    return PressureSample(
        observed_at_epoch_s=observed_at_epoch_s if observed_at_epoch_s is not None else time.time(),
        runtime_overview=runtime_overview,
        gpu_samples=gpu_samples,
    )


def fetch_runtime_overview(url: str, *, timeout_s: float) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=max(timeout_s, 0.1)) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return {"data": {"health": {"ok": False, "issues": [f"runtime_overview_unavailable:{exc}"]}}}


def query_gpu_samples() -> list[GpuSample]:
    query = "index,name,utilization.gpu,utilization.decoder,memory.used,memory.total"
    try:
        completed = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return parse_gpu_samples(completed.stdout)


def parse_gpu_samples(text: str) -> list[GpuSample]:
    samples: list[GpuSample] = []
    for raw_line in (text or "").splitlines():
        parts = [part.strip() for part in raw_line.split(",")]
        if len(parts) < 6:
            continue
        samples.append(
            GpuSample(
                index=parts[0],
                name=parts[1],
                gpu_util_pct=_float_or_none(parts[2]),
                decoder_util_pct=_float_or_none(parts[3]),
                memory_used_mib=_float_or_none(parts[4]),
                memory_total_mib=_float_or_none(parts[5]),
            )
        )
    return samples


def count_evidence_summaries(evidence_root: Path, *, since_epoch_s: float | None = None) -> int:
    if not evidence_root.exists():
        return 0
    count = 0
    for path in evidence_root.glob("*/summary.json"):
        try:
            if since_epoch_s is not None and path.stat().st_mtime < since_epoch_s:
                continue
        except OSError:
            continue
        count += 1
    return count


def _check_runtime_health(samples: list[PressureSample]) -> AcceptanceCheck:
    bad = [
        index
        for index, sample in enumerate(samples)
        if _health(sample.runtime_overview).get("ok") is not True
    ]
    return AcceptanceCheck("runtime_health_ok", not bad, f"bad_sample_indexes={bad}")


def _check_source_count(samples: list[PressureSample], *, min_sources: int) -> AcceptanceCheck:
    counts = [len(_source_rows(sample.runtime_overview)) for sample in samples]
    min_seen = min(counts) if counts else 0
    return AcceptanceCheck(
        "source_count",
        min_seen >= min_sources,
        f"min_seen={min_seen} required>={min_sources}",
    )


def _check_fps_sustained(
    samples: list[PressureSample],
    *,
    target_fps: float,
    tolerance: float,
) -> AcceptanceCheck:
    final_sources = sorted(_source_rows(samples[-1].runtime_overview))
    low = target_fps * (1.0 - tolerance)
    high = target_fps * (1.0 + tolerance)
    failed: list[str] = []
    averages: dict[str, float] = {}
    for source_id in final_sources:
        values = [
            _float_or_none(_source_rows(sample.runtime_overview).get(source_id, {}).get("effective_fps"))
            for sample in samples
        ]
        finite = [value for value in values if value is not None]
        if not finite:
            failed.append(f"{source_id}=missing")
            continue
        avg = sum(finite) / len(finite)
        averages[source_id] = round(avg, 3)
        if avg < low or avg > high:
            failed.append(f"{source_id}={avg:.3f}")
    detail = f"target={target_fps:g} tolerance={tolerance:g} range=[{low:.3f},{high:.3f}]"
    if failed:
        detail += " failed=" + ",".join(failed[:10])
    else:
        detail += f" sources={len(averages)}"
    return AcceptanceCheck("effective_fps_sustained", not failed, detail)


def _check_annotation_counters(
    first: PressureSample,
    last: PressureSample,
    *,
    min_sources: int,
) -> AcceptanceCheck:
    first_sources = _source_rows(first.runtime_overview)
    last_sources = _source_rows(last.runtime_overview)
    failed: list[str] = []
    for source_id, last_row in sorted(last_sources.items()):
        first_row = first_sources.get(source_id, {})
        frames_delta = _counter_delta(first_row, last_row, "frames_seen_total")
        annotations_delta = _counter_delta(first_row, last_row, "frame_annotations_exported_total")
        if frames_delta is None or frames_delta <= 0:
            failed.append(f"{source_id}:frames_delta={frames_delta}")
            continue
        if annotations_delta is None or annotations_delta <= 0:
            failed.append(f"{source_id}:annotations_delta={annotations_delta}")
    ok = len(last_sources) >= min_sources and not failed
    detail = f"sources={len(last_sources)} required>={min_sources}"
    if failed:
        detail += " failed=" + ",".join(failed[:10])
    return AcceptanceCheck("annotations_advancing", ok, detail)


def _check_restart_counts_flat(first: PressureSample, last: PressureSample) -> AcceptanceCheck:
    first_rows = _container_restart_counts(first.runtime_overview)
    last_rows = _container_restart_counts(last.runtime_overview)
    failed = []
    for name, last_count in sorted(last_rows.items()):
        first_count = first_rows.get(name)
        if first_count is not None and last_count != first_count:
            failed.append(f"{name}:{first_count}->{last_count}")
    return AcceptanceCheck("restart_counts_flat", not failed, "changed=" + (",".join(failed) or "none"))


def _check_forwarder_send_failures_flat(first: PressureSample, last: PressureSample) -> AcceptanceCheck:
    first_rows = _forwarder_rows(first.runtime_overview)
    last_rows = _forwarder_rows(last.runtime_overview)
    failed = []
    for source_id, last_row in sorted(last_rows.items()):
        first_value = _int_or_none(first_rows.get(source_id, {}).get("savant_send_failures_total"))
        last_value = _int_or_none(last_row.get("savant_send_failures_total"))
        if first_value is not None and last_value is not None and last_value != first_value:
            failed.append(f"{source_id}:{first_value}->{last_value}")
    return AcceptanceCheck(
        "forwarder_send_failures_flat",
        not failed,
        "changed=" + (",".join(failed) or "none"),
    )


def _check_forwarder_queue(samples: list[PressureSample], *, max_queue_depth: int) -> AcceptanceCheck:
    depths = [
        _int_or_none(_forwarder_global(sample.runtime_overview).get("queue_depth"))
        for sample in samples
    ]
    finite = [value for value in depths if value is not None]
    max_seen = max(finite) if finite else None
    return AcceptanceCheck(
        "forwarder_queue_depth",
        max_seen is not None and max_seen <= max_queue_depth,
        f"max_seen={max_seen} allowed<={max_queue_depth}",
    )


def _check_gpu_budget(
    samples: list[PressureSample],
    *,
    max_gpu_util_pct: float,
    max_decoder_util_pct: float,
    min_memory_headroom_pct: float,
) -> AcceptanceCheck:
    gpu_values = [gpu for sample in samples for gpu in sample.gpu_samples]
    if not gpu_values:
        return AcceptanceCheck("gpu_budget", False, "no_gpu_samples")
    max_gpu = _max_present(gpu.gpu_util_pct for gpu in gpu_values)
    max_decoder = _max_present(gpu.decoder_util_pct for gpu in gpu_values)
    min_headroom = _min_present(
        _memory_headroom_pct(gpu.memory_used_mib, gpu.memory_total_mib)
        for gpu in gpu_values
    )
    ok = (
        max_gpu is not None
        and max_gpu <= max_gpu_util_pct
        and max_decoder is not None
        and max_decoder <= max_decoder_util_pct
        and min_headroom is not None
        and min_headroom >= min_memory_headroom_pct
    )
    return AcceptanceCheck(
        "gpu_budget",
        ok,
        "max_gpu_pct=%s allowed<=%s max_decoder_pct=%s allowed<=%s min_mem_headroom_pct=%s required>=%s"
        % (
            _fmt(max_gpu),
            _fmt(max_gpu_util_pct),
            _fmt(max_decoder),
            _fmt(max_decoder_util_pct),
            _fmt(min_headroom),
            _fmt(min_memory_headroom_pct),
        ),
    )


def _source_rows(runtime_overview: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metrics = _data(runtime_overview).get("metrics")
    sources = metrics.get("sources") if isinstance(metrics, dict) else []
    return {
        str(row.get("source_id")): row
        for row in sources
        if isinstance(row, dict) and row.get("source_id")
    }


def _forwarder_rows(runtime_overview: dict[str, Any]) -> dict[str, dict[str, Any]]:
    forwarder = _data(runtime_overview).get("forwarder")
    sources = forwarder.get("sources") if isinstance(forwarder, dict) else []
    return {
        str(row.get("source_id")): row
        for row in sources
        if isinstance(row, dict) and row.get("source_id")
    }


def _forwarder_global(runtime_overview: dict[str, Any]) -> dict[str, Any]:
    forwarder = _data(runtime_overview).get("forwarder")
    global_row = forwarder.get("global") if isinstance(forwarder, dict) else {}
    return global_row if isinstance(global_row, dict) else {}


def _container_restart_counts(runtime_overview: dict[str, Any]) -> dict[str, int]:
    containers = _data(runtime_overview).get("containers")
    if not isinstance(containers, dict):
        return {}
    rows: list[dict[str, Any]] = []
    fixed = containers.get("fixed")
    if isinstance(fixed, dict):
        rows.extend(row for row in fixed.values() if isinstance(row, dict))
    dynamic = containers.get("dynamic_sources")
    if isinstance(dynamic, list):
        rows.extend(row for row in dynamic if isinstance(row, dict))
    counts = {}
    for row in rows:
        name = str(row.get("name") or row.get("id") or "")
        count = _int_or_none(row.get("restart_count"))
        if name and count is not None:
            counts[name] = count
    return counts


def _health(runtime_overview: dict[str, Any]) -> dict[str, Any]:
    health = _data(runtime_overview).get("health")
    return health if isinstance(health, dict) else {}


def _data(runtime_overview: dict[str, Any]) -> dict[str, Any]:
    data = runtime_overview.get("data") if isinstance(runtime_overview, dict) else {}
    return data if isinstance(data, dict) else runtime_overview


def _counter_delta(first_row: dict[str, Any], last_row: dict[str, Any], name: str) -> float | None:
    first_value = _float_or_none(first_row.get(name))
    last_value = _float_or_none(last_row.get(name))
    if first_value is None or last_value is None:
        return None
    return last_value - first_value


def _memory_headroom_pct(used_mib: float | None, total_mib: float | None) -> float | None:
    if used_mib is None or total_mib is None or total_mib <= 0:
        return None
    return max(0.0, ((total_mib - used_mib) / total_mib) * 100.0)


def _max_present(values: Any) -> float | None:
    finite = [value for value in values if value is not None]
    return max(finite) if finite else None


def _min_present(values: Any) -> float | None:
    finite = [value for value in values if value is not None]
    return min(finite) if finite else None


def _float_or_none(value: Any) -> float | None:
    if value in (None, "", "N/A", "[Not Supported]"):
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: float | None) -> str:
    return "missing" if value is None else f"{value:.3f}"


def _load_readiness_module() -> Any:
    global readiness
    if readiness is not None:
        return readiness
    script = Path(__file__).with_name("check_phase2_single_t4_readiness.py")
    spec = importlib.util.spec_from_file_location("phase2_readiness", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load readiness module: {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase2_readiness"] = module
    spec.loader.exec_module(module)
    readiness = module
    return module


def _run_readiness(args: argparse.Namespace, runtime_overview: dict[str, Any]) -> list[Any]:
    module = _load_readiness_module()
    gpu_names = module.gpu_names_from_nvidia_smi()
    return module.evaluate_phase2_readiness(
        root=args.root,
        sources_config=args.sources_config,
        compose_file=args.compose_file,
        replay_config=args.replay_config,
        phase1_doc=args.phase1_doc,
        gpu_names=gpu_names,
        min_sources=args.min_sources,
        runtime_overview=runtime_overview,
    )


def run_pressure(args: argparse.Namespace) -> tuple[list[AcceptanceCheck], list[PressureSample], int | None]:
    start_runtime = fetch_runtime_overview(args.runtime_overview_url, timeout_s=args.runtime_timeout_s)
    if not args.skip_readiness:
        readiness_results = _run_readiness(args, start_runtime)
        failed = [result for result in readiness_results if not result.ok]
        if failed:
            checks = [
                AcceptanceCheck(
                    f"readiness:{result.name}",
                    bool(result.ok),
                    result.detail,
                )
                for result in readiness_results
            ]
            return checks, [], None

    start_epoch = time.time()
    samples = [build_pressure_sample(start_runtime, query_gpu_samples(), observed_at_epoch_s=start_epoch)]
    deadline = time.monotonic() + max(args.duration_s, 0)
    while time.monotonic() < deadline:
        sleep_s = min(max(args.sample_interval_s, 1), max(deadline - time.monotonic(), 0))
        if sleep_s > 0:
            time.sleep(sleep_s)
        samples.append(
            build_pressure_sample(
                fetch_runtime_overview(args.runtime_overview_url, timeout_s=args.runtime_timeout_s),
                query_gpu_samples(),
            )
        )
    evidence_delta = count_evidence_summaries(args.evidence_root, since_epoch_s=start_epoch)
    checks = evaluate_pressure_run(
        samples,
        target_fps=args.target_fps,
        fps_tolerance=args.fps_tolerance,
        min_sources=args.min_sources,
        max_gpu_util_pct=args.max_gpu_util_pct,
        max_decoder_util_pct=args.max_decoder_util_pct,
        min_gpu_memory_headroom_pct=args.min_gpu_memory_headroom_pct,
        max_forwarder_queue_depth=args.max_forwarder_queue_depth,
        evidence_count_delta=evidence_delta,
        min_evidence_events=args.min_evidence_events,
    )
    return checks, samples, evidence_delta


def build_report(
    checks: list[AcceptanceCheck],
    samples: list[PressureSample],
    *,
    evidence_count_delta: int | None,
) -> dict[str, Any]:
    return {
        "passed": all(check.ok for check in checks),
        "pass_token": PASS_TOKEN if all(check.ok for check in checks) else None,
        "checks": [
            {"name": check.name, "ok": check.ok, "detail": check.detail}
            for check in checks
        ],
        "sample_count": len(samples),
        "started_at_epoch_s": samples[0].observed_at_epoch_s if samples else None,
        "finished_at_epoch_s": samples[-1].observed_at_epoch_s if samples else None,
        "evidence_count_delta": evidence_count_delta,
    }


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--runtime-overview-url", default=DEFAULT_RUNTIME_OVERVIEW_URL)
    parser.add_argument("--runtime-timeout-s", type=float, default=2.0)
    parser.add_argument("--duration-s", type=int, default=DEFAULT_DURATION_S)
    parser.add_argument("--sample-interval-s", type=int, default=DEFAULT_SAMPLE_INTERVAL_S)
    parser.add_argument("--target-fps", type=float, default=DEFAULT_TARGET_FPS)
    parser.add_argument("--fps-tolerance", type=float, default=DEFAULT_FPS_TOLERANCE)
    parser.add_argument("--min-sources", type=int, default=DEFAULT_MIN_SOURCES)
    parser.add_argument("--max-gpu-util-pct", type=float, default=95.0)
    parser.add_argument("--max-decoder-util-pct", type=float, default=90.0)
    parser.add_argument("--min-gpu-memory-headroom-pct", type=float, default=10.0)
    parser.add_argument("--max-forwarder-queue-depth", type=int, default=256)
    parser.add_argument("--min-evidence-events", type=int, default=1)
    parser.add_argument("--evidence-root", type=Path, default=Path("/data/video-analytics/media/evidence"))
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--skip-readiness", action="store_true")
    parser.add_argument("--sources-config", type=Path, default=root / "infra/generated/sources.generated.yml")
    parser.add_argument("--compose-file", type=Path, default=root / "infra/docker-compose.midterm.yml")
    parser.add_argument("--replay-config", type=Path, default=root / "modules/savant_replay/config.midterm.json")
    parser.add_argument(
        "--phase1-doc",
        type=Path,
        default=root / "docs/repair_goal/midterm_phase1_analysis_forwarder_2026-06-15.md",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    checks, samples, evidence_delta = run_pressure(args)
    report = build_report(checks, samples, evidence_count_delta=evidence_delta)
    if args.report_path is not None:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("check=phase2_single_t4_pressure")
        for check in checks:
            prefix = "PASS" if check.ok else "FAIL"
            print(f"{prefix} {check.name} {check.detail}")
        if report["passed"]:
            print(PASS_TOKEN)
        else:
            print("PASS_PHASE2_SINGLE_T4_30=false")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
