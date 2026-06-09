"""Algorithm activation mapping used by the Savant runtime builder."""

from __future__ import annotations


BEHAVIOR_ALGORITHM_IDS = {
    "behavior.intrusion",
    "behavior.loitering",
    "behavior.crowd_gathering",
    "behavior.running",
    "behavior.chasing",
    "behavior.fall",
    "behavior.wall_climb_suspicious",
}

FACE_RULE_ALGORITHM_IDS = {
    "face.observation",
    "face.watchlist",
    "face.live_search",
}

ALGORITHM_ID_TO_RULE_TYPE = {
    "behavior.intrusion": "intrusion",
    "behavior.loitering": "loitering",
    "behavior.crowd_gathering": "crowd_gathering",
    "behavior.running": "running",
    "behavior.chasing": "chasing",
    "behavior.fall": "fall",
    "behavior.wall_climb_suspicious": "wall_climb",
}

LEGACY_ALIASES = {
    "intrusion": "behavior.intrusion",
    "loitering": "behavior.loitering",
    "crowd_gathering": "behavior.crowd_gathering",
    "running": "behavior.running",
    "chasing": "behavior.chasing",
    "fall": "behavior.fall",
    "wall_climb": "behavior.wall_climb_suspicious",
    "wall_climb_suspicious": "behavior.wall_climb_suspicious",
}


def normalize_algorithm_id(value: str) -> str:
    return LEGACY_ALIASES.get(value, value)


def behavior_rule_type_for_algorithm_id(value: str) -> str | None:
    return ALGORITHM_ID_TO_RULE_TYPE.get(normalize_algorithm_id(value))


def is_behavior_algorithm_id(value: str) -> bool:
    return normalize_algorithm_id(value) in BEHAVIOR_ALGORITHM_IDS


def is_face_rule_algorithm_id(value: str) -> bool:
    return normalize_algorithm_id(value) in FACE_RULE_ALGORITHM_IDS
