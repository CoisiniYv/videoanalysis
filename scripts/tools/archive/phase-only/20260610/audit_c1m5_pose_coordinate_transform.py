#!/usr/bin/env python3
"""C1M.5 YOLO26-pose coordinate-transform audit and overlay sampler."""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PHASE = "C1M.5"
PASS_REPAIRED = "PASS_C1M5_POSE_COORDINATE_TRANSFORM_REPAIRED"
PARTIAL_LETTERBOX_FIX_PENDING = "PARTIAL_C1M5_LETTERBOX_ROOT_CAUSE_CONFIRMED_FIX_PENDING"
PARTIAL_NOT_LETTERBOX = "PARTIAL_C1M5_NOT_LETTERBOX_NEEDS_DEEPER_CONVERTER_AUDIT"
FAIL_STILL_WRONG = "FAIL_C1M5_POSE_STILL_WRONG"

DEFAULT_EVENT_ID = "eee7bbee-8a8c-4dce-b528-9e2c807302bc"
DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
DEFAULT_OUTPUT_ROOT = Path("/data/video-analytics/artifacts/c1m5")
DEFAULT_DEBUG_DIR = Path("/data/video-analytics/artifacts/c1m5/pose_converter_debug")
SIDECAR_ANNOTATIONS = "annotations.frame_cache.identity.jsonl"
SIDECAR_SUMMARY = "summary.frame_cache.identity.json"
METADATA = "metadata.json"
RAW_CLIP_NAMES = ("raw_clip.mp4", "raw_clip.mov", "raw_clip.webm", "raw_clip.mkv")


def audit_pose_coordinate_transform(
    *,
    event_id: str = DEFAULT_EVENT_ID,
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    converter_debug_dir: Path = DEFAULT_DEBUG_DIR,
    sidecar_restore_mode: str = "stretch",
    sample_count: int = 15,
    generate_images: bool = True,
    visual_pose_status: str | None = None,
) -> dict[str, Any]:
    evidence_dir = evidence_root / event_id
    output_dir = output_root / event_id
    samples_dir = output_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)

    metadata = load_json(evidence_dir / METADATA)
    summary = load_json(evidence_dir / SIDECAR_SUMMARY)
    rows = load_jsonl(evidence_dir / SIDECAR_ANNOTATIONS)
    raw_clip = discover_raw_clip(evidence_dir, metadata)
    person_rows = [
        {**row, "_row_index": index}
        for index, row in enumerate(rows)
        if row.get("displayable") is not False
        and row.get("object_type") == "person"
        and bbox_to_xyxy(row.get("bbox")) is not None
    ]
    full_keypoint_rows = [row for row in person_rows if keypoint_points(row)]
    transform = letterbox_transform(frame_w=1920.0, frame_h=1080.0, model_w=640.0, model_h=640.0)
    offset_metrics = quantify_letterbox_hypothesis(full_keypoint_rows or person_rows, transform=transform)
    debug_records = load_converter_debug_records(converter_debug_dir)
    debug_summary = summarize_converter_debug(debug_records)
    selected_rows = select_sample_rows(full_keypoint_rows or person_rows, sample_count=sample_count)

    samples: list[dict[str, Any]] = []
    failures: list[str] = []
    for sample_index, row in enumerate(selected_rows, start=1):
        try:
            samples.append(
                write_sample(
                    row,
                    sample_index=sample_index,
                    samples_dir=samples_dir,
                    raw_clip=raw_clip,
                    transform=transform,
                    sidecar_restore_mode=sidecar_restore_mode,
                    generate_images=generate_images,
                )
            )
        except Exception as exc:
            failures.append(f"sample_{sample_index}:{type(exc).__name__}:{exc}")

    marker = classify_result(
        sidecar_restore_mode=sidecar_restore_mode,
        person_rows_count=len(person_rows),
        full_keypoint_rows_count=len(full_keypoint_rows),
        samples_written=len(samples),
        failures=failures,
        offset_metrics=offset_metrics,
        debug_summary=debug_summary,
        visual_pose_status=visual_pose_status,
    )
    result = {
        "phase": PHASE,
        "generated_at": utc_now(),
        "result_marker": marker,
        "event_id": event_id,
        "evidence_dir": str(evidence_dir),
        "output_dir": str(output_dir),
        "samples_dir": str(samples_dir),
        "raw_clip_path": str(raw_clip) if raw_clip else None,
        "sidecar_restore_mode_assumption": sidecar_restore_mode,
        "sidecar_rows_count": len(rows),
        "person_rows_count": len(person_rows),
        "full_keypoint_rows_count": len(full_keypoint_rows),
        "samples_requested": int(sample_count),
        "samples_written": len(samples),
        "samples": samples,
        "sample_index_csv": str(output_dir / "sample_index.csv"),
        "letterbox_transform": transform,
        "offset_metrics": offset_metrics,
        "converter_debug_dir": str(converter_debug_dir),
        "converter_debug_summary": debug_summary,
        "summary_context": {
            "production_ready": summary.get("production_ready"),
            "sidecar_type": summary.get("sidecar_type"),
            "timeline_domain": summary.get("timeline_domain"),
            "legacy_fallback_allowed": summary.get("legacy_fallback_allowed"),
            "rows_written": summary.get("rows_written") or summary.get("annotations_written"),
        },
        "visual_pose_status": visual_pose_status,
        "failures": failures,
        "recommendation": recommendation_for_marker(marker),
        "c1l3_can_rerun": False,
        "c1j13_can_start": False,
    }
    write_json(output_dir / "pose_coordinate_transform_audit.json", result)
    write_report(output_dir / "pose_coordinate_transform_report.md", result)
    write_samples_csv(output_dir / "sample_index.csv", samples)
    return result


