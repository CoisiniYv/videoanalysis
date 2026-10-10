"""Analysis forwarders get a strict FPS budget and a lag cap; raw fanout does not."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "infra" / "docker-compose.midterm.yml"
ENV_FILE = REPO_ROOT / "infra" / "env" / "midterm.env"
INDEX_HTML = REPO_ROOT / "services" / "evidence-viewer" / "app" / "static" / "index.html"
OPERATOR_JS = REPO_ROOT / "services" / "evidence-viewer" / "app" / "static" / "operator.js"
PRESSURE_SCRIPT = REPO_ROOT / "scripts" / "runtime" / "run_midterm_pressure60.py"


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _activate_api():
    api_root = str(REPO_ROOT / "services" / "api")
    for name in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        sys.modules.pop(name, None)
    if api_root in sys.path:
        sys.path.remove(api_root)
    sys.path.insert(0, api_root)


def test_compose_analysis_forwarders_cap_lag_and_raw_fanout_does_not() -> None:
    services = _services()
    for name in ("analysis-forwarder", "analysis-forwarder-a", "analysis-forwarder-b"):
        env = services[name]["environment"]
        assert env["FORWARDER_STRICT_FPS_BUDGET"] == (
            "${ANALYSIS_FORWARDER_STRICT_FPS_BUDGET:-true}"
        ), name
        assert env["FORWARDER_MAX_QUEUE_AGE_MS"] == (
            "${ANALYSIS_FORWARDER_MAX_QUEUE_AGE_MS:-30000}"
        ), name
    for name, service in services.items():
        if name.startswith("replay-raw-fanout"):
            assert "FORWARDER_MAX_QUEUE_AGE_MS" not in service.get("environment", {}), name
    # A shared env-file key would also reach the raw fanout services.
    assert "FORWARDER_MAX_QUEUE_AGE_MS" not in ENV_FILE.read_text(encoding="utf-8")


def test_dual_branch_topology_sets_budget_and_lag_cap() -> None:
    _activate_api()
    topology = importlib.import_module("app.services.runtime_topology")
    keys = [field["key"] for field in topology.BRANCH_FIELDS]
    assert "analysis_max_lag_ms" in keys
    assert topology.DEFAULT_BRANCH["analysis_max_lag_ms"] == 30000
    for preset in topology.RUNTIME_PROFILE_PRESETS.values():
        assert preset["branch"]["analysis_max_lag_ms"] == 30000

    branch = dict(topology.DEFAULT_BRANCH, gpu_id=0, analysis_max_lag_ms=20000)
    for full_pipeline in (False, True):
        env = topology._forwarder_env(branch, branch_id="a", full_pipeline=full_pipeline)
        assert env["FORWARDER_STRICT_FPS_BUDGET"] == "true"
        assert env["FORWARDER_MAX_QUEUE_AGE_MS"] == "20000"

    legacy = {k: v for k, v in branch.items() if k != "analysis_max_lag_ms"}
    assert topology._forwarder_env(legacy, branch_id="a")["FORWARDER_MAX_QUEUE_AGE_MS"] == "30000"
    assert topology._branch_config("a", dict(branch), gpu_id=0).analysis_max_lag_ms == 20000

    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'name="a.analysis_max_lag_ms"' in html
    assert 'name="b.analysis_max_lag_ms"' in html
    operator_js = OPERATOR_JS.read_text(encoding="utf-8")
    branch_keys = operator_js[operator_js.index("const branchKeys = [") :]
    assert '"analysis_max_lag_ms"' in branch_keys[: branch_keys.index("];")]


def test_single_branch_performance_form_controls_lag_cap() -> None:
    _activate_api()
    performance = importlib.import_module("app.services.runtime_performance")
    field = performance.FIELDS_BY_KEY["forwarder_max_queue_age_ms"]
    assert field.env == "FORWARDER_MAX_QUEUE_AGE_MS"
    assert field.target == "forwarder"
    assert field.default == 30000
    assert field.min_value == 0
    assert 'name="forwarder_max_queue_age_ms"' in INDEX_HTML.read_text(encoding="utf-8")


def test_pressure_direct_ingress_forwarders_use_budget_and_lag_cap() -> None:
    spec = importlib.util.spec_from_file_location("pressure60_lag_cap", PRESSURE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["pressure60_lag_cap"] = module
    spec.loader.exec_module(module)

    assert module.PressureConfig.__dataclass_fields__["analysis_max_queue_age_ms"].default == 30000
    args = module.parse_args(["--analysis-max-queue-age-ms", "15000"])
    assert args.analysis_max_queue_age_ms == 15000

    source = PRESSURE_SCRIPT.read_text(encoding="utf-8")
    block = source[source.index('service = f"replay-raw-fanout-{shard}"') :]
    block = block[: block.index("if cfg.adaface_roi_redis:")]
    assert '"FORWARDER_STRICT_FPS_BUDGET": "true"' in block
    assert '"FORWARDER_MAX_QUEUE_AGE_MS": str(cfg.analysis_max_queue_age_ms)' in block
