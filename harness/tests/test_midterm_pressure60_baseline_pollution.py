import json
from types import SimpleNamespace

import pytest

from scripts.runtime.pressure_baseline_guard import baseline_pollution
from test_midterm_pressure60_script import _load_module


def test_known_drift_warns_without_becoming_gate():
    result = baseline_pollution(
        environments={"media-worker": {"ROLLING_CACHE_SOURCES": "pressure60_old_00,formal",
                                       "ROLLING_CACHE_RETENTION_SECONDS": "4020"}},
        retention_limits={"media-worker": 300},
        cameras=[{"source_id": "formal", "enabled": False}],
        rules=[{"source_id": "pressure60_old_00", "enabled": True, "algorithm": "fall"}],
        expected_source_count=40,
    )
    assert result["blocking"] is False
    assert result["severity"] == "red"
    assert {x["code"] for x in result["findings"]} == {
        "historical_pressure_sources", "retention_above_formal",
        "enabled_pressure_rules", "runtime_camera_count_mismatch",
    }
    assert result["actual_enabled_source_count"] == 0


def test_clean_baseline_and_unknown_are_distinct():
    clean = baseline_pollution(environments={}, retention_limits={}, cameras=[], rules=[],
                               expected_source_count=0)
    assert clean["findings"] == [] and clean["severity"] == "none"
    unknown = baseline_pollution(environments={}, retention_limits={}, cameras=None, rules=None,
                                 expected_source_count=None)
    assert unknown["unverified"] == ["camera_population", "camera_rules", "runtime_source_count"]
    assert unknown["blocking"] is False


@pytest.mark.parametrize("identity", [
    {"site_id": "pressure", "location": "different-label"},
    {"site_id": "legacy-site", "location": "pressure-00"},
])
def test_custom_run_id_uses_stable_pressure_camera_identity(identity):
    camera = {"source_id": "canary_old_00", "enabled": False, **identity}
    result = baseline_pollution(
        environments={"media-worker": {"ROLLING_CACHE_SOURCES": '["canary_old_00", "formal"]'}},
        retention_limits={}, cameras=[camera],
        rules=[{"source_id": "canary_old_00", "enabled": True,
                "algorithm_id": "behavior.fall", **identity}], expected_source_count=0,
    )
    assert {finding["code"] for finding in result["findings"]} == {
        "historical_pressure_sources", "enabled_pressure_rules",
    }
    assert result["blocking"] is False


def test_rule_identity_can_be_joined_from_camera_source_id():
    result = baseline_pollution(
        environments={}, retention_limits={},
        cameras=[{"site_id": "pressure", "location": "pressure-00",
                  "source_id": "canary_old_00", "enabled": False}],
        rules=[{"source_id": "canary_old_00", "enabled": True}], expected_source_count=0,
    )
    assert [finding["code"] for finding in result["findings"]] == ["enabled_pressure_rules"]


def test_empty_environment_is_unverified_without_blocking():
    result = baseline_pollution(
        environments={"rolling-cache-sink": {}}, retention_limits={},
        cameras=[], rules=[], expected_source_count=0,
    )
    assert result["unverified"] == ["rolling-cache-sink.environment"]
    assert result["blocking"] is False


@pytest.mark.parametrize("dual", [False, True])
def test_collector_reads_mode_sinks_and_all_four_pollution_checks(tmp_path, monkeypatch, dual):
    module = _load_module()
    calls = []
    queries = []
    cameras = [{"source_id": "canary_old_00", "site_id": "pressure",
                "location": "pressure-00", "enabled": True}]

    class Connection:
        def execute(self, query):
            queries.append(query)
            return SimpleNamespace(fetchall=lambda: (
                [{**cameras[0], "algorithm_id": "behavior.fall"}]
                if "camera_rules" in query else cameras))

    def environment(container):
        calls.append(container)
        return {"ROLLING_CACHE_SOURCES": "canary_old_00", "ROLLING_CACHE_RETENTION_SECONDS": "4020"}

    services = ["rolling-cache-sink-a", "rolling-cache-sink-b"] if dual else ["rolling-cache-sink"]
    monkeypatch.setattr(module, "docker_container_env", environment)
    def compose_args(cfg, *, additional_profiles=()):
        assert additional_profiles == ("*",)
        return ["docker", "compose"]

    monkeypatch.setattr(module, "_compose_base_args", compose_args)
    monkeypatch.setattr(module, "_readonly_command", lambda args: SimpleNamespace(
        returncode=0, stdout=json.dumps({"services": {
            service: {"environment": {"ROLLING_CACHE_RETENTION_SECONDS": "300"}}
            for service in [*services, "media-worker"]}})))
    monkeypatch.setattr(module, "api_json", lambda *args, **kwargs: {"data": {"preflight": {"checks": [
        {"name": "profile_enabled_source_count", "expected_source_count": 40},
    ]}}})
    result = module.collect_baseline_pollution(Connection(), SimpleNamespace(
        dual_shard_same_gpu=dual, api_base="http://unused", artifact_dir=tmp_path), SimpleNamespace())
    assert calls == [f"video-analytics-midterm-{service}" for service in [*services, "media-worker"]]
    assert {finding["code"] for finding in result["findings"]} == {
        "historical_pressure_sources", "retention_above_formal",
        "enabled_pressure_rules", "runtime_camera_count_mismatch",
    }
    assert all("site_id" in query and "location" in query for query in queries)
    assert result["blocking"] is False


def test_collector_environment_errors_remain_nonblocking_and_unverified(tmp_path, monkeypatch):
    module = _load_module()

    def unavailable(*args, **kwargs):
        raise OSError("read-only inspect unavailable")

    monkeypatch.setattr(module, "docker_container_env", unavailable)
    monkeypatch.setattr(module, "_compose_base_args", lambda cfg, **kwargs: ["docker", "compose"])
    monkeypatch.setattr(module, "_readonly_command", lambda args: SimpleNamespace(
        returncode=0, stdout='{"services": {}}'))
    monkeypatch.setattr(module, "api_json", lambda *args, **kwargs: {"data": {}})
    connection = SimpleNamespace(execute=lambda query: SimpleNamespace(fetchall=lambda: []))
    result = module.collect_baseline_pollution(connection, SimpleNamespace(
        dual_shard_same_gpu=False, api_base="http://unused", artifact_dir=tmp_path), SimpleNamespace())
    assert result["blocking"] is False
    assert "rolling-cache-sink.environment" in result["unverified"]
    assert "media-worker.environment" in result["unverified"]
