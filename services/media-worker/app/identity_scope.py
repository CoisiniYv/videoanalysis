"""Strict watchlist identity scoping helpers for evidence annotations."""

from __future__ import annotations

import copy
from typing import Any


UNKNOWN_IDENTITY = {
    "status": "unknown",
    "person_id": None,
    "external_person_id": None,
    "display_name": "",
    "similarity": None,
    "rank": None,
    "threshold": None,
    "match_status": "not_searched",
}


def apply_watchlist_identity_strict(
    annotation: dict[str, Any],
    *,
    trigger_source_observation_id: str | None,
    identity: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply watchlist identity only to the exact trigger source observation.

    The input annotation is never mutated. Non-face objects are returned
    unchanged. Unknown/mismatched face rows keep an unknown identity and carry a
    skip reason so downstream audits can distinguish missing and mismatched
    scope from true unknown faces.
    """

    result = copy.deepcopy(annotation)
    if result.get("object_type") != "face":
        return result

    source_observation_id = _source_observation_id(result)
    trigger = str(trigger_source_observation_id or "").strip()
    incoming = copy.deepcopy(identity) if isinstance(identity, dict) else {}
    if not trigger:
        return _mark_unknown(result, "missing_trigger_source_observation_id")
    if not source_observation_id:
        return _mark_unknown(result, "missing_source_observation_id")
    if source_observation_id != trigger:
        return _mark_unknown(result, "source_observation_id_mismatch")

    known_identity = {
        "status": incoming.get("status") or "matched",
        "person_id": incoming.get("person_id"),
        "external_person_id": incoming.get("external_person_id"),
        "display_name": incoming.get("display_name") or "",
        "similarity": incoming.get("similarity"),
        "rank": incoming.get("rank"),
        "threshold": incoming.get("threshold"),
        "match_status": incoming.get("match_status") or "above_threshold",
    }
    result["identity"] = known_identity
    result["annotation_role"] = "watchlist_trigger_face"
    label = _label(result)
    label.update(
        {
            "kind": "known_face",
            "display_name": known_identity["display_name"],
            "person_id": known_identity["person_id"],
            "external_person_id": known_identity["external_person_id"],
            "similarity": known_identity["similarity"],
            "threshold": known_identity["threshold"],
        }
    )
    result["label"] = label
    result.pop("identity_skip_reason", None)
    return result


def unknown_watchlist_identity(
    *,
    threshold: Any = None,
    skip_reason: str = "not_searched",
) -> dict[str, Any]:
    identity = copy.deepcopy(UNKNOWN_IDENTITY)
    identity["threshold"] = threshold
    identity["identity_skip_reason"] = skip_reason
    return identity


def _mark_unknown(annotation: dict[str, Any], reason: str) -> dict[str, Any]:
    existing_identity = annotation.get("identity") if isinstance(annotation.get("identity"), dict) else {}
    identity = copy.deepcopy(UNKNOWN_IDENTITY)
    identity["threshold"] = existing_identity.get("threshold")
    identity["identity_skip_reason"] = reason
    annotation["identity"] = identity
    annotation["identity_skip_reason"] = reason
    label = _label(annotation)
    if label.get("kind") in (None, "", "known_face", "unknown_face"):
        label["kind"] = "unknown_face"
    for key in (
        "display_name",
        "person_id",
        "external_person_id",
        "similarity",
        "threshold",
        "rank",
        "text",
    ):
        label.pop(key, None)
    annotation["label"] = label
    annotation.pop("annotation_role", None)
    return annotation


def _source_observation_id(annotation: dict[str, Any]) -> str:
    value = annotation.get("source_observation_id")
    if value not in (None, ""):
        return str(value)
    return ""


def _label(annotation: dict[str, Any]) -> dict[str, Any]:
    label = annotation.get("label")
    return copy.deepcopy(label) if isinstance(label, dict) else {}
