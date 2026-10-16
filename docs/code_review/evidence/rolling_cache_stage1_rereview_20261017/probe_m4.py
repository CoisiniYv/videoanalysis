"""Probe: --preflight-only --preflight-runtime-readback against uos157-like state.

savant-a/b and replay-raw-fanout-a/b are *exited* residual containers (10-13 run).
"""
import importlib.util, json, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(sys.argv[1])
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("runner_probe", ROOT / "scripts/runtime/run_midterm_pressure60.py")
m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)

calls = []
def fake_storage(cfg):
    # Use the real override writer for the rendered expectations.
    path = m.write_dual_shard_same_gpu_compose_override(cfg)
    import yaml
    class L(yaml.SafeLoader): pass
    L.add_multi_constructor('!', lambda loader, suffix, node: loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode) else loader.construct_scalar(node))
    doc = yaml.load(path.read_text(), Loader=L)
    return {"status": "passed"}, doc

residual = {
    "video-analytics-midterm-replay-raw-fanout-a": {"FORWARDER_MAX_QUEUE_AGE_MS": "30000", "FORWARDER_STRICT_FPS_BUDGET": "true", "FORWARDER_QUEUE_MAX_SIZE": "8192"},
    "video-analytics-midterm-replay-raw-fanout-b": {"FORWARDER_MAX_QUEUE_AGE_MS": "30000", "FORWARDER_STRICT_FPS_BUDGET": "true", "FORWARDER_QUEUE_MAX_SIZE": "8192"},
    "video-analytics-midterm-savant-a": {"FACE_INFER_INTERVAL": "3", "FACE_EMBEDDING_INFER_INTERVAL": "3"},
    "video-analytics-midterm-savant-b": {"FACE_INFER_INTERVAL": "3", "FACE_EMBEDDING_INFER_INTERVAL": "3"},
}
def fake_env(name):
    calls.append(("inspect-env", name)); return residual.get(name, {})
def fake_cmd(argv):
    calls.append(("cmd", argv))
    if argv[:2] == ["docker", "exec"]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=f"Error response from daemon: container {argv[2]} is not running")
    raise AssertionError(argv)

m.perform_storage_preflight = fake_storage
m.docker_container_env = fake_env
m._readonly_command = fake_cmd
def no_db(*a, **k): raise AssertionError("db opened")
m.psycopg.connect = no_db

art = Path(tempfile.mkdtemp())
rc = m.main([
    "--preflight-only", "--preflight-runtime-readback", "--artifact-root", str(art), "--run-id", "probe-m4",
    "--dual-shard-same-gpu", "--rolling-cache-evidence", "--adaface-roi-redis", "--cuda-mps",
    "--face-infer-interval", "3", "--face-embedding-infer-interval", "3", "--analysis-max-queue-age-ms", "30000",
    "--streams", "60", "--fps", "4/1", "--min-fps", "99/25", "--savant-output-mode", "metadata-only",
    "--cpu-isolation-profile", "t4-16cpu-evidence",
])
rep = json.loads((art / "probe-m4/report.json").read_text())
print("rc", rc, "status", rep["status"])
print("errors", rep.get("errors"))
print(json.dumps({k:v for k,v in rep.items() if k not in ("config",)}, indent=1, default=str)[:4000])

print("calls", [c[1] if c[0]=="inspect-env" else " ".join(c[1][:3]) for c in calls])
