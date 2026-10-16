"""Storage and logging contracts for pressure-run Compose commands."""

from __future__ import annotations

import ast
from dataclasses import replace
import json
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from test_midterm_pressure60_script import SCRIPT, _config, _load_module


COMPOSE_CALLERS = (
    "set_compose_operating_point",
    "stop_single_inference_runtime_for_dual_pressure",
    "start_dual_shard_runtime",
    "stop_dual_shard_runtime",
    "stop_legacy_replay_services_for_direct_rolling_cache",
    "recreate_dual_video_sinks_for_current_epoch",
    "restore_worker_cpu_isolation",
    "configure_media_worker_rolling_cache",
    "start_rolling_cache_sinks_for_pressure",
    "stop_rolling_cache_sinks_before_pressure_reconfigure",
    "restore_rolling_cache_sinks_after_pressure",
    "configure_event_worker_evidence_admission",
    "configure_clip_worker_replay_shards",
    "restore_runtime",
)


@pytest.fixture
def module():
    return _load_module()


@pytest.fixture
def cfg(module, tmp_path):
    infra = tmp_path / "infra"
    infra.mkdir()
    compose = infra / "docker-compose.midterm.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    storage = infra / "midterm-storage.override.yml"
    storage.write_text("services: {}\n", encoding="utf-8")
    env = infra / "midterm.env"
    env.write_text("", encoding="utf-8")
    return _config(
        module,
        compose_file=str(compose),
        env_file=str(env),
        artifact_dir=tmp_path / "artifacts",
        pressure_rolling_cache_host_root="/dev/shm/video-analytics-pressure/pressure60_test/rolling-cache",
        pressure_rolling_cache_materialized_host_root="/dev/shm/video-analytics-pressure/pressure60_test/materialized",
    )


def _compose_files(command):
    return [command[index + 1] for index, value in enumerate(command) if value == "-f"]


def test_compose_base_resolves_storage_and_report_paths(module, cfg):
    paths = module.compose_config_paths(cfg)
    assert paths == {
        "compose_file": str(Path(cfg.compose_file).resolve()),
        "env_file": str(Path(cfg.env_file).resolve()),
        "storage_override": str(Path(cfg.compose_file).with_name("midterm-storage.override.yml").resolve()),
        "profiles": ["rolling-cache"],
    }
    assert module._compose_base_args(cfg) == [
        "docker", "compose", "--env-file", paths["env_file"],
        "-f", paths["compose_file"], "-f", paths["storage_override"],
        "--profile", "rolling-cache",
    ]


def test_compose_base_omits_absent_storage(module, cfg):
    Path(cfg.compose_file).with_name("midterm-storage.override.yml").unlink()
    assert module.compose_config_paths(cfg)["storage_override"] is None
    assert _compose_files(module._compose_base_args(cfg)) == [str(Path(cfg.compose_file).resolve())]