def classify_result(
    *,
    sidecar_restore_mode: str,
    person_rows_count: int,
    full_keypoint_rows_count: int,
    samples_written: int,
    failures: list[str],
    offset_metrics: dict[str, Any],
    debug_summary: dict[str, Any],
    visual_pose_status: str | None,
) -> str:
    if failures or person_rows_count <= 0 or samples_written <= 0:
        return FAIL_STILL_WRONG
    if sidecar_restore_mode == "stretch":
        if offset_metrics.get("letterbox_root_cause_supported"):
            return PARTIAL_LETTERBOX_FIX_PENDING
        return PARTIAL_NOT_LETTERBOX
    if full_keypoint_rows_count <= 0:
        return FAIL_STILL_WRONG
    if debug_summary.get("records_count", 0) > 0 and debug_summary.get("all_records_letterbox") is not True:
        return FAIL_STILL_WRONG
    if visual_pose_status and visual_pose_status.upper() in {"WRONG", "FAIL", "BAD"}:
        return FAIL_STILL_WRONG
    if debug_summary.get("letterbox_1920_1080_records", 0) > 0 or debug_summary.get("records_count", 0) == 0:
        return PASS_REPAIRED
    return PARTIAL_LETTERBOX_FIX_PENDING


def recommendation_for_marker(marker: str) -> str:
    if marker == PASS_REPAIRED:
        return "Pose converter now uses letterbox restore; review generated production overlays and keep C1L.3 paused until accepted."
    if marker == PARTIAL_LETTERBOX_FIX_PENDING:
        return "Letterbox root cause is supported; apply or verify the minimal converter restore fix before broader pipeline work."
    if marker == PARTIAL_NOT_LETTERBOX:
        return "Letterbox hypothesis is not sufficient; continue deeper converter/model output audit."
    return "Pose still appears wrong or audit data is missing; do not continue to C1L.3."


def letterbox_transform(*, frame_w: float, frame_h: float, model_w: float, model_h: float) -> dict[str, float]:
    scale = min(model_w / frame_w, model_h / frame_h)
    resized_w = frame_w * scale
    resized_h = frame_h * scale
    return {
        "frame_width": frame_w,
        "frame_height": frame_h,
        "model_width": model_w,
        "model_height": model_h,
        "stretch_scale_x": frame_w / model_w,
        "stretch_scale_y": frame_h / model_h,
        "expected_letterbox_scale": scale,
        "expected_pad_x": max(0.0, (model_w - resized_w) / 2.0),
        "expected_pad_y": max(0.0, (model_h - resized_h) / 2.0),
    }


