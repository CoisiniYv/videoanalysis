"""Algorithm ID constants and mapping helpers for activation config.

External API/config contracts use ``algorithm_id``. Behavior rules still use
short in-process ``rule_type`` values when instantiated in Savant.
"""

from __future__ import annotations

from typing import Iterable


BEHAVIOR_ALGORITHM_IDS = (
    "behavior.intrusion",
    "behavior.loitering",
    "behavior.crowd_gathering",
    "behavior.running",
    "behavior.chasing",
    "behavior.fall",
    "behavior.wall_climb_suspicious",
)

FACE_INTELLIGENCE_ALGORITHM_ID = "face_intelligence"

FACE_RULE_ALGORITHM_IDS = (
    "face.observation",
    "face.watchlist",
    "face.live_search",
)

ALGORITHM_FAMILY_IDS = BEHAVIOR_ALGORITHM_IDS + (FACE_INTELLIGENCE_ALGORITHM_ID,)
RULE_ALGORITHM_IDS = BEHAVIOR_ALGORITHM_IDS + FACE_RULE_ALGORITHM_IDS

LEGACY_ALGORITHM_ID_ALIASES = {
    "intrusion": "behavior.intrusion",
    "loitering": "behavior.loitering",
    "crowd_gathering": "behavior.crowd_gathering",
    "running": "behavior.running",
    "chasing": "behavior.chasing",
    "fall": "behavior.fall",
    "wall_climb": "behavior.wall_climb_suspicious",
    "wall_climb_suspicious": "behavior.wall_climb_suspicious",
}

BEHAVIOR_RULE_TYPE_BY_ALGORITHM_ID = {
    "behavior.intrusion": "intrusion",
    "behavior.loitering": "loitering",
    "behavior.crowd_gathering": "crowd_gathering",
    "behavior.running": "running",
    "behavior.chasing": "chasing",
    "behavior.fall": "fall",
    "behavior.wall_climb_suspicious": "wall_climb",
}


def normalize_algorithm_id(value: str) -> str:
    """Return the canonical external algorithm_id for a legacy or new value."""
    return LEGACY_ALGORITHM_ID_ALIASES.get(value, value)


def is_algorithm_family_id(value: str) -> bool:
    return normalize_algorithm_id(value) in ALGORITHM_FAMILY_IDS


def is_rule_algorithm_id(value: str) -> bool:
    return normalize_algorithm_id(value) in RULE_ALGORITHM_IDS


def is_behavior_algorithm_id(value: str) -> bool:
    return normalize_algorithm_id(value) in BEHAVIOR_ALGORITHM_IDS


def is_face_rule_algorithm_id(value: str) -> bool:
    return normalize_algorithm_id(value) in FACE_RULE_ALGORITHM_IDS


def behavior_rule_type_for_algorithm_id(value: str) -> str | None:
    return BEHAVIOR_RULE_TYPE_BY_ALGORITHM_ID.get(normalize_algorithm_id(value))


def runtime_rule_type_for_algorithm_id(value: str) -> str:
    """Return the stored/runtime rule_type for a camera rule instance."""
    normalized = normalize_algorithm_id(value)
    return behavior_rule_type_for_algorithm_id(normalized) or normalized


def family_algorithm_id_for_rule(value: str) -> str:
    normalized = normalize_algorithm_id(value)
    if normalized in FACE_RULE_ALGORITHM_IDS:
        return FACE_INTELLIGENCE_ALGORITHM_ID
    return normalized


def known_rule_algorithm_ids() -> Iterable[str]:
    return RULE_ALGORITHM_IDS
