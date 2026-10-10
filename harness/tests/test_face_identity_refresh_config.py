"""Face identity refresh settings reach every face gate and the 8090 forms."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_YML = REPO_ROOT / "modules" / "savant_security" / "module.yml"
ENV_FILE = REPO_ROOT / "infra" / "env" / "midterm.env"
INDEX_HTML = REPO_ROOT / "services" / "evidence-viewer" / "app" / "static" / "index.html"
OPERATOR_JS = REPO_ROOT / "services" / "evidence-viewer" / "app" / "static" / "operator.js"
PRESSURE_SCRIPT = REPO_ROOT / "scripts" / "runtime" / "run_midterm_pressure60.py"

IDENTITY_KWARGS = {
    "identity_refresh_ms": "${oc.decode:${oc.env:FACE_IDENTITY_REFRESH_MS, 5000}}",
    "clear_min_face_size": "${oc.decode:${oc.env:FACE_CLEAR_MIN_SIZE_PX, 64}}",
    "clear_min_confidence": "${oc.decode:${oc.env:FACE_CLEAR_MIN_CONFIDENCE, 0.6}}",
    "clear_max_yaw_ratio": "${oc.decode:${oc.env:FACE_CLEAR_MAX_YAW_RATIO, 0.35}}",
}


def _env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _elements() -> dict[str, dict]:
    doc = yaml.safe_load(MODULE_YML.read_text(encoding="utf-8"))
    return {element["name"]: element for element in doc["pipeline"]["elements"] if "name" in element}


def _activate_api():
    api_root = str(REPO_ROOT / "services" / "api")
    for name in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        sys.modules.pop(name, None)
    if api_root in sys.path:
        sys.path.remove(api_root)
    sys.path.insert(0, api_root)


def test_midterm_env_sets_identity_refresh_defaults() -> None:
    env = _env_file()
    assert env["FACE_REID_MIN_INTERVAL_MS"] == "1000"
    assert env["FACE_IDENTITY_REFRESH_MS"] == "5000"
    assert env["FACE_CLEAR_MIN_SIZE_PX"] == "64"
    assert env["FACE_CLEAR_MIN_CONFIDENCE"] == "0.60"
    assert env["FACE_CLEAR_MAX_YAW_RATIO"] == "0.35"


def test_module_face_gates_receive_identity_refresh_kwargs() -> None:
    elements = _elements()
    for name in ("face_roi_exporter", "face_reid_gate"):
        kwargs = elements[name]["kwargs"]
        for key, value in IDENTITY_KWARGS.items():
            assert kwargs[key] == value, (name, key)


def test_pressure_pre_gate_candidate_receives_identity_refresh_kwargs() -> None:
    source = PRESSURE_SCRIPT.read_text(encoding="utf-8")
    start = source.index('"name": "face_reid_candidate_gate"')
    block = source[start : source.index("},", source.index('"kwargs"', start))]
    for key, value in IDENTITY_KWARGS.items():
        assert f'"{key}": "{value}"' in block, key


def test_single_branch_performance_form_controls_refresh_interval() -> None:
    _activate_api()
    performance = importlib.import_module("app.services.runtime_performance")
    field = performance.FIELDS_BY_KEY["face_identity_refresh_ms"]
    assert field.env == "FACE_IDENTITY_REFRESH_MS"
    assert field.target == "savant"
    assert field.kind == "int"
    assert field.default == 5000
    assert field.min_value == 0
    assert 'name="face_identity_refresh_ms"' in INDEX_HTML.read_text(encoding="utf-8")


def test_dual_branch_topology_controls_refresh_interval() -> None:
    _activate_api()
    topology = importlib.import_module("app.services.runtime_topology")
    keys = [field["key"] for field in topology.BRANCH_FIELDS]
    assert "face_identity_refresh_ms" in keys
    assert topology.DEFAULT_BRANCH["face_identity_refresh_ms"] == 5000
    for preset in topology.RUNTIME_PROFILE_PRESETS.values():
        assert preset["branch"]["face_identity_refresh_ms"] == 5000

    branch = dict(topology.DEFAULT_BRANCH, gpu_id=0, face_identity_refresh_ms=8000)
    env = topology._savant_env(branch, mode="dual_same_gpu", full_pipeline=True)
    assert env["FACE_IDENTITY_REFRESH_MS"] == "8000"

    legacy_branch = {k: v for k, v in branch.items() if k != "face_identity_refresh_ms"}
    env = topology._savant_env(legacy_branch, mode="dual_same_gpu", full_pipeline=True)
    assert env["FACE_IDENTITY_REFRESH_MS"] == "5000"

    config = topology._branch_config("a", dict(branch), gpu_id=0)
    assert config.face_identity_refresh_ms == 8000

    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'name="a.face_identity_refresh_ms"' in html
    assert 'name="b.face_identity_refresh_ms"' in html
    operator_js = OPERATOR_JS.read_text(encoding="utf-8")
    branch_keys = operator_js[operator_js.index("const branchKeys = [") :]
    branch_keys = branch_keys[: branch_keys.index("];")]
    assert '"face_identity_refresh_ms"' in branch_keys
