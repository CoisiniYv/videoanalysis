from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "runtime"
    / "check_phase2_single_t4_pressure.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase2_pressure", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase2_pressure"] = module
    spec.loader.exec_module(module)
    return module


phase2_pressure = _load_module()


def _overview(
    *,
    source_count: int = 30,
    fps: float = 8.0,
    frames: int = 1000,
    annotations: int = 1000,
    restart_count: int = 0,
    send_failures: int = 0,
    queue_depth: int = 0,
):
    sources = [
        {
            "source_id": f"cam_{index:02d}",
            "effective_fps": fps,
            "frames_seen_total": frames,
            "frame_annotations_exported_total": annotations,
        }
        for index in range(source_count)
    ]
    forwarder_sources = [
        {
            "source_id": f"cam_{index:02d}",
            "savant_send_failures_total": send_failures,
        }
        for index in range(source_count)
    ]
    dynamic_sources = [
        {
            "name": f"video-analytics-source-cam_{index:02d}",
            "restart_count": restart_count,
        }
        for index in range(source_count)
    ]
    return {
        "data": {
            "health": {"ok": True, "issues": [], "source_count": source_count},
            "metrics": {"sources": sources},
            "forwarder": {
                "available": True,
                "global": {"queue_depth": queue_depth},
                "sources": forwarder_sources,
            },
            "containers": {
                "fixed": {
                    "analysis_forwarder": {
                        "name": "video-analytics-midterm-analysis-forwarder",
                        "restart_count": restart_count,
                    },
                    "savant": {
                        "name": "video-analytics-midterm-savant",
                        "restart_count": restart_count,
                    },
                },
                "dynamic_sources": dynamic_sources,
            },
        }
    }


def _gpu(
    *,
    gpu_util_pct: float = 70,
    decoder_util_pct: float = 50,
    memory_used_mib: float = 8000,
    memory_total_mib: float = 16384,
):
    return [
        phase2_pressure.GpuSample(
            index="0",
            name="NVIDIA T4",
            gpu_util_pct=gpu_util_pct,
            decoder_util_pct=decoder_util_pct,
            memory_used_mib=memory_used_mib,
            memory_total_mib=memory_total_mib,
        )
    ]


def _sample(overview, gpu_samples=None, observed_at=1000.0):
    return phase2_pressure.build_pressure_sample(
        overview,
        gpu_samples if gpu_samples is not None else _gpu(),
        observed_at_epoch_s=observed_at,
    )


def _checks_by_name(checks):
    return {check.name: check for check in checks}


def test_phase2_pressure_accepts_stable_30_stream_run() -> None:
    checks = phase2_pressure.evaluate_pressure_run(
        [
            _sample(_overview(frames=1000, annotations=1000), observed_at=1000),
            _sample(_overview(frames=1300, annotations=1300), observed_at=1030),
        ],
        evidence_count_delta=1,
    )

    assert all(check.ok for check in checks)


def test_phase2_pressure_rejects_low_fps_and_restart_delta() -> None:
    checks = _checks_by_name(
        phase2_pressure.evaluate_pressure_run(
            [
                _sample(_overview(frames=1000, annotations=1000, restart_count=0), observed_at=1000),
                _sample(_overview(fps=5.0, frames=1300, annotations=1300, restart_count=1), observed_at=1030),
            ],
            evidence_count_delta=1,
        )
    )

    assert checks["effective_fps_sustained"].ok is False
    assert checks["restart_counts_flat"].ok is False


def test_phase2_pressure_rejects_missing_annotation_flow_and_evidence() -> None:
    checks = _checks_by_name(
        phase2_pressure.evaluate_pressure_run(
            [
                _sample(_overview(frames=1000, annotations=1000), observed_at=1000),
                _sample(_overview(frames=1300, annotations=1000), observed_at=1030),
            ],
            evidence_count_delta=0,
        )
    )

    assert checks["annotations_advancing"].ok is False
    assert checks["evidence_generated"].ok is False


def test_phase2_pressure_rejects_gpu_budget_overrun() -> None:
    checks = _checks_by_name(
        phase2_pressure.evaluate_pressure_run(
            [
                _sample(_overview(frames=1000, annotations=1000), _gpu(memory_used_mib=8000), observed_at=1000),
                _sample(
                    _overview(frames=1300, annotations=1300),
                    _gpu(gpu_util_pct=99, decoder_util_pct=95, memory_used_mib=15800),
                    observed_at=1030,
                ),
            ],
            evidence_count_delta=1,
        )
    )

    assert checks["gpu_budget"].ok is False


def test_parse_gpu_samples_handles_nvidia_smi_csv() -> None:
    samples = phase2_pressure.parse_gpu_samples("0, NVIDIA T4, 71, 42, 8000, 15360\n")

    assert samples == [
        phase2_pressure.GpuSample(
            index="0",
            name="NVIDIA T4",
            gpu_util_pct=71,
            decoder_util_pct=42,
            memory_used_mib=8000,
            memory_total_mib=15360,
        )
    ]


def test_run_pressure_preserves_readiness_pass_fail_status(monkeypatch) -> None:
    readiness_result = SimpleNamespace
    monkeypatch.setattr(phase2_pressure, "fetch_runtime_overview", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        phase2_pressure,
        "_run_readiness",
        lambda _args, _overview: [
            readiness_result(name="phase1_forwarder_passed", ok=True, detail="doc=phase1.md"),
            readiness_result(name="gpu_t4_count", ok=False, detail="actual=0 required>=1"),
        ],
    )
    args = SimpleNamespace(
        runtime_overview_url="http://127.0.0.1:8090/api/v1/runtime/overview",
        runtime_timeout_s=2.0,
        skip_readiness=False,
    )

    checks, samples, evidence_delta = phase2_pressure.run_pressure(args)
    by_name = _checks_by_name(checks)

    assert samples == []
    assert evidence_delta is None
    assert by_name["readiness:phase1_forwarder_passed"].ok is True
    assert by_name["readiness:gpu_t4_count"].ok is False
