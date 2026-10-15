"""Pure storage preflight may render files but must not change services or roots."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts/runtime/rolling_cache_storage_preflight.py"


def _load():
    spec = importlib.util.spec_from_file_location("rolling_cache_storage_preflight", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cfg(tmp_path, **overrides):
    values = dict(
        run_id="test", dual_shard_same_gpu=True, dual_shard_api=False,
        rolling_cache_evidence=True, duration_s=600, drain_s=120,
        pressure_rolling_cache_retention_s=100, stream_count=2,
        pressure_rolling_cache_host_root="", pressure_rolling_cache_materialized_host_root="",
        rtsp_republish_input_uri="/fixtures/movie.mp4", rtsp_uri="rtsp://camera/live",
        artifact_dir=tmp_path / "artifacts", evidence_root="/data/evidence",
        evidence_policy_groups=((2, 2), (5, 5)), behavior_rules=(),
        pressure_algorithm_cooldown_s=30, media_worker_materialization_max_active=4,
        media_worker_finalizer_queue_capacity=4, allow_rotational_rolling_cache=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _base(tmp_path):
    return ["docker", "compose", "--env-file", str(tmp_path / "midterm.env"),
            "-f", str(tmp_path / "docker-compose.midterm.yml"),
            "-f", str(tmp_path / "midterm-storage.override.yml")]


def _config(single=False):
    services = ["rolling-cache-sink"] if single else ["rolling-cache-sink-a", "rolling-cache-sink-b"]
    config = {"services": {
        service: {"volumes": [
            {"type": "bind", "source": "/fast/cache", "target": "/media/rolling-cache"},
            {"type": "bind", "source": "/fast/materialized", "target": "/media/rolling-cache-materialized"},
        ], "environment": {"SECRET_TOKEN": "never-persist"}}
        for service in [*services, "media-worker"]
    }}
    for service in ("replay-raw-fanout-a", "replay-raw-fanout-b"):
        config["services"][service] = {"environment": {
            "FORWARDER_MAX_QUEUE_AGE_MS": "30000", "FORWARDER_STRICT_FPS_BUDGET": "true",
            "FORWARDER_QUEUE_MAX_SIZE": "8192", "SECRET_TOKEN": "never-persist",
        }}
    return config


def _inspector(path):
    path = str(path)
    return {
        "path": path, "device": "259:3" if path.startswith("/fast") else path,
        "filesystem_type": "tmpfs" if path.startswith("/dev/shm") else "xfs",
        "rotational": False, "is_tmpfs": path.startswith("/dev/shm"),
        "total_bytes": 10_000_000_000, "available_bytes": 9_000_000_000,
        "device_chain": [], "leaf_devices": ["nvme0n1"],
    }


def _runner(commands, config):
    def run(command):
        commands.append(command)
        if command[0] == "ffprobe":
            result = {"format": {"bit_rate": "800"}}
        else:
            result = json.loads(json.dumps(config))
            for index, value in enumerate(command[:-1]):
                if value == "-f" and command[index + 1].endswith("compose.rolling-cache-pressure-storage.override.json"):
                    overlay = json.loads(Path(command[index + 1]).read_text())
                    for service, values in overlay["services"].items():
                        result["services"][service]["volumes"] = values["volumes"]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(result))
    return run


def test_preflight_uses_exact_base_and_records_projection_formulas(tmp_path):
    module = _load()
    commands = []
    base = _base(tmp_path)
    report, rendered = module.rolling_cache_storage_preflight(
        _cfg(tmp_path), compose_base_args=base, run_command=_runner(commands, _config()), inspector=_inspector,
    )
    assert report["status"] == "passed"
    assert commands[0] == [*base, "config", "--format", "json"]
    assert commands[1] == ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", "/fixtures/movie.mp4"]
    assert report["estimated_cache_bytes"] == 26_000
    assert report["estimated_materialized_bytes"] == 10_400
    assert report["estimated_evidence_bytes"] == 104_000
    assert report["space_estimates"]["materialized"]["formula"]
    assert rendered["services"]["media-worker"]["environment"]["SECRET_TOKEN"] == "never-persist"
    assert "never-persist" not in json.dumps(report)
    assert all("up" not in command and "run" not in command for command in commands)


def test_retention_override_uses_actual_retention_helper_value(tmp_path):
    module = _load()
    report, _ = module.rolling_cache_storage_preflight(
        _cfg(tmp_path), compose_base_args=_base(tmp_path),
        run_command=_runner([], _config()), inspector=_inspector, retention_s=4320,
    )
    assert report["retention_seconds"] == 4320


def test_default_retention_matches_duration_drain_plus_120_minimum_900(tmp_path):
    module = _load()
    report, _ = module.rolling_cache_storage_preflight(
        _cfg(tmp_path, pressure_rolling_cache_retention_s=0), compose_base_args=_base(tmp_path),
        run_command=_runner([], _config()), inspector=_inspector,
    )
    assert report["retention_seconds"] == 900


def test_tmpfs_overlay_is_last_and_has_both_mounts_for_every_service(tmp_path):
    module = _load()
    commands = []
    cache = "/dev/shm/video-analytics-pressure/test/cache"
    materialized = "/dev/shm/video-analytics-pressure/test/materialized"
    cfg = _cfg(tmp_path, pressure_rolling_cache_host_root=cache,
               pressure_rolling_cache_materialized_host_root=materialized)
    overlay_path = module.pressure_storage_override(cfg, ["rolling-cache-sink-a", "rolling-cache-sink-b"])
    report, rendered = module.rolling_cache_storage_preflight(
        cfg, compose_base_args=[*_base(tmp_path), "-f", str(tmp_path / "pressure.override.yml"),
                               "-f", str(overlay_path)],
        run_command=_runner(commands, _config()), inspector=_inspector,
    )
    args = commands[0]
    assert args[-5] == "-f"
    assert args[-4].endswith("compose.rolling-cache-pressure-storage.override.json")
    assert args.index(str(tmp_path / "pressure.override.yml")) < len(args) - 4
    overlay = json.loads(Path(args[-4]).read_text())
    for service in ("rolling-cache-sink-a", "rolling-cache-sink-b", "media-worker"):
        assert {volume["target"] for volume in overlay["services"][service]["volumes"]} == {
            "/media/rolling-cache", "/media/rolling-cache-materialized",
        }
    assert report["services"]["media-worker"]["/media/rolling-cache"] == cache
    assert not Path(cache).exists()
    assert not Path(materialized).exists()


@pytest.mark.parametrize("roots", [("/dev/shm/a", ""), ("/tmp/cache", "/tmp/materialized")])
def test_tmpfs_roots_must_be_paired_and_run_scoped(tmp_path, roots):
    module = _load()
    with pytest.raises(module.StoragePreflightError, match="together|run-scoped"):
        module.pressure_storage_override(
            _cfg(tmp_path, pressure_rolling_cache_host_root=roots[0], pressure_rolling_cache_materialized_host_root=roots[1]),
            ["rolling-cache-sink-a", "rolling-cache-sink-b"],
        )


def test_single_mode_selects_only_single_sink(tmp_path):
    module = _load()
    report, _ = module.rolling_cache_storage_preflight(
        _cfg(tmp_path, dual_shard_same_gpu=False), compose_base_args=_base(tmp_path),
        run_command=_runner([], _config(single=True)), inspector=_inspector,
    )
    assert set(report["services"]) == {"rolling-cache-sink", "media-worker"}


def test_input_falls_back_to_rtsp_and_redacts_credentials_in_report(tmp_path):
    module = _load()
    commands = []
    report, _ = module.rolling_cache_storage_preflight(
        _cfg(tmp_path, rtsp_republish_input_uri="", rtsp_uri="rtsp://alice:secret@host/live"),
        compose_base_args=_base(tmp_path), run_command=_runner(commands, _config()), inspector=_inspector,
    )
    assert commands[1][-1] == "rtsp://alice:secret@host/live"
    assert "alice" not in json.dumps(report)
    assert "secret" not in json.dumps(report)


def test_behavior_rules_increase_evidence_projection(tmp_path):
    module = _load()
    report, _ = module.rolling_cache_storage_preflight(
        _cfg(tmp_path, behavior_rules=("fall", "crowd_gathering", "chasing")), compose_base_args=_base(tmp_path),
        run_command=_runner([], _config()), inspector=_inspector,
    )
    assert report["space_estimates"]["evidence"]["event_type_count"] == 5
    assert report["estimated_evidence_bytes"] == 260_000


def test_compose_failure_preserves_diagnostics_without_probing_input(tmp_path):
    module = _load()
    commands = []
    def run(command):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="invalid config")
    with pytest.raises(module.StoragePreflightError, match="compose") as error:
        module.rolling_cache_storage_preflight(_cfg(tmp_path), compose_base_args=_base(tmp_path), run_command=run, inspector=_inspector)
    assert len(commands) == 1
    assert error.value.report["compose"]["returncode"] == 1


def test_unknown_bitrate_is_failure(tmp_path):
    module = _load()
    runner = _runner([], _config())
    def run(command):
        return subprocess.CompletedProcess(command, 0, stdout="{}") if command[0] == "ffprobe" else runner(command)
    with pytest.raises(module.StoragePreflightError, match="bitrate"):
        module.rolling_cache_storage_preflight(_cfg(tmp_path), compose_base_args=_base(tmp_path), run_command=run, inspector=_inspector)


def test_failed_rotational_guard_keeps_compose_and_budget_evidence(tmp_path):
    module = _load()
    def inspect(path):
        return {**_inspector(path), "rotational": True}
    with pytest.raises(module.StoragePreflightError, match="rotational") as error:
        module.rolling_cache_storage_preflight(_cfg(tmp_path), compose_base_args=_base(tmp_path), run_command=_runner([], _config()), inspector=inspect)
    assert error.value.report["compose"]["command"]
    assert error.value.report["space_estimates"]["evidence"]


def test_preflight_rotational_override_is_forwarded(tmp_path):
    module = _load()
    report, _ = module.rolling_cache_storage_preflight(
        _cfg(tmp_path, allow_rotational_rolling_cache=True), compose_base_args=_base(tmp_path),
        run_command=_runner([], _config()), inspector=lambda path: {**_inspector(path), "rotational": True},
    )
    assert report["allow_rotational"] is True


def test_unbounded_cooldown_cannot_produce_a_space_estimate(tmp_path):
    module = _load()
    with pytest.raises(module.StoragePreflightError, match="cooldown"):
        module.rolling_cache_storage_preflight(
            _cfg(tmp_path, pressure_algorithm_cooldown_s=0), compose_base_args=_base(tmp_path),
            run_command=_runner([], _config()), inspector=_inspector,
        )


def test_probe_timeout_preserves_report_and_redacts_exception_command(tmp_path):
    module = _load()
    runner = _runner([], _config())
    def run(command):
        if command[0] == "ffprobe":
            raise subprocess.TimeoutExpired(command, 30)
        return runner(command)
    with pytest.raises(module.StoragePreflightError, match="TimeoutExpired") as error:
        module.rolling_cache_storage_preflight(
            _cfg(tmp_path, rtsp_republish_input_uri="rtsp://alice:secret@host/live"),
            compose_base_args=_base(tmp_path), run_command=run, inspector=_inspector,
        )
    assert error.value.report["input"]["timeout_seconds"] == 30
    assert "alice" not in json.dumps(error.value.report)
    assert "secret" not in json.dumps(error.value.report)


def test_invalid_format_bitrate_records_stream_sum_fallback(tmp_path):
    module = _load()
    runner = _runner([], _config())
    def run(command):
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
                "format": {"bit_rate": "invalid"}, "streams": [{"bit_rate": "800"}],
            }))
        return runner(command)
    report, _ = module.rolling_cache_storage_preflight(
        _cfg(tmp_path), compose_base_args=_base(tmp_path), run_command=run, inspector=_inspector,
    )
    assert report["input"]["bitrate_source"] == "sum(streams[].bit_rate)"