@pytest.mark.parametrize("dual", [False, True])
def test_common_compose_invocation_profiles_match_render_and_up(module, cfg, monkeypatch, dual):
    cfg = replace(cfg, dual_shard_same_gpu=dual)
    expected = [module.DUAL_SHARD_PROFILE, "rolling-cache-dual"] if dual else ["rolling-cache"]
    assert module.compose_config_paths(cfg)["profiles"] == expected
    assert module._compose_base_args(cfg) == module.compose_invocation_args(cfg)
    common = module.compose_invocation_args(cfg, pressure_storage=True)
    commands = []

    def capture(command, *args, **kwargs):
        commands.append(command)
        raise RuntimeError("captured before service operations")

    monkeypatch.setattr(module, "run", capture)
    monkeypatch.setattr(module, "probe_video_input_fps", lambda *_args: {"fps": 24})
    with pytest.raises(RuntimeError, match="captured"):
        module.start_rolling_cache_sinks_for_pressure(cfg)
    command = commands[0]
    assert command[:len(common)] == common
    assert [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--profile"] == expected


@pytest.mark.parametrize("dual", [False, True])
def test_restore_config_render_includes_profile_gated_cache_services(module, cfg, monkeypatch, dual):
    cfg = replace(cfg, dual_shard_same_gpu=dual)
    services = {
        "media-worker": {"volumes": [
            {"type": "bind", "source": "/nvme/rolling", "target": "/media/rolling-cache"},
            {"type": "bind", "source": "/nvme/materialized", "target": "/media/rolling-cache-materialized"},
        ]},
        **{
            service: {"profiles": [profile], "volumes": [
                {"type": "bind", "source": "/nvme/rolling", "target": "/media/rolling-cache"},
            ]}
            for service, profile in (
                ("rolling-cache-sink", "rolling-cache"),
                ("rolling-cache-sink-a", "rolling-cache-dual"),
                ("rolling-cache-sink-b", "rolling-cache-dual"),
            )
        },
    }

    def profile_sensitive_docker(command):
        enabled = {command[index + 1] for index, value in enumerate(command[:-1]) if value == "--profile"}
        active = {name: spec for name, spec in services.items() if not spec.get("profiles") or "*" in enabled or enabled.intersection(spec["profiles"])}
        return subprocess.CompletedProcess(command, 0, json.dumps({"services": active}))

    assert set(json.loads(profile_sensitive_docker(["docker", "compose", "config"]).stdout)["services"]) == {"media-worker"}
    observed = []
    monkeypatch.setattr(module, "_readonly_command", profile_sensitive_docker)
    monkeypatch.setattr(module, "pressure_cache_readback", lambda _cfg, expected: observed.append(expected) or {"status": "passed"})
    assert module.restored_cache_readback(cfg) == {"status": "passed"}
    expected_sinks = {"rolling-cache-sink-a", "rolling-cache-sink-b"} if dual else {"rolling-cache-sink"}
    assert set(observed[0]) == {"media-worker", *expected_sinks}


def test_common_compose_invocation_deduplicates_extra_profiles(module, cfg):
    command = module.compose_invocation_args(cfg, additional_profiles=("rolling-cache", "*", "*"))
    assert [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--profile"] == ["rolling-cache", "*"]


def test_preflight_without_storage_override_keeps_shared_pressure_stack(module, cfg):
    cfg = replace(cfg, preflight_without_storage_override=True)
    paths = module.compose_config_paths(cfg)
    assert paths["storage_override"] is None
    files = _compose_files(module.compose_invocation_args(cfg, pressure_storage=True))
    assert files == [
        str(Path(cfg.compose_file).resolve()),
        str(cfg.artifact_dir / "compose.rolling-cache-pressure-storage.override.json"),
    ]


def test_profile_arguments_are_owned_only_by_common_invocation():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    owners = [
        function.name
        for function in tree.body if isinstance(function, ast.FunctionDef)
        if any(isinstance(node, ast.Constant) and node.value == "--profile" for node in ast.walk(function))
    ]
    assert owners == ["compose_invocation_args"]


@pytest.mark.parametrize("dual", [False, True])
def test_preflight_sink_and_media_up_share_ordered_files_and_profiles(module, cfg, monkeypatch, dual):
    cfg = replace(cfg, dual_shard_same_gpu=dual, rolling_cache_evidence=True)
    commands = []

    class Captured(Exception):
        pass

    def capture(command, *args, **kwargs):
        commands.append(command)
        raise Captured

    monkeypatch.setattr(module, "_readonly_command", capture)
    monkeypatch.setattr(module, "run", capture)
    monkeypatch.setattr(module, "probe_video_input_fps", lambda *_args: {"fps": 24})
    with pytest.raises(module.StoragePreflightError, match="Captured"):
        module.perform_storage_preflight(cfg)
    with pytest.raises(Captured):
        module.start_rolling_cache_sinks_for_pressure(cfg)
    with pytest.raises(Captured):
        module.configure_media_worker_rolling_cache(
            cfg, values={"ROLLING_CACHE_ENABLED": "true"}, artifact_name="media-pressure.log", pressure=True,
        )
    preflight, sink_up, media_up = commands
    shared_files = _compose_files(preflight)
    assert _compose_files(sink_up) == shared_files
    assert _compose_files(media_up)[:-1] == shared_files
    assert shared_files[:2] == _compose_files(module._compose_base_args(cfg))
    assert shared_files[-1] == str(cfg.artifact_dir / "compose.rolling-cache-pressure-storage.override.json")
    if dual:
        assert shared_files[2] == str(cfg.artifact_dir / "compose.dual-shard-same-gpu.override.yml")
    profiles = lambda command: [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--profile"]
    assert profiles(preflight) == profiles(sink_up) == profiles(media_up)
    environment_only = yaml.safe_load(Path(_compose_files(media_up)[-1]).read_text())
    assert environment_only == {"services": {"media-worker": {"environment": {"ROLLING_CACHE_ENABLED": "true"}}}}


@pytest.mark.parametrize("name", COMPOSE_CALLERS)
def test_every_compose_call_uses_shared_storage_arguments(name):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_compose_base_args"
        for node in ast.walk(function)
    ), name


def test_no_compose_up_can_build_images():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            literals = [item.value for item in node.elts if isinstance(item, ast.Constant)]
            if "up" in literals:
                assert "--no-build" in literals, f"up without --no-build at line {node.lineno}"


@pytest.mark.parametrize("pressure", [False, True])
def test_media_worker_tmpfs_is_pressure_only(module, cfg, monkeypatch, pressure):
    commands = []
    values = {"ROLLING_CACHE_ENABLED": "true"}
    monkeypatch.setattr(module, "run", lambda command, *args, **kwargs: commands.append(command))
    monkeypatch.setattr(module, "media_worker_rolling_cache_env_snapshot", lambda **kwargs: values)
    module.configure_media_worker_rolling_cache(
        cfg, values=values, artifact_name="worker.log", pressure=pressure,
    )
    override = yaml.safe_load((cfg.artifact_dir / "worker.override.yml").read_text(encoding="utf-8"))
    service = override["services"]["media-worker"]
    assert "volumes" not in service
    if pressure:
        storage = json.loads((cfg.artifact_dir / "compose.rolling-cache-pressure-storage.override.json").read_text())
        assert all("/dev/shm/" in volume["source"] for volume in storage["services"]["media-worker"]["volumes"])
    else:
        assert not (cfg.artifact_dir / "compose.rolling-cache-pressure-storage.override.json").exists()
    command = commands[0]
    assert _compose_files(command)[:2] == _compose_files(module._compose_base_args(cfg))
    assert "--no-build" in command


def test_sink_tmpfs_override_follows_storage(module, cfg, monkeypatch):
    class Captured(Exception):
        pass

    commands = []

    def capture(command, *args, **kwargs):
        commands.append(command)
        raise Captured

    monkeypatch.setattr(module, "run", capture)
    monkeypatch.setattr(module, "probe_video_input_fps", lambda *args: {"fps": 24})
    with pytest.raises(Captured):
        module.start_rolling_cache_sinks_for_pressure(cfg)
    files = _compose_files(commands[0])
    assert files[:2] == _compose_files(module._compose_base_args(cfg))
    assert files[2] == str(cfg.artifact_dir / "compose.rolling-cache-pressure-storage.override.json")
    assert "--no-build" in commands[0]


def test_sink_restore_does_not_reuse_pressure_override(module, cfg, monkeypatch):
    commands = []
    daily = dict(module.ROLLING_CACHE_SINK_COMPOSE_ENV_DEFAULTS)
    original = {"rolling-cache-sink": {"running": True, "status": "running", "environment": daily}}
    monkeypatch.setattr(module, "run", lambda command, *args, **kwargs: commands.append(command))
    monkeypatch.setattr(module, "rolling_cache_sink_state_snapshot", lambda: original)
    monkeypatch.setattr(module, "daily_rolling_cache_sink_env", lambda cfg: daily)
    monkeypatch.setattr(module, "docker_container_env", lambda name: daily)
    module.restore_rolling_cache_sinks_after_pressure(
        cfg, original_states=original, artifact_name="restore.log",
    )
    assert _compose_files(commands[0])[:2] == _compose_files(module._compose_base_args(cfg))
    override = yaml.safe_load(Path(_compose_files(commands[0])[-1]).read_text(encoding="utf-8"))
    assert "volumes" not in override["services"]["rolling-cache-sink"]
    assert "/dev/shm/" not in " ".join(commands[0])


def test_sink_snapshot_captures_original_environment(module, monkeypatch):
    monkeypatch.setattr(module, "docker_container_state", lambda name: {"exists": True, "running": True})
    monkeypatch.setattr(module, "docker_container_env", lambda name: {"RUNTIME_EPOCH_ID": name})
    snapshot = module.rolling_cache_sink_state_snapshot()
    assert len(snapshot) == 3
    for service, state in snapshot.items():
        assert state["environment"] == {"RUNTIME_EPOCH_ID": f"video-analytics-midterm-{service}"}


def test_media_worker_full_environment_retains_unselected_values(module, monkeypatch):
    original = {"ROLLING_CACHE_ENABLED": "true", "OTHER_ORIGINAL_VALUE": "preserved"}
    monkeypatch.setattr(module, "docker_container_env", lambda name: original)
    assert module.media_worker_rolling_cache_env_snapshot(full=True) == original
    assert "OTHER_ORIGINAL_VALUE" not in module.media_worker_rolling_cache_env_snapshot()


def test_sink_restore_preserves_all_original_environment_and_known_drift(module, cfg, monkeypatch):
    commands = []
    original_env = {
        "ROLLING_CACHE_RETENTION_SECONDS": "4020", "RUNTIME_EPOCH_ID": "previous-epoch",
        "ROLLING_CACHE_SOURCES": "pressure60_historical_00", "EXTRA_VALUE": "literal${VALUE}",
    }
    original = {"rolling-cache-sink": {"running": True, "environment": original_env}}
    monkeypatch.setattr(module, "run", lambda command, *args, **kwargs: commands.append(command))
    monkeypatch.setattr(module, "rolling_cache_sink_state_snapshot", lambda: original)
    monkeypatch.setattr(module, "docker_container_env", lambda name: dict(original_env))
    summary = module.restore_rolling_cache_sinks_after_pressure(cfg, original_states=original, artifact_name="restore.log")
    override = yaml.safe_load(Path(_compose_files(commands[0])[-1]).read_text(encoding="utf-8"))
    assert override["services"]["rolling-cache-sink"]["environment"] == {
        **original_env, "EXTRA_VALUE": "literal$${VALUE}",
    }
    assert summary["expected_env"] == {"rolling-cache-sink": original_env}
    assert summary["env_mismatches"] == {}


def test_sink_restore_rejects_missing_environment_snapshot_before_recreate(module, cfg, monkeypatch):
    commands = []
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: commands.append(args))
    with pytest.raises(RuntimeError, match="environment snapshot"):
        module.restore_rolling_cache_sinks_after_pressure(
            cfg, original_states={"rolling-cache-sink": {"running": True}}, artifact_name="restore.log",
        )
    assert commands == []


def _dual_inspect_row(name, *, running=False, status="created"):
    return {
        "Name": f"/{name}",
        "Image": "sha256:formal-image",
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/data/video-analytics/media",
                "Destination": "/media",
                "RW": True,
            }
        ],
        "Config": {
            "Env": ["ORIGINAL=value", "LITERAL=raw${VALUE}"],
            "Labels": {
                "com.docker.compose.project.config_files": "/formal/infra/docker-compose.midterm.yml",
                "com.docker.compose.service": name.removeprefix("video-analytics-midterm-"),
            },
        },
        "State": {"Running": running, "Status": status},
    }


