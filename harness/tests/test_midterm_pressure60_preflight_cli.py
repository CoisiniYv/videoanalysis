"""Exercise the real script entry point with isolated command and kernel fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/runtime/run_midterm_pressure60.py"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(content))
    path.chmod(0o755)


def _fixture(tmp_path: Path, mode: str) -> tuple[list[str], dict[str, str], Path]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    startup = tmp_path / "startup"
    startup.mkdir()
    kernel = tmp_path / "kernel"
    sysfs = kernel / "sys"
    (sysfs / "dev/block").mkdir(parents=True)
    for device, name, rotational in (("8:1", "sda" if mode == "hdd" else "nvme0n1", "1" if mode == "hdd" else "0"),
                                     ("8:16", "sdb", "1")):
        node = sysfs / "devices" / name
        (node / "queue").mkdir(parents=True)
        (node / "queue/rotational").write_text(rotational + "\n")
        if mode != "unknown_device" or device != "8:1":
            (sysfs / "dev/block" / device).symlink_to(node, target_is_directory=True)
    virtual = "/preflight-test-fixture"
    storage = {
        virtual + "/cache": "8:1", virtual + "/materialized": "8:1",
        virtual + "/evidence": "8:16", "/data": "8:16", "/dev/shm": "0:42",
    }
    mountinfo = "\n".join(
        f"{index} 0 {device} / {path} rw - {'tmpfs' if path == '/dev/shm' else 'xfs'} fixture rw"
        for index, (path, device) in enumerate(storage.items(), 1)
    )
    # The startup shim changes only these exact virtual roots and kernel reads.
    startup_source = '''
        import os
        from pathlib import Path
        from types import SimpleNamespace

        storage = STORAGE
        original_stat = os.stat
        original_statvfs = os.statvfs
        original_resolve = Path.resolve
        original_read_text = Path.read_text

        def fixture_stat(path, *args, **kwargs):
            key = os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else None
            if key in storage:
                major, minor = map(int, storage[key].split(':'))
                return SimpleNamespace(st_dev=os.makedev(major, minor))
            return original_stat(path, *args, **kwargs)

        def fixture_statvfs(path):
            if os.fsdecode(path) in storage:
                return SimpleNamespace(f_blocks=1_000_000_000, f_frsize=4096, f_bavail=900_000_000)
            return original_statvfs(path)

        def fixture_resolve(path, *args, **kwargs):
            text = str(path)
            if text == COMPOSE_CRASH:
                raise RuntimeError('fixture compose path failure')
            if text.startswith('/sys/dev/block/') and text.rsplit('/', 1)[-1] in set(storage.values()):
                path = Path(SYSFS) / 'dev/block' / text.rsplit('/', 1)[-1]
            return original_resolve(path, *args, **kwargs)

        def fixture_read_text(path, *args, **kwargs):
            if str(path) == '/proc/self/mountinfo':
                return MOUNTINFO
            return original_read_text(path, *args, **kwargs)

        os.stat = fixture_stat
        os.statvfs = fixture_statvfs
        Path.resolve = fixture_resolve
        Path.read_text = fixture_read_text
    '''
    compose_crash = str(tmp_path / "compose-crash.yml") if mode == "setup_exception" else ""
    startup_source = textwrap.dedent(startup_source).replace("STORAGE", repr(storage)).replace(
        "SYSFS", repr(str(sysfs))).replace("MOUNTINFO", repr(mountinfo)).replace("COMPOSE_CRASH", repr(compose_crash))
    (startup / "sitecustomize.py").write_text(startup_source)

    artifact_root = tmp_path / "artifacts"
    module_path = str((artifact_root / "cli-test/module.pressure.yml").resolve())
    config = {"services": {}}
    for service in ("rolling-cache-sink-a", "rolling-cache-sink-b", "media-worker"):
        config["services"][service] = {"volumes": [
            {"type": "bind", "source": virtual + "/cache", "target": "/media/rolling-cache"},
        ]}
    config["services"]["media-worker"]["volumes"].append({
        "type": "bind", "source": virtual + "/materialized", "target": "/media/rolling-cache-materialized",
    })
    for service in ("replay-raw-fanout-a", "replay-raw-fanout-b"):
        config["services"][service] = {"environment": {
            "FORWARDER_MAX_QUEUE_AGE_MS": "30000",
            "FORWARDER_STRICT_FPS_BUDGET": "true",
            "FORWARDER_QUEUE_MAX_SIZE": "8192",
        }}
    for service in ("savant-a", "savant-b"):
        config["services"][service] = {"environment": {
            "FACE_INFER_INTERVAL": "7",
            "FACE_EMBEDDING_INFER_INTERVAL": "7",
            "SAVANT_MODULE_FILE": module_path,
            "FACE_ROI_EXPORT_ENABLED": "false",
            "FACE_OBSERVATION_EXPORT_ENABLED": "true",
            "ADAFACE_INPUT_OBJECT": "yolov8_face.face",
        }}
    config_path = tmp_path / "compose.json"
    config_path.write_text(json.dumps(config))
    command_log = tmp_path / "commands.jsonl"
    _write_executable(fakebin / "docker", '''
        import json, os, sys
        from pathlib import Path
        args = sys.argv[1:]
        with open(os.environ['PREFLIGHT_COMMAND_LOG'], 'a') as output:
            output.write(json.dumps(['docker', *args]) + '\\n')
        if args and args[0] == 'inspect':
            print(json.dumps([{'Config': {'Labels': {
                'com.docker.compose.project.working_dir': os.getcwd(),
                'com.docker.compose.project.config_files': os.path.join(os.getcwd(), 'infra/docker-compose.midterm.yml'),
            }}}]))
            raise SystemExit(0)
        if not args or args[0] != 'compose' or args[-3:] != ['config', '--format', 'json']:
            raise SystemExit(93)
        profiles = [args[index + 1] for index, value in enumerate(args[:-1]) if value == '--profile']
        if 'rolling-cache-dual' not in profiles or 'dual-4090-two-source' not in profiles:
            raise SystemExit(94)
        mode = os.environ['PREFLIGHT_CASE']
        if mode == 'compose_failure':
            print('fixture compose failure', file=sys.stderr)
            raise SystemExit(17)
        print('{invalid' if mode == 'compose_invalid_json' else Path(os.environ['PREFLIGHT_COMPOSE_JSON']).read_text())
    ''')
    _write_executable(fakebin / "ffprobe", '''
        import json, os, sys
        with open(os.environ['PREFLIGHT_COMMAND_LOG'], 'a') as output:
            output.write(json.dumps(['ffprobe', *sys.argv[1:]]) + '\\n')
        mode = os.environ['PREFLIGHT_CASE']
        if mode == 'ffprobe_failure':
            raise SystemExit(18)
        if mode == 'ffprobe_invalid_json':
            print('{invalid')
        else:
            print(json.dumps({} if mode == 'unknown_bitrate' else {'format': {'bit_rate': '8000000'}}))
    ''')
    argv = [sys.executable, str(SCRIPT), "--preflight-only", "--run-id", "cli-test",
            "--artifact-root", str(artifact_root), "--dual-shard-same-gpu", "--rolling-cache-evidence",
            "--streams", "60", "--duration-s", "3600", "--drain-s", "300",
            "--pressure-rolling-cache-retention-s", "600", "--evidence-policy-groups", "5:5",
            "--evidence-group-size", "60", "--pressure-algorithm-cooldown-s", "60",
            "--media-worker-materialization-max-active", "4", "--media-worker-finalizer-workers", "4",
            "--media-worker-finalizer-queue-capacity", "4", "--media-worker-rolling-remux-workers", "1",
            "--evidence-root", virtual + "/evidence", "--rtsp-republish-input-uri", "/fixture/movie.mp4"]
    if compose_crash:
        argv.extend(("--compose-file", compose_crash))
    environment = os.environ.copy()
    environment.update(PATH=str(fakebin) + os.pathsep + os.defpath, PYTHONPATH=str(startup),
                       PYTHONDONTWRITEBYTECODE="1", PREFLIGHT_COMMAND_LOG=str(command_log),
                       PREFLIGHT_COMPOSE_JSON=str(config_path), PREFLIGHT_CASE=mode)
    environment.pop("DATABASE_URL", None)
    environment.pop("VIDEO_ANALYTICS_DATABASE_URL", None)
    return argv, environment, artifact_root


@pytest.mark.parametrize("mode,reason", [
    ("nvme", None),
    ("hdd", "rotational rolling-cache storage"),
    ("unknown_device", "unresolved storage"),
    ("unknown_bitrate", "bitrate is unavailable"),
    ("compose_failure", "docker compose config failed: rc=17"),
    ("compose_invalid_json", "docker compose config returned invalid JSON"),
    ("ffprobe_failure", "ffprobe bitrate probe failed: rc=18"),
    ("ffprobe_invalid_json", "ffprobe bitrate probe returned invalid JSON"),
    ("setup_exception", "fixture compose path failure"),
])
def test_real_script_preflight_always_writes_result(tmp_path, mode, reason):
    argv, environment, artifacts = _fixture(tmp_path, mode)
    completed = subprocess.run(argv, cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30)
    assert completed.returncode == (0 if reason is None else 2), completed.stderr
    report = json.loads((artifacts / "cli-test/report.json").read_text())
    storage = report["rolling_cache_storage"]
    if reason is None:
        assert report["status"] == "preflight_passed"
        assert report["reason"] == storage["reason"] == "passed"
        assert storage["errors"] == []
        assert storage["paths"][0]["leaf_devices"] == ["nvme0n1"]
        assert storage["estimated_cache_bytes"] == 46_800_000_000
    else:
        assert report["status"] == "preflight_failed"
        assert reason in report["reason"]
        assert storage["status"] == "failed"
        assert storage["errors"]
        assert report["preflight_recovery"]["status"] == "unchanged"
    assert "Traceback" not in completed.stderr
    command_log = tmp_path / "commands.jsonl"
    commands = [json.loads(line) for line in command_log.read_text().splitlines()] if command_log.exists() else []
    expected_docker_calls = {
        # A passing dual-shard preflight renders both the pressure config and
        # the formal recovery config; the inspect call is the third Docker
        # invocation.
        "nvme": 3,
        # Storage and probe failures happen before recovery rendering.
        "hdd": 1,
        "unknown_device": 1,
        "unknown_bitrate": 1,
        "compose_failure": 1,
        "compose_invalid_json": 1,
        "ffprobe_failure": 1,
        "ffprobe_invalid_json": 1,
        "setup_exception": 0,
    }
    assert len([command for command in commands if command[0] == "docker"]) == expected_docker_calls[mode]
    assert all(
        command[0] == "ffprobe"
        or (command[0] == "docker" and (
            (len(command) > 1 and command[1] == "inspect")
            or command[-3:] == ["config", "--format", "json"]
        ))
        for command in commands
    )


def test_storage_override_opt_out_is_pure_only_and_has_no_side_effect(tmp_path):
    argv, environment, artifacts = _fixture(tmp_path, "nvme")
    argv.remove("--preflight-only")
    argv.append("--preflight-without-storage-override")
    completed = subprocess.run(argv, cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30)
    assert completed.returncode != 0
    assert "requires --preflight-only" in completed.stderr
    report = json.loads((artifacts / "cli-test/report.json").read_text())
    assert report["status"] == "preflight_failed"
    assert report["phase"] == "argument_validation"
    assert report["exit_code"] == completed.returncode == 2
    assert "requires --preflight-only" in report["reason"]
    assert not (tmp_path / "commands.jsonl").exists()


@pytest.mark.parametrize("extra,reason", [
    (["--media-worker-segment-index-io-concurrency", "0"], "must be positive"),
    (["--streams", "invalid"], "invalid int value"),
    (["--evidence-policy-groups", "bad"], "invalid evidence policy group"),
    (["--unknown-preflight-option"], "unrecognized arguments"),
])
def test_invalid_arguments_before_config_write_minimal_report(tmp_path, extra, reason):
    argv, environment, artifacts = _fixture(tmp_path, "nvme")
    completed = subprocess.run([*argv, *extra], cwd=ROOT, env=environment, text=True,
                               capture_output=True, timeout=30)
    assert completed.returncode == 2, completed.stderr
    report = json.loads((artifacts / "cli-test/report.json").read_text())
    assert report["status"] == "preflight_failed"
    assert report["phase"] == "argument_validation"
    assert report["exit_code"] == completed.returncode
    assert reason in report["reason"]
    assert report["preflight_recovery"]["status"] == "unchanged"
    assert "config" not in report
    assert not (artifacts / "cli-test/run_config.json").exists()
    assert not (tmp_path / "commands.jsonl").exists()
    assert "Traceback" not in completed.stderr


def test_pure_storage_override_opt_out_is_recorded_in_compose_files(tmp_path):
    argv, environment, artifacts = _fixture(tmp_path, "nvme")
    argv.append("--preflight-without-storage-override")
    completed = subprocess.run(argv, cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    report = json.loads((artifacts / "cli-test/report.json").read_text())
    assert report["compose_config"]["storage_override"] is None
    assert not any(path.endswith("midterm-storage.override.yml")
                   for path in report["rolling_cache_storage"]["compose"]["files"])


def test_guard_exception_identity_is_canonical():
    from scripts.runtime import pressure_storage_guard, rolling_cache_storage_preflight
    assert rolling_cache_storage_preflight.StoragePreflightError is pressure_storage_guard.StoragePreflightError
