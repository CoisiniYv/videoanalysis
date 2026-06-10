#!/usr/bin/env python3
"""Audit C2 post-Savant evidence bundle output correctness.

This tool is intentionally offline: it reads an existing evidence bundle and
does not call Redis, PostgreSQL, FastAPI, Docker, or the evidence viewer.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SIDECAR_FILE = "annotations.frame_cache.identity.jsonl"
SUMMARY_FILE = "summary.json"
SINK_METADATA_FILE = "sink_metadata.json"
RAW_CLIP_CANDIDATES = ("raw_clip.mov", "raw_clip.mp4", "video.mov", "video.mp4")
RESULT_PASS = "PASS_C2_3Q_EVIDENCE_OUTPUT_AUDIT_READY"
RESULT_PARTIAL = "PARTIAL_C2_3Q_MANUAL_REVIEW_REQUIRED"
RESULT_FAIL = "FAIL_C2_3Q_EVIDENCE_OUTPUT_AUDIT_BLOCKED"
ANNOTATION_SOURCE_KIND_PRODUCTION = "production_sidecar"

FRAME_TABLE_FIELDS = [
    "frame_index",
    "decoded",
    "has_sidecar",
    "person_count",
    "face_count",
    "known_face_count",
    "keypoint_count",
    "landmark_count",
    "audit_status",
    "notes",
    "overlay_path",
]

OBJECT_TABLE_FIELDS = [
    "frame_index",
    "object_type",
    "track_id",
    "bbox_format",
    "x1",
    "y1",
    "x2",
    "y2",
    "confidence",
    "in_bounds",
    "geometry_status",
    "label_kind",
    "source_observation_id",
]

COCO17_EDGES = (
    (5, 6),
    (5, 7),
    (7, 9),
    (6, 8),
    (8, 10),
    (5, 11),
    (6, 12),
    (11, 12),
    (11, 13),
    (13, 15),
    (12, 14),
    (14, 16),
    (0, 1),
    (0, 2),
    (1, 3),
    (2, 4),
)


@dataclass(frozen=True)
class AuditResult:
    bundle_dir: Path
    output_dir: Path
    audit_summary_path: Path
    report_path: Path
    index_path: Path
    frame_table_path: Path
    object_table_path: Path
    contact_sheet_path: Path
    summary: dict[str, Any]


@dataclass(frozen=True)
class BBox:
    x1: float
    y1: float
    x2: float
    y2: float
    fmt: str
    confidence: float | None
    source: str | None


def audit_bundle(
    *,
    bundle_dir: Path,
    output_dir: Path,
    sample_frames: list[int] | None = None,
    overwrite: bool = False,
    video_frame_count_reader: Callable[[Path], int] | None = None,
    frame_loader: Callable[[Path, int], Any | None] | None = None,
    audit_time: datetime | None = None,
) -> AuditResult:
    """Run the full offline audit and write HTML/JSON/CSV/image outputs."""

    bundle_dir = bundle_dir.resolve(strict=False)
    output_dir = output_dir.resolve(strict=False)
    if not bundle_dir.is_dir():
        raise FileNotFoundError(f"bundle directory missing: {bundle_dir}")
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        if output_dir.is_dir():
            shutil.rmtree(output_dir)
        else:
            output_dir.unlink()

    raw_clip_path = _find_first_existing(bundle_dir, RAW_CLIP_CANDIDATES)
    if raw_clip_path is None:
        raise FileNotFoundError(f"raw clip missing in bundle: {bundle_dir}")
    sidecar_path = bundle_dir / SIDECAR_FILE
    summary_path = bundle_dir / SUMMARY_FILE
    sink_metadata_path = bundle_dir / SINK_METADATA_FILE
    _require_file(sidecar_path, "production sidecar")
    _require_file(summary_path, "summary")
    _require_file(sink_metadata_path, "sink metadata")

    summary = _read_json(summary_path)
    sidecar_rows = _read_jsonl(sidecar_path)
    sink_metadata = _read_json_or_jsonl(sink_metadata_path)
    metadata_frame_count = _metadata_frame_count(sink_metadata, summary)
    video_frame_count = (
        video_frame_count_reader(raw_clip_path)
        if video_frame_count_reader is not None
        else read_decoded_video_frame_count(raw_clip_path)
    )
    sidecar_frame_count = len(sidecar_rows)
    if sample_frames is None:
        sample_frames = _default_sample_frames(video_frame_count)
    sample_frames = _normalize_sample_frames(sample_frames)

    output_dir.mkdir(parents=True, exist_ok=False)
    frames_dir = output_dir / "frames"
    overlays_dir = output_dir / "overlays"
    debug_dir = output_dir / "debug"
    frames_dir.mkdir()
    overlays_dir.mkdir()
    debug_dir.mkdir()

    object_counts = _count_objects(sidecar_rows)
    summary_flags = _summary_flags(summary)
    warnings: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    frame_table: list[dict[str, Any]] = []
    object_table: list[dict[str, Any]] = []
    sample_results: list[dict[str, Any]] = []
    overlay_paths: list[Path] = []

    _audit_timeline_and_policy(
        summary=summary,
        video_frame_count=video_frame_count,
        metadata_frame_count=metadata_frame_count,
        sidecar_frame_count=sidecar_frame_count,
        summary_flags=summary_flags,
        warnings=warnings,
        failures=failures,
    )

    for frame_index in sample_frames:
        frame_result = _audit_sample_frame(
            raw_clip_path=raw_clip_path,
            output_dir=output_dir,
            frames_dir=frames_dir,
            overlays_dir=overlays_dir,
            sidecar_rows=sidecar_rows,
            frame_index=frame_index,
            frame_loader=frame_loader,
            warnings=warnings,
            failures=failures,
        )
        frame_table.append(frame_result["frame_row"])
        object_table.extend(frame_result["object_rows"])
        sample_results.append(frame_result["sample_result"])
        overlay_paths.append(frame_result["overlay_path"])

    if not any(result["person_count"] or result["face_count"] or result["known_face_count"] for result in sample_results):
        warnings.append(
            _issue(
                "all_sampled_frames_no_objects",
                "all sampled frames have no displayable objects",
            )
        )

    result_marker = _result_marker(warnings=warnings, failures=failures)
    audit_summary = {
        "result_marker": result_marker,
        "bundle_dir": str(bundle_dir),
        "output_dir": str(output_dir),
        "raw_clip_path": str(raw_clip_path),
        "sidecar_path": str(sidecar_path),
        "sink_metadata_path": str(sink_metadata_path),
        "summary_path": str(summary_path),
        "audit_time": _iso_time(audit_time),
        "timeline": {
            "video_frame_count": video_frame_count,
            "metadata_frame_count": metadata_frame_count,
            "sidecar_frame_count": sidecar_frame_count,
            "trim_occurred": bool(summary.get("trim_occurred") or summary.get("sidecar_trimmed")),
            "timeline_reconciliation_status": summary.get("timeline_reconciliation_status"),
        },
        "summary_flags": summary_flags,
        "object_counts": object_counts,
        "sampled_frames": sample_frames,
        "sampled_frames_count": len(sample_frames),
        "sample_results": sample_results,
        "frames_with_objects": sum(1 for row in sidecar_rows if _frame_has_objects(row)),
        "frames_without_objects": sum(1 for row in sidecar_rows if not _frame_has_objects(row)),
        "warnings": warnings,
        "failures": failures,
        "identity_semantics": _identity_semantics(summary, object_counts),
    }

    contact_sheet_path = output_dir / "contact_sheet.jpg"
    _write_contact_sheet(
        output_path=contact_sheet_path,
        overlay_paths=overlay_paths,
        sample_results=sample_results,
    )
    _write_json(output_dir / "audit_summary.json", audit_summary)
    _write_json(debug_dir / "parsed_summary.json", summary)
    _write_json(
        debug_dir / "parsed_sidecar_stats.json",
        {
            "rows": sidecar_frame_count,
            "object_counts": object_counts,
            "frames_with_objects": audit_summary["frames_with_objects"],
            "frames_without_objects": audit_summary["frames_without_objects"],
        },
    )
    _write_csv(output_dir / "frame_table.csv", FRAME_TABLE_FIELDS, frame_table)
    _write_csv(output_dir / "object_table.csv", OBJECT_TABLE_FIELDS, object_table)
    _write_report(
        output_dir / "report.md",
        audit_summary=audit_summary,
        frame_table=frame_table,
    )
    _write_html(
        output_dir / "index.html",
        audit_summary=audit_summary,
        frame_table=frame_table,
    )

    return AuditResult(
        bundle_dir=bundle_dir,
        output_dir=output_dir,
        audit_summary_path=output_dir / "audit_summary.json",
        report_path=output_dir / "report.md",
        index_path=output_dir / "index.html",
        frame_table_path=output_dir / "frame_table.csv",
        object_table_path=output_dir / "object_table.csv",
        contact_sheet_path=contact_sheet_path,
        summary=audit_summary,
    )


def read_decoded_video_frame_count(video_path: Path) -> int:
    ffprobe_count = _read_frame_count_ffprobe(video_path)
    if ffprobe_count is not None:
        return ffprobe_count
    cv2_count = _read_frame_count_cv2(video_path)
    if cv2_count is not None:
        return cv2_count
    raise RuntimeError("decoded_video_frame_count_unavailable")


def parse_sample_frames(value: str | None) -> list[int] | None:
    if not value:
        return None
    frames: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        frames.append(int(part))
    return frames


def extract_bbox(obj: dict[str, Any]) -> BBox | None:
    bbox = obj.get("bbox")
    if isinstance(bbox, list) and len(bbox) >= 4:
        values = [_number_or_none(value) for value in bbox[:4]]
        if all(value is not None for value in values):
            return BBox(values[0], values[1], values[2], values[3], "xyxy", None, None)  # type: ignore[arg-type]
    if not isinstance(bbox, dict):
        return None

    fmt = str(bbox.get("format") or "").lower()
    confidence = _number_or_none(bbox.get("confidence"))
    source = _text_or_none(bbox.get("source"))
    if fmt == "xyxy" or bbox.get("xyxy") is not None:
        values = _numeric_list(bbox.get("xyxy"))
        if len(values) >= 4:
            return BBox(values[0], values[1], values[2], values[3], "xyxy", confidence, source)
    if fmt == "xywh" or bbox.get("xywh") is not None:
        values = _numeric_list(bbox.get("xywh"))
        if len(values) >= 4:
            x, y, width, height = values[:4]
            return BBox(x, y, x + width, y + height, "xywh", confidence, source)

    keys = ("x1", "y1", "x2", "y2")
    keyed = [_number_or_none(bbox.get(key)) for key in keys]
    if all(value is not None for value in keyed):
        return BBox(keyed[0], keyed[1], keyed[2], keyed[3], fmt or "xyxy", confidence, source)  # type: ignore[arg-type]
    return None


def audit_object_geometry(obj: dict[str, Any], *, width: int, height: int, frame_index: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    bbox = extract_bbox(obj)
    object_type = _object_type(obj)
    label_kind = _label_kind(obj)
    source_observation_id = _source_observation_id(obj)
    track_id = _text_or_none(obj.get("track_id"))
    row = {
        "frame_index": frame_index,
        "object_type": object_type,
        "track_id": track_id or "",
        "bbox_format": "",
        "x1": "",
        "y1": "",
        "x2": "",
        "y2": "",
        "confidence": "",
        "in_bounds": "",
        "geometry_status": "fail",
        "label_kind": label_kind or "",
        "source_observation_id": source_observation_id or "",
    }
    if bbox is None:
        failures.append(_issue("bbox_missing_or_unparseable", "object bbox missing or unparseable", frame_index, object_type))
        return row, warnings, failures

    row.update(
        {
            "bbox_format": bbox.fmt,
            "x1": _fmt_number(bbox.x1),
            "y1": _fmt_number(bbox.y1),
            "x2": _fmt_number(bbox.x2),
            "y2": _fmt_number(bbox.y2),
            "confidence": "" if bbox.confidence is None else _fmt_number(bbox.confidence),
        }
    )
    bbox_width = bbox.x2 - bbox.x1
    bbox_height = bbox.y2 - bbox.y1
    if bbox_width <= 0 or bbox_height <= 0:
        failures.append(_issue("bbox_negative_or_zero_size", "bbox has non-positive width or height", frame_index, object_type))
        row["in_bounds"] = False
        row["geometry_status"] = "fail"
        return row, warnings, failures

    in_bounds = 0 <= bbox.x1 <= width and 0 <= bbox.x2 <= width and 0 <= bbox.y1 <= height and 0 <= bbox.y2 <= height
    row["in_bounds"] = in_bounds
    row["geometry_status"] = "ok"
    if _looks_normalized(bbox):
        warnings.append(_issue("bbox_format_suspicious", "bbox looks normalized instead of pixel coordinates", frame_index, object_type))
        row["geometry_status"] = "suspicious"
    if not in_bounds:
        outside_ratio = _bbox_outside_ratio(bbox, width=width, height=height)
        if outside_ratio >= 0.50:
            failures.append(_issue("bbox_out_of_bounds", f"bbox is mostly outside image: outside_ratio={outside_ratio:.3f}", frame_index, object_type))
            row["geometry_status"] = "fail"
        else:
            warnings.append(_issue("bbox_partly_out_of_bounds", f"bbox is partly outside image: outside_ratio={outside_ratio:.3f}", frame_index, object_type))
            row["geometry_status"] = "suspicious"

    if object_type in {"face", "known_face"}:
        _audit_landmarks(obj, bbox=bbox, width=width, height=height, frame_index=frame_index, warnings=warnings, failures=failures)
    if object_type == "person":
        _audit_keypoints(obj, bbox=bbox, width=width, height=height, frame_index=frame_index, warnings=warnings, failures=failures)
    if row["geometry_status"] == "ok" and failures:
        row["geometry_status"] = "fail"
    elif row["geometry_status"] == "ok" and warnings:
        row["geometry_status"] = "suspicious"
    return row, warnings, failures


def _audit_sample_frame(
    *,
    raw_clip_path: Path,
    output_dir: Path,
    frames_dir: Path,
    overlays_dir: Path,
    sidecar_rows: list[dict[str, Any]],
    frame_index: int,
    frame_loader: Callable[[Path, int], Any | None] | None,
    warnings: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    frame = frame_loader(raw_clip_path, frame_index) if frame_loader is not None else _read_video_frame(raw_clip_path, frame_index)
    decoded = frame is not None
    sidecar_row = sidecar_rows[frame_index] if 0 <= frame_index < len(sidecar_rows) else None
    has_sidecar = sidecar_row is not None
    if not has_sidecar:
        failures.append(_issue("frame_missing_sidecar", "sample frame is missing sidecar row", frame_index))
    objects = _objects(sidecar_row) if sidecar_row else []

    if not decoded:
        failures.append(_issue("decoded_frame_unavailable", "sample frame could not be decoded from video", frame_index))
        width = _int_or_none(_dict(sidecar_row).get("width")) or 1280
        height = _int_or_none(_dict(sidecar_row).get("height")) or 720
        frame = _placeholder_frame(width=width, height=height, text="decoded frame unavailable")
    height, width = int(frame.shape[0]), int(frame.shape[1])

    frame_issues_before = len(warnings) + len(failures)
    object_rows: list[dict[str, Any]] = []
    local_warnings: list[dict[str, Any]] = []
    local_failures: list[dict[str, Any]] = []
    for obj in objects:
        object_row, obj_warnings, obj_failures = audit_object_geometry(obj, width=width, height=height, frame_index=frame_index)
        object_rows.append(object_row)
        local_warnings.extend(obj_warnings)
        local_failures.extend(obj_failures)
    _audit_face_person_relations(objects, width=width, height=height, frame_index=frame_index, warnings=local_warnings)
    warnings.extend(local_warnings)
    failures.extend(local_failures)

    counts = _count_frame_objects(objects)
    keypoint_count = _frame_keypoint_count(objects)
    landmark_count = _frame_landmark_count(objects)
    status = _frame_status(
        decoded=decoded,
        has_sidecar=has_sidecar,
        object_count=len(objects),
        local_warnings=local_warnings,
        local_failures=local_failures,
    )
    if status == "no_objects":
        note = "no displayable objects in sampled frame"
    elif len(warnings) + len(failures) > frame_issues_before:
        note = "; ".join(issue["code"] for issue in [*local_failures, *local_warnings])
    else:
        note = "ok"
    bbox_violation_count = _count_issue_codes(
        [*local_failures, *local_warnings],
        {
            "bbox_missing_or_unparseable",
            "bbox_negative_or_zero_size",
            "bbox_out_of_bounds",
            "bbox_partly_out_of_bounds",
            "bbox_format_suspicious",
        },
    )
    keypoint_suspicious_count = _count_issue_codes(
        [*local_failures, *local_warnings],
        {"keypoints_outside_image", "keypoints_far_from_person_bbox"},
    )
    landmark_suspicious_count = _count_issue_codes(
        [*local_failures, *local_warnings],
        {"landmarks_outside_image", "face_landmarks_far_from_bbox", "face_landmark_count_not_5"},
    )

    frame_path = frames_dir / f"frame_{frame_index:06d}.jpg"
    overlay_path = overlays_dir / f"frame_{frame_index:06d}_overlay.jpg"
    _write_image(frame_path, frame)
    overlay = _draw_overlay(
        frame.copy(),
        frame_index=frame_index,
        sidecar_row=sidecar_row or {},
        objects=objects,
        status=status,
    )
    _write_image(overlay_path, overlay)
    rel_overlay = _relative_path(overlay_path, output_dir)
    frame_row = {
        "frame_index": frame_index,
        "decoded": decoded,
        "has_sidecar": has_sidecar,
        "person_count": counts["person"],
        "face_count": counts["face"],
        "known_face_count": counts["known_face"],
        "keypoint_count": keypoint_count,
        "landmark_count": landmark_count,
        "bbox_in_bounds_violations": bbox_violation_count,
        "keypoint_in_person_bbox_suspicious_count": keypoint_suspicious_count,
        "landmark_near_face_suspicious_count": landmark_suspicious_count,
        "audit_status": status,
        "notes": note,
        "overlay_path": rel_overlay,
    }
    sample_result = {
        "frame_index": frame_index,
        "decoded": decoded,
        "has_sidecar": has_sidecar,
        "person_count": counts["person"],
        "face_count": counts["face"],
        "known_face_count": counts["known_face"],
        "keypoint_count": keypoint_count,
        "landmark_count": landmark_count,
        "bbox_in_bounds_violations": bbox_violation_count,
        "keypoint_in_person_bbox_suspicious_count": keypoint_suspicious_count,
        "landmark_near_face_suspicious_count": landmark_suspicious_count,
        "audit_status": status,
        "notes": note,
        "frame_path": _relative_path(frame_path, output_dir),
        "overlay_path": rel_overlay,
        "frame_pts": _dict(sidecar_row).get("frame_pts") if sidecar_row else None,
    }
    return {
        "frame_row": frame_row,
        "object_rows": object_rows,
        "sample_result": sample_result,
        "overlay_path": overlay_path,
    }


def _audit_timeline_and_policy(
    *,
    summary: dict[str, Any],
    video_frame_count: int,
    metadata_frame_count: int,
    sidecar_frame_count: int,
    summary_flags: dict[str, Any],
    warnings: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> None:
    if video_frame_count != sidecar_frame_count:
        failures.append(
            _issue(
                "video_sidecar_frame_count_mismatch",
                f"video_frame_count={video_frame_count} sidecar_frame_count={sidecar_frame_count}",
            )
        )
    if metadata_frame_count != sidecar_frame_count:
        warnings.append(
            _issue(
                "metadata_sidecar_frame_count_mismatch",
                f"metadata_frame_count={metadata_frame_count} sidecar_frame_count={sidecar_frame_count}",
            )
        )
    trim_occurred = bool(summary.get("trim_occurred") or summary.get("sidecar_trimmed"))
    if trim_occurred:
        if summary.get("production_ready") is True and summary.get("timeline_reconciliation_status") == "frame_counts_match":
            warnings.append(_issue("trim_occurred_but_summary_claims_match", "trim occurred while summary claims frame count match"))
        else:
            failures.append(_issue("trim_occurred", "bundle sidecar was trimmed and timeline is not fully verified"))
    if summary_flags["fallback_used"] is True:
        failures.append(_issue("fallback_used", "summary indicates annotation fallback was used"))
    if summary_flags["legacy_used_for_visual_binding"] is True:
        failures.append(_issue("legacy_used_for_visual_binding", "legacy annotations were used for visual binding"))
    if summary_flags["annotation_source_kind"] != ANNOTATION_SOURCE_KIND_PRODUCTION:
        failures.append(
            _issue(
                "annotation_source_kind_not_production_sidecar",
                f"annotation_source_kind={summary_flags['annotation_source_kind']}",
            )
        )
    if summary_flags["production_ready"] is not True:
        failures.append(_issue("production_not_ready", "summary production_ready is not true"))


def _audit_landmarks(
    obj: dict[str, Any],
    *,
    bbox: BBox,
    width: int,
    height: int,
    frame_index: int,
    warnings: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> None:
    points = _landmark_points(obj)
    if not points:
        warnings.append(_issue("face_landmarks_missing", "face object has no landmarks", frame_index, _object_type(obj)))
        return
    if len(points) != 5:
        warnings.append(_issue("face_landmark_count_not_5", f"landmark_count={len(points)}", frame_index, _object_type(obj)))
    outside_image = [point for point in points if not _point_in_image(point, width=width, height=height)]
    if outside_image:
        failures.append(_issue("landmarks_outside_image", f"outside_count={len(outside_image)}", frame_index, _object_type(obj)))
    inflated = _inflate_bbox(bbox, width=width, height=height, factor=0.30, min_margin=20)
    outside_bbox = [point for point in points if not _point_in_bbox(point, inflated)]
    if len(outside_bbox) >= max(3, math.ceil(len(points) * 0.60)):
        warnings.append(
            _issue(
                "face_landmarks_far_from_bbox",
                f"outside_inflated_bbox_count={len(outside_bbox)}",
                frame_index,
                _object_type(obj),
            )
        )


def _audit_keypoints(
    obj: dict[str, Any],
    *,
    bbox: BBox,
    width: int,
    height: int,
    frame_index: int,
    warnings: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> None:
    points = _pose_keypoints(obj)
    visible = [point for point in points if point[2] is None or point[2] > 0.05]
    if not visible:
        return
    outside_image = [point for point in visible if not _point_in_image(point, width=width, height=height)]
    if outside_image:
        code = "keypoints_outside_image"
        message = f"outside_count={len(outside_image)} visible_count={len(visible)}"
        warnings.append(_issue(code, message, frame_index, _object_type(obj)))
    inflated = _inflate_bbox(bbox, width=width, height=height, factor=0.25, min_margin=30)
    outside_bbox = [point for point in visible if not _point_in_bbox(point, inflated)]
    if len(outside_bbox) > len(visible) * 0.50:
        warnings.append(
            _issue(
                "keypoints_far_from_person_bbox",
                f"outside_inflated_bbox_count={len(outside_bbox)} visible_count={len(visible)}",
                frame_index,
                _object_type(obj),
            )
        )


def _audit_face_person_relations(
    objects: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    frame_index: int,
    warnings: list[dict[str, Any]],
) -> None:
    del width, height
    persons: dict[str, BBox] = {}
    faces: list[tuple[dict[str, Any], BBox]] = []
    for obj in objects:
        bbox = extract_bbox(obj)
        if bbox is None:
            continue
        track_id = _text_or_none(obj.get("track_id"))
        object_type = _object_type(obj)
        if object_type == "person" and track_id:
            persons[track_id] = bbox
        elif object_type in {"face", "known_face"}:
            faces.append((obj, bbox))
    if faces and not any(_text_or_none(face.get("track_id")) for face, _bbox in faces):
        warnings.append(_issue("association_missing_or_unknown", "all face objects are missing track_id", frame_index, "face"))
        return
    for face, face_bbox in faces:
        track_id = _text_or_none(face.get("track_id"))
        if not track_id or track_id not in persons:
            continue
        person_bbox = persons[track_id]
        face_center = ((face_bbox.x1 + face_bbox.x2) / 2.0, (face_bbox.y1 + face_bbox.y2) / 2.0, None)
        person_head_region = BBox(
            person_bbox.x1 - 30,
            person_bbox.y1 - 30,
            person_bbox.x2 + 30,
            person_bbox.y1 + max(60, (person_bbox.y2 - person_bbox.y1) * 0.55),
            "xyxy",
            None,
            None,
        )
        if not _point_in_bbox(face_center, person_head_region):
            warnings.append(
                _issue(
                    "face_not_near_same_track_person_head_region",
                    f"track_id={track_id}",
                    frame_index,
                    _object_type(face),
                )
            )


def _draw_overlay(
    frame: Any,
    *,
    frame_index: int,
    sidecar_row: dict[str, Any],
    objects: list[dict[str, Any]],
    status: str,
) -> Any:
    cv2 = _cv2()
    counts = _count_frame_objects(objects)
    annotation_source = sidecar_row.get("annotation_source") or ""
    source_id = sidecar_row.get("source_id") or ""
    width = int(frame.shape[1])
    height = int(frame.shape[0])
    for obj in objects:
        bbox = extract_bbox(obj)
        if bbox is None:
            continue
        object_type = _object_type(obj)
        track_id = _text_or_none(obj.get("track_id")) or "na"
        label_kind = _label_kind(obj) or object_type
        if object_type == "person":
            color = (30, 220, 70)
            label = f"person track={track_id} conf={_conf_text(bbox.confidence)}"
        elif object_type == "known_face":
            color = (220, 40, 220)
            display_name = _display_name(obj)
            label = f"known_face {display_name} score={_identity_score_text(obj)}"
        else:
            color = (30, 210, 230)
            label = f"unknown_face track={track_id} conf={_conf_text(bbox.confidence)}"
        x1, y1, x2, y2 = _clamped_box(bbox, width=width, height=height)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        _put_label(frame, label, x1, max(0, y1 - 8), color)
        if object_type == "person":
            _draw_pose(frame, obj)
        elif object_type in {"face", "known_face"}:
            _draw_landmarks(frame, obj)
            if label_kind == "known_face":
                _put_label(frame, "identity-bound", x1, min(height - 4, y2 + 18), color)
    header = (
        f"frame={frame_index} size={width}x{height} source_id={source_id} "
        f"person={counts['person']} face={counts['face']} known_face={counts['known_face']} "
        f"annotation_source={annotation_source} status={status}"
    )
    cv2.rectangle(frame, (0, 0), (min(width - 1, 1500), 34), (0, 0, 0), -1)
    cv2.putText(frame, header, (8, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2, cv2.LINE_AA)
    return frame


def _draw_pose(frame: Any, obj: dict[str, Any]) -> None:
    cv2 = _cv2()
    points = _pose_keypoints(obj)
    keyed = {index: point for index, point in enumerate(points)}
    for start, end in COCO17_EDGES:
        p1 = keyed.get(start)
        p2 = keyed.get(end)
        if not p1 or not p2:
            continue
        if (p1[2] is not None and p1[2] <= 0.05) or (p2[2] is not None and p2[2] <= 0.05):
            continue
        cv2.line(frame, (int(round(p1[0])), int(round(p1[1]))), (int(round(p2[0])), int(round(p2[1]))), (80, 140, 255), 2)
    for x, y, confidence in points:
        if confidence is not None and confidence <= 0.05:
            continue
        cv2.circle(frame, (int(round(x)), int(round(y))), 4, (255, 80, 40), -1)


def _draw_landmarks(frame: Any, obj: dict[str, Any]) -> None:
    cv2 = _cv2()
    for x, y, _confidence in _landmark_points(obj):
        cv2.circle(frame, (int(round(x)), int(round(y))), 5, (0, 255, 255), -1)
        cv2.circle(frame, (int(round(x)), int(round(y))), 5, (0, 0, 0), 1)


def _write_contact_sheet(*, output_path: Path, overlay_paths: list[Path], sample_results: list[dict[str, Any]]) -> None:
    if not overlay_paths:
        return
    cv2 = _cv2()
    np = _numpy()
    thumbs: list[Any] = []
    target_width = 420
    target_height = 260
    header_height = 58
    for path, result in zip(overlay_paths, sample_results):
        image = cv2.imread(str(path))
        if image is None:
            image = _placeholder_frame(width=target_width, height=target_height, text="missing overlay")
        resized = cv2.resize(image, (target_width, target_height), interpolation=cv2.INTER_AREA)
        tile = np.full((target_height + header_height, target_width, 3), 245, dtype=np.uint8)
        tile[header_height:, :] = resized
        status = result.get("audit_status")
        color = (30, 130, 30) if status == "ok" else (0, 140, 220) if status == "no_objects" else (0, 165, 255) if status == "suspicious" else (0, 0, 220)
        cv2.putText(tile, f"frame {result.get('frame_index')} status={status}", (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.58, color, 2, cv2.LINE_AA)
        counts = f"person={result.get('person_count')} face={result.get('face_count')} known={result.get('known_face_count')}"
        cv2.putText(tile, counts, (10, 46), cv2.FONT_HERSHEY_SIMPLEX, 0.54, (20, 20, 20), 1, cv2.LINE_AA)
        thumbs.append(tile)
    columns = 3
    rows = math.ceil(len(thumbs) / columns)
    sheet = np.full((rows * (target_height + header_height), columns * target_width, 3), 255, dtype=np.uint8)
    for index, tile in enumerate(thumbs):
        row = index // columns
        col = index % columns
        y = row * (target_height + header_height)
        x = col * target_width
        sheet[y : y + tile.shape[0], x : x + tile.shape[1]] = tile
    _write_image(output_path, sheet)


def _write_report(path: Path, *, audit_summary: dict[str, Any], frame_table: list[dict[str, Any]]) -> None:
    warnings = audit_summary["warnings"]
    failures = audit_summary["failures"]
    passed = [
        "video, sink metadata, production sidecar, and summary were present",
        f"video frame count={audit_summary['timeline']['video_frame_count']}",
        f"sidecar frame count={audit_summary['timeline']['sidecar_frame_count']}",
        f"fallback_used={audit_summary['summary_flags']['fallback_used']}",
        f"legacy_used_for_visual_binding={audit_summary['summary_flags']['legacy_used_for_visual_binding']}",
        "known_face_count=0 treated as expected before C2.4 identity binding",
    ]
    sampled = ", ".join(str(frame) for frame in audit_summary["sampled_frames"])
    next_step = _next_step_text(audit_summary["result_marker"])
    lines = [
        "# C2.3Q Evidence Output Correctness Audit",
        "",
        f"- Audit object: {audit_summary['bundle_dir']}",
        f"- Audit time: {audit_summary['audit_time']}",
        f"- Input bundle: {audit_summary['bundle_dir']}",
        f"- Output directory: {audit_summary['output_dir']}",
        f"- Result: {audit_summary['result_marker']}",
        f"- Sampled frames: {sampled}",
        "",
        "## Overall Conclusion",
        "",
        _conclusion_text(audit_summary["result_marker"]),
        "",
        "## Passed Items",
        "",
        *[f"- {item}" for item in passed],
        "",
        "## Suspicious Items",
        "",
        *(_issue_lines(warnings) if warnings else ["- None"]),
        "",
        "## Failure Items",
        "",
        *(_issue_lines(failures) if failures else ["- None"]),
        "",
        "## Identity Semantics",
        "",
        "- known_face_count=0 is not an automatic failure in C2.3Q.",
        "- C2.4 identity binding is not connected in this audit.",
        "- This audit validates unknown face observations, person boxes, pose keypoints, and face landmarks only.",
        "- Do not claim that any specific person was recognized from this bundle.",
        "",
        "## Manual Check Instructions",
        "",
        "- Open index.html first.",
        "- Start with contact_sheet.jpg for the sampled-frame overview.",
        "- Then inspect overlay images for frames marked suspicious or fail.",
        "- Confirm person boxes enclose bodies, pose points sit on bodies, face boxes enclose faces, and five landmarks sit on the face.",
        "- No-object sampled frames are displayed for context and do not fail the audit by themselves.",
        "",
        "## Frame Summary",
        "",
        "| frame | status | person | face | known_face | keypoints | landmarks | notes |",
        "|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in frame_table:
        lines.append(
            f"| {row['frame_index']} | {row['audit_status']} | {row['person_count']} | {row['face_count']} | "
            f"{row['known_face_count']} | {row['keypoint_count']} | {row['landmark_count']} | {row['notes']} |"
        )
    lines.extend(
        [
            "",
            "## Next Step Recommendation",
            "",
            next_step,
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_html(path: Path, *, audit_summary: dict[str, Any], frame_table: list[dict[str, Any]]) -> None:
    warnings = audit_summary["warnings"]
    failures = audit_summary["failures"]
    result = audit_summary["result_marker"]
    timeline = audit_summary["timeline"]
    flags = audit_summary["summary_flags"]
    counts = audit_summary["object_counts"]
    card_rows = [
        ("result_marker", result),
        ("bundle_dir", audit_summary["bundle_dir"]),
        ("raw_clip path", audit_summary["raw_clip_path"]),
        ("frame_count_video", timeline["video_frame_count"]),
        ("frame_count_metadata", timeline["metadata_frame_count"]),
        ("frame_count_sidecar", timeline["sidecar_frame_count"]),
        ("trim_occurred", timeline["trim_occurred"]),
        ("timeline_reconciliation_status", timeline["timeline_reconciliation_status"]),
        ("production_ready", flags["production_ready"]),
        ("fallback_used", flags["fallback_used"]),
        ("legacy_used_for_visual_binding", flags["legacy_used_for_visual_binding"]),
        ("annotation_source_kind", flags["annotation_source_kind"]),
        ("person_count", counts["person"]),
        ("face_count", counts["face"]),
        ("known_face_count", counts["known_face"]),
        ("keypoints_count", counts["keypoints"]),
        ("face_landmarks_count", counts["face_landmarks"]),
        ("sampled_frames_count", audit_summary["sampled_frames_count"]),
        ("frames_with_objects", audit_summary["frames_with_objects"]),
        ("frames_without_objects", audit_summary["frames_without_objects"]),
    ]
    sample_tiles = "\n".join(_sample_tile(result) for result in audit_summary["sample_results"])
    frame_rows = "\n".join(_frame_html_row(row) for row in frame_table)
    issue_items = _html_issue_list([*failures, *warnings])
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>C2.3Q Evidence Output Correctness Audit</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 24px; color: #1f2933; background: #f8fafc; }}
    h1, h2 {{ color: #102a43; }}
    .card {{ background: white; border: 1px solid #d9e2ec; border-radius: 6px; padding: 16px; margin-bottom: 18px; }}
    .marker {{ display: inline-block; padding: 6px 10px; border-radius: 4px; font-weight: 700; background: {_result_color(result)}; color: white; }}
    table {{ width: 100%; border-collapse: collapse; background: white; }}
    th, td {{ border: 1px solid #d9e2ec; padding: 7px; text-align: left; vertical-align: top; }}
    th {{ background: #eef2f7; }}
    img {{ max-width: 100%; border: 1px solid #d9e2ec; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }}
    .tile {{ background: white; border: 1px solid #d9e2ec; border-radius: 6px; padding: 10px; }}
    .ok {{ color: #137333; font-weight: 700; }}
    .no_objects {{ color: #0369a1; font-weight: 700; }}
    .suspicious {{ color: #b45309; font-weight: 700; }}
    .fail {{ color: #b91c1c; font-weight: 700; }}
    code {{ background: #eef2f7; padding: 2px 4px; border-radius: 3px; }}
  </style>
</head>
<body>
  <h1>C2.3Q Evidence Output Correctness Audit</h1>
  <div class="card">
    <p><span class="marker">{_e(result)}</span></p>
    <table>
      <tbody>
        {_html_key_value_rows(card_rows)}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>Identity Semantics</h2>
    <p><strong>known_face_count=0 is not an automatic failure in C2.3Q.</strong></p>
    <p>C2.4 identity binding is not connected yet. This audit validates unknown face observations, person boxes, pose keypoints, and face landmarks. It must not be used to claim that a specific person was recognized.</p>
  </div>

  <div class="card">
    <h2>Contact Sheet</h2>
    <p><a href="contact_sheet.jpg">Open contact_sheet.jpg</a></p>
    <img src="contact_sheet.jpg" alt="C2 audit contact sheet">
  </div>

  <div class="card">
    <h2>Sample Frame Tiles</h2>
    <div class="grid">{sample_tiles}</div>
  </div>

  <div class="card">
    <h2>Single Frame Details</h2>
    <table>
      <thead>
        <tr>
          <th>frame</th><th>raw</th><th>overlay</th><th>person</th><th>face</th><th>known_face</th>
          <th>keypoints</th><th>landmarks</th><th>bbox violations</th><th>keypoint suspicious</th>
          <th>landmark suspicious</th><th>bbox/keypoint/landmark status</th><th>notes</th>
        </tr>
      </thead>
      <tbody>{frame_rows}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>Warnings and Failures</h2>
    {issue_items}
  </div>
</body>
</html>
"""
    path.write_text(html_text, encoding="utf-8")


