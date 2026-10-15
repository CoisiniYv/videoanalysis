"""Pressure overrides must write, verify, and restore scheduler settings."""

import json

import pytest

from test_midterm_pressure60_script import _config, _load_module


SCHEDULER_ENV = {
    "MEDIA_WORKER_COMPLETION_WAKE_ENABLED": "true",
    "ROLLING_CACHE_MATERIALIZATION_CANDIDATE_LOOKAHEAD": "4",
    "ROLLING_CACHE_MATERIALIZATION_AGING_SECONDS": "60",
}


def _mock_media_worker(module, monkeypatch, environment, *, unapplied=None):
    applied = []

    def recreate(command, log_path, *, env):
        override = module.yaml.safe_load(log_path.with_suffix(".override.yml").read_text())
        values = override["services"]["media-worker"]["environment"]
        assert command[-1] == "media-worker"
        applied.append(values)
        environment.clear()
        environment.update({key: value.replace("$$", "$") for key, value in values.items()})
        if unapplied:
            environment[unapplied] = "unapplied"

    monkeypatch.setattr(module, "run", recreate)
    monkeypatch.setattr(module, "docker_container_env", lambda name: dict(environment))
    monkeypatch.setattr(module, "configure_event_worker_evidence_admission", lambda *args, **kwargs: {})
    return applied


@pytest.mark.parametrize("completion_wake", [True, False])
def test_scheduler_pressure_override_is_written_verified_and_fully_restored(
    tmp_path, monkeypatch, completion_wake,
):
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, rolling_cache_evidence=True,
                  media_worker_completion_wake=completion_wake)
    original = {
        "MEDIA_WORKER_COMPLETION_WAKE_ENABLED": "false" if completion_wake else "true",
        "ROLLING_CACHE_MATERIALIZATION_CANDIDATE_LOOKAHEAD": "8",
        "ROLLING_CACHE_MATERIALIZATION_AGING_SECONDS": "90",
        "UNRELATED_ORIGINAL_ENV": "$original",
    }
    environment = dict(original)
    applied = _mock_media_worker(module, monkeypatch, environment)
    before = module.media_worker_rolling_cache_env_snapshot(full=True)
    requested = dict(SCHEDULER_ENV,
                     MEDIA_WORKER_COMPLETION_WAKE_ENABLED="true" if completion_wake else "false")

    result = module.configure_rolling_cache_workers_for_pressure(cfg)["media_worker"]

    for key, value in requested.items():
        assert applied[0][key] == result["requested"][key] == result["observed"][key] == value
    report = json.loads((tmp_path / "compose_recreate_media_worker_rolling_cache.json").read_text())
    assert all(report["observed"][key] == value for key, value in requested.items())
    restored = module.configure_media_worker_rolling_cache(
        cfg, values=before, artifact_name="restore.log",
    )
    assert restored["requested"] == restored["observed"] == original
    assert environment == original


@pytest.mark.parametrize("key", tuple(SCHEDULER_ENV))
def test_scheduler_pressure_override_rejects_each_unapplied_setting(tmp_path, monkeypatch, key):
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, rolling_cache_evidence=True)
    _mock_media_worker(module, monkeypatch, {}, unapplied=key)

    with pytest.raises(RuntimeError, match=f"{key} was not applied"):
        module.configure_rolling_cache_workers_for_pressure(cfg)

    report = json.loads((tmp_path / "compose_recreate_media_worker_rolling_cache.json").read_text())
    assert report["requested"][key] == SCHEDULER_ENV[key]
    assert report["observed"][key] == "unapplied"
