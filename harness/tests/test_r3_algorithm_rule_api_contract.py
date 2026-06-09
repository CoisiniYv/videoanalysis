"""R3 algorithm-rule API contract checks."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from fastapi.responses import JSONResponse

from app.routers.algorithms import (
    algorithms_list,
    camera_algorithm_rules_create,
    camera_algorithm_rules_disable,
    camera_algorithm_rules_enable,
)
from app.schemas.algorithms import AlgorithmRuleCreate, EvidencePolicy


NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


class FakeCameraRepository:
    def __init__(self) -> None:
        self.cameras = {
            "cam_001": {
                "id": "cam_001",
                "source_id": "source_001",
                "name": "Camera 001",
                "rtsp_url": "rtsp://example/stream",
            }
        }
        self.zones = [
            {
                "id": 1,
                "camera_id": "cam_001",
                "zone_name": "perimeter",
                "zone_type": "polygon",
                "points": [[0, 0], [1, 0], [1, 1], [0, 1]],
            },
            {
                "id": 2,
                "camera_id": "cam_001",
                "zone_name": "wall_line",
                "zone_type": "line",
                "points": [[0, 0], [1, 1]],
            },
        ]
        self.rules: List[Dict[str, Any]] = []
        self._rule_id = 0

    def get_camera(self, camera_id: str) -> Optional[Dict[str, Any]]:
        return self.cameras.get(camera_id)

    def list_zones(self, camera_id: str) -> List[Dict[str, Any]]:
        return [z for z in self.zones if z["camera_id"] == camera_id]

    def create_algorithm_rule(
        self,
        *,
        camera_id: str,
        rule_id: str,
        algorithm_id: str,
        rule_type: str,
        enabled: bool,
        zone_id: Optional[str],
        line_id: Optional[str],
        config: Dict[str, Any],
        evidence_policy: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._rule_id += 1
        row = {
            "id": self._rule_id,
            "rule_id": rule_id,
            "camera_id": camera_id,
            "algorithm_id": algorithm_id,
            "rule_type": rule_type,
            "algorithm_type": algorithm_id,
            "enabled": enabled,
            "zone_id": zone_id,
            "line_id": line_id,
            "config": dict(config),
            "evidence_policy": dict(evidence_policy),
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.rules.append(row)
        return row

    def list_algorithm_rules(self, camera_id: str) -> List[Dict[str, Any]]:
        return [r for r in self.rules if r["camera_id"] == camera_id]

    def get_algorithm_rule(self, camera_id: str, rule_id: int | str) -> Optional[Dict[str, Any]]:
        for rule in self.rules:
            if rule["camera_id"] == camera_id and (
                str(rule["id"]) == str(rule_id) or rule["rule_id"] == str(rule_id)
            ):
                return rule
        return None

    def update_algorithm_rule(
        self,
        *,
        camera_id: str,
        rule_id: int | str,
        enabled: Optional[bool],
        zone_id: Optional[str],
        line_id: Optional[str],
        config: Optional[Dict[str, Any]],
        evidence_policy: Optional[Dict[str, Any]],
        algorithm_id: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        row = self.get_algorithm_rule(camera_id, rule_id)
        if row is None:
            return None
        if algorithm_id is not None:
            row["algorithm_id"] = algorithm_id
            row["algorithm_type"] = algorithm_id
        if rule_type is not None:
            row["rule_type"] = rule_type
        if enabled is not None:
            row["enabled"] = enabled
        if zone_id is not None:
            row["zone_id"] = zone_id
        if line_id is not None:
            row["line_id"] = line_id
        if config is not None:
            row["config"] = config
        if evidence_policy is not None:
            row["evidence_policy"] = evidence_policy
        row["updated_at"] = NOW
        return row

    def set_algorithm_rule_enabled(
        self, camera_id: str, rule_id: int | str, enabled: bool
    ) -> Optional[Dict[str, Any]]:
        row = self.get_algorithm_rule(camera_id, rule_id)
        if row is None:
            return None
        row["enabled"] = enabled
        return row


@pytest.fixture
def repo() -> FakeCameraRepository:
    return FakeCameraRepository()


def test_algorithm_schema_supports_config_and_evidence_policy():
    body = AlgorithmRuleCreate(
        algorithm_id="behavior.intrusion",
        enabled=True,
        zone_id="perimeter",
        config={"min_inside_ms": 1000},
        evidence_policy=EvidencePolicy(
            snapshot_required=True,
            clip_required=True,
            pre_seconds=5,
            post_seconds=10,
        ),
    )
    assert body.algorithm_id == "behavior.intrusion"
    assert body.config["min_inside_ms"] == 1000
    assert body.evidence_policy.clip_required is True


def _json_response_body(response: JSONResponse) -> dict:
    import json

    return json.loads(response.body.decode("utf-8"))


def test_get_algorithms():
    body = algorithms_list(request_id="req-1")
    algorithms = body["data"]["algorithms"]
    assert {a["algorithm_id"] for a in algorithms} == {
        "behavior.intrusion",
        "behavior.loitering",
        "behavior.crowd_gathering",
        "behavior.running",
        "behavior.chasing",
        "behavior.fall",
        "behavior.wall_climb_suspicious",
        "face_intelligence",
    }
    assert "behavior.chasing" in {a["algorithm_id"] for a in algorithms}


def test_create_camera_algorithm_rule_validates_registry_zone_and_policy(repo):
    body = AlgorithmRuleCreate(
        **{
            "algorithm_type": "intrusion",
            "enabled": True,
            "zone_id": "perimeter",
            "config": {"min_inside_ms": 1000, "cooldown_s": 30},
            "evidence_policy": {
                "snapshot_required": True,
                "clip_required": True,
                "pre_seconds": 5,
                "post_seconds": 10,
            },
        }
    )
    resp = camera_algorithm_rules_create(
        "cam_001", body, repo=repo, request_id="req-1"
    )
    data = resp["data"]
    assert data["algorithm_id"] == "behavior.intrusion"
    assert data["algorithm_type"] == "behavior.intrusion"
    assert data["zone_id"] == "perimeter"
    assert data["evidence_policy"]["snapshot_required"] is True
    assert data["evidence_policy"]["clip_required"] is True


def test_unknown_algorithm_type_is_rejected():
    with pytest.raises(ValueError):
        AlgorithmRuleCreate(algorithm_id="unknown_algo", config={})


def test_zone_id_must_belong_to_camera(repo):
    body = AlgorithmRuleCreate(
        **{
            "algorithm_type": "intrusion",
            "zone_id": "missing_zone",
            "config": {"min_inside_ms": 1000, "cooldown_s": 30},
        }
    )
    resp = camera_algorithm_rules_create(
        "cam_001", body, repo=repo, request_id="req-1"
    )
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    assert "zone_id" in _json_response_body(resp)["error"]["message"]


def test_enable_disable_algorithm_rule(repo):
    body = AlgorithmRuleCreate(
        **{
            "algorithm_type": "wall_climb",
            "line_id": "wall_line",
            "config": {"cooldown_s": 60},
            "evidence_policy": {"snapshot_required": True, "clip_required": True},
        }
    )
    created = camera_algorithm_rules_create(
        "cam_001", body, repo=repo, request_id="req-1"
    )
    assert created["data"]["algorithm_id"] == "behavior.wall_climb_suspicious"
    assert repo.rules[0]["rule_type"] == "wall_climb"
    rule_id = created["data"]["rule_id"]

    disabled = camera_algorithm_rules_disable(
        "cam_001", rule_id, repo=repo, request_id="req-1"
    )
    assert disabled["data"]["enabled"] is False

    enabled = camera_algorithm_rules_enable(
        "cam_001", rule_id, repo=repo, request_id="req-1"
    )
    assert enabled["data"]["enabled"] is True
