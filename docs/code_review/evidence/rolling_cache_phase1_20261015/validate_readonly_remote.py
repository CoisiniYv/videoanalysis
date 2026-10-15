"""Run only the two authorized readonly CLI probes in an isolated checkout."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timedelta, timezone


ROOT = Path(sys.argv[1]).resolve()
INPUT = Path(sys.argv[2]).resolve()
KST = timezone(timedelta(hours=9))


def now():
    value = datetime.now(timezone.utc)
    return {"utc": value.isoformat(), "kst": value.astimezone(KST).isoformat()}


def snapshot():
    ids = subprocess.run([
        "docker", "ps", "-a", "--filter", "name=video-analytics-midterm-", "--format", "{{.ID}}"
    ], check=True, text=True, stdout=subprocess.PIPE).stdout.split()
    values = json.loads(subprocess.run([
        "docker", "inspect", *ids
    ], check=True, text=True, stdout=subprocess.PIPE).stdout)
    result = []
    for value in values:
        labels = value["Config"].get("Labels") or {}
        state = value["State"]
        result.append({
            "id": value["Id"], "name": value["Name"], "image": value["Image"],
            "state": {key: state.get(key) for key in (
                "Status", "Running", "Paused", "Restarting", "OOMKilled", "Dead", "Pid",
                "ExitCode", "Error", "StartedAt", "FinishedAt",
            )},
            "restart_count": value["RestartCount"],
            "mounts": sorted(value["Mounts"], key=lambda item: (
                item.get("Destination", ""), item.get("Source", ""), item.get("Type", "")
            )),
            "cpuset": value["HostConfig"]["CpusetCpus"],
            "environment_sha256": hashlib.sha256(
                json.dumps(sorted(value["Config"]["Env"])).encode()
            ).hexdigest(),
            "working_dir": labels.get("com.docker.compose.project.working_dir"),
            "config_files": labels.get("com.docker.compose.project.config_files"),
        })
    return sorted(result, key=lambda item: item["name"])


def save(record):
    (ROOT / "readonly_validation.json").write_text(json.dumps(record, indent=2) + "\n")


paths = [
    "scripts/runtime/run_midterm_pressure60.py", "scripts/runtime/pressure_storage_guard.py",
    "scripts/runtime/rolling_cache_storage_preflight.py", "scripts/runtime/pressure_runtime_readback.py",
    "scripts/runtime/pressure_baseline_guard.py", "scripts/tools/parse_video_file_sink_pressure.py",
    "libs/evidence_lifecycle/__init__.py", "libs/evidence_lifecycle/contract.py",
    "infra/docker-compose.midterm.yml", "infra/midterm-storage.override.yml", "infra/env/midterm.env",
]
record = {
    "started": now(), "python": sys.version,
    "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths},
    "input": {"path": str(INPUT), "sha256": hashlib.sha256(INPUT.read_bytes()).hexdigest()},
    "snapshot_scope": "Identity/image, stable State fields (not health-log samples), mounts, cpuset, Env digest, compose labels, restart count",
    "before": snapshot(), "rounds": [],
}
base = [
    sys.executable, "scripts/runtime/run_midterm_pressure60.py", "--preflight-only",
    "--compose-file", str(ROOT / "infra/docker-compose.midterm.yml"),
    "--env-file", str(ROOT / "infra/env/midterm.env"), "--artifact-root", str(ROOT / "artifacts"),
    "--dual-shard-same-gpu", "--rolling-cache-evidence", "--streams", "60", "--fps", "4/1",
    "--min-fps", "99/25", "--duration-s", "3600", "--drain-s", "300",
    "--pressure-rolling-cache-retention-s", "600", "--evidence-policy-groups", "5:5",
    "--evidence-group-size", "60", "--pressure-algorithm-cooldown-s", "60",
    "--media-worker-materialization-max-active", "4", "--media-worker-finalizer-workers", "4",
    "--media-worker-finalizer-queue-capacity", "4", "--media-worker-rolling-remux-workers", "1",
    "--face-infer-interval", "3", "--face-embedding-infer-interval", "3",
    "--analysis-max-queue-age-ms", "30000", "--adaface-roi-redis", "--savant-output-mode", "metadata-only",
    "--rtsp-republish-input-uri", str(INPUT),
]
try:
    for label, extra, expected_rc in (
        ("nvme", [], 0),
        ("rotational", ["--preflight-without-storage-override"], 2),
    ):
        run_id = ROOT.name.replace("-", "_") + "_" + label
        argv = [*base, "--run-id", run_id, *extra]
        environment = os.environ.copy()
        for key in (
            "DATABASE_URL", "VIDEO_ANALYTICS_DATABASE_URL", "COMPOSE_PROFILES", "COMPOSE_FILE",
            "ROLLING_CACHE_HOST_ROOT", "ROLLING_CACHE_MATERIALIZED_HOST_ROOT",
            "PRESSURE_ROLLING_CACHE_ROOT_HOST", "PRESSURE_ROLLING_CACHE_MATERIALIZED_ROOT_HOST",
        ):
            environment.pop(key, None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        row = {"label": label, "argv": argv, "cwd": str(ROOT), "started": now(), "expected_returncode": expected_rc}
        try:
            completed = subprocess.run(argv, cwd=str(ROOT), env=environment, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
            row.update(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
        except subprocess.TimeoutExpired as exc:
            row.update(returncode=124, error="CLI timed out after 180 seconds",
                       stdout=str(exc.stdout or ""), stderr=str(exc.stderr or ""))
        report_path = ROOT / "artifacts" / run_id / "report.json"
        row["report_path"] = str(report_path)
        if report_path.exists():
            row["report"] = json.loads(report_path.read_text())
        row["completed"] = now()
        report = row.get("report") or {}
        storage = report.get("rolling_cache_storage") or {}
        cache_paths = [entry for entry in storage.get("paths", [])
                       if set(entry.get("roles", [])).intersection(("rolling_cache", "materialized"))]
        if label == "nvme":
            medium_matches = len(cache_paths) == 2 and all(
                entry.get("rotational") is False and entry.get("path", "").startswith("/home/user/video-analytics-fast/")
                and any(name.startswith("nvme") for name in entry.get("leaf_devices", []))
                for entry in cache_paths
            )
            status_matches = report.get("status") == "preflight_passed"
        else:
            medium_matches = any(entry.get("rotational") is True and entry.get("path", "").startswith("/data/")
                                 for entry in cache_paths)
            status_matches = report.get("status") == "preflight_failed" and any(
                "rotational" in str(error) for error in storage.get("errors", [])
            )
        row["expectation_met"] = row["returncode"] == expected_rc and medium_matches and status_matches
        record["rounds"].append(row)
        save(record)
        if not row["expectation_met"]:
            record["stop_reason"] = "unexpected_cli_result"
            break
finally:
    record["after"] = snapshot()
    record["container_count"] = len(record["before"])
    record["containers_unchanged"] = record["before"] == record["after"]
    record["completed"] = now()
    save(record)
print(json.dumps({key: record.get(key) for key in (
    "started", "completed", "container_count", "containers_unchanged", "stop_reason"
)}, indent=2))
print(json.dumps([{key: row.get(key) for key in (
    "label", "returncode", "expected_returncode", "expectation_met", "report_path"
)} for row in record["rounds"]], indent=2))