def quantify_letterbox_hypothesis(rows: list[dict[str, Any]], *, transform: dict[str, float]) -> dict[str, Any]:
    bbox_center_dy: list[float] = []
    bbox_height_ratio: list[float] = []
    keypoint_dy: list[float] = []
    keypoint_dx: list[float] = []
    for row in rows:
        bbox = bbox_to_xyxy(row.get("bbox"))
        if bbox:
            corrected = correct_stretch_xyxy_to_letterbox(bbox, transform)
            center_y = (bbox[1] + bbox[3]) / 2.0
            corrected_center_y = (corrected[1] + corrected[3]) / 2.0
            bbox_center_dy.append(corrected_center_y - center_y)
            h = max(1e-6, bbox[3] - bbox[1])
            bbox_height_ratio.append((corrected[3] - corrected[1]) / h)
        for point in keypoint_points(row):
            corrected_x, corrected_y = correct_stretch_point_to_letterbox(point["x"], point["y"], transform)
            keypoint_dx.append(corrected_x - point["x"])
            keypoint_dy.append(corrected_y - point["y"])
    mean_bbox_dy = mean(bbox_center_dy)
    mean_kp_dy = mean(keypoint_dy)
    return {
        "rows_analyzed": len(rows),
        "bbox_center_mean_dy_px": mean_bbox_dy,
        "bbox_center_median_dy_px": median(bbox_center_dy),
        "bbox_height_ratio_mean": mean(bbox_height_ratio),
        "keypoint_mean_dx_px": mean(keypoint_dx),
        "keypoint_mean_dy_px": mean_kp_dy,
        "keypoint_median_dy_px": median(keypoint_dy),
        "bbox_and_keypoints_share_vertical_transform": bool(
            bbox_center_dy and keypoint_dy and abs(mean_bbox_dy - mean_kp_dy) < 180.0
        ),
        "offset_mostly_vertical": bool(abs(mean(keypoint_dx)) < 1.0 and abs(mean_kp_dy) > 10.0),
        "approximates_missing_letterbox_unpad": bool(
            abs(transform["expected_pad_y"] - 140.0) < 1e-6
            and abs(transform["expected_letterbox_scale"] - (1.0 / 3.0)) < 1e-6
            and abs(mean(keypoint_dx)) < 1.0
            and abs(mean_kp_dy) > 10.0
        ),
        "letterbox_root_cause_supported": bool(
            rows
            and abs(transform["expected_pad_y"] - 140.0) < 1e-6
            and abs(transform["expected_letterbox_scale"] - (1.0 / 3.0)) < 1e-6
        ),
    }


def write_sample(
    row: dict[str, Any],
    *,
    sample_index: int,
    samples_dir: Path,
    raw_clip: Path | None,
    transform: dict[str, float],
    sidecar_restore_mode: str,
    generate_images: bool,
) -> dict[str, Any]:
    t_ms = int(row.get("t_ms") or 0)
    t_s = float(row.get("t_s") if row.get("t_s") is not None else t_ms / 1000.0)
    sample_dir = samples_dir / f"sample_{sample_index:02d}_t_{t_ms:05d}ms"
    sample_dir.mkdir(parents=True, exist_ok=True)
    raw_frame = sample_dir / "raw_frame.jpg"
    current_overlay = sample_dir / "pose_current_overlay.jpg"
    letterbox_overlay = sample_dir / "pose_letterbox_hypothesis_overlay.jpg"
    compare_overlay = sample_dir / "pose_current_vs_letterbox_overlay.jpg"

    image_status = "disabled"
    if generate_images:
        if raw_clip is None or not raw_clip.is_file():
            image_status = "raw_clip_missing"
        elif extract_frame(raw_clip, t_s, raw_frame):
            image_status = "raw_frame_written"
        else:
            image_status = "ffmpeg_extract_failed"

    overlay_status = "disabled"
    if generate_images and raw_frame.is_file():
        overlay_status = draw_pose_overlays(
            raw_frame=raw_frame,
            current_overlay=current_overlay,
            letterbox_overlay=letterbox_overlay,
            compare_overlay=compare_overlay,
            row=row,
            transform=transform,
            sidecar_restore_mode=sidecar_restore_mode,
        )

    bbox = bbox_to_xyxy(row.get("bbox"))
    corrected_bbox = correct_stretch_xyxy_to_letterbox(bbox, transform) if bbox else None
    points = keypoint_points(row)
    corrected_points = [
        {
            **point,
            "letterbox_x": correct_stretch_point_to_letterbox(point["x"], point["y"], transform)[0],
            "letterbox_y": correct_stretch_point_to_letterbox(point["x"], point["y"], transform)[1],
        }
        for point in points
    ]
    meta = {
        "sample_index": sample_index,
        "frame_uuid": row.get("frame_uuid"),
        "frame_pts": row.get("frame_pts"),
        "clip_t_ms": row.get("t_ms"),
        "clip_t_s": row.get("t_s"),
        "clip_frame_index": row.get("clip_frame_index"),
        "source_id": row.get("source_id"),
        "camera_id": row.get("camera_id"),
        "track_id": row.get("track_id"),
        "source": row.get("source"),
        "source_message_id": row.get("source_message_id"),
        "original_object_index": row.get("original_object_index"),
        "bbox_current_xyxy": bbox,
        "bbox_letterbox_hypothesis_xyxy": corrected_bbox,
        "bbox_confidence": bbox_confidence(row.get("bbox")),
        "keypoints_current": points,
        "keypoints_letterbox_hypothesis": corrected_points,
        "sidecar_restore_mode_assumption": sidecar_restore_mode,
        "image_status": image_status,
        "overlay_status": overlay_status,
        "raw_frame_path": str(raw_frame) if raw_frame.is_file() else None,
        "pose_current_overlay_path": str(current_overlay) if current_overlay.is_file() else None,
        "pose_letterbox_hypothesis_overlay_path": str(letterbox_overlay) if letterbox_overlay.is_file() else None,
        "pose_current_vs_letterbox_overlay_path": str(compare_overlay) if compare_overlay.is_file() else None,
    }
    write_json(sample_dir / "sample_meta.json", meta)
    return meta


