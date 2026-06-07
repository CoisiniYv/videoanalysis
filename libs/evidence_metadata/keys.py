"""Deterministic cache key helpers for C1J evidence metadata."""

from __future__ import annotations


def _require_key_part(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    if any(ch.isspace() for ch in value):
        raise ValueError(f"invalid_{field_name}_contains_space")
    return value


def frame_pts_key(source_id: str, frame_pts: int) -> str:
    source = _require_key_part(source_id, "source_id")
    if not isinstance(frame_pts, int) or isinstance(frame_pts, bool):
        raise ValueError("invalid_frame_pts")
    return f"frameann:{source}:pts:{frame_pts}"


def frame_uuid_key(source_id: str, frame_uuid: str) -> str:
    source = _require_key_part(source_id, "source_id")
    uuid = _require_key_part(frame_uuid, "frame_uuid")
    return f"frameann:{source}:uuid:{uuid}"


def identity_patch_key(source_observation_id: str) -> str:
    source_observation = _require_key_part(
        source_observation_id,
        "source_observation_id",
    )
    return f"idpatch:{source_observation}"
