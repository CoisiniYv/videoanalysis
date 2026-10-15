"""Requested pressure algorithms must be reproducible on every camera slot."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/runtime/run_midterm_pressure60.py"


def _load():
    spec = importlib.util.spec_from_file_location("pressure60_behavior_tests_runner", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _cfg(tmp_path, **changes):
    values = dict(
        run_id="pressure_behavior_test", stream_count=2,
        rtsp_uri="rtsp://camera/live", rtsp_republish_output_base="",
        behavior_rules=("fall", "crowd_gathering", "chasing"),
        pressure_algorithm_cooldown_s=60, artifact_dir=tmp_path,
        savant_ablation_stage="full-evidence", evidence_group_size=10,
        evidence_policy_groups=((2, 2),),
    )
    values.update(changes)
    return SimpleNamespace(**values)


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class RuleConnection:
    def __init__(self, *, readback_change=None):
        self.calls = []
        self.rules = [
            dict(id=1, camera_id="camera-0", algorithm_id="behavior.running",
                 rule_type="running", rule_id="old_running", enabled=True),
            dict(id=2, camera_id="other-camera", algorithm_id="behavior.running",
                 rule_type="running", rule_id="other_running", enabled=True),
            dict(id=3, camera_id="camera-0", algorithm_id="behavior.fall",
                 rule_type="fall", rule_id="old_fall", enabled=True),
            dict(id=4, camera_id="camera-0", algorithm_id="behavior.fall",
                 rule_type="fall", rule_id="duplicate_fall", enabled=True),
        ]
        self.readback_change = readback_change
        self.rolled_back = False

    def transaction(self):
        conn = self

        class Transaction:
            def __enter__(self):
                self.before = copy.deepcopy(conn.rules)

            def __exit__(self, exc_type, exc, tb):
                if exc_type:
                    conn.rules = self.before
                    conn.rolled_back = True
                return False

        return Transaction()

    def execute(self, query, params=None):
        query = " ".join(query.split())
        self.calls.append((query, params))
        if "site_id='pressure' AND location=%s" in query:
            return Result([{"id": f"camera-{int(params[0].split('-')[-1])}"}])
        if query.startswith("UPDATE camera_rules SET enabled=false"):
            for row in self.rules:
                if row["camera_id"] == params[0]:
                    row["enabled"] = False
        elif "FROM camera_rules" in query and "LIMIT 1" in query:
            camera_id, rule_id, algorithm_id, rule_type, *_ = params
            matches = [row for row in self.rules if row["camera_id"] == camera_id
                       and (row["rule_id"] == rule_id or row["algorithm_id"] == algorithm_id
                            or row["rule_type"] == rule_type)]
            return Result(sorted(matches, key=lambda row: (
                0 if row["rule_id"] == rule_id else
                1 if row["algorithm_id"] == algorithm_id else 2, row["id"])))
        elif query.startswith("UPDATE camera_rules SET zone_id="):
            zone_id, rule_type, config, rule_id, algorithm_id, policy, row_id = params
            row = next(row for row in self.rules if row["id"] == row_id)
            row.update(zone_id=zone_id, rule_type=rule_type, config=json.loads(config),
                       rule_id=rule_id, algorithm_id=algorithm_id,
                       evidence_policy=json.loads(policy), enabled=True)
        elif query.startswith("INSERT INTO camera_rules"):
            camera_id, zone_id, rule_type, config, rule_id, algorithm_id, policy = params
            self.rules.append(dict(id=max(row["id"] for row in self.rules) + 1,
                                   camera_id=camera_id, zone_id=zone_id,
                                   rule_type=rule_type, config=json.loads(config),
                                   rule_id=rule_id, algorithm_id=algorithm_id,
                                   evidence_policy=json.loads(policy), enabled=True))
        elif "FROM camera_rules" in query and "enabled=true" in query:
            rows = copy.deepcopy([row for row in self.rules
                                  if row["camera_id"] in params[0] and row["enabled"]])
            if self.readback_change:
                self.readback_change(rows)
            return Result(rows)
        return Result()


def test_behavior_rules_config_and_cli_defaults_are_empty():
    module = _load()
    assert next(field for field in fields(module.PressureConfig)
                if field.name == "behavior_rules").default == ()
    assert module.parse_args([]).behavior_rules == ()


def test_behavior_rules_cli_preserves_requested_rules():
    module = _load()
    assert module.parse_args([
        "--behavior-rules", "fall,crowd_gathering,chasing",
    ]).behavior_rules == ("fall", "crowd_gathering", "chasing")


@pytest.mark.parametrize("value", ["running", "fall,", "fall,fall", "behavior.fall"])
def test_behavior_rules_cli_rejects_unsupported_or_ambiguous_lists(value):
    with pytest.raises(SystemExit):
        _load().parse_args(["--behavior-rules", value])


def test_registry_defaults_are_literals_and_copied():
    module = _load()
    defaults = module.pressure_behavior_rule_defaults(("fall", "crowd_gathering", "chasing"))
    assert defaults["fall"]["min_down_ms"] == 1500
    assert defaults["crowd_gathering"]["min_person_count"] == 5
    assert defaults["chasing"]["cooldown_s"] == 30
    defaults["fall"]["min_down_ms"] = 9
    assert module.pressure_behavior_rule_defaults(("fall",))["fall"]["min_down_ms"] == 1500


@pytest.mark.parametrize("registry_source", [
    "_REGISTRY: dict = make_registry()",
    "_REGISTRY: dict = {'behavior.fall': AlgorithmDefinition(algorithm_id='behavior.fall', default_config=defaults())}",
    "_REGISTRY: dict = {'behavior.fall': AlgorithmDefinition(algorithm_id='behavior.fall', default_config=[])}",
    "_REGISTRY: dict = {'behavior.fall': AlgorithmDefinition(algorithm_id='behavior.fall', default_config={})}",
    "_REGISTRY: dict = {'behavior.fall': OtherDefinition(algorithm_id='behavior.fall', default_config={'cooldown_s': 60})}",
    "_REGISTRY: dict = {'behavior.fall': AlgorithmDefinition(default_config={'cooldown_s': 60})}",
])
def test_registry_structure_changes_fail_closed(tmp_path, registry_source):
    module = _load()
    registry = tmp_path / "registry.py"
    registry.write_text(registry_source, encoding="utf-8")
    with pytest.raises(ValueError, match="registry"):
        module.pressure_behavior_rule_defaults(("fall",), registry_path=registry)


def test_provisioning_clears_residual_rules_and_reads_each_camera(tmp_path):
    module = _load()
    conn = RuleConnection()
    cfg = _cfg(tmp_path)
    report = module.insert_pressure_cameras(conn, cfg)

    expected = ["behavior.intrusion", "face.watchlist", "behavior.fall",
                "behavior.crowd_gathering", "behavior.chasing"]
    assert module.pressure_requested_algorithm_ids(cfg) == expected
    for camera_id in ("camera-0", "camera-1"):
        rows = [row for row in conn.rules if row["camera_id"] == camera_id and row["enabled"]]
        assert sorted(row["algorithm_id"] for row in rows) == sorted(expected)
        assert all(row["config"]["cooldown_s"] == 60 for row in rows)
        for row in rows:
            if row["algorithm_id"] in {"behavior.fall", "behavior.crowd_gathering", "behavior.chasing"}:
                assert row["zone_id"] == f"pressure-{camera_id[-1]:0>2}_full_frame"
                assert row["evidence_policy"]["clip_required"] is True
        chasing = next(row for row in rows if row["algorithm_id"] == "behavior.chasing")
        assert chasing["config"]["min_speed_px_s"] == 120.0
    assert next(row for row in conn.rules if row["camera_id"] == "other-camera")["enabled"]
    disable_queries = [(query, params) for query, params in conn.calls
                       if query.startswith("UPDATE camera_rules SET enabled=false")]
    assert len(disable_queries) == 2
    assert all("WHERE camera_id=%s" in query and len(params) == 1
               for query, params in disable_queries)
    artifact = json.loads((tmp_path / "pressure_camera_rules.json").read_text())
    assert artifact == report["enabled_rule_readback"]
    assert artifact["status"] == "passed"
    assert set(artifact["cameras"]) == {"camera-0", "camera-1"}
    assert all(len(camera["enabled_rules"]) == 5 for camera in artifact["cameras"].values())


def test_default_requested_rules_disable_previous_behavior_rules(tmp_path):
    module = _load()
    conn = RuleConnection()
    module.insert_pressure_cameras(conn, _cfg(tmp_path, behavior_rules=()))
    assert {row["algorithm_id"] for row in conn.rules
            if row["camera_id"] == "camera-0" and row["enabled"]} == {
                "behavior.intrusion", "face.watchlist"}


@pytest.mark.parametrize("change", [
    lambda rows: rows.pop(),
    lambda rows: rows.append({**rows[0], "id": 999}),
    lambda rows: rows.append({**rows[0], "id": 999, "algorithm_id": "behavior.running"}),
    lambda rows: rows.__setitem__(slice(None), [row for row in rows if row["camera_id"] != "camera-1"]),
])
def test_rule_readback_mismatch_aborts_transaction_and_records_actual_rows(tmp_path, change):
    module = _load()
    conn = RuleConnection(readback_change=change)
    with pytest.raises(RuntimeError, match="pressure camera rules"):
        module.insert_pressure_cameras(conn, _cfg(tmp_path))
    assert conn.rolled_back
    artifact = json.loads((tmp_path / "pressure_camera_rules.json").read_text())
    assert artifact["status"] == "failed"
    assert artifact["mismatches"]


def test_invalid_registry_fails_before_provisioning_mutations(tmp_path, monkeypatch):
    module = _load()
    conn = RuleConnection()

    def invalid(*args, **kwargs):
        raise ValueError("registry structure changed")

    monkeypatch.setattr(module, "pressure_behavior_rule_defaults", invalid)
    with pytest.raises(ValueError, match="registry"):
        module.insert_pressure_cameras(conn, _cfg(tmp_path))
    assert conn.calls == []


def test_no_evidence_ablation_applies_to_behavior_rules(tmp_path):
    module = _load()
    conn = RuleConnection()
    module.insert_pressure_cameras(conn, _cfg(tmp_path, savant_ablation_stage="detector-only"))
    assert all(row["evidence_policy"]["clip_required"] is False for row in conn.rules
               if row["camera_id"].startswith("camera-") and row["enabled"])
