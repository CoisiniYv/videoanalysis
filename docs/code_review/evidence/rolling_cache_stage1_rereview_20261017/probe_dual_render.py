"""Render the exact dual-runtime restore compose stack with the real 10-13 snapshot env.

Uses the real restore_dual_shard_runtime() with `run` redirected to `docker compose config`,
and an inspect stub that returns the phase-0 full inspect (pre-run) and then a synthetic
"after" built from the rendered config, to show which comparisons can/can't pass.
"""
import copy, importlib.util, json, os, subprocess, sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(sys.argv[1]); OUT = Path(sys.argv[2]); OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("pm", ROOT / "scripts/runtime/run_midterm_pressure60.py")
m = importlib.util.module_from_spec(spec); sys.modules["pm"] = m; spec.loader.exec_module(m)

snap = json.load(open(ROOT / "docs/code_review/evidence/rolling_cache_phase0_20261015/snapshot/pre_repair_snapshot.json"))
by_name = {c["Name"].lstrip("/"): c for c in snap["containers"]}

args, cfg = m._prepare_pressure_run([
    "--artifact-root", str(OUT), "--run-id", "render", "--dual-shard-same-gpu", "--rolling-cache-evidence",
    "--cuda-mps", "--adaface-roi-redis", "--cpu-isolation-profile", "t4-16cpu-evidence",
    "--compose-file", str(ROOT / "infra/docker-compose.midterm.yml"),
])
cfg = replace(cfg, compose_file=str(ROOT / "infra/docker-compose.midterm.yml"),
              env_file=str(ROOT / "infra/env/midterm.env"), artifact_dir=OUT / "render")
cfg.artifact_dir.mkdir(parents=True, exist_ok=True)

# 1) Snapshot exactly as the script would, from the real phase-0 inspect docs.
m.inspect_container = lambda name: copy.deepcopy(by_name.get(name, {}))
before = m.dual_shard_runtime_snapshot(cfg)

# 2) Run restore with `run` -> `compose config --format json` (no daemon, no mutation).
rendered = {}
def fake_run(cmd, log_path, **kw):
    i = cmd.index("up")
    services = [c for c in cmd[i+1:] if not c.startswith("--") and c != "-d"]
    env = dict(os.environ)
    env.pop("SAVANT_B_MODEL_ROOT", None)
    cp = subprocess.run([*cmd[:i], "config", "--format", "json", *services], text=True, capture_output=True, env=env)
    if cp.returncode:
        print("compose config failed:", cp.stderr[:2000]); raise SystemExit(1)
    doc = json.loads(cp.stdout)
    for s in services:
        rendered[s] = doc["services"][s]
    rendered.setdefault("_files", [cmd[j+1] for j, v in enumerate(cmd) if v == "-f"])
    return subprocess.CompletedProcess(cmd, 0, "")
m.run = fake_run

# after-restore inspect: keep the before doc but overlay what compose would produce from rendered config
def after_inspect(name):
    svc = name.replace("video-analytics-midterm-", "")
    doc = copy.deepcopy(by_name.get(name, {}))
    r = rendered.get(svc)
    if not r: return doc
    # env as compose would set it: rendered environment (image env merged by docker - keep image keys from before)
    env = dict(e.split("=", 1) for e in doc["Config"]["Env"] if "=" in e)
    new_env = {k: ("" if v is None else str(v)) for k, v in (r.get("environment") or {}).items()}
    doc["Config"]["Env"] = [f"{k}={v}" for k, v in new_env.items()]
    # labels compose 2.27 would write on recreate
    labels = dict(doc["Config"]["Labels"])
    labels["com.docker.compose.project.config_files"] = ",".join(rendered["_files"])
    labels["com.docker.compose.replace"] = "<id-of-pressure-container>"
    labels["com.docker.compose.config-hash"] = "<new-hash>"
    doc["Config"]["Labels"] = labels
    doc["Mounts"] = [{"Type": "bind", "Source": v["source"], "Destination": v["target"],
                      "Mode": "" , "RW": not v.get("read_only", False), "Propagation": "rprivate"}
                     for v in r.get("volumes", []) if v.get("type") == "bind"]
    return doc

m.inspect_container = after_inspect
try:
    res = m.restore_dual_shard_runtime(cfg, before)
    print("restore status:", res["status"])
except RuntimeError as exc:
    print("restore raised:", str(exc)[:200], "...")
summary = json.load(open(cfg.artifact_dir / "compose_restore_dual_shard_runtime.json"))
print("compose files:", summary["compose_files"])
for svc, diff in summary["differences"].items():
    print("==", svc, "diff keys:", sorted(diff))
    if "labels" in diff:
        e, o = diff["labels"]["expected"], diff["labels"]["observed"]
        for k in sorted(set(e) | set(o)):
            if e.get(k) != o.get(k):
                print("    label", k, ":", e.get(k), "->", o.get(k))
    if "mounts" in diff:
        e = {(x["Source"], x["Destination"]) for x in diff["mounts"]["expected"]}
        o = {(x["Source"], x["Destination"]) for x in diff["mounts"]["observed"]}
        print("    mounts only-before:", sorted(e - o)); print("    mounts only-after:", sorted(o - e))
    if "environment" in diff:
        e, o = diff["environment"]["expected"], diff["environment"]["observed"]
        print("    env only-after:", sorted(set(o) - set(e))[:10], "only-before:", sorted(set(e) - set(o))[:10])
for svc in ("savant-a", "savant-b", "adaface-roi-worker", "replay-raw-fanout-a"):
    r = rendered[svc]
    hc_before = by_name[f"video-analytics-midterm-{svc}"]["HostConfig"]
    print(f"HostConfig {svc}: restart {hc_before['RestartPolicy']['Name']} -> {r.get('restart')}; "
          f"devices before {[d['DeviceIDs'] for d in (hc_before.get('DeviceRequests') or [])]} -> "
          f"{[d.get('device_ids') for d in (((r.get('deploy') or {}).get('resources') or {}).get('reservations') or {}).get('devices') or []]}; "
          f"depends_on -> {sorted((r.get('depends_on') or {}).keys())}")
