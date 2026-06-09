"""Timeline matching helpers for frame annotation metadata."""

from __future__ import annotations

import bisect
import copy
from statistics import median
from typing import Any


DEFAULT_MATCH_MODE = "frame_uuid_then_pts"
DEFAULT_NEAREST_PTS_RATIO = 0.5


def build_frame_annotation_index(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Build deterministic exact and nearest-match indexes."""

    by_uuid: dict[str, dict[str, Any]] = {}
    by_pts: dict[int, dict[str, Any]] = {}
    pts_messages: list[tuple[int, int, dict[str, Any]]] = []

    for order, message in enumerate(messages):
        frame_uuid = message.get("frame_uuid")
        if isinstance(frame_uuid, str) and frame_uuid and frame_uuid not in by_uuid:
            by_uuid[frame_uuid] = message

        frame_pts = message.get("frame_pts")
        if isinstance(frame_pts, int):
            if frame_pts not in by_pts:
                by_pts[frame_pts] = message
            pts_messages.append((frame_pts, order, message))

    pts_messages.sort(key=lambda item: (item[0], item[1]))
    pts_values = [item[0] for item in pts_messages]
    distinct_pts = sorted(set(pts_values))
    deltas = [
        distinct_pts[index] - distinct_pts[index - 1]
        for index in range(1, len(distinct_pts))
        if distinct_pts[index] > distinct_pts[index - 1]
    ]
    median_delta = float(median(deltas)) if deltas else None

    return {
        "by_uuid": by_uuid,
        "by_pts": by_pts,
        "pts_values": pts_values,
        "pts_messages": pts_messages,
        "median_annotation_pts_delta": median_delta,
    }


def match_clip_frames_to_annotations(
    clip_frames: list[dict[str, Any]],
    annotation_messages: list[dict[str, Any]],
    *,
    match_mode: str = DEFAULT_MATCH_MODE,
    nearest_pts_ratio: float = DEFAULT_NEAREST_PTS_RATIO,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Match clip frame anchors to frame annotation messages."""

    if nearest_pts_ratio < 0:
        raise ValueError("invalid_nearest_pts_ratio")

    index = build_frame_annotation_index(annotation_messages)
    median_delta = index["median_annotation_pts_delta"]
    nearest_tolerance = (
        float(median_delta) * float(nearest_pts_ratio)
        if median_delta is not None
        else None
    )

    matches: list[dict[str, Any]] = []
    counts = {
        "clip_frames_requested": len(clip_frames),
        "frames_matched_uuid": 0,
        "frames_matched_pts_exact": 0,
        "frames_matched_pts_nearest": 0,
        "frames_missed": 0,
        "match_hit_ratio": 0.0,
        "median_annotation_pts_delta": median_delta,
        "nearest_pts_tolerance": nearest_tolerance,
        "match_mode": match_mode,
    }

    for frame in clip_frames:
        match_type = "miss"
        annotation = None
        frame_uuid = frame.get("frame_uuid")
        frame_pts = frame.get("frame_pts")

        if (
            match_mode in ("frame_uuid_then_pts", "frame_uuid")
            and isinstance(frame_uuid, str)
            and frame_uuid
        ):
            annotation = index["by_uuid"].get(frame_uuid)
            if annotation is not None:
                match_type = "frame_uuid"
                counts["frames_matched_uuid"] += 1

        if annotation is None and isinstance(frame_pts, int):
            annotation = index["by_pts"].get(frame_pts)
            if annotation is not None:
                match_type = "frame_pts_exact"
                counts["frames_matched_pts_exact"] += 1

        if (
            annotation is None
            and isinstance(frame_pts, int)
            and nearest_tolerance is not None
            and match_mode in ("frame_uuid_then_pts", "frame_pts", "pts")
        ):
            annotation = _nearest_pts_message(
                frame_pts,
                index["pts_values"],
                index["pts_messages"],
                nearest_tolerance,
            )
            if annotation is not None:
                match_type = "frame_pts_nearest"
                counts["frames_matched_pts_nearest"] += 1

        if annotation is None:
            counts["frames_missed"] += 1

        match_record = copy.deepcopy(frame)
        match_record["matched"] = annotation is not None
        match_record["match_type"] = match_type
        match_record["annotation_message"] = copy.deepcopy(annotation) if annotation is not None else None
        matches.append(match_record)

    matched_count = (
        counts["frames_matched_uuid"]
        + counts["frames_matched_pts_exact"]
        + counts["frames_matched_pts_nearest"]
    )
    if clip_frames:
        counts["match_hit_ratio"] = matched_count / len(clip_frames)

    return matches, counts


def _nearest_pts_message(
    frame_pts: int,
    pts_values: list[int],
    pts_messages: list[tuple[int, int, dict[str, Any]]],
    tolerance: float,
) -> dict[str, Any] | None:
    if not pts_values:
        return None

    position = bisect.bisect_left(pts_values, frame_pts)
    candidate_indexes = []
    if position < len(pts_values):
        candidate_indexes.append(position)
    if position > 0:
        candidate_indexes.append(position - 1)

    best: tuple[float, int, dict[str, Any]] | None = None
    for index in candidate_indexes:
        candidate_pts, order, message = pts_messages[index]
        distance = abs(candidate_pts - frame_pts)
        if distance > tolerance:
            continue
        current = (float(distance), int(order), message)
        if best is None or current[:2] < best[:2]:
            best = current

    return best[2] if best is not None else None
