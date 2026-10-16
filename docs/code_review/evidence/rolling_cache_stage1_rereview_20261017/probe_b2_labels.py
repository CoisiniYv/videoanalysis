"""Probe: dual runtime restore verification with Compose-written labels.

Docker Compose stamps com.docker.compose.project.config_files / config-hash on
every container it (re)creates.  The snapshot comes from the residual 10-13
containers; the restore uses a different -f list, so the labels differ even
when env/image/mounts are restored exactly.
"""
import importlib.util, subprocess, sys, tempfile, json
from dataclasses import replace
from pathlib import Path

ROOT = Path(sys.argv[1]); sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("runner_b2", ROOT / "scripts/runtime/run_midterm_pressure60.py")
m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)

tmp = Path(tempfile.mkdtemp())
formal = tmp / "video-analytics/infra"; formal.mkdir(parents=True)
for name in ("docker-compose.midterm.yml", "midterm-storage.override.yml", "operator-dual-runtime.override.yml"):
    (formal / name).write_text("services: {}\n")
(formal / "env").mkdir(); (formal / "env/midterm.env").write_text("\n")
args, cfg = m._prepare_pressure_run(["--artifact-root", str(tmp / "art"), "--run-id", "roundA", "--dual-shard-same-gpu", "--rolling-cache-evidence", "--adaface-roi-redis"])
cfg = replace(cfg, compose_file=str(formal / "docker-compose.midterm.yml"), env_file=str(formal / "env/midterm.env"))
cfg.artifact_dir.mkdir(parents=True, exist_ok=True)

residual_files = f"{formal}/docker-compose.midterm.yml,/data/video-analytics/artifacts/pressure60_1013/compose.dual-shard-same-gpu.override.yml"
state = {}
def row(name, files, cfg_hash, running=False):
    return {"Name": "/" + name, "Image": "sha256:same", "Mounts": [{"Type": "bind", "Source": "/x", "Destination": "/x"}],
            "Config": {"Env": ["A=1"], "Labels": {"com.docker.compose.project": "video-analytics-midterm",
                        "com.docker.compose.service": name.split("midterm-")[1],
                        "com.docker.compose.project.config_files": files,
                        "com.docker.compose.config-hash": cfg_hash}},
            "State": {"Running": running, "Status": "exited"}}
for svc in m.dual_shard_operator_runtime_services(cfg):
    state[f"video-analytics-midterm-{svc}"] = row(f"video-analytics-midterm-{svc}", residual_files, "hash-1013")
m.inspect_container = lambda name: state.get(name, {})
snap = m.dual_shard_runtime_snapshot(cfg)

def fake_run(command, *a, **k):
    files = [command[i + 1] for i, v in enumerate(command[:-1]) if v == "-f"]
    for svc in command[command.index("--no-build") + 1:]:
        name = f"video-analytics-midterm-{svc}"
        state[name] = row(name, ",".join(files), "hash-restore", running="-d" in command)
        state[name]["State"]["Status"] = "created"
    return subprocess.CompletedProcess(command, 0, "")
m.run = fake_run
try:
    m.restore_dual_shard_runtime(cfg, snap)
    print("restore passed")
except RuntimeError as exc:
    summary = json.loads((cfg.artifact_dir / "compose_restore_dual_shard_runtime.json").read_text())
    print("restore FAILED; differing keys per service:", {s: sorted(d) for s, d in summary["differences"].items()})