def test_dual_runtime_snapshot_captures_env_image_mounts_labels_and_state(module, cfg, monkeypatch):
    cfg = replace(cfg, dual_shard_same_gpu=True, adaface_roi_redis=True)
    inspected = {}

    def inspect(name):
        inspected[name] = True
        return _dual_inspect_row(name)

    monkeypatch.setattr(module, "inspect_container", inspect)
    snapshot = module.dual_shard_runtime_snapshot(cfg)
    assert snapshot["services"] == [
        "savant-a",
        "savant-b",
        "replay-raw-fanout-a",
        "replay-raw-fanout-b",
        "adaface-roi-worker",
    ]
    assert set(inspected) == {
        f"video-analytics-midterm-{service}" for service in snapshot["services"]
    }
    row = snapshot["containers"]["savant-a"]
    assert row["environment"]["LITERAL"] == "raw${VALUE}"
    assert row["image"] == "sha256:formal-image"
    assert row["mounts"][0]["Destination"] == "/media"
    assert row["labels"]["com.docker.compose.service"] == "savant-a"
    assert row["running"] is False
    assert (cfg.artifact_dir / "dual_shard_runtime_before.json").exists()


def test_dual_runtime_restore_uses_formal_operator_storage_and_snapshot_env(
    module, cfg, tmp_path, monkeypatch
):
    formal = tmp_path / "formal" / "infra"
    formal.mkdir(parents=True)
    compose = formal / "docker-compose.midterm.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    (formal / "midterm-storage.override.yml").write_text("services: {}\n", encoding="utf-8")
    (formal / "operator-dual-runtime.override.yml").write_text("services: {}\n", encoding="utf-8")
    env = formal / "midterm.env"
    env.write_text("\n", encoding="utf-8")
    cfg = replace(
        cfg,
        compose_file=str(compose),
        env_file=str(env),
        dual_shard_same_gpu=True,
        artifact_dir=tmp_path / "artifacts",
    )
    services = module.dual_shard_operator_runtime_services(cfg)
    snapshot = {
        "status": "captured",
        "services": services,
        "containers": {
            service: {
                "service": service,
                "container": f"video-analytics-midterm-{service}",
                "environment": {"ORIGINAL": "value", "LITERAL": "raw${VALUE}"},
                "image": "sha256:formal-image",
                "mounts": [{"Type": "bind", "Source": "/data/video-analytics/media", "Destination": "/media", "RW": True}],
                "labels": {
                    "com.docker.compose.project.config_files": "/formal/infra/docker-compose.midterm.yml",
                    "com.docker.compose.service": service,
                },
                "running": service == "savant-a",
                "status": "running" if service == "savant-a" else "created",
            }
            for service in services
        },
    }
    commands = []
    monkeypatch.setattr(
        module,
        "run",
        lambda command, *args, **kwargs: commands.append(command)
        or subprocess.CompletedProcess(command, 0, ""),
    )
    monkeypatch.setattr(
        module,
        "inspect_container",
        lambda name: _dual_inspect_row(
            name,
            running=name.endswith("savant-a"),
            status="running" if name.endswith("savant-a") else "created",
        ),
    )
    result = module.restore_dual_shard_runtime(cfg, snapshot)
    assert result["status"] == "passed"
    assert len(commands) == 2
    stopped, running = commands
    assert "--no-start" in stopped and "-d" not in stopped
    assert "-d" in running and "--no-start" not in running
    for command in commands:
        files = _compose_files(command)
        assert files == [
            str(compose.resolve()),
            str((formal / "midterm-storage.override.yml").resolve()),
            str((formal / "operator-dual-runtime.override.yml").resolve()),
            str(cfg.artifact_dir / "compose_restore_dual_shard_runtime.environment.override.yml"),
        ]
        assert "--profile" in command
        assert "operator-dual-runtime" in command
    override = yaml.safe_load(Path(_compose_files(commands[0])[-1]).read_text())
    assert override["services"]["savant-a"]["environment"]["LITERAL"] == "raw$${VALUE}"
    assert set(result["recreated_stopped"]) == set(services) - {"savant-a"}
    assert result["restarted_running"] == ["savant-a"]