def _sample_tile(result: dict[str, Any]) -> str:
    status = str(result.get("audit_status") or "")
    return (
        f"<div class=\"tile\">"
        f"<p><strong>frame {_e(result.get('frame_index'))}</strong> "
        f"<span class=\"{_e(status)}\">{_e(status)}</span></p>"
        f"<p>person={_e(result.get('person_count'))} face={_e(result.get('face_count'))} "
        f"known_face={_e(result.get('known_face_count'))}</p>"
        f"<a href=\"{_e(result.get('overlay_path'))}\"><img src=\"{_e(result.get('overlay_path'))}\" alt=\"frame {_e(result.get('frame_index'))} overlay\"></a>"
        f"</div>"
    )


def _frame_html_row(row: dict[str, Any]) -> str:
    frame_index = int(row["frame_index"])
    raw_path = f"frames/frame_{frame_index:06d}.jpg"
    overlay_path = str(row["overlay_path"])
    status = str(row["audit_status"])
    return (
        "<tr>"
        f"<td>{frame_index}</td>"
        f"<td><a href=\"{_e(raw_path)}\">raw</a></td>"
        f"<td><a href=\"{_e(overlay_path)}\">overlay</a></td>"
        f"<td>{_e(row['person_count'])}</td>"
        f"<td>{_e(row['face_count'])}</td>"
        f"<td>{_e(row['known_face_count'])}</td>"
        f"<td>{_e(row['keypoint_count'])}</td>"
        f"<td>{_e(row['landmark_count'])}</td>"
        f"<td>{_e(row.get('bbox_in_bounds_violations', 0))}</td>"
        f"<td>{_e(row.get('keypoint_in_person_bbox_suspicious_count', 0))}</td>"
        f"<td>{_e(row.get('landmark_near_face_suspicious_count', 0))}</td>"
        f"<td class=\"{_e(status)}\">{_e(status)}</td>"
        f"<td>{_e(row['notes'])}</td>"
        "</tr>"
    )


