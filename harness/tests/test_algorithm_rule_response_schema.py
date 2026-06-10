"""Algorithm rule response schema regression tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.schemas.algorithms import AlgorithmRuleResponse


def test_algorithm_rule_response_accepts_uuid_camera_id() -> None:
    row = {
        "id": 1,
        "rule_id": "rule_behavior_intrusion",
        "camera_id": UUID("00000000-0000-4000-8000-000000000100"),
        "algorithm_id": "behavior.intrusion",
        "rule_type": "intrusion",
        "enabled": True,
        "zone_id": "perimeter",
        "line_id": None,
        "config": {"min_inside_ms": 1000, "cooldown_s": 30},
        "evidence_policy": {
            "snapshot_required": True,
            "clip_required": True,
            "pre_seconds": 5,
            "post_seconds": 10,
        },
        "created_at": datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 10, 12, 1, tzinfo=timezone.utc),
    }

    response = AlgorithmRuleResponse.from_db_row(row)

    assert response.camera_id == "00000000-0000-4000-8000-000000000100"
    assert response.algorithm_id == "behavior.intrusion"
    assert response.evidence_policy.pre_seconds == 5


def test_algorithm_rule_response_derives_policy_from_config_defaults() -> None:
    row = {
        "id": 2,
        "rule_id": "lab_intrusion_rule",
        "camera_id": "00000000-0000-4000-8000-781078565686",
        "algorithm_id": "behavior.intrusion",
        "rule_type": "intrusion",
        "enabled": True,
        "config": {
            "zone": "lab_full_frame",
            "snapshot_required": False,
            "clip_required": True,
        },
        "evidence_policy": {},
    }

    response = AlgorithmRuleResponse.from_db_row(row)

    assert response.evidence_policy.snapshot_required is False
    assert response.evidence_policy.clip_required is True
    assert response.evidence_policy.pre_seconds == 5
    assert response.evidence_policy.post_seconds == 5
