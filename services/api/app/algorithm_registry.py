"""R3 algorithm registry.

The registry is a contract layer only. It declares available algorithm
families, expected inputs, default config, and evidence defaults; it does not
implement behavior detection or face intelligence logic.
"""

from __future__ import annotations

from typing import Dict, Iterable

from app.schemas.algorithms import AlgorithmDefinition, EvidencePolicy


def _policy(snapshot: bool = True, clip: bool = True) -> EvidencePolicy:
    return EvidencePolicy(
        snapshot_required=snapshot,
        clip_required=clip,
        pre_seconds=5,
        post_seconds=10,
    )


_REGISTRY: Dict[str, AlgorithmDefinition] = {
    "intrusion": AlgorithmDefinition(
        algorithm_type="intrusion",
        display_name="Intrusion",
        category="behavior_rule",
        input_requirements=["person_bbox", "track_id", "roi_polygon"],
        supports_roi=True,
        supports_line=False,
        default_config={"min_inside_ms": 1000, "cooldown_s": 30},
        config_schema={
            "required": ["min_inside_ms", "cooldown_s"],
            "properties": {
                "min_inside_ms": {"type": "integer", "minimum": 1},
                "cooldown_s": {"type": "integer", "minimum": 0},
            },
        },
        evidence_policy=_policy(),
        evidence_policy_schema={"pre_seconds": {"minimum": 0}, "post_seconds": {"minimum": 0}},
        enabled=True,
    ),
    "loitering": AlgorithmDefinition(
        algorithm_type="loitering",
        display_name="Loitering",
        category="behavior_rule",
        input_requirements=["person_bbox", "track_id", "roi_polygon"],
        supports_roi=True,
        supports_line=False,
        default_config={
            "min_duration_s": 60,
            "max_avg_speed_px_s": 20,
            "cooldown_s": 60,
        },
        evidence_policy=_policy(),
    ),
    "crowd_gathering": AlgorithmDefinition(
        algorithm_type="crowd_gathering",
        display_name="Crowd Gathering",
        category="behavior_rule",
        input_requirements=["person_bbox", "track_id", "roi_polygon"],
        supports_roi=True,
        supports_line=False,
        default_config={"min_person_count": 5, "min_duration_s": 10, "cooldown_s": 60},
        evidence_policy=_policy(),
    ),
    "running": AlgorithmDefinition(
        algorithm_type="running",
        display_name="Running",
        category="behavior_rule",
        input_requirements=["person_bbox", "track_id", "track_velocity"],
        supports_roi=True,
        supports_line=False,
        default_config={"min_speed_px_s": 250, "min_duration_ms": 500, "cooldown_s": 20},
        evidence_policy=_policy(),
    ),
    "chasing": AlgorithmDefinition(
        algorithm_type="chasing",
        display_name="Chasing",
        category="behavior_rule",
        input_requirements=["person_bbox", "track_id", "multi_track_motion"],
        supports_roi=True,
        supports_line=False,
        default_config={"min_pair_duration_s": 2, "max_distance_px": 180, "cooldown_s": 30},
        evidence_policy=_policy(),
    ),
    "fall": AlgorithmDefinition(
        algorithm_type="fall",
        display_name="Fall",
        category="behavior_rule",
        input_requirements=["person_bbox", "keypoints", "track_id"],
        supports_roi=True,
        supports_line=False,
        default_config={"min_down_ms": 1500, "cooldown_s": 60},
        evidence_policy=_policy(),
    ),
    "wall_climb": AlgorithmDefinition(
        algorithm_type="wall_climb",
        display_name="Wall Climb Suspicious",
        category="behavior_rule",
        input_requirements=["person_bbox", "keypoints", "track_id", "line"],
        supports_roi=False,
        supports_line=True,
        default_config={"min_crossing_ms": 500, "max_crossing_ms": 5000, "cooldown_s": 60},
        evidence_policy=_policy(),
    ),
    "face_intelligence": AlgorithmDefinition(
        algorithm_type="face_intelligence",
        display_name="Face Intelligence",
        category="face_intelligence",
        input_requirements=[
            "face_bbox",
            "landmarks",
            "person_track_id",
            "adaface_embedding",
        ],
        supports_roi=False,
        supports_line=False,
        default_config={
            "watchlist_enabled": True,
            "live_search_enabled": True,
            "min_similarity": 0.75,
            "cooldown_s": 60,
        },
        evidence_policy=_policy(snapshot=True, clip=True),
    ),
}


def list_algorithms() -> list[AlgorithmDefinition]:
    return list(_REGISTRY.values())


def get_algorithm(algorithm_type: str) -> AlgorithmDefinition | None:
    return _REGISTRY.get(algorithm_type)


def require_algorithm(algorithm_type: str) -> AlgorithmDefinition:
    definition = get_algorithm(algorithm_type)
    if definition is None:
        raise ValueError(f"unknown algorithm_type: {algorithm_type}")
    return definition


def known_algorithm_types() -> Iterable[str]:
    return _REGISTRY.keys()
