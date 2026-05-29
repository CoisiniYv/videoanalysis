"""Schemas for R3 algorithm registry and camera algorithm rules."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


ALGORITHM_TYPES = (
    "intrusion",
    "loitering",
    "crowd_gathering",
    "running",
    "chasing",
    "fall",
    "wall_climb",
    "face_intelligence",
)

EVENT_TYPES = (
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

SEVERITIES = ("low", "medium", "high", "critical", "info")


def _iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


class EvidencePolicy(BaseModel):
    """Unified snapshot/clip policy shared by every algorithm rule."""

    snapshot_required: bool = True
    clip_required: bool = True
    pre_seconds: int = Field(default=5, ge=0, le=300)
    post_seconds: int = Field(default=10, ge=0, le=300)


class AlgorithmDefinition(BaseModel):
    algorithm_type: str
    display_name: str
    category: str
    input_requirements: List[str] = Field(default_factory=list)
    supports_roi: bool = False
    supports_line: bool = False
    default_config: Dict[str, Any] = Field(default_factory=dict)
    config_schema: Dict[str, Any] = Field(default_factory=dict)
    evidence_policy_schema: Dict[str, Any] = Field(default_factory=dict)
    evidence_policy: EvidencePolicy = Field(default_factory=EvidencePolicy)
    enabled: bool = True

    @field_validator("algorithm_type")
    @classmethod
    def _algorithm_type_known(cls, value: str) -> str:
        if value not in ALGORITHM_TYPES:
            raise ValueError(f"unknown algorithm_type: {value}")
        return value


class AlgorithmRuleCreate(BaseModel):
    algorithm_type: str
    enabled: bool = True
    zone_id: Optional[str] = None
    line_id: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    evidence_policy: EvidencePolicy = Field(default_factory=EvidencePolicy)

    @field_validator("algorithm_type")
    @classmethod
    def _algorithm_type_known(cls, value: str) -> str:
        if value not in ALGORITHM_TYPES:
            raise ValueError(f"unknown algorithm_type: {value}")
        return value


class AlgorithmRuleUpdate(BaseModel):
    enabled: Optional[bool] = None
    zone_id: Optional[str] = None
    line_id: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    evidence_policy: Optional[EvidencePolicy] = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "AlgorithmRuleUpdate":
        if (
            self.enabled is None
            and self.zone_id is None
            and self.line_id is None
            and self.config is None
            and self.evidence_policy is None
        ):
            raise ValueError("at least one rule field must be provided")
        return self


class AlgorithmRuleResponse(BaseModel):
    rule_id: int
    camera_id: str
    algorithm_type: str
    enabled: bool
    zone_id: Optional[str] = None
    line_id: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    evidence_policy: EvidencePolicy = Field(default_factory=EvidencePolicy)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: Dict[str, Any]) -> "AlgorithmRuleResponse":
        import json

        config = row.get("config") or {}
        if isinstance(config, str):
            config = json.loads(config)

        evidence_policy = row.get("evidence_policy") or config.get(
            "evidence_policy", {}
        )
        if isinstance(evidence_policy, str):
            evidence_policy = json.loads(evidence_policy)

        return cls(
            rule_id=int(row["id"]),
            camera_id=row["camera_id"],
            algorithm_type=row.get("algorithm_type") or row.get("rule_type", ""),
            enabled=bool(row.get("enabled", True)),
            zone_id=row.get("zone_id") or config.get("zone_id"),
            line_id=row.get("line_id") or config.get("line_id"),
            config=config.get("config", config),
            evidence_policy=EvidencePolicy(**evidence_policy),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


class SecurityEventContract(BaseModel):
    """API-side mirror of the unified R3 SecurityEvent contract."""

    event_type: str
    source_event_id: str
    camera_id: str
    source_id: str
    track_id: Optional[str] = None
    person_id: Optional[int] = None
    algorithm_type: str
    algorithm_version: Optional[str] = None
    severity: str = "medium"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    start_ts_ms: int
    end_ts_ms: Optional[int] = None
    snapshot_required: bool = False
    clip_required: bool = False
    evidence_policy: Dict[str, Any] = Field(default_factory=dict)
    payload: Dict[str, Any] = Field(default_factory=dict)


class EvidenceTask(BaseModel):
    """Unified event evidence task contract."""

    task_id: str
    event_id: int | str
    source_event_id: str
    camera_id: str
    source_id: str
    event_type: str
    event_ts_ms: int
    snapshot_required: bool
    clip_required: bool
    pre_seconds: int = Field(default=5, ge=0)
    post_seconds: int = Field(default=10, ge=0)
    status: str = "pending"
    snapshot_path: Optional[str] = None
    clip_path: Optional[str] = None
    metadata_path: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