def test_dual_runtime_snapshot_records_absent_decoupled_extras_without_recreating_them(
    module, cfg, monkeypatch
):
    cfg = replace(cfg, dual_shard_same_gpu=True, adaface_decoupled=True)

    def inspect(name):
        if "adaface-forwarder" in name or "savant-adaface" in name:
            return {}
        return _dual_inspect_row(name)

    monkeypatch.setattr(module, "inspect_container", inspect)
    snapshot = module.dual_shard_runtime_snapshot(cfg)
    assert snapshot["containers"]["adaface-forwarder-a"]["exists"] is False
    assert snapshot["containers"]["savant-adaface-central"]["exists"] is False


def test_stop_dual_restore_does_not_load_pressure_override(module, cfg, monkeypatch):
    commands = []
    cfg.artifact_dir.mkdir(parents=True)
    (cfg.artifact_dir / "compose.dual-shard-same-gpu.override.yml").write_text("services: {}\n")
    monkeypatch.setattr(module, "run", lambda command, *args, **kwargs: commands.append(command))
    module.stop_dual_shard_runtime(cfg)
    assert _compose_files(commands[0]) == _compose_files(module._compose_base_args(cfg))


def test_run_logs_full_shell_quoted_command_on_first_line(module, tmp_path, monkeypatch):
    command = ["printf", "%s", "path with spaces", "$(not-executed)"]
    completed = subprocess.CompletedProcess(command, 0, "command output\n")
    monkeypatch.setattr(module, "_run_captured_subprocess", lambda *args, **kwargs: completed)
    log = tmp_path / "run.log"
    assert module.run(command, log) is completed
    assert log.read_text(encoding="utf-8").splitlines() == [
        "$ " + " ".join(shlex.quote(value) for value in command), "command output",
    ]


