import importlib.util, sys, json, ast
from pathlib import Path
from unittest import mock

ROOT = Path(sys.argv[1])
ART = Path(sys.argv[2])
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("pm", ROOT / "scripts/runtime/run_midterm_pressure60.py")
m = importlib.util.module_from_spec(spec); sys.modules["pm"] = m; spec.loader.exec_module(m)

calls = []
# find all names called inside main
tree = ast.parse((ROOT / "scripts/runtime/run_midterm_pressure60.py").read_text())
main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
names = {n.func.id for n in ast.walk(main) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
keep = {"replace", "write_json", "write_text", "_jsonable_config", "_prepare_pressure_run", "dict", "str", "int", "float", "bool", "len", "set", "list", "print", "repr", "isinstance", "Path", "datetime", "_env_bool", "sorted", "max", "min", "PressureArgumentError", "lambda", "_atomic_write_text", "preflight_failure_report", "compose_config_paths", "parse_args", "_default_run_id", "infer_interval_for_target_fps", "parse_evidence_policy_groups", "pressure_materialization_event_type_quotas", "PressureConfig", "Fraction"}
for name in names - keep:
    obj = getattr(m, name, None)
    if callable(obj) and not isinstance(obj, type):
        def make(nm):
            def stub(*a, **kw):
                calls.append(nm)
                if nm == FAIL_AT:
                    raise RuntimeError("injected failure at " + nm)
                if nm == "configure_media_worker_rolling_cache" and "restore" in kw.get("artifact_name", "") and __import__("os").environ.get("MWFAIL"):
                    raise RuntimeError("media-worker restore readback mismatch")
                if nm == "perform_storage_preflight":
                    return ({"status": "passed", "services": {}}, {"services": {}})
                if nm == "formal_restore_config":
                    return a[0]
                if nm == "api_json":
                    return {"data": {"saved_config": {"x": 1}}}
                if nm == "prepare_evidence_guard":
                    return {"blocking_count": int(__import__("os").environ.get("BLOCK","0"))}
                if nm == "fetch_cameras":
                    return [{"id": 1, "enabled": True}]
                return mock.MagicMock()
            return stub
        setattr(m, name, make(name))
FAIL_AT = sys.argv[3]
m.psycopg = mock.MagicMock()
m.Redis = mock.MagicMock()
rc = m.main(["--artifact-root", str(ART), "--run-id", "probe", "--dual-shard-same-gpu", "--rolling-cache-evidence",
             "--cuda-mps", "--adaface-roi-redis", "--cpu-isolation-profile", "t4-16cpu-evidence",
             "--savant-ablation-stage", "full-evidence"])
report = json.loads((ART / "probe/report.json").read_text())
order = [c for c in calls if c in ("configure_clip_worker_replay_shards","configure_event_worker_evidence_admission","configure_media_worker_rolling_cache","restore_rolling_cache_sinks_after_pressure","restore_runtime","restore_worker_cpu_isolation","restored_cache_readback","restore_cameras","stop_cuda_mps","precreate_operator_dual_runtime_after_pressure")]
print("rc", rc, "status", report.get("status"))
print("restore-related calls:", order)
print("restore_runtime called:", "restore_runtime" in calls)
print("report keys mentioning restore:", [k for k in report if "restore" in k])