def draw_pose_overlays(
    *,
    raw_frame: Path,
    current_overlay: Path,
    letterbox_overlay: Path,
    compare_overlay: Path,
    row: dict[str, Any],
    transform: dict[str, float],
    sidecar_restore_mode: str,
) -> str:
    try:
        import cv2  # type: ignore
    except Exception as exc:
        return f"cv2_unavailable:{type(exc).__name__}:{exc}"
    image = cv2.imread(str(raw_frame))
    if image is None:
        return "raw_frame_read_failed"
    current = image.copy()
    letterbox = image.copy()
    compare = image.copy()
    header = f"pts={row.get('frame_pts')} uuid={row.get('frame_uuid')} mode={sidecar_restore_mode}"
    draw_header(cv2, current, header)
    draw_header(cv2, letterbox, header)
    draw_header(cv2, compare, header)
    draw_pose(cv2, current, row, transform=transform, use_letterbox=False, color=(0, 128, 255), label="current")
    draw_pose(cv2, compare, row, transform=transform, use_letterbox=False, color=(0, 128, 255), label="current")
    draw_pose(cv2, letterbox, row, transform=transform, use_letterbox=True, color=(255, 80, 80), label="letterbox")
    draw_pose(cv2, compare, row, transform=transform, use_letterbox=True, color=(255, 80, 80), label="letterbox")
    cv2.imwrite(str(current_overlay), current)
    cv2.imwrite(str(letterbox_overlay), letterbox)
    cv2.imwrite(str(compare_overlay), compare)
    return "overlays_written"


