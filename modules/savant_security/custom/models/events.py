"""SecurityEvent — canonical structured security event with standardized schema.

Schema version ``1.0``.

All time fields are milliseconds since epoch.
``source_event_id`` is generated per-project convention::

    {producer}:{camera_id}:{track_id_or_none}:{event_type}:{start_ts_ms}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

SECURITY_EVENT_SCHEMA_VERSION = "1.0"

BEHAVIOR_ALGORITHM_TYPES = (
    "behavior.intrusion",
    "behavior.loitering",
    "behavior.crowd_gathering",
    "behavior.running",
    "behavior.chasing",
    "behavior.fall",
    "behavior.wall_climb_suspicious",
)

FACE_INTELLIGENCE_ALGORITHM_TYPE = "face_intelligence"

ALGORITHM_TYPES = BEHAVIOR_ALGORITHM_TYPES + (FACE_INTELLIGENCE_ALGORITHM_TYPE,)

SECURITY_EVENT_TYPES = (
    "intrusion",
    "loitering",
    "crowd_gathering",
    "running",
    "chasing",
    "fall",
    "wall_climb_suspicious",
    "face_observed",
    "watchlist_hit",
    "live_search_hit",
)


def build_source_event_id(
    producer: str,
    camera_id: str,
    track_id: int,
    event_type: str,
    start_ts_ms: int,
) -> str:
    """Generate a deterministic ``source_event_id``.

    Format: ``{producer}:{camera_id}:{track_id_or_none}:{event_type}:{start_ts_ms}``

    When ``track_id <= 0`` the segment becomes the literal ``"none"`` so the
    format stays consistent across tracked and untracked objects.
    """
    track_part = str(track_id) if track_id > 0 else "none"
    return f"{producer}:{camera_id}:{track_part}:{event_type}:{start_ts_ms}"


@dataclass
class SecurityEvent:
    """A structured security event emitted by a behavior rule.

    Fields follow the standardised SecurityEvent schema (``schema_version``
    ``"1.0"``).  All fields are included in serialization, including
    ``None`` values, so downstream consumers see a predictable shape.
    """

    schema_version: str = SECURITY_EVENT_SCHEMA_VERSION
    source_event_id: str = ""
    producer: str = "savant_security"
    gpu_id: int = 0
    event_type: str = ""
    camera_id: str = ""
    source_id: str = ""
    track_id: str | None = None
    person_id: int | None = None
    algorithm_type: str = ""
    algorithm_version: Optional[str] = None
    start_ts_ms: int = 0
    end_ts_ms: Optional[int] = None
    event_ts_ms: int = 0
    frame_id: int = 0
    frame_uuid: Optional[str] = None
    keyframe_uuid: Optional[str] = None
    confidence: float = 0.0
    severity: str = "medium"
    zone: str = ""
    rule_name: str = ""
    description: str = ""
    snapshot_required: bool = False
    clip_required: bool = False
    evidence_policy: Dict[str, Any] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Fill midterm-compatible defaults while preserving older producers."""
        if not self.algorithm_type and self.event_type:
            if self.event_type in (
                "face_observed",
                "watchlist_hit",
                "live_search_hit",
            ):
                self.algorithm_type = FACE_INTELLIGENCE_ALGORITHM_TYPE
            elif self.event_type == "wall_climb_suspicious":
                self.algorithm_type = "behavior.wall_climb_suspicious"
            else:
                self.algorithm_type = f"behavior.{self.event_type}"

        if self.end_ts_ms is None and self.start_ts_ms:
            self.end_ts_ms = self.start_ts_ms

        if not self.event_ts_ms:
            self.event_ts_ms = int(self.start_ts_ms or 0)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict.

        Uses ``dataclasses.asdict`` for deep conversion of nested fields.
        """
        from dataclasses import asdict

        return asdict(self)

    def to_json(self, **kwargs) -> str:
        """Serialize to a JSON string.

        Args:
            **kwargs: Passed through to ``json.dumps``.
        """
        return json.dumps(self.to_dict(), **kwargs)
