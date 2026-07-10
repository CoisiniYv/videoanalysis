"""Algorithm activation registry.

The registry is a contract layer only. It declares available algorithm
families, expected inputs, default config, and evidence defaults; it does not
implement behavior detection or face intelligence logic.
"""

from __future__ import annotations

from typing import Dict, Iterable

from app.algorithm_ids import (
    ALGORITHM_FAMILY_IDS,
    FACE_INTELLIGENCE_ALGORITHM_ID,
    RULE_ALGORITHM_IDS,
    normalize_algorithm_id,
)
from app.schemas.algorithms import (
    AlgorithmDefinition,
    AlgorithmSupportDefinition,
    EvidencePolicy,
)


def _policy(snapshot: bool = True, clip: bool = True) -> EvidencePolicy:
    return EvidencePolicy(
        snapshot_required=snapshot,
        clip_required=clip,
        pre_seconds=5,
        post_seconds=5,
    )


_REGISTRY: Dict[str, AlgorithmDefinition] = {
    "behavior.intrusion": AlgorithmDefinition(
        algorithm_id="behavior.intrusion",
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
    "behavior.loitering": AlgorithmDefinition(
        algorithm_id="behavior.loitering",
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
    "behavior.crowd_gathering": AlgorithmDefinition(
        algorithm_id="behavior.crowd_gathering",
        display_name="Crowd Gathering",
        category="behavior_rule",
        input_requirements=["person_bbox", "track_id", "roi_polygon"],
        supports_roi=True,
        supports_line=False,
        default_config={
            "min_person_count": 5,
            "exit_person_count": 3,
            "min_duration_s": 2,
            "eps_px": 180.0,
            "require_in_zone": True,
            "cooldown_s": 60,
        },
        evidence_policy=_policy(),
    ),
    "behavior.running": AlgorithmDefinition(
        algorithm_id="behavior.running",
        display_name="Running",
        category="behavior_rule",
        input_requirements=["person_bbox", "track_id", "track_velocity"],
        supports_roi=True,
        supports_line=False,
        default_config={"min_speed_px_s": 250, "min_duration_ms": 500, "cooldown_s": 20},
        evidence_policy=_policy(),
    ),
    "behavior.chasing": AlgorithmDefinition(
        algorithm_id="behavior.chasing",
        display_name="Chasing",
        category="behavior_rule",
        input_requirements=["person_bbox", "track_id", "multi_track_motion"],
        supports_roi=True,
        supports_line=False,
        default_config={
            "min_speed_px_s": 120.0,
            "max_distance_px": 220.0,
            "min_cos_alignment": 0.80,
            "speed_ratio_tolerance": 0.60,
            "behind_cos_min": 0.50,
            "velocity_window_ms": 700,
            "min_pair_duration_s": 1.5,
            "cooldown_s": 30,
        },
        evidence_policy=_policy(),
    ),
    "behavior.fall": AlgorithmDefinition(
        algorithm_id="behavior.fall",
        display_name="Fall",
        category="behavior_rule",
        input_requirements=["person_bbox", "keypoints", "track_id"],
        supports_roi=True,
        supports_line=False,
        default_config={
            "min_down_ms": 1500,
            "cooldown_s": 60,
            "require_transition": True,
            "lying_aspect_ratio": 0.85,
            "upright_aspect_ratio": 0.55,
            "torso_horizontal_deg": 45.0,
            "head_hip_collapse_ratio": 0.22,
            "min_visible_keypoints": 0,
            "keypoint_threshold": 0.25,
        },
        evidence_policy=_policy(),
    ),
    "behavior.wall_climb_suspicious": AlgorithmDefinition(
        algorithm_id="behavior.wall_climb_suspicious",
        display_name="Wall Climb Suspicious",
        category="behavior_rule",
        input_requirements=["person_bbox", "keypoints", "track_id", "line"],
        supports_roi=False,
        supports_line=True,
        default_config={"min_crossing_ms": 500, "max_crossing_ms": 5000, "cooldown_s": 60},
        evidence_policy=_policy(),
    ),
    FACE_INTELLIGENCE_ALGORITHM_ID: AlgorithmDefinition(
        algorithm_id=FACE_INTELLIGENCE_ALGORITHM_ID,
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
            "threshold": 0.75,
            "cooldown_s": 60,
            "target_person_ids": [],
            "target_external_person_ids": [],
            "target_names": [],
        },
        evidence_policy=_policy(snapshot=True, clip=True),
    ),
}


def _support(
    *,
    algorithm_id: str,
    display_name: str,
    category: str,
    configurable: bool,
    per_camera_gate: bool,
    runtime_detecting: bool,
    event_enabled: bool,
    evidence_enabled: bool,
    production_ready: bool,
    status: str,
    status_reason: str,
    requires_runtime_apply: bool = True,
) -> AlgorithmSupportDefinition:
    return AlgorithmSupportDefinition(
        algorithm_id=algorithm_id,
        display_name=display_name,
        category=category,
        configurable=configurable,
        per_camera_gate=per_camera_gate,
        runtime_detecting=runtime_detecting,
        event_enabled=event_enabled,
        evidence_enabled=evidence_enabled,
        production_ready=production_ready,
        status=status,
        status_reason=status_reason,
        requires_runtime_apply=requires_runtime_apply,
    )


_SUPPORT_MATRIX: Dict[str, AlgorithmSupportDefinition] = {
    "behavior.intrusion": _support(
        algorithm_id="behavior.intrusion",
        display_name="Intrusion",
        category="behavior",
        configurable=True,
        per_camera_gate=True,
        runtime_detecting=True,
        event_enabled=True,
        evidence_enabled=True,
        production_ready=True,
        status="production_ready",
        status_reason=(
            "baseline behavior rule: Savant consumes camera_rules and the "
            "event/evidence pipeline can materialize intrusion evidence"
        ),
    ),
    "behavior.crowd_gathering": _support(
        algorithm_id="behavior.crowd_gathering",
        display_name="Crowd Gathering",
        category="behavior",
        configurable=True,
        per_camera_gate=True,
        runtime_detecting=True,
        event_enabled=True,
        evidence_enabled=False,
        production_ready=False,
        status="event_only",
        status_reason=(
            "Savant rule is registered and can emit events, but non-intrusion "
            "behavior evidence still starts as not_implemented"
        ),
    ),
    "behavior.fall": _support(
        algorithm_id="behavior.fall",
        display_name="Fall",
        category="behavior",
        configurable=True,
        per_camera_gate=True,
        runtime_detecting=True,
        event_enabled=True,
        evidence_enabled=False,
        production_ready=False,
        status="event_only",
        status_reason=(
            "Savant rule is registered and can emit events, but non-intrusion "
            "behavior evidence still starts as not_implemented"
        ),
    ),
    "behavior.chasing": _support(
        algorithm_id="behavior.chasing",
        display_name="Chasing",
        category="behavior",
        configurable=True,
        per_camera_gate=True,
        runtime_detecting=True,
        event_enabled=True,
        evidence_enabled=False,
        production_ready=False,
        status="event_only",
        status_reason=(
            "Savant rule is registered and can emit events, but non-intrusion "
            "behavior evidence still starts as not_implemented"
        ),
    ),
    "behavior.loitering": _support(
        algorithm_id="behavior.loitering",
        display_name="Loitering",
        category="behavior",
        configurable=True,
        per_camera_gate=True,
        runtime_detecting=True,
        event_enabled=True,
        evidence_enabled=False,
        production_ready=False,
        status="event_only",
        status_reason=(
            "Savant rule is registered and can emit events, but non-intrusion "
            "behavior evidence still starts as not_implemented"
        ),
    ),
    "behavior.running": _support(
        algorithm_id="behavior.running",
        display_name="Running",
        category="behavior",
        configurable=True,
        per_camera_gate=True,
        runtime_detecting=True,
        event_enabled=True,
        evidence_enabled=False,
        production_ready=False,
        status="event_only",
        status_reason=(
            "Savant rule is registered and can emit events, but non-intrusion "
            "behavior evidence still starts as not_implemented"
        ),
    ),
    "behavior.wall_climb_suspicious": _support(
        algorithm_id="behavior.wall_climb_suspicious",
        display_name="Wall Climb Suspicious",
        category="behavior",
        configurable=False,
        per_camera_gate=False,
        runtime_detecting=False,
        event_enabled=False,
        evidence_enabled=False,
        production_ready=False,
        status="unsupported",
        status_reason=(
            "rule module is not registered in custom.rules; runtime would skip "
            "this camera_rules entry"
        ),
    ),
    "face.observation": _support(
        algorithm_id="face.observation",
        display_name="Face Observation",
        category="face",
        configurable=True,
        per_camera_gate=False,
        runtime_detecting=True,
        event_enabled=True,
        evidence_enabled=False,
        production_ready=False,
        status="config_only",
        status_reason=(
            "face_observation_exporter is controlled by the pipeline/env and "
            "does not use per-camera camera_rules as the runtime gate yet"
        ),
    ),
    "face.watchlist": _support(
        algorithm_id="face.watchlist",
        display_name="Watchlist Hit",
        category="face",
        configurable=True,
        per_camera_gate=True,
        runtime_detecting=True,
        event_enabled=True,
        evidence_enabled=True,
        production_ready=True,
        status="production_ready",
        status_reason=(
            "face-worker resolves enabled per-camera face.watchlist rules from "
            "camera_rules and emits image/latest-location evidence by default"
        ),
    ),
    "face.live_search": _support(
        algorithm_id="face.live_search",
        display_name="Live Search",
        category="face",
        configurable=False,
        per_camera_gate=False,
        runtime_detecting=True,
        event_enabled=True,
        evidence_enabled=True,
        production_ready=False,
        status="deferred",
        status_reason=(
            "live search uses face.watchlist image/latest-location evidence semantics"
        ),
        requires_runtime_apply=False,
    ),
}


def list_algorithms() -> list[AlgorithmDefinition]:
    return [_REGISTRY[algorithm_id] for algorithm_id in ALGORITHM_FAMILY_IDS]


def list_algorithm_support_matrix() -> list[AlgorithmSupportDefinition]:
    return [_SUPPORT_MATRIX[algorithm_id] for algorithm_id in RULE_ALGORITHM_IDS]


def get_algorithm(algorithm_id: str) -> AlgorithmDefinition | None:
    return _REGISTRY.get(normalize_algorithm_id(algorithm_id))


def get_algorithm_support(algorithm_id: str) -> AlgorithmSupportDefinition | None:
    return _SUPPORT_MATRIX.get(normalize_algorithm_id(algorithm_id))


def runtime_apply_state_for_algorithm(
    algorithm_id: str,
    *,
    rule_enabled: bool = True,
    camera_enabled: bool = True,
) -> dict[str, object]:
    support = get_algorithm_support(algorithm_id)
    if support is None:
        return {
            "support_status": "unsupported",
            "support_status_reason": f"unknown algorithm_id: {algorithm_id}",
            "runtime_apply_state": "unsupported",
            "runtime_consumed": False,
            "runtime_skip_reason": "unknown_algorithm",
        }

    if not rule_enabled:
        return {
            "support_status": support.status,
            "support_status_reason": support.status_reason,
            "runtime_apply_state": "skipped",
            "runtime_consumed": False,
            "runtime_skip_reason": "rule_disabled",
        }
    if not camera_enabled:
        return {
            "support_status": support.status,
            "support_status_reason": support.status_reason,
            "runtime_apply_state": "skipped",
            "runtime_consumed": False,
            "runtime_skip_reason": "camera_disabled",
        }
    if support.status == "unsupported":
        return {
            "support_status": support.status,
            "support_status_reason": support.status_reason,
            "runtime_apply_state": "unsupported",
            "runtime_consumed": False,
            "runtime_skip_reason": "unsupported_algorithm",
        }
    if support.status == "deferred":
        return {
            "support_status": support.status,
            "support_status_reason": support.status_reason,
            "runtime_apply_state": "skipped",
            "runtime_consumed": False,
            "runtime_skip_reason": "deferred_algorithm",
        }
    if support.status == "config_only":
        return {
            "support_status": support.status,
            "support_status_reason": support.status_reason,
            "runtime_apply_state": "skipped",
            "runtime_consumed": False,
            "runtime_skip_reason": "config_only_not_runtime_gate",
        }
    return {
        "support_status": support.status,
        "support_status_reason": support.status_reason,
        "runtime_apply_state": "applied",
        "runtime_consumed": True,
        "runtime_skip_reason": "",
    }


def require_algorithm(algorithm_id: str) -> AlgorithmDefinition:
    definition = get_algorithm(algorithm_id)
    if definition is None:
        raise ValueError(f"unknown algorithm_id: {algorithm_id}")
    return definition


def known_algorithm_ids() -> Iterable[str]:
    return _REGISTRY.keys()


def known_algorithm_types() -> Iterable[str]:
    """Compatibility alias for older call sites."""
    return known_algorithm_ids()
