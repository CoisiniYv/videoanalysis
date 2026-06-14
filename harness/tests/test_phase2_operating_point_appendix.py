from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "runtime"
    / "update_phase2_operating_point.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase2_appendix", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase2_appendix"] = module
    spec.loader.exec_module(module)
    return module


phase2_appendix = _load_module()


def _passing_report() -> dict:
    return {
        "passed": True,
        "pass_token": "PASS_PHASE2_SINGLE_T4_30",
        "evidence_count_delta": 3,
        "checks": [{"name": "effective_fps_sustained", "ok": True, "detail": "ok"}],
        "operating_point": {
            "gpu_names": ["NVIDIA T4"],
            "source_count": 30,
            "duration_seconds": 1800.5,
            "target_fps": 8.0,
            "fps_tolerance": 0.1,
            "per_source_effective_fps_avg_min": 7.9,
            "per_source_effective_fps_avg_mean": 8.05,
            "per_source_effective_fps_avg_max": 8.2,
            "max_decoder_util_pct": 55,
            "max_gpu_util_pct": 72,
            "memory_peak_mib": 9000,
            "min_gpu_memory_headroom_pct": 41.3,
        },
    }


def test_render_appendix_requires_passing_phase2_report(tmp_path: Path) -> None:
    report = _passing_report()
    report["passed"] = False
    report["checks"] = [{"name": "gpu_budget", "ok": False, "detail": "over"}]

    with pytest.raises(phase2_appendix.AppendixUpdateError, match="gpu_budget"):
        phase2_appendix.render_appendix(report, report_path=tmp_path / "report.json")


def test_update_appendix_text_replaces_existing_block(tmp_path: Path) -> None:
    spec_text = """# Spec

## Appendix A — Measured Operating Point (fill in Phase 2/3)

```text
T4 model / driver / CUDA:
old
```
"""
    appendix = phase2_appendix.render_appendix(
        _passing_report(),
        report_path=tmp_path / "phase2.json",
    )
    updated = phase2_appendix.update_appendix_text(spec_text, appendix)

    assert "NVIDIA T4" in updated
    assert "observed_avg_mean=8.050" in updated
    assert "evidence_count_delta=3" in updated
    assert "old" not in updated


def test_update_spec_writes_output_path(tmp_path: Path) -> None:
    report_path = tmp_path / "phase2.json"
    spec_path = tmp_path / "spec.md"
    output_path = tmp_path / "updated.md"
    report_path.write_text(json.dumps(_passing_report()), encoding="utf-8")
    spec_path.write_text(
        "## Appendix A — Measured Operating Point (fill in Phase 2/3)\n\n```text\nempty\n```\n",
        encoding="utf-8",
    )

    phase2_appendix.update_spec(
        report_path=report_path,
        spec_path=spec_path,
        output_path=output_path,
    )

    assert "source_count=30" in output_path.read_text(encoding="utf-8")
