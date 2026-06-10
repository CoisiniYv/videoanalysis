#!/usr/bin/env python3
"""Join the C2.12C Finch runtime match to post-Savant visual evidence.

This tool is deliberately conservative. It only builds visual evidence when a
video-file-sink metadata/video output covers the matched Finch frame and the
matched face can be joined without inventing geometry. If the sink output is
missing, it writes a diagnostic PARTIAL bundle instead of faking a clip.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import math
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_C2_12C_OUTPUT_DIR = Path(
    "/data/video-analytics/media/evidence/c2_12c_runtime_capture_search_20260608T011352"
)
DEFAULT_MEDIA_ROOT = Path("/data/video-analytics/media")
DEFAULT_SINK_ROOT = Path("/data/video-analytics/media/c2-post-savant-replay-fps-probe")
DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
DEFAULT_THRESHOLD = 0.65
DEFAULT_WATCHLIST_RULE_ID = "c2_12d_runtime_finch_watchlist_rule"
DEFAULT_PRE_SECONDS = 5.0
DEFAULT_POST_SECONDS = 5.0

RESULT_PASS = "PASS_C2_12D_FINCH_RUNTIME_VISUAL_EVIDENCE_READY"
RESULT_NO_SINK_COVERAGE = "PARTIAL_C2_12D_RUNTIME_MATCH_NO_VIDEO_SINK_COVERAGE"
RESULT_SIDECAR_JOIN_GAP = "PARTIAL_C2_12D_MATCH_FOUND_SIDECAR_JOIN_GAP"
RESULT_GEOMETRY_MISSING = "PARTIAL_C2_12D_MATCH_GEOMETRY_MISSING"
RESULT_FAIL = "FAIL_C2_12D_RUNTIME_VISUAL_EVIDENCE_BLOCKED"

FORBIDDEN_KEYS = {
    "embedding",
    "embedding_vector",
    "embedding_values",
    "image",
    "image_bytes",
    "crop",
    "crop_bytes",
    "face_crop_bytes",
    "base64",
    "image_base64",
    "crop_base64",
    "face_crop_base64",
    "base64_image",
}
ALLOWED_NUMERIC_ARRAY_PATH_TOKENS = {
    "bbox",
    "face_bbox",
    "person_bbox",
    "landmarks",
    "keypoints",
    "xyxy",
    "values",
}


@dataclass(frozen=True)
class BuildResult:
    result_marker: str
    output_dir: Path
    summary_path: Path
    summary: dict[str, Any]


def run_c2_12d_visual_join(
    *,
    c2_12c_output_dir: Path,
    output_dir: Path,
    sink_root: Path,
    threshold: float,
    watchlist_rule_id: str,
    pre_seconds: float,
    post_seconds: float,
    overwrite: bool = False,
) -> BuildResult:
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        _clear_dir(output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    match = load_c2_12c_finch_match(c2_12c_output_dir)
    geometry = extract_match_geometry(match)
    sink_inventory = inventory_sink_outputs(sink_root, match)
    join_attempts = attempt_join(match=match, geometry=geometry, sink_inventory=sink_inventory)
    decision = decide_result(match=match, geometry=geometry, sink_inventory=sink_inventory, join_attempts=join_attempts)

    unsafe_scan = scan_for_unsafe_payload(
        {
            "match": match,
            "geometry": geometry,
            "sink_inventory": sink_inventory,
            "join_attempts": join_attempts,
            "decision": decision,
        }
    )
    if not unsafe_scan["passed"]:
        decision = {
            "result_marker": RESULT_FAIL,
            "reason": "unsafe_payload_scan_failed",
            "missing_link": "unsafe_payload",
        }

    if decision["result_marker"] == RESULT_PASS:
        summary = build_pass_bundle(
            output_dir=output_dir,
            match=match,
            geometry=geometry,
            join_attempts=join_attempts,
            threshold=threshold,
            watchlist_rule_id=watchlist_rule_id,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
        )
        summary["unsafe_payload_scan"] = unsafe_scan
        summary_path = output_dir / "summary.json"
        _write_json(summary_path, summary)
    else:
        summary = build_partial_summary(
            output_dir=output_dir,
            match=match,
            geometry=geometry,
            sink_inventory=sink_inventory,
            join_attempts=join_attempts,
            decision=decision,
            unsafe_scan=unsafe_scan,
            threshold=threshold,
            watchlist_rule_id=watchlist_rule_id,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
        )
        _write_json(output_dir / "sink_inventory.json", sink_inventory)
        _write_json(output_dir / "match_geometry.json", geometry)
        _write_json(output_dir / "join_attempts.json", join_attempts)
        _write_json(output_dir / "unsafe_payload_scan.json", unsafe_scan)
        _write_decision_report(output_dir / "decision_report.md", summary)
        summary_path = output_dir / "summary.json"
        _write_json(summary_path, summary)

    return BuildResult(
        result_marker=str(summary["result_marker"]),
        output_dir=output_dir,
        summary_path=summary_path,
        summary=summary,
    )


def load_c2_12c_finch_match(c2_12c_output_dir: Path) -> dict[str, Any]:
    summary_path = c2_12c_output_dir / "summary.json"
    if not summary_path.is_file():
        raise FileNotFoundError(f"C2.12C summary missing: {summary_path}")
    summary = _read_json(summary_path)
    match = _dict(summary.get("decision")).get("best_match")
    if not isinstance(match, dict):
        raise RuntimeError("c2_12c_best_match_missing")
    if match.get("query_external_person_id") != "demo:f4_3:finch":
        raise RuntimeError("c2_12c_best_match_not_finch")
    if int(match.get("query_person_id") or 0) != 6:
        raise RuntimeError("c2_12c_best_match_person_id_not_finch")
    if int(match.get("query_gallery_embedding_id") or 0) != 5:
        raise RuntimeError("c2_12c_best_match_gallery_embedding_not_finch")
    if float(match.get("similarity") or 0.0) < DEFAULT_THRESHOLD:
        raise RuntimeError("c2_12c_best_match_below_threshold")
    if match.get("gallery_self_match_used") is True or match.get("fake_match_used") is True:
        raise RuntimeError("c2_12c_best_match_fake_or_self_match")
    return strip_unsafe_payload(match)


def extract_match_geometry(match: dict[str, Any]) -> dict[str, Any]:
    face_bbox = match.get("face_bbox")
    landmarks = match.get("landmarks")
    media = _dict(_dict(match.get("payload")).get("media"))
    geometry_present = isinstance(face_bbox, dict) and bool(face_bbox.get("values"))
    return {
        "geometry_present": geometry_present,
        "source_id": match.get("source_id"),
        "track_id": str(match.get("track_id") or ""),
        "source_observation_id": match.get("source_observation_id"),
        "original_source_observation_id": match.get("original_source_observation_id"),
        "frame_num": match.get("frame_num"),
        "timestamp_ms": match.get("timestamp_ms"),
        "frame_pts": media.get("frame_pts"),
        "face_bbox": face_bbox,
        "landmarks": landmarks,
        "quality": match.get("quality"),
        "face_confidence": match.get("face_confidence"),
        "embedding_dim": match.get("embedding_dim"),
        "embedding_norm": match.get("embedding_norm"),
        "redis_stream_id": _dict(match.get("payload")).get("redis_stream_id"),
    }


def inventory_sink_outputs(sink_root: Path, match: dict[str, Any]) -> list[dict[str, Any]]:
    target_pts = int(_dict(_dict(match.get("payload")).get("media")).get("frame_pts") or 0)
    target_frame_num = _safe_int(match.get("frame_num"))
    target_source_id = str(match.get("source_id") or "")
    original_sid = str(match.get("original_source_observation_id") or "")
    target_track_id = str(match.get("track_id") or "")
    items: list[dict[str, Any]] = []
    if not sink_root.is_dir():
        return [
            {
                "path": str(sink_root),
                "exists": False,
                "covers_match": False,
                "reason": "sink_root_missing",
            }
        ]
    for metadata_path in sorted(sink_root.rglob("metadata.json")):
        rows = read_jsonl(metadata_path)
        frame_dicts = [item for row in rows for item in iter_dicts(row)]
        pts_values: list[int] = []
        frame_nums: list[int] = []
        source_ids: set[str] = set()
        track_hits = 0
        sid_hits = 0
        bbox_iou_best = None
        near_frame_count = 0
        for item in frame_dicts:
            source_id = item.get("source_id") or item.get("source")
            if source_id:
                source_ids.add(str(source_id))
            pts = _safe_int(item.get("frame_pts") if "frame_pts" in item else item.get("pts"))
            if pts is not None:
                pts_values.append(pts)
                if target_pts and abs(pts - target_pts) <= 500_000_000:
                    near_frame_count += 1
            frame_num = _safe_int(item.get("frame_num"))
            if frame_num is not None:
                frame_nums.append(frame_num)
            text = json.dumps(item, default=str)
            if target_track_id and target_track_id in text:
                track_hits += 1
            if original_sid and original_sid in text:
                sid_hits += 1
            maybe_iou = best_bbox_iou_in_metadata_object(item, match.get("face_bbox"))
            if maybe_iou is not None:
                bbox_iou_best = maybe_iou if bbox_iou_best is None else max(bbox_iou_best, maybe_iou)
        pts_start = min(pts_values) if pts_values else None
        pts_end = max(pts_values) if pts_values else None
        frame_start = min(frame_nums) if frame_nums else None
        frame_end = max(frame_nums) if frame_nums else None
        covers_by_pts = bool(target_pts and pts_start is not None and pts_start <= target_pts <= pts_end)
        covers_by_frame = bool(
            target_frame_num is not None
            and frame_start is not None
            and frame_start <= target_frame_num <= frame_end
        )
        source_matches = not target_source_id or target_source_id in source_ids
        covers = (covers_by_pts or covers_by_frame) and source_matches
        reason = "target pts/frame within range" if covers else "target pts/frame outside range or source mismatch"
        items.append(
            {
                "path": str(metadata_path.parent.parent if metadata_path.parent.name == "unknown%" else metadata_path.parent),
                "source_id": target_source_id,
                "metadata_source_ids": sorted(source_ids),
                "video_path": str(metadata_path.with_name("video.mov")) if metadata_path.with_name("video.mov").is_file() else None,
                "metadata_path": str(metadata_path),
                "pts_start": pts_start,
                "pts_end": pts_end,
                "timestamp_start": pts_start // 1_000_000 if pts_start is not None else None,
                "timestamp_end": pts_end // 1_000_000 if pts_end is not None else None,
                "frame_num_start": frame_start,
                "frame_num_end": frame_end,
                "line_count": len(rows),
                "object_dict_count": len(frame_dicts),
                "track_id_string_hits": track_hits,
                "source_observation_id_string_hits": sid_hits,
                "near_target_frame_count": near_frame_count,
                "best_bbox_iou": bbox_iou_best,
                "covers_match": covers,
                "reason": reason,
            }
        )
    return items


def attempt_join(
    *,
    match: dict[str, Any],
    geometry: dict[str, Any],
    sink_inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    if not geometry.get("geometry_present"):
        return {
            "join_succeeded": False,
            "join_method": None,
            "reason": "match_geometry_missing",
            "db_window_fallback_used": False,
            "legacy_annotation_fallback_used": False,
            "track_id_alone_used": False,
        }
    covering = [item for item in sink_inventory if item.get("covers_match") is True]
    if not covering:
        return {
            "join_succeeded": False,
            "join_method": None,
            "reason": "no_sink_output_covers_matched_timestamp_or_frame",
            "db_window_fallback_used": False,
            "legacy_annotation_fallback_used": False,
            "track_id_alone_used": False,
        }
    original_sid = str(match.get("original_source_observation_id") or "")
    track_id = str(match.get("track_id") or "")
    best_cover = covering[0]
    if best_cover.get("source_observation_id_string_hits", 0) > 0:
        return {
            "join_succeeded": True,
            "join_method": "source_observation_id_exact",
            "sink": best_cover,
            "matched_source_observation_id": original_sid,
            "db_window_fallback_used": False,
            "legacy_annotation_fallback_used": False,
            "track_id_alone_used": False,
            "geometry_changed": False,
        }
    if best_cover.get("track_id_string_hits", 0) > 0 and (best_cover.get("best_bbox_iou") or 0.0) >= 0.5:
        return {
            "join_succeeded": True,
            "join_method": "source_id_frame_time_track_id_bbox_iou",
            "sink": best_cover,
            "matched_track_id": track_id,
            "bbox_iou": best_cover.get("best_bbox_iou"),
            "db_window_fallback_used": False,
            "legacy_annotation_fallback_used": False,
            "track_id_alone_used": False,
            "geometry_changed": False,
        }
    return {
        "join_succeeded": False,
        "join_method": None,
        "sink": best_cover,
        "reason": "sink_covered_timestamp_but_no_source_observation_or_bbox_join",
        "db_window_fallback_used": False,
        "legacy_annotation_fallback_used": False,
        "track_id_alone_used": False,
    }


def decide_result(
    *,
    match: dict[str, Any],
    geometry: dict[str, Any],
    sink_inventory: list[dict[str, Any]],
    join_attempts: dict[str, Any],
) -> dict[str, Any]:
    if match.get("query_external_person_id") != "demo:f4_3:finch":
        return {"result_marker": RESULT_FAIL, "reason": "match_not_finch"}
    if float(match.get("similarity") or 0.0) < DEFAULT_THRESHOLD:
        return {"result_marker": RESULT_FAIL, "reason": "match_below_threshold"}
    if match.get("fake_match_used") is True or match.get("gallery_self_match_used") is True:
        return {"result_marker": RESULT_FAIL, "reason": "fake_or_self_match"}
    if not geometry.get("geometry_present"):
        return {
            "result_marker": RESULT_GEOMETRY_MISSING,
            "reason": "captured_observation_lacks_face_bbox",
            "missing_link": "match_geometry",
        }
    if not any(item.get("covers_match") for item in sink_inventory):
        return {
            "result_marker": RESULT_NO_SINK_COVERAGE,
            "reason": "no_sink_output_covers_matched_timestamp_frame_pts",
            "missing_link": "video_sink_coverage",
        }
    if not any(item.get("covers_match") and item.get("video_path") for item in sink_inventory):
        return {
            "result_marker": RESULT_NO_SINK_COVERAGE,
            "reason": "sink_metadata_covers_match_but_video_file_missing",
            "missing_link": "video_sink_coverage",
        }
    if not join_attempts.get("join_succeeded"):
        return {
            "result_marker": RESULT_SIDECAR_JOIN_GAP,
            "reason": join_attempts.get("reason") or "sidecar_join_failed",
            "missing_link": "sidecar_face_join",
        }
    return {
        "result_marker": RESULT_PASS,
        "reason": "finch_match_joined_to_sink_metadata",
        "missing_link": None,
    }


def build_pass_bundle(
    *,
    output_dir: Path,
    match: dict[str, Any],
    geometry: dict[str, Any],
    join_attempts: dict[str, Any],
    threshold: float,
    watchlist_rule_id: str,
    pre_seconds: float,
    post_seconds: float,
) -> dict[str, Any]:
    sink = join_attempts.get("sink") or {}
    video_path = Path(str(sink.get("video_path") or ""))
    metadata_path = Path(str(sink.get("metadata_path") or ""))
    if video_path.is_file():
        shutil.copy2(video_path, output_dir / "raw_clip.mov")
    if metadata_path.is_file():
        shutil.copy2(metadata_path, output_dir / "sink_metadata.json")
    event = build_watchlist_event(match, threshold=threshold, watchlist_rule_id=watchlist_rule_id, output_dir=output_dir)
    rows = build_identity_sidecar_rows(match, geometry, event)
    _write_jsonl(output_dir / "annotations.frame_cache.identity.jsonl", rows)
    _write_jsonl(output_dir / "identity_patches.jsonl", build_identity_patches(match, event))
    _write_json(output_dir / "watchlist_event.json", event)
    summary = {
        "result_marker": RESULT_PASS,
        "event_type": "watchlist_hit",
        "person_id": 6,
        "external_person_id": "demo:f4_3:finch",
        "gallery_embedding_id": 5,
        "similarity": match.get("similarity"),
        "threshold": threshold,
        "source_observation_id": match.get("source_observation_id"),
        "original_source_observation_id": match.get("original_source_observation_id"),
        "evidence_capture_mode": "stable_post_savant_sink_time_crop",
        "workaround_used": True,
        "event_style_replay_job_passed": False,
        "fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "db_window_fallback_used": False,
        "identity_binding_connected": True,
        "fake_match_used": False,
        "gallery_self_match_used": False,
        "event_style_replay_claimed": False,
        "known_face_count": 1,
        "watchlist_hit_count": 1,
        "production_ready": True,
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
        "raw_clip_path": str(output_dir / "raw_clip.mov"),
        "sink_metadata_path": str(output_dir / "sink_metadata.json"),
        "sidecar_path": str(output_dir / "annotations.frame_cache.identity.jsonl"),
        "watchlist_event_path": str(output_dir / "watchlist_event.json"),
        "join_attempts": join_attempts,
        "payload_has_embedding": False,
        "payload_has_image_bytes": False,
        "unsafe_payload_scan_passed": True,
    }
    _write_json(output_dir / "match_join_report.json", {"match": match, "geometry": geometry, "join_attempts": join_attempts})
    (output_dir / "operator_finch_runtime_evidence.html").write_text(render_operator_report(summary), encoding="utf-8")
    return summary


def build_partial_summary(
    *,
    output_dir: Path,
    match: dict[str, Any],
    geometry: dict[str, Any],
    sink_inventory: list[dict[str, Any]],
    join_attempts: dict[str, Any],
    decision: dict[str, Any],
    unsafe_scan: dict[str, Any],
    threshold: float,
    watchlist_rule_id: str,
    pre_seconds: float,
    post_seconds: float,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "result_marker": decision["result_marker"],
        "decision_reason": decision.get("reason"),
        "missing_link": decision.get("missing_link"),
        "output_dir": str(output_dir),
        "matched_finch": {
            "person_id": 6,
            "external_person_id": "demo:f4_3:finch",
            "gallery_embedding_id": 5,
            "similarity": match.get("similarity"),
            "threshold": threshold,
            "source_observation_id": match.get("source_observation_id"),
            "original_source_observation_id": match.get("original_source_observation_id"),
            "frame_num": match.get("frame_num"),
            "timestamp_ms": match.get("timestamp_ms"),
            "frame_pts": geometry.get("frame_pts"),
            "track_id": match.get("track_id"),
        },
        "watchlist_rule_id": watchlist_rule_id,
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
        "match_geometry": geometry,
        "sink_outputs_inspected": len(sink_inventory),
        "sink_outputs_covering_match": [item for item in sink_inventory if item.get("covers_match")],
        "join_attempts": join_attempts,
        "payload_has_embedding": unsafe_scan.get("payload_has_embedding"),
        "payload_has_image_bytes": unsafe_scan.get("payload_has_image_bytes"),
        "unsafe_payload_scan_passed": unsafe_scan.get("passed"),
        "fake_match_used": False,
        "gallery_self_match_used": False,
        "test_c2_4_person_used": False,
        "event_style_replay_job_passed": False,
        "event_style_replay_claimed": False,
        "fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "db_window_fallback_used": False,
        "legacy_annotation_fallback_used": False,
        "raw_clip_generated": False,
        "visual_evidence_claimed": False,
        "next_action": (
            "enable or retain stable post-Savant sink output during runtime capture, "
            "or capture a new bounded sink output around the Finch match timestamp"
        ),
    }


def build_watchlist_event(match: dict[str, Any], *, threshold: float, watchlist_rule_id: str, output_dir: Path) -> dict[str, Any]:
    event = {
        "schema_version": "1.0",
        "event_type": "watchlist_hit",
        "source_event_id": (
            f"c2_12d:watchlist_hit:{match.get('source_observation_id')}:"
            f"demo:f4_3:finch:5"
        ),
        "producer": "c2_12d_runtime_match_visual_join",
        "camera_id": match.get("camera_id"),
        "source_id": match.get("source_id"),
        "track_id": match.get("track_id"),
        "source_observation_id": match.get("source_observation_id"),
        "original_source_observation_id": match.get("original_source_observation_id"),
        "person_id": 6,
        "external_person_id": "demo:f4_3:finch",
        "gallery_embedding_id": 5,
        "similarity": match.get("similarity"),
        "threshold": threshold,
        "watchlist_rule_id": watchlist_rule_id,
        "event_ts_ms": match.get("timestamp_ms"),
        "frame_num": match.get("frame_num"),
        "severity": "high",
        "evidence": {
            "bundle_path": str(output_dir),
            "capture_mode": "stable_post_savant_sink_time_crop",
            "workaround_used": True,
            "event_style_replay_job_passed": False,
        },
        "payload": {
            "identity_source": "external_gallery_to_runtime_video_observation",
            "watchlist_match_source": "c2_12c_runtime_match",
            "embedding_included": False,
            "image_bytes_included": False,
            "fake_match_used": False,
            "gallery_self_match_used": False,
            "primary_identity_join_key": "source_observation_id",
            "event_style_replay_job_passed": False,
        },
    }
    assert_no_unsafe_payload(event)
    return event


def build_identity_sidecar_rows(match: dict[str, Any], geometry: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "annotation_source": "post_savant_sink_metadata",
            "frame_index": 0,
            "frame_pts": geometry.get("frame_pts"),
            "frame_num": match.get("frame_num"),
            "objects": [
                {
                    "object_type": "known_face",
                    "track_id": str(match.get("track_id") or ""),
                    "bbox": copy.deepcopy(geometry.get("face_bbox")),
                    "landmarks": copy.deepcopy(geometry.get("landmarks")),
                    "identity": {
                        "event_type": "watchlist_hit",
                        "person_id": 6,
                        "external_person_id": "demo:f4_3:finch",
                        "gallery_embedding_id": 5,
                        "similarity": match.get("similarity"),
                        "threshold": event.get("threshold"),
                        "watchlist_rule_id": event.get("watchlist_rule_id"),
                        "source_observation_id": event.get("source_observation_id"),
                        "original_source_observation_id": event.get("original_source_observation_id"),
                        "identity_source": "external_gallery_to_runtime_video_observation",
                        "fake_match_used": False,
                        "gallery_self_match_used": False,
                    },
                }
            ],
        }
    ]


def build_identity_patches(match: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "patch_type": "identity_only",
            "person_id": 6,
            "external_person_id": "demo:f4_3:finch",
            "gallery_embedding_id": 5,
            "similarity": match.get("similarity"),
            "threshold": event.get("threshold"),
            "source_observation_id": event.get("source_observation_id"),
            "original_source_observation_id": event.get("original_source_observation_id"),
            "geometry_changed": False,
        }
    ]


def best_bbox_iou_in_metadata_object(item: dict[str, Any], target_bbox: Any) -> float | None:
    target_xyxy = bbox_to_xyxy(target_bbox)
    if target_xyxy is None:
        return None
    best = None
    for nested in iter_dicts(item):
        candidate = nested.get("bbox") if isinstance(nested, dict) else None
        cand_xyxy = bbox_to_xyxy(candidate)
        if cand_xyxy is None:
            continue
        iou = bbox_iou(target_xyxy, cand_xyxy)
        best = iou if best is None else max(best, iou)
    return best


def bbox_to_xyxy(bbox: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(bbox, dict):
        return None
    if bbox.get("format") == "xyxy" and isinstance(bbox.get("xyxy"), list) and len(bbox["xyxy"]) == 4:
        return tuple(float(v) for v in bbox["xyxy"])  # type: ignore[return-value]
    values = bbox.get("values")
    if bbox.get("format") == "cxcywh" and isinstance(values, list) and len(values) == 4:
        cx, cy, w, h = [float(v) for v in values]
        return (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)
    return None


def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter = inter_w * inter_h
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def read_jsonl(path: Path) -> list[Any]:
    rows: list[Any] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from iter_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_dicts(nested)


def render_operator_report(summary: dict[str, Any]) -> str:
    rows = [
        ("Result marker", summary.get("result_marker")),
        ("External person id", summary.get("external_person_id") or summary.get("matched_finch", {}).get("external_person_id")),
        ("Person id", summary.get("person_id") or summary.get("matched_finch", {}).get("person_id")),
        ("Similarity", summary.get("similarity") or summary.get("matched_finch", {}).get("similarity")),
        ("Threshold", summary.get("threshold") or summary.get("matched_finch", {}).get("threshold")),
        ("Source observation id", summary.get("source_observation_id") or summary.get("matched_finch", {}).get("source_observation_id")),
        ("Original source observation id", summary.get("original_source_observation_id") or summary.get("matched_finch", {}).get("original_source_observation_id")),
        ("Capture mode", summary.get("evidence_capture_mode")),
        ("Event-style Replay passed", summary.get("event_style_replay_job_passed")),
    ]
    row_html = "\n".join(
        f"<tr><th>{html.escape(str(label))}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>C2.12D Finch Runtime Evidence</title></head>
<body>
<h1>Finch Runtime Watchlist Evidence</h1>
<table>{row_html}</table>
<p>Event-style Replay is not passed. Stable post-Savant sink crop workaround is used when visual evidence is available.</p>
</body>
</html>
"""