def _html_key_value_rows(rows: list[tuple[str, Any]]) -> str:
    return "\n".join(f"<tr><th>{_e(key)}</th><td>{_e(value)}</td></tr>" for key, value in rows)


def _html_issue_list(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return "<p class=\"ok\">No warnings or failures.</p>"
    items = []
    for issue in issues:
        cls = "fail" if issue.get("severity") == "failure" else "suspicious"
        items.append(
            f"<li class=\"{cls}\"><code>{_e(issue.get('code'))}</code> "
            f"frame={_e(issue.get('frame_index'))} object={_e(issue.get('object_type'))}: "
            f"{_e(issue.get('message'))}</li>"
        )
    return "<ul>" + "\n".join(items) + "</ul>"


def _issue_lines(issues: list[dict[str, Any]]) -> list[str]:
    return [
        f"- {issue.get('severity')}: {issue.get('code')} frame={issue.get('frame_index')} "
        f"object={issue.get('object_type')} - {issue.get('message')}"
        for issue in issues
    ]


def _count_issue_codes(issues: list[dict[str, Any]], codes: set[str]) -> int:
    return sum(1 for issue in issues if issue.get("code") in codes)


def _conclusion_text(result_marker: str) -> str:
    if result_marker == RESULT_PASS:
        return "Automatic C2.3Q checks passed. The generated visual materials are ready for manual confirmation before C2.3B."
    if result_marker == RESULT_PARTIAL:
        return "Automatic C2.3Q checks found suspicious but non-blocking items. Manual review is required before C2.3B."
    return "Automatic C2.3Q checks found blocking failures. Do not continue to C2.3B before fixing the reported evidence output issue."


def _next_step_text(result_marker: str) -> str:
    if result_marker == RESULT_PASS:
        return "If the human visual check agrees with the overlays, proceed to C2.3B Event / Clip Post-Savant Replay Stream Mapping."
    if result_marker == RESULT_PARTIAL:
        return "First inspect the frames called out as suspicious in index.html, then decide whether the evidence content is acceptable."
    return "Do not enter C2.3B. Fix the bbox, timeline, or sidecar issue identified by this audit first."


def _summary_flags(summary: dict[str, Any]) -> dict[str, Any]:
    annotation_source_kind = summary.get("annotation_source_kind")
    if not annotation_source_kind:
        annotation_source = summary.get("annotation_source")
        sidecar_type = summary.get("sidecar_type")
        if annotation_source == "post_savant_sink_metadata" or sidecar_type == "production":
            annotation_source_kind = ANNOTATION_SOURCE_KIND_PRODUCTION
        else:
            annotation_source_kind = "unknown"
    return {
        "production_ready": bool(summary.get("production_ready")),
        "fallback_used": bool(summary.get("fallback_used", False)),
        "legacy_used_for_visual_binding": bool(summary.get("legacy_used_for_visual_binding", False)),
        "annotation_source_kind": annotation_source_kind,
        "annotation_source": summary.get("annotation_source"),
        "visual_evidence_status": summary.get("visual_evidence_status") or summary.get("visual_binding_status"),
    }


def _count_objects(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"person": 0, "face": 0, "known_face": 0, "keypoints": 0, "face_landmarks": 0}
    for row in rows:
        for obj in _objects(row):
            object_type = _object_type(obj)
            if object_type in {"person", "face", "known_face"}:
                counts[object_type] += 1
            counts["keypoints"] += len(_pose_keypoints(obj))
            counts["face_landmarks"] += len(_landmark_points(obj))
    return counts


def _identity_semantics(summary: dict[str, Any], object_counts: dict[str, int]) -> dict[str, Any]:
    identity_binding_connected = bool(summary.get("identity_binding_connected"))
    known_face_count = int(object_counts.get("known_face") or 0)
    recognition_claim_allowed = bool(summary.get("recognition_claim_allowed")) and known_face_count > 0
    return {
        "known_face_zero_expected_before_c2_4": not identity_binding_connected,
        "identity_binding_connected": identity_binding_connected,
        "identity_patch_source": summary.get("identity_patch_source"),
        "recognition_claim_allowed": recognition_claim_allowed,
        "known_face_zero_is_failure": identity_binding_connected,
        "known_face_count": known_face_count,
        "unknown_face_count": int(object_counts.get("face") or 0),
    }


def _count_frame_objects(objects: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"person": 0, "face": 0, "known_face": 0}
    for obj in objects:
        object_type = _object_type(obj)
        if object_type in counts:
            counts[object_type] += 1
    return counts


def _frame_keypoint_count(objects: list[dict[str, Any]]) -> int:
    return sum(len(_pose_keypoints(obj)) for obj in objects)


def _frame_landmark_count(objects: list[dict[str, Any]]) -> int:
    return sum(len(_landmark_points(obj)) for obj in objects)


def _frame_status(
    *,
    decoded: bool,
    has_sidecar: bool,
    object_count: int,
    local_warnings: list[dict[str, Any]],
    local_failures: list[dict[str, Any]],
) -> str:
    if not decoded or not has_sidecar or local_failures:
        return "fail"
    if not object_count:
        return "no_objects"
    if local_warnings:
        return "suspicious"
    return "ok"


def _result_marker(*, warnings: list[dict[str, Any]], failures: list[dict[str, Any]]) -> str:
    if failures:
        return RESULT_FAIL
    if warnings:
        return RESULT_PARTIAL
    return RESULT_PASS


def _issue(code: str, message: str, frame_index: int | None = None, object_type: str | None = None) -> dict[str, Any]:
    severity = "failure" if code in {
        "video_sidecar_frame_count_mismatch",
        "trim_occurred",
        "fallback_used",
        "legacy_used_for_visual_binding",
        "annotation_source_kind_not_production_sidecar",
        "production_not_ready",
        "frame_missing_sidecar",
        "decoded_frame_unavailable",
        "bbox_missing_or_unparseable",
        "bbox_negative_or_zero_size",
        "bbox_out_of_bounds",
        "landmarks_outside_image",
    } else "warning"
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "frame_index": frame_index,
        "object_type": object_type,
    }


