"""Capture read-only CLI acceptance evidence; write only in the isolated tree."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timedelta, timezone


ROOT = Path("/tmp/pressure60-phase1-preflight-20261015")
KST = timezone(timedelta(hours=9))


def now():
    return {"utc": datetime.now(timezone.utc).isoformat(), "kst": datetime.now(KST).isoformat()}


def snapshot():
    completed = subprocess.run(["docker", "ps", "-a", "--filter", "name=video-analytics-midterm-", "--format", "{{.ID}}"],
                               check=True, text=True, stdout=subprocess.PIPE)
    ids = completed.stdout.split()
    data = json.loads(subprocess.run(["docker", "inspect", *ids], check=True, text=True, stdout=subprocess.PIPE).stdout)
    return sorted([{
        "id": value["Id"], "name": value["Name"], "image": value["Image"],
        "status": value["State"]["Status"], "started_at": value["State"]["StartedAt"],
        "finished_at": value["State"]["FinishedAt"], "restart_count": value["RestartCount"],
        "mounts": value["Mounts"], "cpuset": value["HostConfig"]["CpusetCpus"],
        "environment_sha256": hashlib.sha256(json.dumps(value["Config"]["Env"], sort_keys=True).encode()).hexdigest(),
    } for value in data], key=lambda value: value["name"])


def write(name, value):
    (ROOT / name).write_text(json.dumps(value, indent=2) + "\n")


source_paths = [
    "scripts/runtime/run_midterm_pressure60.py", "scripts/runtime/pressure_storage_guard.py",
    "scripts/runtime/pressure_storage_preflight.py", "scripts/runtime/pressure_runtime_readback.py",
    "scripts/runtime/pressure_baseline_guard.py", "scripts/tools/parse_video_file_sink_pressure.py",
    "libs/evidence_lifecycle/__init__.py", "libs/evidence_lifecycle/contract.py",
    "infra/docker-compose.midterm.yml", "infra/midterm-storage.override.yml",
    "infra/env/midterm.env", "modules/savant_security/module.yml",
]
record = {
    "started": now(), "python": sys.version,
    "source_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in source_paths},
    "input": {"source_uri": "rtsp://192.168.1.105:8554/live/1080movie",
              "sample_path": str(ROOT / "preflight-input.mp4"),
              "sample_sha256": hashlib.sha256((ROOT / "preflight-input.mp4").read_bytes()).hexdigest()},
    "before": snapshot(), "rounds": [],
}
probe = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(ROOT / "preflight-input.mp4")],
                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
record["input"]["ffprobe"] = json.loads(probe.stdout)
base = [sys.executable, "scripts/runtime/run_midterm_pressure60.py", "--preflight-only",
        "--compose-file", str(ROOT / "infra/docker-compose.midterm.yml"),
        "--env-file", str(ROOT / "infra/env/midterm.env"), "--artifact-root", str(ROOT / "artifacts"),
        "--dual-shard-same-gpu", "--rolling-cache-evidence", "--streams", "60", "--fps", "4/1",
        "--min-fps", "99/25", "--duration-s", "3600", "--drain-s", "300",
        "--pressure-rolling-cache-retention-s", "600", "--evidence-policy-groups", "5:5",
        "--evidence-group-size", "60", "--pressure-algorithm-cooldown-s", "60",
        "--media-worker-materialization-max-active", "4", "--media-worker-finalizer-workers", "4",
        "--media-worker-finalizer-queue-capacity", "4", "--media-worker-rolling-remux-workers", "1",
        "--face-infer-interval", "3", "--face-embedding-infer-interval", "3",
        "--analysis-max-queue-age-ms", "30000", "--adaface-roi-redis", "--savant-output-mode", "metadata-only"]
for label, input_uri, environment in (
    ("live_unknown_bitrate", record["input"]["source_uri"], {}),
    ("nvme", str(ROOT / "preflight-input.mp4"), {}),
    ("rotational", str(ROOT / "preflight-input.mp4"), {
        "ROLLING_CACHE_HOST_ROOT": "/data/video-analytics/media/rolling-cache",
        "ROLLING_CACHE_MATERIALIZED_HOST_ROOT": "/data/video-analytics/media/rolling-cache-materialized",
    }),
):
    run_id = "phase1_preflight_20261015_" + label
    argv = [*base, "--run-id", run_id, "--rtsp-republish-input-uri", input_uri]
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env.pop("VIDEO_ANALYTICS_DATABASE_URL", None)
    env.pop("ROLLING_CACHE_HOST_ROOT", None)
    env.pop("ROLLING_CACHE_MATERIALIZED_HOST_ROOT", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(environment)
    row = {"label": label, "command": argv, "cwd": str(ROOT), "process_environment": environment,
           "started": now()}
    try:
        completed = subprocess.run(argv, cwd=str(ROOT), env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=160)
        row.update(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
    except subprocess.TimeoutExpired:
        row.update(returncode=124, error="CLI timeout after 160 seconds")
    report_path = ROOT / "artifacts" / run_id / "report.json"
    if report_path.exists():
        row["report"] = json.loads(report_path.read_text())
    row["completed"] = now()
    record["rounds"].append(row)
    write("cli_validation.json", record)
record["after"] = snapshot()
record["formal_container_snapshot_unchanged"] = record["before"] == record["after"]
record["completed"] = now()
write("cli_validation.json", record)
print(json.dumps({"output": str(ROOT / "cli_validation.json"),
                  "formal_container_snapshot_unchanged": record["formal_container_snapshot_unchanged"],
                  "rounds": [{"label": row["label"], "returncode": row["returncode"],
                              "status": row.get("report", {}).get("status")} for row in record["rounds"]]}, indent=2))
