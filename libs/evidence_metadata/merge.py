"""Pure identity-patch merge helpers for frame annotation messages."""

from __future__ import annotations

import copy
from typing import Any

from .validation import validate_identity_patch_message


def merge_identity_patches(
    frame_message: dict[str, Any],
    identity_patches: list[dict[str, Any]],
    *,
    trigger_source_observation_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge identity patches into a frame annotation message.

    The function does not mutate its inputs. Patches only match when
    `source_observation_id`, `source_id`, and `camera_id` all agree with the
    frame message/object context. If duplicate patches target the same
    observation, the first valid patch wins.
    """

    merged = copy.deepcopy(frame_message)
    source_id = merged.get("source_id")
    camera_id = merged.get("camera_id")

    patch_index: dict[str, dict[str, Any]] = {}
    duplicate_ignored = 0
    source_camera_mismatch = 0
    for patch in identity_patches:
        normalized = validate_identity_patch_message(patch)
        if normalized.get("source_id") != source_id or normalized.get("camera_id") != camera_id:
            source_camera_mismatch += 1
            continue
        source_observation_id = normalized["source_observation_id"]
        if source_observation_id in patch_index:
            duplicate_ignored += 1
            continue
        patch_index[source_observation_id] = normalized

    matched_patch_ids: set[str] = set()
    known_face_count = 0
    unknown_face_count = 0
    trigger_known_face_present = False

    objects = merged.get("objects", [])
    if not isinstance(objects, list):
        objects = []
        merged["objects"] = objects

    for obj in objects:
        if not isinstance(obj, dict):
            continue
        object_type = obj.get("object_type")
        label = obj.get("label")
        if not isinstance(label, dict):
            label = {}
            obj["label"] = label

        if object_type == "face":
            source_observation_id = obj.get("source_observation_id")
            patch = patch_index.get(source_observation_id)
            if patch is not None:
                matched_patch_ids.add(source_observation_id)
                label["kind"] = "known_face"
                display = (
                    patch.get("display_name")
                    or patch.get("external_person_id")
                    or patch.get("person_id")
                )
                if display is not None:
                    label["display_name"] = str(display)
                label["person_id"] = patch.get("person_id")
                label["external_person_id"] = patch.get("external_person_id")
                label["similarity"] = patch.get("similarity")
                label["threshold"] = patch.get("threshold")
                obj["source"] = "identity_patch"
                if source_observation_id == trigger_source_observation_id:
                    obj["annotation_role"] = "watchlist_trigger_face"
                    trigger_known_face_present = True
                known_face_count += 1
            else:
                if label.get("kind") is None:
                    label["kind"] = "unknown_face"
                if label.get("kind") == "unknown_face":
                    unknown_face_count += 1
        elif object_type == "person":
            if label.get("kind") is None:
                label["kind"] = "person"

    requested = len(identity_patches)
    matched = len(matched_patch_ids)
    unmatched = requested - matched

    if trigger_source_observation_id is not None:
        annotation_status = (
            "complete"
            if trigger_known_face_present
            else "missing_trigger_face_annotation"
        )
    elif requested > 0 and matched == 0:
        annotation_status = "partial"
    else:
        annotation_status = "complete"

    summary = {
        "identity_patches_requested": requested,
        "identity_patches_matched": matched,
        "identity_patches_unmatched": unmatched,
        "identity_patches_duplicate_ignored": duplicate_ignored,
        "identity_patches_source_camera_mismatch": source_camera_mismatch,
        "known_face_count": known_face_count,
        "unknown_face_count": unknown_face_count,
        "trigger_known_face_present": trigger_known_face_present,
        "trigger_source_observation_id": trigger_source_observation_id,
        "annotation_status": annotation_status,
    }
    return merged, summary