def _objects(row: Any) -> list[dict[str, Any]]:
    objects = _dict(row).get("objects")
    if not isinstance(objects, list):
        return []
    return [obj for obj in objects if isinstance(obj, dict)]


def _frame_has_objects(row: dict[str, Any]) -> bool:
    return bool(_objects(row))


def _metadata_frame_count(metadata: Any, summary: dict[str, Any]) -> int:
    for key in ("original_metadata_frame_count", "metadata_frame_count", "frame_count_metadata"):
        value = _int_or_none(summary.get(key))
        if value is not None:
            return value
    if isinstance(metadata, list):
        return len(metadata)
    if isinstance(metadata, dict):
        frames = metadata.get("frames") or metadata.get("metadata")
        if isinstance(frames, list):
            return len(frames)
    return 0


def _default_sample_frames(video_frame_count: int) -> list[int]:
    if video_frame_count <= 0:
        return [0]
    candidates = [0, video_frame_count // 4, video_frame_count // 2, (video_frame_count * 3) // 4, video_frame_count - 1]
    return _normalize_sample_frames(candidates)


def _normalize_sample_frames(frames: Iterable[int]) -> list[int]:
    output: list[int] = []
    for frame in frames:
        if frame < 0:
            continue
        if frame not in output:
            output.append(frame)
    return output


def _pose_keypoints(obj: dict[str, Any]) -> list[tuple[float, float, float | None]]:
    pose = _dict(obj.get("pose"))
    raw_points = pose.get("keypoints")
    if not isinstance(raw_points, list):
        return []
    points: list[tuple[float, float, float | None]] = []
    for point in raw_points:
        if isinstance(point, dict):
            x = _number_or_none(point.get("x"))
            y = _number_or_none(point.get("y"))
            confidence = _number_or_none(point.get("confidence"))
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            x = _number_or_none(point[0])
            y = _number_or_none(point[1])
            confidence = _number_or_none(point[2]) if len(point) >= 3 else None
        else:
            continue
        if x is not None and y is not None:
            points.append((x, y, confidence))
    return points


def _landmark_points(obj: dict[str, Any]) -> list[tuple[float, float, float | None]]:
    landmarks = _dict(obj.get("landmarks"))
    raw_points = landmarks.get("points")
    if not isinstance(raw_points, list):
        return []
    points: list[tuple[float, float, float | None]] = []
    for point in raw_points:
        if isinstance(point, dict):
            x = _number_or_none(point.get("x"))
            y = _number_or_none(point.get("y"))
            confidence = _number_or_none(point.get("confidence"))
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            x = _number_or_none(point[0])
            y = _number_or_none(point[1])
            confidence = _number_or_none(point[2]) if len(point) >= 3 else None
        else:
            continue
        if x is not None and y is not None:
            points.append((x, y, confidence))
    return points


def _object_type(obj: dict[str, Any]) -> str:
    object_type = _text_or_none(obj.get("object_type"))
    if object_type:
        return object_type
    label_kind = _label_kind(obj)
    if label_kind == "known_face":
        return "known_face"
    if label_kind in {"unknown_face", "face"}:
        return "face"
    if label_kind == "person":
        return "person"
    return "unknown"


def _label_kind(obj: dict[str, Any]) -> str | None:
    label = obj.get("label")
    if isinstance(label, dict):
        return _text_or_none(label.get("kind"))
    return _text_or_none(label)


def _source_observation_id(obj: dict[str, Any]) -> str | None:
    identity = _dict(obj.get("identity"))
    return _text_or_none(identity.get("source_observation_id"))


def _display_name(obj: dict[str, Any]) -> str:
    identity = _dict(obj.get("identity"))
    return _text_or_none(identity.get("display_name")) or _text_or_none(identity.get("person_id")) or "unknown"


def _identity_score_text(obj: dict[str, Any]) -> str:
    identity = _dict(obj.get("identity"))
    score = _number_or_none(identity.get("similarity") or identity.get("score"))
    return "na" if score is None else f"{score:.3f}"


def _conf_text(value: float | None) -> str:
    return "na" if value is None else f"{value:.2f}"


def _point_in_image(point: tuple[float, float, float | None], *, width: int, height: int) -> bool:
    return 0 <= point[0] <= width and 0 <= point[1] <= height


def _point_in_bbox(point: tuple[float, float, float | None], bbox: BBox) -> bool:
    return bbox.x1 <= point[0] <= bbox.x2 and bbox.y1 <= point[1] <= bbox.y2


def _inflate_bbox(bbox: BBox, *, width: int, height: int, factor: float, min_margin: int) -> BBox:
    box_width = bbox.x2 - bbox.x1
    box_height = bbox.y2 - bbox.y1
    margin_x = max(min_margin, box_width * factor)
    margin_y = max(min_margin, box_height * factor)
    return BBox(
        max(0.0, bbox.x1 - margin_x),
        max(0.0, bbox.y1 - margin_y),
        min(float(width), bbox.x2 + margin_x),
        min(float(height), bbox.y2 + margin_y),
        bbox.fmt,
        bbox.confidence,
        bbox.source,
    )


def _bbox_outside_ratio(bbox: BBox, *, width: int, height: int) -> float:
    box_area = max(0.0, bbox.x2 - bbox.x1) * max(0.0, bbox.y2 - bbox.y1)
    if box_area <= 0:
        return 1.0
    ix1 = max(0.0, bbox.x1)
    iy1 = max(0.0, bbox.y1)
    ix2 = min(float(width), bbox.x2)
    iy2 = min(float(height), bbox.y2)
    inside_area = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    return max(0.0, min(1.0, 1.0 - inside_area / box_area))


def _looks_normalized(bbox: BBox) -> bool:
    values = [bbox.x1, bbox.y1, bbox.x2, bbox.y2]
    return all(0.0 <= value <= 1.0 for value in values)


def _clamped_box(bbox: BBox, *, width: int, height: int) -> tuple[int, int, int, int]:
    x1 = max(0, min(width - 1, int(round(bbox.x1))))
    y1 = max(0, min(height - 1, int(round(bbox.y1))))
    x2 = max(0, min(width - 1, int(round(bbox.x2))))
    y2 = max(0, min(height - 1, int(round(bbox.y2))))
    return x1, y1, x2, y2


def _put_label(frame: Any, text: str, x: int, y: int, color: tuple[int, int, int]) -> None:
    cv2 = _cv2()
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.55
    thickness = 2
    (text_width, text_height), baseline = cv2.getTextSize(text, font, scale, thickness)
    y = max(text_height + 4, y)
    x = max(0, min(frame.shape[1] - text_width - 6, x))
    cv2.rectangle(frame, (x, y - text_height - baseline - 5), (x + text_width + 6, y + baseline + 3), (0, 0, 0), -1)
    cv2.putText(frame, text, (x + 3, y), font, scale, color, thickness, cv2.LINE_AA)


def _read_video_frame(video_path: Path, frame_index: int) -> Any | None:
    cv2 = _cv2()
    capture = cv2.VideoCapture(str(video_path))
    try:
        if not capture.isOpened():
            return None
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if not ok:
            return None
        return frame
    finally:
        capture.release()


def _placeholder_frame(*, width: int, height: int, text: str) -> Any:
    cv2 = _cv2()
    np = _numpy()
    frame = np.full((height, width, 3), 220, dtype=np.uint8)
    cv2.putText(frame, text, (30, min(height - 30, 60)), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (40, 40, 40), 2, cv2.LINE_AA)
    return frame


def _write_image(path: Path, frame: Any) -> None:
    cv2 = _cv2()
    if not cv2.imwrite(str(path), frame):
        raise RuntimeError(f"failed to write image: {path}")


def _read_frame_count_ffprobe(video_path: Path) -> int | None:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_read_frames,nb_frames",
                "-of",
                "json",
                str(video_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    streams = data.get("streams")
    if not isinstance(streams, list) or not streams:
        return None
    stream = streams[0]
    for key in ("nb_read_frames", "nb_frames"):
        value = _int_or_none(stream.get(key))
        if value and value > 0:
            return value
    return None


def _read_frame_count_cv2(video_path: Path) -> int | None:
    cv2 = _cv2(required=False)
    if cv2 is None:
        return None
    capture = cv2.VideoCapture(str(video_path))
    try:
        if not capture.isOpened():
            return None
        value = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        return value if value > 0 else None
    finally:
        capture.release()


def _find_first_existing(directory: Path, names: Iterable[str]) -> Path | None:
    for name in names:
        path = directory / name
        if path.is_file():
            return path
    return None


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} missing: {path}")


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_json_or_jsonl(path: Path) -> Any:
    try:
        return _read_json(path)
    except json.JSONDecodeError:
        return _read_jsonl(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _relative_path(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def _numeric_list(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return []
    output: list[float] = []
    for item in value:
        number = _number_or_none(item)
        if number is None:
            return []
        output.append(number)
    return output


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _int_or_none(value: Any) -> int | None:
    number = _number_or_none(value)
    if number is None:
        return None
    return int(number)


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _fmt_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _iso_time(value: datetime | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _result_color(result: str) -> str:
    if result == RESULT_PASS:
        return "#137333"
    if result == RESULT_PARTIAL:
        return "#b45309"
    return "#b91c1c"


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _cv2(required: bool = True) -> Any:
    try:
        import cv2  # type: ignore[import-not-found]
    except Exception as exc:
        if required:
            raise RuntimeError("OpenCV/cv2 is required for C2.3Q visual audit output") from exc
        return None
    return cv2


def _numpy() -> Any:
    try:
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:
        raise RuntimeError("numpy is required for C2.3Q visual audit output") from exc
    return np


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--sample-frames", help="comma-separated frame indices, for example 0,30,60")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    result = audit_bundle(
        bundle_dir=args.bundle_dir,
        output_dir=args.output_dir,
        sample_frames=parse_sample_frames(args.sample_frames),
        overwrite=args.overwrite,
    )
    print(json.dumps({"result_marker": result.summary["result_marker"], "output_dir": str(result.output_dir)}, indent=2))
    return 0 if result.summary["result_marker"] != RESULT_FAIL else 2


if __name__ == "__main__":
    raise SystemExit(main())