def test_run_writes_command_before_start_and_preserves_it_on_failure(module, tmp_path, monkeypatch):
    command = ["missing-command", "argument with spaces"]
    log = tmp_path / "failed.log"

    def fail(*args, **kwargs):
        assert log.read_text(encoding="utf-8").startswith("$ missing-command ")
        raise FileNotFoundError("missing-command")

    monkeypatch.setattr(module, "_run_captured_subprocess", fail)
    with pytest.raises(FileNotFoundError):
        module.run(command, log)
    assert log.read_text(encoding="utf-8").splitlines() == ["$ missing-command 'argument with spaces'"]


def test_run_timeout_still_starts_log_with_command(module, tmp_path, monkeypatch):
    command = ["docker", "logs", "example"]

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(command, 1, output=b"partial output\n")

    monkeypatch.setattr(module.subprocess, "run", timeout)
    log = tmp_path / "timeout.log"
    completed = module.run(command, log, timeout_s=1, check=False)
    assert completed.returncode == 124
    assert log.read_text(encoding="utf-8").startswith("$ docker logs example\npartial output\n")


def test_cpu_recreate_preserves_original_environment_image_and_stopped_state(module, cfg, monkeypatch):
    name = "video-analytics-midterm-media-worker"
    environment = {"ROLLING_CACHE_ENABLED": "true", "ROLLING_CACHE_RETENTION_SECONDS": "4020", "OTHER": "raw${VALUE}"}
    original = {"original_cpuset": "", "environment": environment, "image": "sha256:original", "running": False}
    commands = []
    monkeypatch.setattr(module, "run", lambda command, *args, **kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0, ""))
    monkeypatch.setattr(module, "docker_container_cpuset", lambda name: "")
    monkeypatch.setattr(module, "inspect_container", lambda name: {
        "Image": original["image"], "Config": {"Env": [f"{key}={value}" for key, value in environment.items()]},
        "State": {"Running": False},
    })
    result = module.restore_worker_cpu_isolation(cfg, {"containers": {name: original}})
    command = commands[0]
    assert "--no-start" in command and "-d" not in command
    document = yaml.safe_load(Path(_compose_files(command)[-1]).read_text(encoding="utf-8"))
    service = document["services"]["media-worker"]
    assert service == {"image": "sha256:original", "cpuset": "", "environment": {
        **environment, "OTHER": "raw$${VALUE}",
    }}
    assert result["containers"][name]["ok"] is True


