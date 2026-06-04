#!/usr/bin/env python3
"""人脸/人体检测框与画面"对不齐"根因诊断 —— 区分【空间错位】还是【时间漂移】。

为什么需要它
------------
时间域公式（frame_pts vs epoch）已经修过，offset 也落在 clip 时长内，但框还是
没贴在脸上。"offset 在 0~10s 内"并不能证明对齐——它只说明时间没离谱，不说明
空间坐标对、也不说明那一帧就是脸所在的帧。本脚本把 bbox 直接烧到 raw_clip 的
真实帧上，并做数值边界检查，一次性区分两类根因：

  1. 空间错位（SPATIAL）：bbox 坐标系/分辨率与 clip 不一致 → 框系统性偏移/出界。
     典型：bbox 是 1920x1080 像素，但 clip 实际是别的分辨率；或坐标被缩放过。
  2. 时间漂移（TEMPORAL）：bbox 坐标对，但 time_offset 指向的帧不是脸所在帧 →
     在 t 处框是空的，在 t±Δ 处脸却落进框里（人在移动）。

判读方式
--------
- 数值检查若发现 bbox 超出 [0,W]×[0,H] → 直接判 SPATIAL（无需肉眼）。
- 否则看导出的三联图 (t-Δ, t, t+Δ)：
    * 脸只在某个 t±Δ 落进框、t 处没有 → TEMPORAL（框对、时刻错）。
    * 三张里框都系统性偏离脸 → SPATIAL（坐标系错）。
    * t 处脸就在框里 → 该观测对齐 OK（剩下的是稀疏/其它观测问题）。
- 同时对比 known_face offset 与 event offset：差距过大提示时间映射整体偏移。

依赖：ffmpeg + ffprobe（用 drawbox 滤镜烧框，无需 PIL/cv2）。

用法
----
    python diagnose_overlay_alignment.py /data/.../media/evidence/<event_id>
    # 或显式给文件
    python diagnose_overlay_alignment.py --clip raw_clip.mov --ann annotations.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DELTA_S = 0.4  # 三联图的前后偏移量
PERSON_DEBUG_ROOT = Path("/data/video-analytics/artifacts/c1i2j/person_pose_overlay_debug")
OBJECT_CHOICES = ("face", "person", "all")


def _resolve(tool: str) -> str | None:
    exe = shutil.which(tool)
    if exe:
        return exe
    if tool == "ffmpeg":
        try:
            import imageio_ffmpeg  # type: ignore

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def probe_resolution(ffprobe: str, clip: str) -> tuple[int | None, int | None, float | None]:
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", clip,
    ]
    proc = _run(cmd)
    if proc.returncode != 0:
        return None, None, None
    try:
        data = json.loads(proc.stdout)
        st = (data.get("streams") or [{}])[0]
        w = int(st.get("width")) if st.get("width") else None
        h = int(st.get("height")) if st.get("height") else None
        dur = float(data.get("format", {}).get("duration")) if data.get("format", {}).get("duration") else None
        return w, h, dur
    except Exception:
        return None, None, None


def bbox_to_xyxy(bbox: dict[str, Any]) -> tuple[float, float, float, float] | None:
    if not isinstance(bbox, dict):
        return None
    fmt = str(bbox.get("format") or "").lower()
    vals = bbox.get("values") or bbox.get("xyxy") or bbox.get("cxcywh") or bbox.get("xywh")
    if bbox.get("xyxy") is not None:
        fmt = "xyxy"; vals = bbox.get("xyxy")
    if not isinstance(vals, list) or len(vals) < 4:
        return None
    a, b, c, d = (float(vals[0]), float(vals[1]), float(vals[2]), float(vals[3]))
    if "cxcywh" in fmt or (not fmt and bbox.get("cxcywh") is not None):
        return a - c / 2, b - d / 2, a + c / 2, b + d / 2
    if "xyxy" in fmt:
        return a, b, c, d
    if "xywh" in fmt:
        return a, b, a + c, b + d
    # 无 format：cxcywh 与 xyxy 都试不出语义时，按 cxcywh 猜（与 viewer 默认一致）
    return a - c / 2, b - d / 2, a + c / 2, b + d / 2


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    except Exception:
        return rows
    return rows


def object_record(line: dict[str, Any], obj: dict[str, Any]) -> dict[str, Any]:
    merged = dict(line)
    merged.update(obj)
    if "time_offset_ms" not in merged or merged.get("time_offset_ms") is None:
        merged["time_offset_ms"] = line.get("time_offset_ms")
    if "time_basis" not in merged or merged.get("time_basis") is None:
        merged["time_basis"] = line.get("time_basis")
    if "time_alignment_status" not in merged or merged.get("time_alignment_status") is None:
        merged["time_alignment_status"] = line.get("time_alignment_status")
    if "frame_pts" not in merged or merged.get("frame_pts") is None:
        merged["frame_pts"] = line.get("frame_pts")
    if "frame_num" not in merged or merged.get("frame_num") is None:
        merged["frame_num"] = line.get("frame_num")
    return merged


def annotation_objects(line: dict[str, Any]) -> list[dict[str, Any]]:
    if line.get("record_type") == "object_annotation":
        return [line]
    if isinstance(line.get("objects"), list):
        return [
            object_record(line, obj)
            for obj in line["objects"]
            if isinstance(obj, dict)
        ]
    return []


def annotation_role(item: dict[str, Any]) -> str:
    role = str(item.get("annotation_role") or "")
    label = _as_dict(item.get("label"))
    action = _as_dict(item.get("action"))
    style = _as_dict(item.get("style"))
    if role:
        return role
    if label.get("kind") == "behavior_event" or action.get("event_type") == "intrusion":
        return "behavior_event"
    if style.get("reason") == "person_detection":
        return "person_context"
    if item.get("object_type") == "face":
        return "face"
    return "unknown"


def is_behavior_event(item: dict[str, Any]) -> bool:
    label = _as_dict(item.get("label"))
    action = _as_dict(item.get("action"))
    return (
        item.get("object_type") == "person"
        and (
            annotation_role(item) == "behavior_event"
            or label.get("kind") == "behavior_event"
            or action.get("event_type") == "intrusion"
            or action.get("status") == "event_triggered"
        )
    )


def is_person_context(item: dict[str, Any]) -> bool:
    return item.get("object_type") == "person" and annotation_role(item) == "person_context"


def object_matches(item: dict[str, Any], object_filter: str) -> bool:
    object_type = str(item.get("object_type") or "")
    if object_filter == "all":
        return object_type in {"face", "person"}
    return object_type == object_filter


def iter_object_annotations(ann_path: Path, object_filter: str = "face"):
    """Yield normalized annotation objects for face/person/all."""
    for line in load_jsonl(ann_path):
        for item in annotation_objects(line):
            if object_matches(item, object_filter):
                yield item


def iter_face_annotations(ann_path: Path):
    """产出 (time_offset_ms, bbox_dict, role, identity_status, name, track_id)。"""
    for o in iter_object_annotations(ann_path, "face"):
        t = o.get("time_offset_ms")
        if t is None:
            continue
        ident = _as_dict(o.get("identity"))
        yield (
            float(t),
            o.get("bbox"),
            annotation_role(o),
            str(ident.get("status") or "unknown"),
            str(ident.get("display_name") or ""),
            str(o.get("track_id") or ""),
            str(o.get("time_basis") or ""),
        )


def _identity_status(item: dict[str, Any]) -> str:
    ident = _as_dict(item.get("identity"))
    return str(ident.get("status") or "unknown")


def _display_name(item: dict[str, Any]) -> str:
    ident = _as_dict(item.get("identity"))
    return str(ident.get("display_name") or ident.get("name") or ident.get("person_id") or "")


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distribution(values: list[float], buckets: list[tuple[str, float, float]]) -> dict[str, int]:
    counts = {label: 0 for label, _lo, _hi in buckets}
    for value in values:
        for label, lo, hi in buckets:
            if lo <= value < hi:
                counts[label] += 1
                break
    return counts


def bbox_metrics(
    item: dict[str, Any],
    width: int | None,
    height: int | None,
) -> dict[str, Any]:
    xyxy = bbox_to_xyxy(_as_dict(item.get("bbox")))
    metrics: dict[str, Any] = {
        "xyxy": xyxy,
        "width": None,
        "height": None,
        "area_ratio": None,
        "out_of_frame": False,
        "center": None,
    }
    if xyxy is None:
        metrics["out_of_frame"] = True
        return metrics
    x1, y1, x2, y2 = xyxy
    box_w = max(0.0, x2 - x1)
    box_h = max(0.0, y2 - y1)
    metrics["width"] = box_w
    metrics["height"] = box_h
    metrics["center"] = (x1 + box_w / 2.0, y1 + box_h / 2.0)
    if width and height and width > 0 and height > 0:
        metrics["area_ratio"] = (box_w * box_h) / float(width * height)
        metrics["out_of_frame"] = (
            x1 < -2
            or y1 < -2
            or x2 > width + 2
            or y2 > height + 2
            or box_w <= 0
            or box_h <= 0
        )
    return metrics


def audit_person_annotations(
    records: list[dict[str, Any]],
    *,
    width: int | None,
    height: int | None,
) -> dict[str, Any]:
    persons = [
        item
        for line in records
        for item in annotation_objects(line)
        if item.get("object_type") == "person"
    ]
    person_context = [item for item in persons if is_person_context(item)]
    behavior = [item for item in persons if is_behavior_event(item)]
    bbox_sources: Counter[str] = Counter()
    bbox_formats: Counter[str] = Counter()
    time_basis: Counter[str] = Counter()
    alignment: Counter[str] = Counter()
    by_second: Counter[str] = Counter()
    tracks: set[str] = set()
    missing_format_count = 0
    missing_source_count = 0
    out_of_frame_count = 0
    area_ratios: list[float] = []
    widths: list[float] = []
    heights: list[float] = []
    time_offsets: list[float] = []
    frame_pts_present = 0
    frame_num_present = 0
    timestamp_estimated_count = 0
    created_at_fallback_count = 0
    per_track: dict[str, list[tuple[float, tuple[float, float], tuple[float, float, float, float]]]] = defaultdict(list)

    for item in persons:
        bbox = _as_dict(item.get("bbox"))
        source = str(bbox.get("source") or "")
        fmt = str(bbox.get("format") or "")
        bbox_sources[source] += 1
        bbox_formats[fmt] += 1
        if not fmt:
            missing_format_count += 1
        if not source:
            missing_source_count += 1
        status = str(item.get("time_alignment_status") or "")
        basis = str(item.get("time_basis") or "")
        if not basis and str(item.get("time_alignment_status") or "") == "estimated":
            basis = "timestamp_estimated"
        elif not basis:
            basis = "unspecified"
        if not basis and status == "estimated":
            basis = "timestamp_estimated"
        elif not basis:
            basis = "unspecified"
        time_basis[basis] += 1
        alignment[status] += 1
        if basis == "timestamp_estimated" or (not basis and status == "estimated"):
            timestamp_estimated_count += 1
        if basis == "created_at":
            created_at_fallback_count += 1
        if item.get("frame_pts") is not None:
            frame_pts_present += 1
        if item.get("frame_num") is not None:
            frame_num_present += 1
        track = str(item.get("track_id") or "")
        if track:
            tracks.add(track)
        offset = _to_float(item.get("time_offset_ms"), None)
        if offset is not None:
            time_offsets.append(offset)
            by_second[str(int(offset // 1000))] += 1
        metrics = bbox_metrics(item, width, height)
        if metrics["out_of_frame"]:
            out_of_frame_count += 1
        if metrics["area_ratio"] is not None:
            area_ratios.append(float(metrics["area_ratio"]))
        if metrics["width"] is not None:
            widths.append(float(metrics["width"]))
        if metrics["height"] is not None:
            heights.append(float(metrics["height"]))
        if track and offset is not None and metrics["center"] is not None and metrics["xyxy"] is not None:
            per_track[track].append((offset, metrics["center"], metrics["xyxy"]))

    jumps: list[float] = []
    ious: list[float] = []
    for items in per_track.values():
        items.sort(key=lambda item: item[0])
        for prev, cur in zip(items, items[1:]):
            dt_s = max((cur[0] - prev[0]) / 1000.0, 0.001)
            dx = cur[1][0] - prev[1][0]
            dy = cur[1][1] - prev[1][1]
            jumps.append(math.hypot(dx, dy) / dt_s)
            ious.append(_iou(prev[2], cur[2]))

    area_buckets = [
        ("0-5%", 0.0, 0.05),
        ("5-10%", 0.05, 0.10),
        ("10-25%", 0.10, 0.25),
        ("25-50%", 0.25, 0.50),
        ("50%+", 0.50, float("inf")),
    ]
    width_buckets = [
        ("0-160", 0, 160),
        ("160-320", 160, 320),
        ("320-640", 320, 640),
        ("640-960", 640, 960),
        ("960+", 960, float("inf")),
    ]
    height_buckets = [
        ("0-240", 0, 240),
        ("240-480", 240, 480),
        ("480-720", 480, 720),
        ("720-1080", 720, 1080),
        ("1080+", 1080, float("inf")),
    ]
    diagnosis = classify_person_diagnosis(
        out_of_frame_count=out_of_frame_count,
        timestamp_estimated_count=timestamp_estimated_count,
        person_count=len(persons),
        max_center_jump_px_per_s=max(jumps) if jumps else 0.0,
        median_iou=_median(ious),
        area_ratios=area_ratios,
    )
    return {
        "total_annotation_count": len(records),
        "person_annotation_count": len(persons),
        "person_context_count": len(person_context),
        "behavior_event_count": len(behavior),
        "person_bbox_source_distribution": dict(bbox_sources),
        "person_bbox_format_distribution": dict(bbox_formats),
        "person_time_basis_distribution": dict(time_basis),
        "person_time_alignment_status_distribution": dict(alignment),
        "person_time_offset_ms_min": min(time_offsets) if time_offsets else None,
        "person_time_offset_ms_max": max(time_offsets) if time_offsets else None,
        "person_by_second": dict(sorted(by_second.items(), key=lambda item: int(item[0]))),
        "person_unique_track_count": len(tracks),
        "person_track_count": len(tracks),
        "person_bbox_out_of_frame_count": out_of_frame_count,
        "person_bbox_missing_format_count": missing_format_count,
        "person_bbox_missing_source_count": missing_source_count,
        "person_bbox_area_ratio_distribution": _distribution(area_ratios, area_buckets),
        "person_bbox_area_ratio_min": min(area_ratios) if area_ratios else None,
        "person_bbox_area_ratio_max": max(area_ratios) if area_ratios else None,
        "person_bbox_area_ratio_avg": sum(area_ratios) / len(area_ratios) if area_ratios else None,
        "person_bbox_width_distribution": _distribution(widths, width_buckets),
        "person_bbox_height_distribution": _distribution(heights, height_buckets),
        "person_bbox_width_min": min(widths) if widths else None,
        "person_bbox_width_max": max(widths) if widths else None,
        "person_bbox_height_min": min(heights) if heights else None,
        "person_bbox_height_max": max(heights) if heights else None,
        "person_frame_pts_present_count": frame_pts_present,
        "person_frame_num_present_count": frame_num_present,
        "person_timestamp_estimated_count": timestamp_estimated_count,
        "person_created_at_fallback_count": created_at_fallback_count,
        "person_max_center_jump_px_per_s": max(jumps) if jumps else 0.0,
        "person_median_iou_continuity": _median(ious),
        "diagnosis": diagnosis,
        "person_time_anchor_status": (
            "PERSON_TIME_ANCHOR_NOT_FRAME_BASED"
            if timestamp_estimated_count > 0 or created_at_fallback_count > 0 or not time_basis
            else "PERSON_TIME_ANCHOR_FRAME_BASED"
        ),
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    denom = area_a + area_b - inter
    return inter / denom if denom > 0 else 0.0


def classify_person_diagnosis(
    *,
    out_of_frame_count: int,
    timestamp_estimated_count: int,
    person_count: int,
    max_center_jump_px_per_s: float,
    median_iou: float | None,
    area_ratios: list[float],
) -> str:
    if out_of_frame_count > 0:
        return "PERSON_SPATIAL_MISALIGNMENT"
    if max_center_jump_px_per_s > 2500 and (median_iou is not None and median_iou < 0.2):
        return "PERSON_TRACK_ASSOCIATION_JITTER"
    if area_ratios and max(area_ratios) > 0.25:
        return "MODEL_BBOX_LOOSE_BUT_VALID"
    return "MODEL_BBOX_LOOSE_BUT_VALID"


def _drawtext_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
    )


def draw_frame(
    ffmpeg: str,
    clip: str,
    t_s: float,
    xyxy,
    W: int,
    H: int,
    out_png: str,
    label: str | list[str],
    *,
    color: str = "red",
) -> bool:
    x1, y1, x2, y2 = xyxy
    bx, by = max(0.0, x1), max(0.0, y1)
    bw, bh = max(1.0, x2 - x1), max(1.0, y2 - y1)
    filters = [
        f"drawbox=x={bx:.0f}:y={by:.0f}:w={bw:.0f}:h={bh:.0f}:color={color}@1.0:t=4"
    ]
    lines = label if isinstance(label, list) else [label]
    for idx, text in enumerate(lines[:8]):
        filters.append(
            "drawtext="
            f"text='{_drawtext_escape(str(text)[:120])}':"
            f"x=10:y={10 + idx * 30}:fontsize=24:fontcolor=yellow:"
            "box=1:boxcolor=black@0.6"
        )
    vf = ",".join(filters)
    cmd = [
        ffmpeg, "-y", "-i", clip, "-ss", f"{max(0.0, t_s):.3f}",
        "-frames:v", "1", "-vf", vf, out_png,
    ]
    proc = _run(cmd)
    return proc.returncode == 0 and Path(out_png).is_file()


def person_debug_label(
    *,
    bundle_id: str,
    item: dict[str, Any],
    area_ratio: float | None,
) -> list[str]:
    bbox = _as_dict(item.get("bbox"))
    basis = str(item.get("time_basis") or "")
    if not basis and str(item.get("time_alignment_status") or "") == "estimated":
        basis = "timestamp_estimated"
    elif not basis:
        basis = "unspecified"
    return [
        f"bundle_id={bundle_id}",
        f"object_type={item.get('object_type')}",
        f"annotation_role={annotation_role(item)}",
        f"track_id={item.get('track_id') or ''}",
        f"time_offset_ms={item.get('time_offset_ms')}",
        f"bbox.source={bbox.get('source') or ''}",
        f"bbox.area_ratio={area_ratio:.4f}" if area_ratio is not None else "bbox.area_ratio=",
        f"time_basis={basis}",
    ]


def select_person_debug_samples(
    persons: list[dict[str, Any]],
    max_count: int,
) -> list[dict[str, Any]]:
    behavior = [item for item in persons if is_behavior_event(item)]
    context = [item for item in persons if is_person_context(item)]
    samples: list[dict[str, Any]] = []
    if behavior:
        samples.append(behavior[0])
    seen_tracks = {str(samples[0].get("track_id") or "")} if samples else set()
    for item in context:
        track = str(item.get("track_id") or "")
        if track and track in seen_tracks:
            continue
        samples.append(item)
        if track:
            seen_tracks.add(track)
        if len(samples) >= max_count:
            return samples
    for item in context:
        if item in samples:
            continue
        samples.append(item)
        if len(samples) >= max_count:
            return samples
    return samples[:max_count]


def create_person_triptych_frames(
    *,
    ffmpeg: str,
    clip: str,
    bundle_id: str,
    persons: list[dict[str, Any]],
    width: int,
    height: int,
    out_dir: Path,
    max_count: int,
) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    for idx, item in enumerate(select_person_debug_samples(persons, max_count)):
        offset_ms = _to_float(item.get("time_offset_ms"), None)
        xyxy = bbox_to_xyxy(_as_dict(item.get("bbox")))
        if offset_ms is None or xyxy is None:
            continue
        metrics = bbox_metrics(item, width, height)
        role = annotation_role(item)
        color = "orange" if is_behavior_event(item) else "lime"
        label = person_debug_label(
            bundle_id=bundle_id,
            item=item,
            area_ratio=metrics.get("area_ratio"),
        )
        for dt, suffix in ((-DELTA_S, "a_before"), (0.0, "b_at"), (DELTA_S, "c_after")):
            t_ms = max(0, int(round(offset_ms + dt * 1000.0)))
            out_png = out_dir / f"person_{idx:02d}_t{t_ms:04d}ms__{suffix}.png"
            ok = draw_frame(
                ffmpeg,
                clip,
                t_ms / 1000.0,
                xyxy,
                width,
                height,
                str(out_png),
                [f"{role} {suffix}", *label],
                color=color,
            )
            if ok:
                created.append(str(out_png))
    return created


def build_report_markdown(summary: dict[str, Any]) -> str:
    diagnosis = summary.get("diagnosis", "")
    notes = {
        "MODEL_BBOX_LOOSE_BUT_VALID": (
            "The boxes stay within the raw frame and are best classified as loose "
            "person detector boxes rather than an overlay coordinate failure."
        ),
        "PERSON_TEMPORAL_MISALIGNMENT": (
            "Person offsets are not frame based or are largely estimated; inspect "
            "timestamp/frame_pts alignment before changing spatial logic."
        ),
        "PERSON_SPATIAL_MISALIGNMENT": (
            "At least one person bbox is out of the raw frame or the coordinate "
            "reference appears inconsistent with the clip resolution."
        ),
        "PERSON_TRACK_ASSOCIATION_JITTER": (
            "Same-track center jumps and IoU continuity indicate tracker or "
            "association jitter."
        ),
        "PERSON_SPARSE_OBSERVATION_HOLD_JITTER": (
            "Single frames align, but sparse observations plus hold can look "
            "choppy in video playback."
        ),
    }
    lines = [
        "# C1I.2j Person/Pose Overlay Spatial QA",
        "",
        f"- result_marker: `{summary.get('result_marker', '')}`",
        f"- diagnosis: `{diagnosis}`",
        f"- bundle_id: `{summary.get('bundle_id', '')}`",
        f"- raw_clip: `{summary.get('raw_clip_width')}x{summary.get('raw_clip_height')}`, "
        f"{summary.get('raw_clip_duration_ms')} ms",
        f"- person_context_count: `{summary.get('person_context_count')}`",
        f"- behavior_event_count: `{summary.get('behavior_event_count')}`",
        f"- person_bbox_out_of_frame_count: `{summary.get('person_bbox_out_of_frame_count')}`",
        f"- debug_frame_dir: `{summary.get('debug_frame_dir')}`",
        "",
        "## Distributions",
        "",
        "```json",
        json.dumps(
            {
                "person_bbox_source_distribution": summary.get("person_bbox_source_distribution", {}),
                "person_time_basis_distribution": summary.get("person_time_basis_distribution", {}),
                "person_time_alignment_status_distribution": summary.get(
                    "person_time_alignment_status_distribution", {}
                ),
                "person_bbox_area_ratio_distribution": summary.get(
                    "person_bbox_area_ratio_distribution", {}
                ),
                "person_bbox_width_distribution": summary.get(
                    "person_bbox_width_distribution", {}
                ),
                "person_bbox_height_distribution": summary.get(
                    "person_bbox_height_distribution", {}
                ),
            },
            indent=2,
            sort_keys=True,
        ),
        "```",
        "",
        "## Interpretation",
        "",
        notes.get(str(diagnosis), "No automatic interpretation is available."),
    ]
    if summary.get("person_time_anchor_status") == "PERSON_TIME_ANCHOR_NOT_FRAME_BASED":
        lines.extend(
            [
                "",
                "## Time Anchor Note",
                "",
                "Person annotations are not currently marked with a frame-domain "
                "`time_basis`; this report is diagnostic only and does not change "
                "person bbox generation.",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle_dir", nargs="?", help="evidence/<event_id> 目录")
    ap.add_argument("--clip")
    ap.add_argument("--ann")
    ap.add_argument("--out", default=None, help="输出帧目录")
    ap.add_argument("--object", choices=OBJECT_CHOICES, default="face")
    ap.add_argument("--json-out", default=None, help="写出机器可读诊断 JSON")
    ap.add_argument("--report-out", default=None, help="写出 Markdown 诊断报告")
    ap.add_argument("--max", type=int, default=6, help="最多诊断多少个观测")
    args = ap.parse_args()

    if args.bundle_dir:
        base = Path(args.bundle_dir)
        clip = args.clip or str(next(iter(base.glob("raw_clip.*")), ""))
        ann = args.ann or str(base / "annotations.jsonl")
        summary = base / "summary.json"
        if args.out:
            out_dir = Path(args.out)
        elif args.object == "person":
            out_dir = PERSON_DEBUG_ROOT / base.name
        else:
            out_dir = Path(base / "overlay_align_debug")
    else:
        clip = args.clip or ""
        ann = args.ann or ""
        summary = Path("summary.json")
        out_dir = Path(args.out or "overlay_align_debug")

    if not clip or not Path(clip).is_file():
        print(f"找不到 raw_clip: {clip}"); return 2
    if not ann or not Path(ann).is_file():
        print(f"找不到 annotations.jsonl: {ann}"); return 2

    ffmpeg = _resolve("ffmpeg"); ffprobe = _resolve("ffprobe")
    if not ffmpeg or not ffprobe:
        print("需要 ffmpeg + ffprobe"); return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    W, H, dur = probe_resolution(ffprobe, clip)
    width = W or 1920
    height = H or 1080
    print(f"raw_clip       : {clip}")
    print(f"clip 分辨率    : {W}x{H}   时长: {dur}s")

    summary_data = load_json(summary)
    event_offset_ms = None
    if summary.is_file():
        try:
            ta = summary_data.get("time_anchor") or {}
            event_offset_ms = ta.get("event_offset_in_clip_ms") or summary_data.get("event_offset_in_clip_ms")
            print(f"event_offset   : {event_offset_ms} ms   "
                  f"face_overlay_status: {summary_data.get('face_overlay_time_alignment_status')}")
        except Exception:
            pass

    records = load_jsonl(Path(ann))
    items = list(iter_object_annotations(Path(ann), args.object))
    faces = [item for item in items if item.get("object_type") == "face"]
    persons = [item for item in items if item.get("object_type") == "person"]
    known = [
        item
        for item in faces
        if _identity_status(item) == "matched" or _display_name(item)
    ]
    person_audit = audit_person_annotations(records, width=width, height=height)

    print(f"\nannotation 总数: {len(records)}")
    print(f"object filter  : {args.object}")
    print(f"face 观测总数  : {len(faces)}   其中 known: {len(known)}")
    print(f"person 观测总数: {len(persons)}")
    if persons:
        print(f"person_context : {person_audit['person_context_count']}")
        print(f"behavior_event : {person_audit['behavior_event_count']}")
        print(f"person bbox source: {person_audit['person_bbox_source_distribution']}")
        print(f"person bbox format: {person_audit['person_bbox_format_distribution']}")
        print(f"person time_basis : {person_audit['person_time_basis_distribution']}")
        print(f"person alignment  : {person_audit['person_time_alignment_status_distribution']}")
        print(f"person offset min/max: {person_audit['person_time_offset_ms_min']} / "
              f"{person_audit['person_time_offset_ms_max']}")
        print(f"person by second  : {person_audit['person_by_second']}")
        print(f"person track count: {person_audit['person_track_count']}")
        print(f"person out-of-frame: {person_audit['person_bbox_out_of_frame_count']}")
        print(f"person area ratio : {person_audit['person_bbox_area_ratio_distribution']}")
        print(f"person width dist : {person_audit['person_bbox_width_distribution']}")
        print(f"person height dist: {person_audit['person_bbox_height_distribution']}")
    if not items:
        print("annotations.jsonl 里没有匹配的 annotation。")
        return 0

    spatial_oob = 0
    # 先取 known（命中目标最关键），再用 unknown 补齐到 max，覆盖更全的空间采样。
    if args.object == "person":
        sample_items = select_person_debug_samples(persons, args.max)
    else:
        unknown_faces = [item for item in faces if item not in known]
        sample_items = (known + unknown_faces + persons)[: args.max]
    print(f"\n逐个诊断（最多 {args.max} 个）:")
    created_debug_frames: list[str] = []
    for i, item in enumerate(sample_items):
        t_ms = _to_float(item.get("time_offset_ms"), None)
        if t_ms is None:
            continue
        bbox = item.get("bbox")
        role = annotation_role(item)
        status = _identity_status(item)
        name = _display_name(item)
        track = str(item.get("track_id") or "")
        basis = str(item.get("time_basis") or "")
        if not basis and str(item.get("time_alignment_status") or "") == "estimated":
            basis = "timestamp_estimated"
        elif not basis:
            basis = "unspecified"
        object_type = str(item.get("object_type") or "")
        xyxy = bbox_to_xyxy(bbox or {})
        tag = f"{object_type}_{i:02d}_{role}_{track or status}_t{int(t_ms)}ms"
        print(f"\n  [{i}] object={object_type} t={t_ms:.0f}ms basis={basis} status={status} "
              f"name={name or '-'} track={track} role={role}")
        if xyxy is None:
            print(f"      bbox 解析失败: {bbox}"); continue
        x1, y1, x2, y2 = xyxy
        print(f"      bbox xyxy = ({x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f})  "
              f"宽高=({x2-x1:.0f},{y2-y1:.0f})")
        # 数值空间检查
        if W and H:
            oob = x1 < -2 or y1 < -2 or x2 > W + 2 or y2 > H + 2
            area_ratio = max(0.0, (x2 - x1)) * max(0.0, (y2 - y1)) / (W * H)
            print(f"      在 {W}x{H} 内: {'否(出界!)' if oob else '是'}   占帧面积: {area_ratio*100:.1f}%")
            if oob:
                spatial_oob += 1
                print("      → 该框坐标超出 clip 分辨率，几乎可断定【空间错位】(坐标系/分辨率不一致)")
        # 烧三联图
        for dt, sfx in ((-DELTA_S, "a_before"), (0.0, "b_at"), (DELTA_S, "c_after")):
            if object_type == "person":
                t_out_ms = max(0, int(round(t_ms + dt * 1000.0)))
                png = str(out_dir / f"person_{i:02d}_t{t_out_ms:04d}ms__{sfx}.png")
                label = person_debug_label(
                    bundle_id=Path(args.bundle_dir).name if args.bundle_dir else "",
                    item=item,
                    area_ratio=bbox_metrics(item, width, height).get("area_ratio"),
                )
                color = "orange" if is_behavior_event(item) else "lime"
                ok = draw_frame(
                    ffmpeg,
                    clip,
                    t_out_ms / 1000.0,
                    xyxy,
                    width,
                    height,
                    png,
                    [f"{role} {sfx}", *label],
                    color=color,
                )
            else:
                png = str(out_dir / f"{tag}__{sfx}.png")
                ok = draw_frame(
                    ffmpeg,
                    clip,
                    t_ms / 1000.0 + dt,
                    xyxy,
                    width,
                    height,
                    png,
                    f"{sfx} t={t_ms/1000+dt:.2f}s {name or status}",
                )
            if ok:
                created_debug_frames.append(png)
            print(f"      {'✓' if ok else '✗'} {png}")
        if event_offset_ms is not None:
            drift = t_ms - float(event_offset_ms)
            if abs(drift) > 1500:
                print(f"      ⚠ 该 annotation offset 距 event offset {drift:+.0f}ms（>1.5s）。"
                      "注意：estimated 锚点下 event_offset 恒等于 pre_seconds（占位值），"
                      "此差距未必代表时间错位，需结合 _b_at.png 判读")

    if args.object == "person" and not created_debug_frames:
        created_debug_frames = create_person_triptych_frames(
            ffmpeg=ffmpeg,
            clip=clip,
            bundle_id=Path(args.bundle_dir).name if args.bundle_dir else "",
            persons=persons,
            width=width,
            height=height,
            out_dir=out_dir,
            max_count=args.max,
        )

    result_summary = {
        "bundle_id": Path(args.bundle_dir).name if args.bundle_dir else "",
        "object_filter": args.object,
        "raw_clip": clip,
        "raw_clip_width": W,
        "raw_clip_height": H,
        "raw_clip_duration_ms": int((dur or 0.0) * 1000),
        "debug_frame_dir": str(out_dir),
        "created_debug_frame_count": len(created_debug_frames),
        "created_debug_frames": created_debug_frames,
        "face_annotation_count": len(faces),
        **person_audit,
    }
    if args.object == "face":
        result_summary["diagnosis"] = "FACE_ONLY_DIAGNOSTIC"
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(result_summary, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if args.report_out:
        Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_out).write_text(
            build_report_markdown(result_summary),
            encoding="utf-8",
        )

    print("\n================ 结论指引 ================")
    if spatial_oob:
        print(f"❌ {spatial_oob} 个框坐标出界 → 【空间错位】基本确认：")
        print("   bbox 像素坐标的参考分辨率 ≠ raw_clip 分辨率，或坐标被缩放。")
        print("   重点查：raw_clip 实际分辨率 vs 检测器输出 bbox 的参考分辨率(源帧 1920x1080)，")
        print("   以及 clip_sanitizer/sink 是否改了分辨率。")
    else:
        print("✅ 框坐标都在画面内。打开 overlay_align_debug 里的三联图人工判读：")
        print("   - 脸只在 _a_before / _c_after 落进框、_b_at 是空的 → 【时间漂移】")
        print("   - 三张框都系统性偏离脸 → 【空间错位】(坐标系平移/缩放)")
        print("   - _b_at 脸就在框里 → 该观测对齐 OK，问题在观测太稀疏/其它对象")
    if persons:
        print(f"person diagnosis: {person_audit['diagnosis']}")
        print(f"debug_frame_dir : {out_dir}")
    print("==========================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
