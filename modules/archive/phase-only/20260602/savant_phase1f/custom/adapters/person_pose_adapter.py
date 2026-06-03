"""Adapt Savant official frame metadata into ``PersonPoseObservation`` objects.

This module is the bridge between the Savant DeepStream pipeline and the
internal domain model.  Downstream behavior rules consume only
``PersonPoseObservation`` — they do not directly access DeepStream metadata.

Usage::

    from custom.adapters.person_pose_adapter import build_person_pose_observations

    result = build_person_pose_observations(frame_meta, camera_id="cam_01")
    for obs in result.observations:
        ...
"""

from __future__ import annotations

import time
from typing import List, Optional

from custom.models.pose import (
    AdapterResult,
    BBox,
    Keypoint,
    PersonPoseObservation,
    is_valid_track_id,
    parse_keypoints,
)


def _get_timestamp_ms(frame_meta) -> int:
    """Best-effort frame timestamp in milliseconds.

    Tries (in order):
      1. ``frame_meta.ntp_timestamp``  (Savant wrapper)
      2. ``frame_meta.buf_pts``        (DeepStream buffer PTS, ns)
      3. ``time.time() * 1000``        (wall clock fallback)
    """
    try:
        ntp = getattr(frame_meta, "ntp_timestamp", None)
        if ntp is not None:
            return int(float(ntp))
    except Exception:
        pass

    try:
        pts = getattr(frame_meta, "buf_pts", None)
        if pts is not None and int(pts) > 0:
            return int(int(pts) // 1_000_000)
    except Exception:
        pass

    return int(time.time() * 1000)


def _compute_keypoint_confidence(keypoints: List[Keypoint]) -> float:
    """Average confidence across all keypoints, or 0.0 for empty list."""
    if not keypoints:
        return 0.0
    return sum(kp.confidence for kp in keypoints) / len(keypoints)


def build_person_pose_observations(
    frame_meta,
    camera_id: Optional[str] = None,
) -> AdapterResult:
    """Build ``AdapterResult`` from a Savant ``frame_meta``.

    Uses the official Savant metadata API (``frame_meta.objects``,
    ``obj_meta.get_attr_meta``) — the same path validated in Phase 1E.

    Only objects whose ``label == "person"`` or ``element_name == "yolo26_pose"``
    are included.  Objects without a valid tracker-assigned ``track_id``
    are still included (their ``track_id`` will be 0); their count is reported
    via ``skipped_untracked_person_count``.

    Args:
        frame_meta: A Savant ``NvDsFrameMeta``-compatible frame metadata object.
        camera_id: Optional camera identifier.  Falls back to ``source_id``.

    Returns:
        ``AdapterResult`` with observations and skip counters.
    """
    observations: List[PersonPoseObservation] = []

    source_id = str(getattr(frame_meta, "source_id", ""))
    effective_camera_id = camera_id or source_id
    frame_id = int(getattr(frame_meta, "frame_num", 0))
    timestamp_ms = _get_timestamp_ms(frame_meta)

    skipped_untracked = 0
    skipped_no_kpts = 0
    skipped_invalid_kpts = 0

    for obj_meta in frame_meta.objects:
        label = str(getattr(obj_meta, "label", ""))
        el_name = str(getattr(obj_meta, "element_name", ""))

        # --- Person filter (same logic as Phase 1E official probe) ---------
        if label != "person" and el_name != "yolo26_pose":
            continue

        # --- Track ID ------------------------------------------------------
        track_id = getattr(obj_meta, "track_id", None)
        if track_id is None:
            track_id = getattr(obj_meta, "object_id", None)
        tid_valid = is_valid_track_id(track_id)
        tid = int(track_id) if tid_valid else 0
        if not tid_valid:
            skipped_untracked += 1

        # --- Bounding box --- convert centre → top-left --------------------
        xc = yc = w = h = 0.0
        if hasattr(obj_meta, "bbox"):
            b = obj_meta.bbox
            xc = float(getattr(b, "xc", 0.0))
            yc = float(getattr(b, "yc", 0.0))
            w = float(getattr(b, "width", 0.0))
            h = float(getattr(b, "height", 0.0))
        elif hasattr(obj_meta, "rect_params"):
            r = obj_meta.rect_params
            left = float(getattr(r, "left", 0.0))
            top = float(getattr(r, "top", 0.0))
            rw = float(getattr(r, "width", 0.0))
            rh = float(getattr(r, "height", 0.0))
            xc = left + rw / 2.0
            yc = top + rh / 2.0
            w = rw
            h = rh

        # Convert centre → top-left
        x = xc - w / 2.0
        y = yc - h / 2.0

        confidence = float(getattr(obj_meta, "confidence", 0.0))

        # --- Keypoints -----------------------------------------------------
        keypoints: List[Keypoint] = []
        try:
            attr = obj_meta.get_attr_meta("yolo26_pose", "keypoints")
            if attr is not None:
                value = getattr(attr, "value", None)
                if value is not None:
                    keypoints = parse_keypoints(value)
        except ValueError:
            skipped_invalid_kpts += 1
        except Exception:
            pass

        if not keypoints:
            skipped_no_kpts += 1

        kpt_conf = _compute_keypoint_confidence(keypoints)

        observations.append(
            PersonPoseObservation(
                source_id=source_id,
                camera_id=effective_camera_id,
                frame_id=frame_id,
                timestamp_ms=timestamp_ms,
                bbox=BBox(x=x, y=y, width=w, height=h),
                confidence=confidence,
                track_id=tid,
                keypoints=keypoints,
                keypoint_confidence=kpt_conf,
            )
        )

    return AdapterResult(
        observations=observations,
        skipped_untracked_person_count=skipped_untracked,
        skipped_no_keypoints_count=skipped_no_kpts,
        skipped_invalid_keypoints_count=skipped_invalid_kpts,
    )
