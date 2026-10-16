import importlib.util, json, subprocess, sys
from dataclasses import replace
from pathlib import Path
ROOT = Path(sys.argv[1]); OUT = Path(sys.argv[2])
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("pm", ROOT / "scripts/runtime/run_midterm_pressure60.py")
m = importlib.util.module_from_spec(spec); sys.modules["pm"] = m; spec.loader.exec_module(m)
args, cfg = m._prepare_pressure_run(["--artifact-root", str(OUT), "--run-id", "m3", "--rolling-cache-evidence",
                                     "--compose-file", str(ROOT / "infra/docker-compose.midterm.yml")])
cfg = replace(cfg, compose_file=str(ROOT / "infra/docker-compose.midterm.yml"), env_file=str(ROOT / "infra/env/midterm.env"))
# real `docker compose config` render of the formal stack (no daemon needed)
# live binds: media-worker exists (NVMe binds), rolling-cache-sink was originally absent and stays absent
def resolve(names, dest):
    if names[0].endswith("rolling-cache-sink"):
        return None
    return Path({"/media/rolling-cache": "/home/user/video-analytics-fast/rolling-cache",
                 "/media/rolling-cache-materialized": "/home/user/video-analytics-fast/rolling-cache-materialized"}[dest])
m.resolve_container_bind_source = resolve
m.validate_cache_sources = lambda actual, expected: actual
try:
    print(m.restored_cache_readback(cfg))
except Exception as exc:
    print("restored_cache_readback raised:", type(exc).__name__, exc)
