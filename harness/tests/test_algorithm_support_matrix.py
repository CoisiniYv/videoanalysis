"""Operator algorithm support matrix contract tests."""

from __future__ import annotations

import sys
from pathlib import Path


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.algorithm_registry import runtime_apply_state_for_algorithm  # noqa: E402
from app.routers.algorithms import (  # noqa: E402
    _merge_and_validate_config,
    algorithms_support_matrix,
)


def test_support_matrix_covers_8090_operator_algorithms() -> None:
    body = algorithms_support_matrix(request_id="req-1")
    matrix = {
        item["algorithm_id"]: item
        for item in body["data"]["algorithms"]
    }

    assert set(matrix) == {
        "behavior.intrusion",
        "behavior.loitering",
        "behavior.crowd_gathering",
        "behavior.running",
        "behavior.chasing",
        "behavior.fall",
        "behavior.wall_climb_suspicious",
        "face.observation",
        "face.watchlist",
        "face.live_search",
    }
    assert body["data"]["statuses"] == [
        "production_ready",
        "event_only",
        "config_only",
        "unsupported",
        "deferred",
    ]


def test_support_matrix_marks_runtime_semantics_explicitly() -> None:
    matrix = {
        item["algorithm_id"]: item
        for item in algorithms_support_matrix(request_id="req-1")["data"]["algorithms"]
    }

    assert matrix["behavior.intrusion"]["status"] == "production_ready"
    assert matrix["behavior.intrusion"]["per_camera_gate"] is True
    assert matrix["behavior.intrusion"]["evidence_enabled"] is True

    assert matrix["behavior.fall"]["status"] == "event_only"
    assert matrix["behavior.fall"]["event_enabled"] is True
    assert matrix["behavior.fall"]["evidence_enabled"] is False

    assert matrix["behavior.running"]["status"] == "unsupported"
    assert matrix["behavior.running"]["configurable"] is False

    assert matrix["face.watchlist"]["status"] == "production_ready"
    assert matrix["face.watchlist"]["per_camera_gate"] is True
    assert matrix["face.watchlist"]["production_ready"] is True
    assert "camera_rules" in matrix["face.watchlist"]["status_reason"]

    assert matrix["face.live_search"]["status"] == "deferred"
    assert matrix["face.live_search"]["requires_runtime_apply"] is False


def test_runtime_apply_state_uses_support_matrix() -> None:
    intrusion = runtime_apply_state_for_algorithm("behavior.intrusion")
    assert intrusion["runtime_apply_state"] == "applied"
    assert intrusion["runtime_consumed"] is True

    watchlist = runtime_apply_state_for_algorithm("face.watchlist")
    assert watchlist["runtime_apply_state"] == "applied"
    assert watchlist["runtime_consumed"] is True

    running = runtime_apply_state_for_algorithm("behavior.running")
    assert running["runtime_apply_state"] == "unsupported"
    assert running["runtime_skip_reason"] == "unsupported_algorithm"

    disabled = runtime_apply_state_for_algorithm(
        "behavior.intrusion",
        rule_enabled=False,
    )
    assert disabled["runtime_apply_state"] == "skipped"
    assert disabled["runtime_skip_reason"] == "rule_disabled"


def test_watchlist_config_empty_target_lists_override_legacy_fields() -> None:
    merged, error = _merge_and_validate_config(
        "face.watchlist",
        {
            "threshold": 0.8,
            "target_person_ids": [],
            "target_external_person_ids": [],
            "target_names": [],
            "person_ids": [10],
            "external_person_ids": ["demo:old"],
            "names": ["Old Target"],
        },
    )

    assert error == ""
    assert merged is not None
    assert merged["target_person_ids"] == []
    assert merged["target_external_person_ids"] == []
    assert merged["target_names"] == []
