"""A pressure-only single sink must restore an originally absent container as absent."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/runtime/run_midterm_pressure60.py"


def _load():
    spec = importlib.util.spec_from_file_location("pressure60_single_sink_restore", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_absent_single_sink_is_removed_or_left_absent(monkeypatch, tmp_path):
    module = _load()
    cfg = SimpleNamespace(
        dual_shard_same_gpu=False,
        artifact_dir=tmp_path,
        compose_file=str(tmp_path / "docker-compose.midterm.yml"),
        env_file=str(tmp_path / "midterm.env"),
        preflight_without_storage_override=False,
    )
    commands = []
    sink_state_calls = {"rolling-cache-sink": 0}

    def state(name):
        if name.endswith("rolling-cache-sink"):
            sink_state_calls["rolling-cache-sink"] += 1
            # An unexpected residual is removed on the first check, then absent
            # during the post-restore verification snapshot.
            if sink_state_calls["rolling-cache-sink"] == 1:
                return {"container": name, "exists": True, "running": False, "status": "exited"}
            return {"container": name, "exists": False, "running": False, "status": "missing"}
        return {"container": name, "exists": False, "running": False, "status": "missing"}

    def snapshot():
        return {
            service: state(f"video-analytics-midterm-{service}")
            for service in (
                "rolling-cache-sink",
                "rolling-cache-sink-a",
                "rolling-cache-sink-b",
            )
        }

    monkeypatch.setattr(module, "docker_container_state", state)
    monkeypatch.setattr(module, "rolling_cache_sink_state_snapshot", snapshot)
    monkeypatch.setattr(module, "docker_container_env", lambda _name: {})
    monkeypatch.setattr(module, "_compose_base_args", lambda _cfg: ["docker", "compose"])
    monkeypatch.setattr(module, "run", lambda command, *_args, **_kwargs: commands.append(command))
    monkeypatch.setattr(module, "write_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "write_json", lambda *_args, **_kwargs: None)

    result = module.restore_rolling_cache_sinks_after_pressure(
        cfg,
        original_states={
            "rolling-cache-sink": {
                "exists": False,
                "running": False,
                "environment": {},
            }
        },
        artifact_name="restore.log",
    )

    assert result["status"] == "passed"
    assert result["removed_absent"] == ["rolling-cache-sink"]
    assert ["docker", "rm", "-f", "video-analytics-midterm-rolling-cache-sink"] in commands
    assert not any(command[-1:] == ["rolling-cache-sink"] and "up" in command for command in commands)