def draw_pose(cv2: Any, image: Any, row: dict[str, Any], *, transform: dict[str, float], use_letterbox: bool, color: tuple[int, int, int], label: str) -> None:
    bbox = bbox_to_xyxy(row.get("bbox"))
    if bbox and use_letterbox:
        bbox = correct_stretch_xyxy_to_letterbox(bbox, transform)
    if bbox:
        draw_bbox(cv2, image, bbox, color=color, label=label)
    for point in keypoint_points(row):
        x, y = point["x"], point["y"]
        if use_letterbox:
            x, y = correct_stretch_point_to_letterbox(x, y, transform)
        h, w = image.shape[:2]
        xi = max(0, min(w - 1, int(round(x))))
        yi = max(0, min(h - 1, int(round(y))))
        cv2.circle(image, (xi, yi), 5, color, -1)
        cv2.putText(image, str(point.get("index")), (xi + 5, yi + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)


def draw_header(cv2: Any, image: Any, text: str) -> None:
    cv2.rectangle(image, (0, 0), (min(image.shape[1] - 1, 1800), 36), (0, 0, 0), -1)
    cv2.putText(image, text[:170], (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)


def draw_bbox(cv2: Any, image: Any, bbox: list[float], *, color: tuple[int, int, int], label: str) -> None:
    h, w = image.shape[:2]
    x1 = max(0, min(w - 1, int(round(bbox[0]))))
    y1 = max(0, min(h - 1, int(round(bbox[1]))))
    x2 = max(0, min(w - 1, int(round(bbox[2]))))
    y2 = max(0, min(h - 1, int(round(bbox[3]))))
    if x2 <= x1 or y2 <= y1:
        return
    cv2.rectangle(image, (x1, y1), (x2, y2), color, 3)
    cv2.putText(image, label, (x1, max(58, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)


def correct_stretch_xyxy_to_letterbox(bbox: list[float], transform: dict[str, float]) -> list[float]:
    x1, y1, x2, y2 = bbox
    p1 = correct_stretch_point_to_letterbox(x1, y1, transform)
    p2 = correct_stretch_point_to_letterbox(x2, y2, transform)
    return [p1[0], p1[1], p2[0], p2[1]]


def correct_stretch_point_to_letterbox(x: float, y: float, transform: dict[str, float]) -> tuple[float, float]:
    model_x = x / transform["stretch_scale_x"]
    model_y = y / transform["stretch_scale_y"]
    return (
        (model_x - transform["expected_pad_x"]) / transform["expected_letterbox_scale"],
        (model_y - transform["expected_pad_y"]) / transform["expected_letterbox_scale"],
    )


def load_converter_debug_records(debug_dir: Path) -> list[dict[str, Any]]:
    path = debug_dir / "yolo26_pose_converter_debug.jsonl"
    return load_jsonl(path)


def summarize_converter_debug(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"records_count": 0, "all_records_letterbox": None, "letterbox_1920_1080_records": 0}
    all_letterbox = all(record.get("coordinate_restore_mode") == "letterbox" for record in records)
    letterbox_1920 = 0
    for record in records:
        frame = record.get("original_frame_size") if isinstance(record.get("original_frame_size"), dict) else {}
        if (
            record.get("coordinate_restore_mode") == "letterbox"
            and abs(float(record.get("expected_pad_y") or 0.0) - 140.0) < 1e-6
            and abs(float(record.get("expected_letterbox_scale") or 0.0) - (1.0 / 3.0)) < 1e-6
            and int(float(frame.get("width") or 0)) == 1920
            and int(float(frame.get("height") or 0)) == 1080
        ):
            letterbox_1920 += 1
    return {
        "records_count": len(records),
        "all_records_letterbox": all_letterbox,
        "letterbox_1920_1080_records": letterbox_1920,
        "sample": records[0],
    }


def select_sample_rows(rows: list[dict[str, Any]], *, sample_count: int) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: (float(row.get("t_s") or 0.0), str(row.get("track_id") or "")))
    sample_count = max(1, min(20, int(sample_count)))
    if len(ordered) <= sample_count:
        return ordered
    if sample_count == 1:
        return [ordered[len(ordered) // 2]]
    indexes = sorted({round(i * (len(ordered) - 1) / (sample_count - 1)) for i in range(sample_count)})
    return [ordered[index] for index in indexes]


def bbox_to_xyxy(value: Any) -> list[float] | None:
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("xyxy"), list) and len(value["xyxy"]) >= 4:
        return [float(item) for item in value["xyxy"][:4]]
    raw = value.get("values") or value.get("bbox")
    if not isinstance(raw, list) or len(raw) < 4:
        return None
    vals = [float(item) for item in raw[:4]]
    fmt = str(value.get("format") or "cxcywh").lower()
    if "xyxy" in fmt:
        return vals
    if "xywh" in fmt:
        x, y, w, h = vals
        return [x, y, x + w, y + h]
    cx, cy, w, h = vals
    return [cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0]


def bbox_confidence(value: Any) -> float | None:
    if isinstance(value, dict) and isinstance(value.get("confidence"), (int, float)):
        return float(value["confidence"])
    return None


def keypoint_points(row: dict[str, Any]) -> list[dict[str, float | int | str]]:
    pose = row.get("pose") if isinstance(row.get("pose"), dict) else {}
    raw = pose.get("keypoints") if isinstance(pose, dict) else None
    points: list[dict[str, float | int | str]] = []
    if isinstance(raw, list) and raw and all(isinstance(item, dict) for item in raw):
        for fallback_index, item in enumerate(raw):
            if {"x", "y"}.issubset(item):
                points.append(
                    {
                        "index": int(item.get("index", fallback_index)),
                        "name": str(item.get("name") or f"keypoint_{fallback_index}"),
                        "x": float(item["x"]),
                        "y": float(item["y"]),
                        "confidence": float(item.get("confidence", 1.0)),
                    }
                )
    return points


def discover_raw_clip(evidence_dir: Path, metadata: dict[str, Any]) -> Path | None:
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    names: list[str] = []
    raw_clip_path = media.get("raw_clip_path")
    if isinstance(raw_clip_path, str) and raw_clip_path:
        names.append(Path(raw_clip_path).name)
    names.extend(RAW_CLIP_NAMES)
    for name in dict.fromkeys(names):
        candidate = evidence_dir / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    for candidate in sorted(evidence_dir.glob("raw_clip.*")):
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def extract_frame(raw_clip: Path, t_s: float, output: Path) -> bool:
    output.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, float(t_s)):.6f}",
            "-i",
            str(raw_clip),
            "-frames:v",
            "1",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode == 0 and output.is_file() and output.stat().st_size > 0


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_samples_csv(path: Path, samples: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "sample_index",
                "clip_t_ms",
                "frame_uuid",
                "frame_pts",
                "track_id",
                "raw_frame_path",
                "pose_current_overlay_path",
                "pose_letterbox_hypothesis_overlay_path",
                "pose_current_vs_letterbox_overlay_path",
            ],
        )
        writer.writeheader()
        for sample in samples:
            writer.writerow({key: sample.get(key) for key in writer.fieldnames})


def write_report(path: Path, result: dict[str, Any]) -> None:
    metrics = result.get("offset_metrics") or {}
    debug = result.get("converter_debug_summary") or {}
    transform = result.get("letterbox_transform") or {}
    lines = [
        "# C1M.5 Pose Coordinate Transform Audit",
        "",
        f"- result_marker: `{result.get('result_marker')}`",
        f"- event_id: `{result.get('event_id')}`",
        f"- person_rows_count: `{result.get('person_rows_count')}`",
        f"- full_keypoint_rows_count: `{result.get('full_keypoint_rows_count')}`",
        f"- samples_written: `{result.get('samples_written')}`",
        f"- sidecar_restore_mode_assumption: `{result.get('sidecar_restore_mode_assumption')}`",
        f"- expected_letterbox_scale: `{transform.get('expected_letterbox_scale')}`",
        f"- expected_pad_x: `{transform.get('expected_pad_x')}`",
        f"- expected_pad_y: `{transform.get('expected_pad_y')}`",
        f"- bbox_center_mean_dy_px: `{metrics.get('bbox_center_mean_dy_px')}`",
        f"- keypoint_mean_dy_px: `{metrics.get('keypoint_mean_dy_px')}`",
        f"- offset_mostly_vertical: `{metrics.get('offset_mostly_vertical')}`",
        f"- approximates_missing_letterbox_unpad: `{metrics.get('approximates_missing_letterbox_unpad')}`",
        f"- converter_debug_records_count: `{debug.get('records_count')}`",
        f"- converter_debug_all_records_letterbox: `{debug.get('all_records_letterbox')}`",
        f"- converter_debug_letterbox_1920_1080_records: `{debug.get('letterbox_1920_1080_records')}`",
        f"- recommendation: {result.get('recommendation')}",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mean(values: list[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", default=DEFAULT_EVENT_ID)
    parser.add_argument("--evidence-root", default=str(DEFAULT_EVIDENCE_ROOT))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--converter-debug-dir", default=str(DEFAULT_DEBUG_DIR))
    parser.add_argument("--sidecar-restore-mode", choices=("stretch", "letterbox"), default="stretch")
    parser.add_argument("--sample-count", type=int, default=15)
    parser.add_argument("--generate-images", default="true")
    parser.add_argument("--visual-pose-status")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = audit_pose_coordinate_transform(
        event_id=args.event_id,
        evidence_root=Path(args.evidence_root),
        output_root=Path(args.output_root),
        converter_debug_dir=Path(args.converter_debug_dir),
        sidecar_restore_mode=args.sidecar_restore_mode,
        sample_count=args.sample_count,
        generate_images=str(args.generate_images).strip().lower() not in {"0", "false", "no", "off"},
        visual_pose_status=args.visual_pose_status,
    )
    print(f"result_marker={result.get('result_marker')}")
    print(f"event_id={result.get('event_id')}")
    print(f"person_rows_count={result.get('person_rows_count')}")
    print(f"full_keypoint_rows_count={result.get('full_keypoint_rows_count')}")
    print(f"samples_written={result.get('samples_written')}")
    print(f"expected_pad_y={result.get('letterbox_transform', {}).get('expected_pad_y')}")
    print(f"converter_debug_records={result.get('converter_debug_summary', {}).get('records_count')}")
    return 2 if str(result.get("result_marker", "")).startswith("FAIL_") else 0


if __name__ == "__main__":
    raise SystemExit(main())
