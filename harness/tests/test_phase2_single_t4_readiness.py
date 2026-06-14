from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "runtime"
    / "check_phase2_single_t4_readiness.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase2_readiness", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase2_readiness"] = module
    spec.loader.exec_module(module)
    return module


phase2_readiness = _load_module()


def _write_inputs(tmp_path: Path, *, source_count: int = 30, replay_url: str | None = None) -> dict[str, Path]:
    sources = {
        f"cam_{index:02d}": {
            "source_id": f"cam_{index:02d}",
            "enabled": True,
            "adapter_type": "gstreamer",
            "uri": f"rtsp://example.local/cam_{index:02d}",
        }
        for index in range(source_count)
    }
    paths = {
        "sources": tmp_path / "sources.generated.yml",
        "compose": tmp_path / "docker-compose.yml",
        "replay": tmp_path / "config.midterm.json",
        "phase1": tmp_path / "phase1.md",
    }
    paths["sources"].write_text(yaml.safe_dump({"sources": sources}), encoding="utf-8")
    paths["compose"].write_text(
        yaml.safe_dump(
            {
                "services": {
                    "analysis-forwarder": {
                        "environment": {
                            "FORWARDER_OUT_ENDPOINT": "dealer+connect:tcp://savant-security:5557"
                        }
                    },
                    "savant-security": {"depends_on": {"analysis-forwarder": {"condition": "service_started"}}},
                    "source-adapter": {
                        "environment": {"ZMQ_ENDPOINT": "dealer+connect:tcp://replay-service:5555"}
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    paths["replay"].write_text(
        json.dumps(
            {
                "out_stream": {
                    "url": replay_url or "dealer+connect:tcp://analysis-forwarder:5557"
                }
            }
        ),
        encoding="utf-8",
    )
    paths["phase1"].write_text("`PASS_PHASE1_FORWARDER`\n", encoding="utf-8")
    return paths


def _results_by_name(results):
    return {result.name: result for result in results}


def test_phase2_readiness_passes_for_single_t4_and_30_sources(tmp_path: Path) -> None:
    paths = _write_inputs(tmp_path, source_count=30)

    results = phase2_readiness.evaluate_phase2_readiness(
        root=tmp_path,
        sources_config=paths["sources"],
        compose_file=paths["compose"],
        replay_config=paths["replay"],
        phase1_doc=paths["phase1"],
        gpu_names=["NVIDIA T4"],
    )

    assert all(result.ok for result in results)


def test_phase2_readiness_blocks_non_t4_and_underfilled_sources(tmp_path: Path) -> None:
    paths = _write_inputs(tmp_path, source_count=2)

    results = _results_by_name(
        phase2_readiness.evaluate_phase2_readiness(
            root=tmp_path,
            sources_config=paths["sources"],
            compose_file=paths["compose"],
            replay_config=paths["replay"],
            phase1_doc=paths["phase1"],
            gpu_names=["NVIDIA GeForce RTX 4090"],
        )
    )

    assert results["enabled_rtsp_sources"].ok is False
    assert results["gpu_t4_count"].ok is False


def test_phase2_readiness_requires_forwarder_topology(tmp_path: Path) -> None:
    paths = _write_inputs(
        tmp_path,
        source_count=30,
        replay_url="dealer+connect:tcp://savant-security:5557",
    )

    results = _results_by_name(
        phase2_readiness.evaluate_phase2_readiness(
            root=tmp_path,
            sources_config=paths["sources"],
            compose_file=paths["compose"],
            replay_config=paths["replay"],
            phase1_doc=paths["phase1"],
            gpu_names=["NVIDIA T4"],
        )
    )

    assert results["replay_out_stream_targets_forwarder"].ok is False


def test_phase2_runtime_checks_require_30_runtime_sources() -> None:
    checks = _results_by_name(
        phase2_readiness.runtime_checks(
            {
                "data": {
                    "health": {"ok": True, "issues": [], "source_count": 2},
                    "forwarder": {"available": True},
                    "containers": {
                        "fixed": {
                            "analysis_forwarder": {
                                "running": True,
                                "health": "healthy",
                            }
                        }
                    },
                }
            },
            min_sources=30,
        )
    )

    assert checks["runtime_health_ok"].ok is True
    assert checks["runtime_source_count"].ok is False
    assert checks["runtime_forwarder_metrics_available"].ok is True
    assert checks["runtime_forwarder_container_running"].ok is True
