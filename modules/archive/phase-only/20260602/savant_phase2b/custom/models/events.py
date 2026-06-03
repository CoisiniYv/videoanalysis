"""SecurityEvent — canonical structured output from behavior rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class SecurityEvent:
    """A structured security event emitted by a behavior rule.

    All time fields are milliseconds since epoch.
    """

    source_event_id: str = ""
    event_type: str = ""
    camera_id: str = ""
    source_id: str = ""
    track_id: int = 0
    start_ts_ms: int = 0
    end_ts_ms: int = 0
    confidence: float = 0.0
    severity: str = "medium"
    zone: str = ""
    rule_name: str = ""
    description: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
