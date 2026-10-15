import importlib.util
import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load():
    path = Path(__file__).resolve().parents[2] / "scripts/runtime/run_midterm_pressure60.py"
    spec = importlib.util.spec_from_file_location("pressure_preflight_flow", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_preflight_only_cannot_enter_runtime_or_database(tmp_path, monkeypatch):
    module = load()
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight attempted a runtime/database operation")
    monkeypatch.setattr(module.psycopg, "connect", forbidden)
    monkeypatch.setattr(module, "docker_network_gateway", forbidden)
    monkeypatch.setattr(module, "precreate_operator_dual_runtime_after_pressure", forbidden)
    monkeypatch.setattr(module, "perform_storage_preflight", lambda cfg: (
        {"status": "passed", "sources": {}}, {"services": {}}))
    assert module.main(["--preflight-only", "--artifact-root", str(tmp_path),
                        "--run-id", "readonly", "--rtsp-republish-local-server"]) == 0
    report = json.loads((tmp_path / "readonly/report.json").read_text())
    assert report["status"] == "preflight_passed"


def test_preflight_abort_invokes_recovery_before_any_mutation(tmp_path, monkeypatch):
    module = load()
    calls = []
    def failed(cfg):
        raise module.StoragePreflightError("rotational", {"status": "failed"})
    monkeypatch.setattr(module, "perform_storage_preflight", failed)
    monkeypatch.setattr(module, "restore_after_preflight_abort", lambda: calls.append("restore"))
    monkeypatch.setattr(module.psycopg, "connect", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("database opened before preflight")))
    assert module.main(["--artifact-root", str(tmp_path), "--run-id", "rejected"]) == 2
    assert calls == ["restore"]
    report = json.loads((tmp_path / "rejected/report.json").read_text())
    assert report["rolling_cache_storage"]["status"] == "failed"


@pytest.mark.parametrize("working_directory", ["root", "infra"])
def test_formal_restore_accepts_compose_project_root_and_infra_labels(tmp_path, monkeypatch, working_directory):
    module = load()
    infra = tmp_path / "infra"
    (infra / "env").mkdir(parents=True)
    base = infra / "docker-compose.midterm.yml"
    base.touch()
    (infra / "midterm-storage.override.yml").touch()
    (infra / "env/midterm.env").touch()
    snapshot = {"Config": {"Labels": {
        "com.docker.compose.project.working_dir": str(tmp_path if working_directory == "root" else infra),
        "com.docker.compose.project.config_files": str(base) + ",/data/artifacts/old.override.yml",
    }}}
    monkeypatch.setattr(module, "inspect_container", lambda name: snapshot)
    monkeypatch.setattr(module, "replace", lambda cfg, **values: SimpleNamespace(**values))
    restored = module.formal_restore_config(SimpleNamespace())
    assert restored.compose_file == str(base)
    assert restored.env_file == str(infra / "env/midterm.env")
    assert restored.pressure_rolling_cache_host_root == ""


def test_cache_readback_ignores_unused_sink_materialized_mount(monkeypatch):
    module = load()
    expected = {
        "rolling-cache-sink-a": {module.CACHE_TARGETS[0]: "/fast/cache"},
        "rolling-cache-sink-b": {module.CACHE_TARGETS[0]: "/fast/cache"},
        "media-worker": dict(zip(module.CACHE_TARGETS, ("/fast/cache", "/fast/materialized"))),
    }
    calls = []
    def resolve(names, target):
        service = names[0].removeprefix("video-analytics-midterm-")
        calls.append((service, target))
        return Path(expected[service][target])
    monkeypatch.setattr(module, "resolve_container_bind_source", resolve)
    assert module.pressure_cache_readback(SimpleNamespace(dual_shard_same_gpu=True), expected)["status"] == "passed"
    assert len(calls) == 4


def test_restore_readback_uses_formal_compose_and_rejects_pressure_root(tmp_path, monkeypatch):
    module = load()
    cfg = SimpleNamespace(dual_shard_same_gpu=False)
    sources = {
        "rolling-cache-sink": {module.CACHE_TARGETS[0]: "/fast/cache"},
        "media-worker": dict(zip(module.CACHE_TARGETS, ("/fast/cache", "/fast/materialized"))),
    }
    rendered = {"services": {service: {"volumes": [
        {"type": "bind", "target": target, "source": source}
        for target, source in mounts.items()
    ]} for service, mounts in sources.items()}}
    command = ["docker", "compose", "-f", str(tmp_path / "formal.yml")]
    monkeypatch.setattr(module, "_compose_base_args", lambda actual: command if actual is cfg else None)
    seen = []
    def config(argv):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(rendered))
    monkeypatch.setattr(module, "_readonly_command", config)
    monkeypatch.setattr(module, "resolve_container_bind_source", lambda names, target: Path("/dev/shm/pressure/cache"))
    with pytest.raises(module.StoragePreflightError, match="differ from expected"):
        module.restored_cache_readback(cfg)
    assert seen == [[*command, "config", "--format", "json"]]


def test_final_restore_failure_overrides_an_already_computed_success_exit():
    module = load()
    tree = ast.parse(Path(module.__file__).read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    finalizer = next(node for node in reversed(main.body) if isinstance(node, ast.Try)).finalbody
    overrides = [node for node in finalizer if isinstance(node, ast.If)
                 and any(isinstance(child, ast.Return) for child in ast.walk(node))]
    assert overrides, "final restore verification must override any pending success return"
    wrapper = ast.FunctionDef(name="finish", args=ast.arguments(
        posonlyargs=[], args=[ast.arg(arg="report")], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[ast.Try(body=[ast.Return(value=ast.Constant(value=0))], handlers=[],
                      orelse=[], finalbody=overrides)], decorator_list=[])
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "<restore-exit>", "exec"), namespace)
    assert namespace["finish"]({"status": "passed"}) == 0
    assert namespace["finish"]({"status": "failed_restore_verification"}) == 1