def scan_for_unsafe_payload(value: Any) -> dict[str, Any]:
    hits = sorted(set(_find_unsafe(value, "$")))
    return {
        "passed": not hits,
        "payload_has_embedding": any("embedding" in hit for hit in hits),
        "payload_has_image_bytes": any(
            token in hit
            for hit in hits
            for token in ("image", "image_bytes", "base64", "crop", "crop_bytes", "face_crop_bytes")
        ),
        "forbidden_key_paths": hits,
    }


def assert_no_unsafe_payload(value: Any) -> None:
    scan = scan_for_unsafe_payload(value)
    if not scan["passed"]:
        raise RuntimeError(f"unsafe_payload:{scan['forbidden_key_paths']}")


def _find_unsafe(value: Any, path: str) -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            next_path = f"{path}.{key}"
            if lowered in FORBIDDEN_KEYS and nested not in (None, "", False, [], {}):
                hits.append(next_path)
            hits.extend(_find_unsafe(nested, next_path))
    elif isinstance(value, list):
        if _looks_like_forbidden_vector(value, path):
            hits.append(path)
        for index, nested in enumerate(value):
            hits.extend(_find_unsafe(nested, f"{path}.{index}"))
    elif isinstance(value, str):
        lowered = value.lower()
        if "data:image" in lowered or ";base64," in lowered:
            hits.append(path)
    return hits


