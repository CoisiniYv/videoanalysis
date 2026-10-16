"""Stubbed-main probe: inject failures and record the actual restore call sequence.

usage: probe_main2.py ROOT ART SCENARIO_JSON
SCENARIO_JSON: {"fail": [{"name": fn, "when": "restore"|"run"|"any", "exc": "rt"|"kbint", "nth": 1}], "extra_args": [...]}
"""
import ast
import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(sys.argv[1])
ART = Path(sys.argv[2])
SCEN = json.loads(sys.argv[3])
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("pm", ROOT / "scripts/runtime/run_midterm_pressure60.py")
m = importlib.util.module_from_spec(spec)
sys.modules["pm"] = m
spec.loader.exec_module(m)

calls = []
counts = {}
tree = ast.parse((ROOT / "scripts/runtime/run_midterm_pressure60.py").read_text())
main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
names = {n.func.id for n in ast.walk(main) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
keep = {"replace", "write_json", "write_text", "_jsonable_config", "_prepare_pressure_run", "dict", "str", "int",
        "float", "bool", "len", "set", "list", "print", "repr", "isinstance", "Path", "datetime", "_env_bool",
        "sorted", "max", "min", "PressureArgumentError", "lambda", "_atomic_write_text", "preflight_failure_report",
        "compose_config_paths", "parse_args", "_default_run_id", "infer_interval_for_target_fps",
        "parse_evidence_policy_groups", "pressure_materialization_event_type_quotas", "PressureConfig", "Fraction",
        "restore_step", "restore_abort_steps"}
phase = {"v": "run"}


def make(nm):
    def stub(*a, **kw):
        art = str(kw.get("artifact_name", ""))
        is_restore = ("restore" in art) or nm in (
            "restore_runtime", "restore_dual_shard_runtime", "restore_cameras", "restore_worker_cpu_isolation",
            "restored_cache_readback", "stop_dual_shard_runtime", "restore_rolling_cache_sinks_after_pressure")
        counts[nm] = counts.get(nm, 0) + 1
        calls.append(nm + (f"[{art}]" if art else ""))
        for f in SCEN.get("fail", []):
            if f["name"] != nm:
                continue
            when = f.get("when", "any")
            if when == "restore" and not is_restore:
                continue
            if when == "run" and is_restore:
                continue
            if f.get("nth") and counts[nm] != f["nth"]:
                continue
            if f.get("exc") == "kbint":
                raise KeyboardInterrupt()
            raise RuntimeError(f"injected failure at {nm}")
        if nm == "perform_storage_preflight":
            return ({"status": "passed", "services": {}}, {"services": {}})
        if nm == "formal_restore_config":
            return a[0]
        if nm == "api_json":
            return {"data": {"saved_config": {"x": 1}}}
        if nm == "prepare_evidence_guard":
            return {"blocking_count": 1 if SCEN.get("blocked") else 0}
        if nm == "fetch_cameras":
            return [{"id": 1, "enabled": True}]
        if nm == "prepare_pressure_sampling_window":
            return {"before_sampling": {}, "after_prefill": {}, "sampling_start_event_ts_ms": 0,
                    "sampling_started_monotonic": 1.0}
        if nm == "sample_runtime":
            return {"ended_at": "2026-01-01T00:00:00+00:00"}
        if nm == "rolling_cache_postfill_after_sampling":
            return {"ended_at": "2026-01-01T00:00:01+00:00"}
        if nm == "pressure_failure_reasons":
            return []
        if nm == "pressure_warnings":
            return []
        if nm == "select_kept_evidence":
            return []
        if nm == "select_covered_event_ids_for_bundles":
            return set()
        if nm == "dual_shard_runtime_snapshot":
            return {"status": "captured", "services": [], "containers": {}}
        if nm == "person_trajectory_completeness_summary":
            return {"status": "passed"}
        if nm == "collect_downstream_observability":
            return {}
        return mock.MagicMock()
    return stub


for name in names - keep:
    obj = getattr(m, name, None)
    if callable(obj) and not isinstance(obj, type):
        setattr(m, name, make(name))

import errno
_orig_write_text = m.write_text
_orig_write_json = m.write_json
_full = {"v": False}
def _wt(path, data):
    if "failed_exception_traceback" in str(path):
        _full["v"] = True
    if _full["v"]:
        raise OSError(errno.ENOSPC, "No space left on device", str(path))
    return _orig_write_text(path, data)
def _wj(path, data):
    if _full["v"] and str(path).endswith("report.json"):
        # keep a side copy so the probe can read status
        _orig_write_json(path.with_name("report.json"), data)
        raise OSError(errno.ENOSPC, "No space left on device", str(path))
    return _orig_write_json(path, data)
m.write_text = _wt
m.write_json = _wj

m.psycopg = mock.MagicMock()
m.Redis = mock.MagicMock()
args = ["--artifact-root", str(ART), "--run-id", "probe", "--dual-shard-same-gpu", "--rolling-cache-evidence",
        "--cuda-mps", "--adaface-roi-redis", "--cpu-isolation-profile", "t4-16cpu-evidence",
        "--savant-ablation-stage", "full-evidence", "--force-runtime-restart", "--keep-evidence", "-1",
        *SCEN.get("extra_args", [])]
try:
    rc = m.main(args)
except BaseException as exc:  # noqa
    rc = f"raised {exc!r}"
report = json.loads((ART / "probe/report.json").read_text())
interesting = ("stop_dual_shard_runtime", "restore_cameras", "clear_pressure_evidence_task_gate",
               "configure_clip_worker_replay_shards", "configure_event_worker_evidence_admission",
               "configure_media_worker_rolling_cache", "restore_rolling_cache_sinks_after_pressure",
               "stop_rolling_cache_sinks_after_pressure", "restore_runtime", "read_cuda_mps_stop_summary",
               "restore_dual_shard_runtime", "remove_pressure_source_containers_until_stable",
               "restore_worker_cpu_isolation", "restored_cache_readback", "stop_cuda_mps",
               "cleanup_after_aborted_run", "stop_rtsp_republishers", "cleanup_pressure_runtime_only",
               "start_dual_shard_runtime", "sample_runtime", "apply_worker_cpu_isolation", "api_json",
               "apply_sources_only")
seq = [c for c in calls if c.split("[")[0] in interesting]
print("rc:", rc)
print("status:", report.get("status"))
print("restore_errors:", report.get("restore_errors"))
print("restore_steps:", report.get("restore_steps"))
print("sequence:")
for c in seq:
    print("   ", c)
