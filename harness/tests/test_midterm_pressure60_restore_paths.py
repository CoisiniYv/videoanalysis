"""Main-level recovery contracts for the pressure runner."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/runtime/run_midterm_pressure60.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("pressure_restore_paths", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _stub_main_dependencies(module, calls, fail_at=None, *, blocked=False):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    names = {
        node.func.id
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    keep = {
        "replace", "write_json", "write_text", "_jsonable_config", "_prepare_pressure_run",
        "dict", "str", "int", "float", "bool", "len", "set", "list", "print", "repr",
        "isinstance", "Path", "datetime", "_env_bool", "sorted", "max", "min",
        "PressureArgumentError", "lambda", "_atomic_write_text", "preflight_failure_report",
        "compose_config_paths", "parse_args", "_default_run_id", "infer_interval_for_target_fps",
        "parse_evidence_policy_groups", "pressure_materialization_event_type_quotas",
        "PressureConfig", "Fraction",
    }

    def make(name):
        def stub(*args, **kwargs):
            calls.append((name, kwargs.get("artifact_name", "")))
            is_restore = "restore" in str(kwargs.get("artifact_name", ""))
            if name == fail_at and (is_restore or name == "restore_runtime"):
                raise RuntimeError(f"injected failure at {name}")
            if name == "perform_storage_preflight":
                return ({"status": "passed", "services": {}}, {"services": {}})
            if name == "formal_restore_config":
                return args[0]
            if name == "api_json":
                return {"data": {"saved_config": {"x": 1}}}
            if name == "prepare_evidence_guard":
                return {"blocking_count": 1 if blocked else 0}
            if name == "fetch_cameras":
                return [{"id": 1, "enabled": True}]
            if name == "prepare_pressure_sampling_window":
                return {
                    "before_sampling": {},
                    "after_prefill": {},
                    "sampling_start_event_ts_ms": 0,
                    "sampling_started_monotonic": 1.0,
                }
            if name == "sample_runtime":
                return {"ended_at": "2026-01-01T00:00:00+00:00"}
            if name == "rolling_cache_postfill_after_sampling":
                return {"ended_at": "2026-01-01T00:00:01+00:00"}
            return mock.MagicMock()

        return stub

    for name in names - keep:
        obj = getattr(module, name, None)
        if callable(obj) and not isinstance(obj, type):
            setattr(module, name, make(name))
    module.psycopg = mock.MagicMock()
    module.Redis = mock.MagicMock()


@pytest.mark.parametrize(
    "fail_at",
    [
        "configure_clip_worker_replay_shards",
        "configure_media_worker_rolling_cache",
        "restore_rolling_cache_sinks_after_pressure",
    ],
)
def test_main_continues_recovery_after_three_independent_restore_failures(tmp_path, fail_at):
    module = _load_module()
    calls = []
    _stub_main_dependencies(module, calls, fail_at)

    rc = module.main(
        [
            "--artifact-root", str(tmp_path),
            "--run-id", "restore-probe",
            "--dual-shard-same-gpu",
            "--rolling-cache-evidence",
            "--cuda-mps",
            "--adaface-roi-redis",
            "--cpu-isolation-profile", "t4-16cpu-evidence",
            "--savant-ablation-stage", "full-evidence",
        ]
    )
    report = json.loads((tmp_path / "restore-probe/report.json").read_text(encoding="utf-8"))

    assert rc == 1
    assert report["status"] == "failed_restore_verification"
    restore_step = {
        "configure_clip_worker_replay_shards": "clip_worker_replay_shards_restore",
        "configure_media_worker_rolling_cache": "media_worker_rolling_cache_restore",
        "restore_rolling_cache_sinks_after_pressure": "rolling_cache_sinks_restore",
    }[fail_at]
    assert any(row["step"] == restore_step for row in report["restore_errors"])
    assert "restore_runtime" in report["restore_steps"]
    assert report["restore_steps"].index("stop_dual_shard_runtime") < report["restore_steps"].index("restore_cameras")
    assert report["restore_steps"].index("restore_runtime") > report["restore_steps"].index("restore_cameras")
    assert any(name == "restore_runtime" for name, _ in calls)


def test_blocked_active_evidence_restores_cpu_isolation_before_exit(tmp_path):
    module = _load_module()
    calls = []
    _stub_main_dependencies(module, calls, blocked=True)

    rc = module.main(
        [
            "--artifact-root", str(tmp_path),
            "--run-id", "blocked-probe",
            "--dual-shard-same-gpu",
            "--rolling-cache-evidence",
            "--cuda-mps",
            "--cpu-isolation-profile", "t4-16cpu-evidence",
        ]
    )
    report = json.loads((tmp_path / "blocked-probe/report.json").read_text(encoding="utf-8"))

    assert rc == 2
    assert report["status"] == "blocked_active_evidence"
    names = [name for name, _ in calls]
    assert "restore_worker_cpu_isolation" in names
    assert names.index("restore_worker_cpu_isolation") > names.index("apply_worker_cpu_isolation")