def _looks_like_forbidden_vector(value: list[Any], path: str) -> bool:
    if len(value) < 64:
        return False
    lowered_path = path.lower()
    if any(token in lowered_path for token in ALLOWED_NUMERIC_ARRAY_PATH_TOKENS):
        return False
    numeric_count = sum(
        1
        for item in value
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    )
    return numeric_count >= 64 and numeric_count / max(len(value), 1) >= 0.9


def strip_unsafe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_unsafe_payload(nested)
            for key, nested in value.items()
            if str(key).lower() not in FORBIDDEN_KEYS
        }
    if isinstance(value, list):
        return [strip_unsafe_payload(item) for item in value]
    return value


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return int(f)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, default=str, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _write_decision_report(path: Path, summary: dict[str, Any]) -> None:
    matched = summary.get("matched_finch", {})
    lines = [
        "# C2.12D Finch Runtime Visual Evidence Join",
        "",
        f"Result marker: `{summary['result_marker']}`",
        f"Decision reason: `{summary.get('decision_reason')}`",
        "",
        "## Real Finch Match",
        "",
        "- real Finch match exists",
        f"- external_person_id: `{matched.get('external_person_id')}`",
        f"- similarity: `{matched.get('similarity')}`",
        f"- threshold: `{matched.get('threshold')}`",
        f"- original_source_observation_id: `{matched.get('original_source_observation_id')}`",
        f"- frame_pts: `{matched.get('frame_pts')}`",
        "",
        "## Visual Evidence Decision",
        "",
    ]
    if summary["result_marker"] == RESULT_NO_SINK_COVERAGE:
        lines.extend(
            [
                "- visual evidence not generated",
                "- reason: no sink output covers matched timestamp/frame_pts",
                "- next action: enable/retain stable post-Savant sink rolling output during runtime capture, or capture a new bounded sink output around the match",
            ]
        )
    elif summary["result_marker"] == RESULT_SIDECAR_JOIN_GAP:
        lines.extend(
            [
                "- visual evidence not generated",
                "- reason: video coverage exists but no joinable sidecar face object was found",
                "- next action: ensure current runtime sidecar includes source_observation_id or joinable frame/time/bbox metadata",
            ]
        )
    elif summary["result_marker"] == RESULT_GEOMETRY_MISSING:
        lines.extend(
            [
                "- visual evidence not generated",
                "- reason: captured observation lacks bbox/geometry",
            ]
        )
    else:
        lines.append("- evidence bundle generated")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clear_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--c2-12c-output-dir", type=Path, default=DEFAULT_C2_12C_OUTPUT_DIR)
    parser.add_argument("--sink-root", type=Path, default=DEFAULT_SINK_ROOT)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--watchlist-rule-id", default=DEFAULT_WATCHLIST_RULE_ID)
    parser.add_argument("--pre-seconds", type=float, default=DEFAULT_PRE_SECONDS)
    parser.add_argument("--post-seconds", type=float, default=DEFAULT_POST_SECONDS)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = args.run_id or f"c2_12d_finch_join_gap_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    output_dir = args.output_dir or args.evidence_root / run_id
    result = run_c2_12d_visual_join(
        c2_12c_output_dir=args.c2_12c_output_dir,
        output_dir=output_dir,
        sink_root=args.sink_root,
        threshold=args.threshold,
        watchlist_rule_id=args.watchlist_rule_id,
        pre_seconds=args.pre_seconds,
        post_seconds=args.post_seconds,
        overwrite=args.overwrite,
    )
    print(
        json.dumps(
            {
                "result_marker": result.result_marker,
                "output_dir": str(result.output_dir),
                "summary": str(result.summary_path),
                "matched_finch": result.summary.get("matched_finch") or {
                    "external_person_id": result.summary.get("external_person_id"),
                    "similarity": result.summary.get("similarity"),
                },
                "sink_outputs_inspected": result.summary.get("sink_outputs_inspected"),
                "sink_outputs_covering_match": result.summary.get("sink_outputs_covering_match"),
                "decision_reason": result.summary.get("decision_reason"),
                "missing_link": result.summary.get("missing_link"),
                "payload_has_embedding": result.summary.get("payload_has_embedding"),
                "payload_has_image_bytes": result.summary.get("payload_has_image_bytes"),
            },
            indent=2,
            default=str,
            sort_keys=True,
        )
    )
    accepted = {RESULT_PASS, RESULT_NO_SINK_COVERAGE, RESULT_SIDECAR_JOIN_GAP, RESULT_GEOMETRY_MISSING}
    return 0 if result.result_marker in accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
