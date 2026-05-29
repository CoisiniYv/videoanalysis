"""R3 algorithm registry contract checks."""

from __future__ import annotations

import sys
from pathlib import Path


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.algorithm_registry import get_algorithm, list_algorithms


EXPECTED = {
    "intrusion",
    "loitering",
    "crowd_gathering",
    "running",
    "chasing",
    "fall",
    "wall_climb",
    "face_intelligence",
}


def test_registry_contains_first_eight_algorithms():
    found = {a.algorithm_type for a in list_algorithms()}
    assert EXPECTED <= found
    assert len(found) == 8


def test_all_algorithms_declare_evidence_policy():
    for definition in list_algorithms():
        policy = definition.evidence_policy
        assert isinstance(policy.snapshot_required, bool)
        assert isinstance(policy.clip_required, bool)
        assert policy.pre_seconds >= 0
        assert policy.post_seconds >= 0


def test_behavior_and_face_intelligence_categories_are_not_split():
    face = get_algorithm("face_intelligence")
    assert face is not None
    assert face.category == "face_intelligence"
    assert "adaface_embedding" in face.input_requirements
    assert get_algorithm("watchlist_hit") is None
    assert get_algorithm("live_search_hit") is None


def test_intrusion_defaults_are_declared():
    intrusion = get_algorithm("intrusion")
    assert intrusion is not None
    assert intrusion.supports_roi is True
    assert intrusion.default_config["min_inside_ms"] == 1000
    assert intrusion.default_config["cooldown_s"] == 30
