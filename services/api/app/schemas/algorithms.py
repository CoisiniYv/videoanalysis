"""Schemas for algorithm registry and per-camera algorithm rules."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.algorithm_ids import (
    ALGORITHM_FAMILY_IDS,
    FACE_RULE_ALGORITHM_IDS,
    RULE_ALGORITHM_IDS,
    normalize_algorithm_id,
)

ALGORITHM_IDS = ALGORITHM_FAMILY_IDS
RULE_ALGORITHM_IDS_ALLOWED = RULE_ALGORITHM_IDS

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
    algorithm_id: str
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

    @field_validator("algorithm_id")
    @classmethod
    def _algorithm_id_known(cls, value: str) -> str:
        normalized = normalize_algorithm_id(value)
        if normalized not in ALGORITHM_IDS:
            raise ValueError(f"unknown algorithm_id: {value}")
        return normalized

    @property
    def algorithm_type(self) -> str:
        """Compatibility alias for older tests/clients."""
        return self.algorithm_id


class AlgorithmRuleCreate(BaseModel):
    algorithm_id: Optional[str] = None
    algorithm_type: Optional[str] = None
    rule_id: Optional[str] = None
    enabled: bool = True
    zone_id: Optional[str] = None
    line_id: Optional[str] = None
    severity: str = "medium"
    config: Dict[str, Any] = Field(default_factory=dict)
    evidence_policy: EvidencePolicy = Field(default_factory=EvidencePolicy)

    @model_validator(mode="after")
    def _normalize_algorithm_id(self) -> "AlgorithmRuleCreate":
        value = self.algorithm_id or self.algorithm_type
        if not value:
            raise ValueError("algorithm_id is required")
        normalized = normalize_algorithm_id(value)
        if normalized not in RULE_ALGORITHM_IDS_ALLOWED:
            raise ValueError(f"unknown algorithm_id: {value}")
        self.algorithm_id = normalized
        self.algorithm_type = normalized
        if not self.rule_id:
            self.rule_id = f"rule_{normalized.replace('.', '_')}"
        return self

    @field_validator("rule_id", "zone_id", "line_id")
    @classmethod
    def _non_empty_optional(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and (not value or not value.strip()):
            raise ValueError("must be non-empty when provided")
        return value

    @field_validator("severity")
    @classmethod
    def _severity_allowed(cls, value: str) -> str:
        if value not in SEVERITIES:
            raise ValueError(f"severity must be one of {list(SEVERITIES)}")
        return value


class AlgorithmRuleUpdate(BaseModel):
    algorithm_id: Optional[str] = None
    algorithm_type: Optional[str] = None
    enabled: Optional[bool] = None
    zone_id: Optional[str] = None
    line_id: Optional[str] = None
    severity: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    evidence_policy: Optional[EvidencePolicy] = None

    @field_validator("severity")
    @classmethod
    def _severity_allowed(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in SEVERITIES:
            raise ValueError(f"severity must be one of {list(SEVERITIES)}")
        return value

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "AlgorithmRuleUpdate":
        if (
            self.algorithm_id is None
            and self.algorithm_type is None
            and
            self.enabled is None
            and self.zone_id is None
            and self.line_id is None
            and self.severity is None
            and self.config is None
            and self.evidence_policy is None
        ):
            raise ValueError("at least one rule field must be provided")
        if self.algorithm_id or self.algorithm_type:
            value = self.algorithm_id or self.algorithm_type or ""
            normalized = normalize_algorithm_id(value)
            if normalized not in RULE_ALGORITHM_IDS_ALLOWED:
                raise ValueError(f"unknown algorithm_id: {value}")
            self.algorithm_id = normalized
            self.algorithm_type = normalized
        return self


class AlgorithmRuleResponse(BaseModel):
    id: Optional[int] = None
    rule_id: str
    camera_id: str
    algorithm_id: str
    algorithm_type: str
    family_algorithm_id: str
    enabled: bool
    zone_id: Optional[str] = None
    line_id: Optional[str] = None
    severity: str = "medium"
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
            id=int(row["id"]) if row.get("id") is not None else None,
            rule_id=str(row.get("rule_id") or row.get("id") or ""),
            camera_id=row["camera_id"],
            algorithm_id=normalize_algorithm_id(
                row.get("algorithm_id") or row.get("algorithm_type") or row.get("rule_type", "")
            ),
            algorithm_type=normalize_algorithm_id(
                row.get("algorithm_id") or row.get("algorithm_type") or row.get("rule_type", "")
            ),
            family_algorithm_id=(
                "face_intelligence"
                if normalize_algorithm_id(
                    row.get("algorithm_id")
                    or row.get("algorithm_type")
                    or row.get("rule_type", "")
                )
                in FACE_RULE_ALGORITHM_IDS
                else normalize_algorithm_id(
                    row.get("algorithm_id")
                    or row.get("algorithm_type")
                    or row.get("rule_type", "")
                )
            ),
            enabled=bool(row.get("enabled", True)),
            zone_id=row.get("zone_id") or config.get("zone_id"),
            line_id=row.get("line_id") or config.get("line_id"),
            severity=str(row.get("severity") or config.get("severity") or "medium"),
            config=config.get("config", config),
            evidence_policy=EvidencePolicy(**evidence_policy),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


class SecurityEventContract(BaseModel):
    """API-side mirror of the unified SecurityEvent contract."""

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
