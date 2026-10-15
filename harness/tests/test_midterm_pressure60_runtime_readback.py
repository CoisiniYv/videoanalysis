"""Actual-runtime readback must identify the active sampling path and fail closed."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts/runtime/pressure_runtime_readback.py"
KEYS = ("FORWARDER_MAX_QUEUE_AGE_MS", "FORWARDER_STRICT_FPS_BUDGET", "FORWARDER_QUEUE_MAX_SIZE")
ENV = dict(zip(KEYS, ("30000", "true", "8192")))


def _load():
    spec = importlib.util.spec_from_file_location("pressure_runtime_readback", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cfg(**kwargs):
    values = dict(rolling_cache_evidence=True, dual_shard_same_gpu=True, dual_shard_api=False,
                  analysis_max_queue_age_ms=30000, face_infer_interval=3,
                  face_embedding_infer_interval=3, adaface_roi_redis=False)
    values.update(kwargs)
    return SimpleNamespace(**values)


def _setup(tmp_path, services=("replay-raw-fanout-a", "replay-raw-fanout-b")):
    root = tmp_path / "services/analysis-forwarder/app"
    root.mkdir(parents=True)
    hashes = {}
    for name in ("main.py", "queueing.py", "sampler.py"):
        data = f"# {name}\n".encode()
        (root / name).write_bytes(data)
        hashes[f"/app/app/{name}"] = hashlib.sha256(data).hexdigest()
    expected = {name: dict(ENV) for name in services}
    commands = []
    observed_containers = []
    def docker_env(name):
        observed_containers.append(name)
        return dict(ENV) if "forwarder" in name or "fanout" in name else {
            "FACE_INFER_INTERVAL": "3", "FACE_EMBEDDING_INFER_INTERVAL": "3",
        }
    def run_command(command):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="".join(f"{digest}  {path}\n" for path, digest in hashes.items()))
    return expected, commands, observed_containers, docker_env, run_command


def _collect(module, tmp_path, **kwargs):
    expected, commands, observed, docker_env, run_command = _setup(tmp_path)
    values = dict(cfg=_cfg(), expected_forwarder_env=expected, docker_env=docker_env,
                  run_command=run_command, source_root=tmp_path)
    values.update(kwargs)
    return module.collect_runtime_readback(**values), commands, observed


def test_direct_path_reads_fanout_and_savant_both_shards(tmp_path):
    module = _load()
    report, commands, observed = _collect(module, tmp_path)
    assert report["status"] == "passed"
    assert report["mode"] == "direct_rolling_cache"
    assert set(report["forwarders"]) == {"replay-raw-fanout-a", "replay-raw-fanout-b"}
    assert set(report["savant"]) == {"savant-a", "savant-b"}
    assert len(observed) == 4
    assert all(command[:2] == ["docker", "exec"] for command in commands)
    assert all(command[3] == "sha256sum" for command in commands)
    assert all(record["code"]["status"] == "matched" for record in report["forwarders"].values())
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("dual_api", [False, True])
def test_dual_non_direct_reads_analysis_forwarders(tmp_path, dual_api):
    module = _load()
    expected, commands, observed, env, run = _setup(tmp_path, ("analysis-forwarder-a", "analysis-forwarder-b"))
    report = module.collect_runtime_readback(
        _cfg(rolling_cache_evidence=False, dual_shard_same_gpu=not dual_api, dual_shard_api=dual_api),
        expected_forwarder_env=expected, docker_env=env, run_command=run, source_root=tmp_path,
    )
    assert set(report["forwarders"]) == {"analysis-forwarder-a", "analysis-forwarder-b"}
    assert not any("fanout" in name for name in observed)


def test_single_mode_reads_single_forwarder_and_savant(tmp_path):
    module = _load()
    expected, commands, observed, env, run = _setup(tmp_path, ("analysis-forwarder",))
    report = module.collect_runtime_readback(
        _cfg(rolling_cache_evidence=False, dual_shard_same_gpu=False),
        expected_forwarder_env=expected, docker_env=env, run_command=run, source_root=tmp_path,
    )
    assert set(report["forwarders"]) == {"analysis-forwarder"}
    assert set(report["savant"]) == {"savant"}


def test_forwarder_value_mismatch_fails_and_preserves_observed(tmp_path):
    module = _load()
    def env(name):
        return {**ENV, "FORWARDER_QUEUE_MAX_SIZE": "2048"} if "fanout" in name else {
            "FACE_INFER_INTERVAL": "3", "FACE_EMBEDDING_INFER_INTERVAL": "3",
        }
    with pytest.raises(module.RuntimeReadbackError, match="mismatch") as error:
        _collect(module, tmp_path, docker_env=env)
    assert error.value.report["forwarders"]["replay-raw-fanout-a"]["observed_env"]["FORWARDER_QUEUE_MAX_SIZE"] == "2048"


def test_expected_values_must_include_every_required_key(tmp_path):
    module = _load()
    with pytest.raises(module.RuntimeReadbackError, match="expected"):
        _collect(module, tmp_path, expected_forwarder_env={"replay-raw-fanout-a": {}})


def test_rendered_queue_age_must_agree_with_request(tmp_path):
    module = _load()
    with pytest.raises(module.RuntimeReadbackError, match="request"):
        _collect(module, tmp_path, cfg=_cfg(analysis_max_queue_age_ms=10000))


def test_code_mismatch_stops_even_with_matching_env(tmp_path):
    module = _load()
    def run(command):
        return subprocess.CompletedProcess(command, 0, stdout="".join(f"{'0' * 64}  {path}\n" for path in command[4:]))
    with pytest.raises(module.RuntimeReadbackError, match="code") as error:
        _collect(module, tmp_path, run_command=run)
    assert error.value.report["forwarders"]["replay-raw-fanout-a"]["code"]["status"] == "mismatch"


def test_exec_failure_cannot_silently_skip_code_check(tmp_path):
    module = _load()
    with pytest.raises(module.RuntimeReadbackError, match="exec"):
        _collect(module, tmp_path, run_command=lambda command: subprocess.CompletedProcess(command, 127, stdout="sha256sum missing"))


def test_incomplete_code_hash_output_fails(tmp_path):
    module = _load()
    with pytest.raises(module.RuntimeReadbackError, match="hash"):
        _collect(module, tmp_path, run_command=lambda command: subprocess.CompletedProcess(command, 0, stdout=f"{'0' * 64}  /app/app/main.py\n"))


def test_savant_interval_mismatch_fails(tmp_path):
    module = _load()
    def env(name):
        return dict(ENV) if "fanout" in name else {
            "FACE_INFER_INTERVAL": "3", "FACE_EMBEDDING_INFER_INTERVAL": "4",
        }
    with pytest.raises(module.RuntimeReadbackError, match="FACE_EMBEDDING_INFER_INTERVAL"):
        _collect(module, tmp_path, docker_env=env)


def test_roi_marks_embedding_interval_not_applicable_and_records_value(tmp_path):
    module = _load()
    def env(name):
        return dict(ENV) if "fanout" in name else {
            "FACE_INFER_INTERVAL": "3", "FACE_EMBEDDING_INFER_INTERVAL": "99",
        }
    report, _, _ = _collect(module, tmp_path, cfg=_cfg(adaface_roi_redis=True), docker_env=env)
    assert report["savant"]["savant-a"]["embedding_interval_applicability"] == "not_applicable_roi_mode"
    assert report["savant"]["savant-a"]["observed_env"]["FACE_EMBEDDING_INFER_INTERVAL"] == "99"


def test_missing_observed_env_fails(tmp_path):
    module = _load()
    with pytest.raises(module.RuntimeReadbackError, match="missing|mismatch"):
        _collect(module, tmp_path, docker_env=lambda name: {})


def test_expected_environment_comes_from_exact_compose_config():
    module = _load()
    config = {"services": {"fanout-a": {"environment": dict(ENV)}}}
    assert module.expected_forwarder_environment(config, ("fanout-a",)) == {"fanout-a": ENV}
    with pytest.raises(module.RuntimeReadbackError, match="expected"):
        module.expected_forwarder_environment({"services": {}}, ("fanout-a",))
