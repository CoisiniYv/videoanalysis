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
    "behavior.intrusion",
    "behavior.loitering",
    "behavior.crowd_gathering",
    "behavior.running",
    "behavior.chasing",
    "behavior.fall",
    "behavior.wall_climb_suspicious",
    "face_intelligence",
}


def test_registry_contains_first_eight_algorithms():
    found = {a.algorithm_id for a in list_algorithms()}
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
    intrusion = get_algorithm("behavior.intrusion")
    assert intrusion is not None
    assert intrusion.supports_roi is True
    assert intrusion.default_config["min_inside_ms"] == 1000
    assert intrusion.default_config["cooldown_s"] == 30


def test_chasing_and_wall_climb_external_ids_are_declared():
    chasing = get_algorithm("behavior.chasing")
    wall = get_algorithm("behavior.wall_climb_suspicious")
    assert chasing is not None
    assert wall is not None
    assert wall.supports_line is True


def test_pose_behavior_defaults_include_runtime_canonical_fields():
    fall = get_algorithm("behavior.fall")
    crowd = get_algorithm("behavior.crowd_gathering")
    chasing = get_algorithm("behavior.chasing")

    assert fall.default_config["require_transition"] is True
    assert fall.default_config["lying_aspect_ratio"] == 0.85
    assert fall.default_config["min_visible_keypoints"] == 0

    assert crowd.default_config["min_person_count"] == 5
    assert crowd.default_config["exit_person_count"] == 3
    assert crowd.default_config["eps_px"] == 180.0

    assert chasing.default_config["min_speed_px_s"] == 120.0
    assert chasing.default_config["max_distance_px"] == 220.0
    assert chasing.default_config["min_pair_duration_s"] == 1.5