def test_cpu_recreate_refuses_incomplete_snapshot_before_any_update(module, cfg, monkeypatch):
    mutations = []
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: mutations.append(args))
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: mutations.append(args))
    with pytest.raises(RuntimeError, match="snapshot"):
        module.restore_worker_cpu_isolation(cfg, {"containers": {
            "video-analytics-midterm-face-worker": {"original_cpuset": "1-2"},
            "video-analytics-midterm-media-worker": {"original_cpuset": ""},
        }})
    assert mutations == []


def test_cpu_apply_snapshots_all_workers_before_update_and_rolls_back_using_formal_cfg(module, cfg, tmp_path, monkeypatch):
    from dataclasses import replace

    cfg = replace(cfg, cpu_isolation_profile="t4-16cpu")
    restore_cfg = replace(cfg, compose_file=str(tmp_path / "formal" / "docker-compose.midterm.yml"))
    snapshots = []
    recovered = []

    def inspect(name):
        snapshots.append(name)
        return {"Image": "sha256:original", "Config": {"Env": ["ORIGINAL=value"]},
                "HostConfig": {"CpusetCpus": ""}, "State": {"Running": False}}

    def update(command, **kwargs):
        assert len(snapshots) == len(module.WORKER_CONTAINER_NAMES)
        return subprocess.CompletedProcess(command, 1, "update failed")

    monkeypatch.setattr(module.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(module, "inspect_container", inspect)
    monkeypatch.setattr(module.subprocess, "run", update)
    monkeypatch.setattr(module, "docker_container_cpuset", lambda name: "")
    monkeypatch.setattr(module, "restore_worker_cpu_isolation", lambda used_cfg, snapshot: recovered.append((used_cfg, snapshot)))
    with pytest.raises(RuntimeError, match="failed to apply"):
        module.apply_worker_cpu_isolation(cfg, restore_cfg=restore_cfg)
    assert recovered[0][0] is restore_cfg
    assert all(row["environment"] == {"ORIGINAL": "value"} for row in recovered[0][1]["containers"].values())
