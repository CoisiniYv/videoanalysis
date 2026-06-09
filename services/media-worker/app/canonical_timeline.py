"""Canonical evidence clip timeline helpers for C1J.11h."""

from __future__ import annotations

import bisect
import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from statistics import median
from typing import Any


NANOS_PER_SECOND = 1_000_000_000
PTS_TIME_BASE_SECONDS = 1.0 / NANOS_PER_SECOND
DEFAULT_PTS_NEAREST_TOLERANCE_NS = 50_000_000
MAX_PTS_NEAREST_TOLERANCE_NS = 120_000_000


def canonical_window_from_event(
    *,
    event_pts: int | None,
    pre_seconds: float,
    post_seconds: float,
) -> dict[str, Any]:
    event = _int_or_none(event_pts)
    pre = _positive_float(pre_seconds, 5.0)
    post = _positive_float(post_seconds, 5.0)
    if event is None:
        return {
            "canonical_window_available": False,
            "validation_errors": ["missing_event_pts"],
            "pre_seconds": pre,
            "post_seconds": post,
            "target_duration_seconds": round(pre + post, 6),
            "expected_event_t_s": pre,
            "canonical_start_pts": None,
            "canonical_end_pts": None,
        }
    return {
        "canonical_window_available": True,
        "validation_errors": [],
        "pre_seconds": pre,
        "post_seconds": post,
        "target_duration_seconds": round(pre + post, 6),
        "expected_event_t_s": pre,
        "canonical_start_pts": max(0, int(round(event - pre * NANOS_PER_SECOND))),
        "canonical_end_pts": int(round(event + post * NANOS_PER_SECOND)),
        "event_pts": event,
    }


def canonicalize_sink_metadata(
    rows: list[dict[str, Any]],
    *,
    event_pts: int,
    pre_seconds: float,
    post_seconds: float,
    rebase_pts: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter sink metadata rows to the canonical event-centered window."""

    window = canonical_window_from_event(
        event_pts=event_pts,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
    )
    start = _int_or_none(window.get("canonical_start_pts"))
    end = _int_or_none(window.get("canonical_end_pts"))
    if start is None or end is None:
        return [], {**window, "frames_selected": 0}

    frames = [
        copy.deepcopy(row)
        for row in rows
        if _row_pts(row) is not None and start <= int(_row_pts(row)) <= end
    ]
    frames.sort(key=lambda row: (_int_or_none(row.get("frame_num")) is None, _int_or_none(row.get("frame_num")) or 0, _row_pts(row) or 0))
    source_first_pts = _row_pts(frames[0]) if frames else None
    source_last_pts = _row_pts(frames[-1]) if frames else None
    for index, row in enumerate(frames):
        row["frame_num"] = index
        pts = _row_pts(row)
        if pts is not None and source_first_pts is not None:
            row["clip_t_ms"] = int(round((int(pts) - int(source_first_pts)) / 1_000_000.0))
        if rebase_pts and source_first_pts is not None:
            if pts is not None:
                rebased = int(pts) - int(source_first_pts)
                if "pts" in row:
                    row["pts"] = rebased
                if "frame_pts" in row:
                    row["frame_pts"] = rebased
                if "dts" in row and _int_or_none(row.get("dts")) is not None:
                    row["dts"] = int(row["dts"]) - int(source_first_pts)
    summary = {
        **window,
        "frames_selected": len(frames),
        "source_first_pts": source_first_pts,
        "source_last_pts": source_last_pts,
        "source_duration_seconds": (
            round((int(source_last_pts) - int(source_first_pts)) * PTS_TIME_BASE_SECONDS, 9)
            if source_first_pts is not None and source_last_pts is not None
            else None
        ),
        "event_projected_t_s": (
            round((int(event_pts) - int(source_first_pts)) * PTS_TIME_BASE_SECONDS, 9)
            if source_first_pts is not None
            else None
        ),
        "event_pts_inside_clip": bool(
            source_first_pts is not None
            and source_last_pts is not None
            and int(source_first_pts) <= int(event_pts) <= int(source_last_pts)
        ),
        "rebase_pts": bool(rebase_pts),
    }
    duration = summary.get("source_duration_seconds")
    projected = summary.get("event_projected_t_s")
    summary["event_position_ratio"] = (
        round(float(projected) / float(duration), 9)
        if projected is not None and duration not in (None, 0)
        else None
    )
    return frames, summary


def align_annotations_to_clip_metadata(
    annotations: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Map annotation t_ms/frame index onto final clip metadata by uuid/PTS.

    The source PTS is preserved so audits can still compare an annotation row
    with the original event `frame_pts`; only clip-relative fields are rebased
    to the final evidence clip.
    """

    rows = [copy.deepcopy(row) for row in annotations if isinstance(row, dict)]
    frames = [row for row in metadata_rows if isinstance(row, dict) and _row_pts(row) is not None]
    frames.sort(key=lambda row: (_int_or_none(row.get("frame_num")) is None, _int_or_none(row.get("frame_num")) or 0, _row_pts(row) or 0))
    if not rows or not frames:
        for row in rows:
            row["clip_timeline_match"] = "missing"
            _mark_annotation_not_displayable(row)
        return rows, {
            "enabled": bool(frames),
            "status": "missing_metadata" if not frames else "no_annotations",
            "annotations_aligned": 0,
            "annotations_unmatched": len(rows),
            "annotations_out_of_window": 0,
            "annotations_displayable": 0,
            "annotations_matched_frame_uuid": 0,
            "annotations_matched_frame_pts_exact": 0,
            "annotations_matched_frame_pts_nearest": 0,
            "annotations_missing": len(rows),
            "clip_timeline_match_distribution": {
                "metadata_frame_uuid": 0,
                "metadata_frame_pts_exact": 0,
                "metadata_frame_pts_nearest": 0,
                "out_of_window": 0,
                "missing": len(rows),
            },
            "metadata_frame_count": len(frames),
            "nearest_pts_tolerance_ns": None,
        }

    first_pts = _row_pts(frames[0])
    by_uuid: dict[str, dict[str, Any]] = {}
    by_pts: dict[int, dict[str, Any]] = {}
    pts_frames: list[tuple[int, int, dict[str, Any]]] = []
    for frame in frames:
        frame_uuid = _row_uuid(frame)
        if isinstance(frame_uuid, str) and frame_uuid and frame_uuid not in by_uuid:
            by_uuid[frame_uuid] = frame
        pts = _row_pts(frame)
        if pts is not None and pts not in by_pts:
            by_pts[int(pts)] = frame
        if pts is not None:
            frame_num = _int_or_none(frame.get("frame_num"))
            pts_frames.append((int(pts), int(frame_num) if frame_num is not None else len(pts_frames), frame))
    pts_frames.sort(key=lambda item: (item[0], item[1]))
    pts_values = [item[0] for item in pts_frames]
    nearest_tolerance_ns = infer_metadata_pts_nearest_tolerance_ns(frames)

    aligned = 0
    unmatched = 0
    out_of_window = 0
    matched_uuid = 0
    matched_pts_exact = 0
    matched_pts_nearest = 0
    match_distribution = {
        "metadata_frame_uuid": 0,
        "metadata_frame_pts_exact": 0,
        "metadata_frame_pts_nearest": 0,
        "out_of_window": 0,
        "missing": 0,
    }
    for row in rows:
        frame = None
        match_kind = "missing"
        delta_ns: int | None = None
        frame_uuid = _row_uuid(row)
        if isinstance(frame_uuid, str) and frame_uuid and by_uuid:
            frame = by_uuid.get(frame_uuid)
            if frame is not None:
                match_kind = "metadata_frame_uuid"
                delta_ns = 0
        pts = _int_or_none(row.get("frame_pts"))
        if frame is None and pts is not None:
            frame = by_pts.get(pts)
            if frame is not None:
                match_kind = "metadata_frame_pts_exact"
                delta_ns = 0
        if frame is None and pts is not None:
            nearest = _nearest_metadata_frame(pts, pts_values, pts_frames)
            if nearest is not None:
                nearest_delta_ns, nearest_frame = nearest
                delta_ns = nearest_delta_ns
                if nearest_delta_ns <= nearest_tolerance_ns:
                    frame = nearest_frame
                    match_kind = "metadata_frame_pts_nearest"
        if frame is None:
            if (
                pts is not None
                and first_pts is not None
                and (_row_pts(frames[-1]) is not None)
                and (int(pts) < int(first_pts) or int(pts) > int(_row_pts(frames[-1])))
            ):
                match_kind = "out_of_window"
                out_of_window += 1
            unmatched += 1
            match_distribution[match_kind] = match_distribution.get(match_kind, 0) + 1
            row["clip_timeline_match"] = match_kind
            _mark_annotation_not_displayable(row)
            if delta_ns is not None:
                row["clip_timeline_delta_ns"] = int(delta_ns)
            continue
        frame_pts = _row_pts(frame)
        frame_num = _int_or_none(frame.get("frame_num"))
        if frame_num is not None:
            row["clip_frame_index"] = frame_num
        if frame_pts is not None and first_pts is not None:
            row["t_ms"] = int(round((int(frame_pts) - int(first_pts)) / 1_000_000.0))
            row["t_s"] = round((int(frame_pts) - int(first_pts)) * PTS_TIME_BASE_SECONDS, 9)
            row["matched_metadata_pts"] = int(frame_pts)
        row["clip_timeline_match"] = match_kind
        row["clip_timeline_delta_ns"] = int(delta_ns or 0)
        row["displayable"] = True
        aligned += 1
        match_distribution[match_kind] = match_distribution.get(match_kind, 0) + 1
        if match_kind == "metadata_frame_uuid":
            matched_uuid += 1
        elif match_kind == "metadata_frame_pts_exact":
            matched_pts_exact += 1
        elif match_kind == "metadata_frame_pts_nearest":
            matched_pts_nearest += 1

    return rows, {
        "enabled": True,
        "status": "aligned" if aligned > 0 and unmatched == 0 else ("partial" if aligned > 0 else "no_matches"),
        "annotations_aligned": aligned,
        "annotations_unmatched": unmatched,
        "annotations_out_of_window": out_of_window,
        "annotations_displayable": aligned,
        "annotations_matched_frame_uuid": matched_uuid,
        "annotations_matched_frame_pts_exact": matched_pts_exact,
        "annotations_matched_frame_pts_nearest": matched_pts_nearest,
        "annotations_missing": unmatched,
        "clip_timeline_match_distribution": match_distribution,
        "metadata_frame_count": len(frames),
        "first_pts": first_pts,
        "last_pts": _row_pts(frames[-1]),
        "nearest_pts_tolerance_ns": nearest_tolerance_ns,
    }


def infer_metadata_pts_nearest_tolerance_ns(metadata_rows: list[dict[str, Any]]) -> int:
    """Infer a bounded nearest-PTS tolerance from final metadata cadence."""

    pts_values = sorted({
        int(pts)
        for row in metadata_rows
        if isinstance(row, dict)
        for pts in [_row_pts(row)]
        if pts is not None
    })
    deltas = [
        pts_values[index] - pts_values[index - 1]
        for index in range(1, len(pts_values))
        if pts_values[index] > pts_values[index - 1]
    ]
    if deltas:
        inferred = int(round(float(median(deltas)) / 2.0))
        tolerance = max(inferred, DEFAULT_PTS_NEAREST_TOLERANCE_NS)
    else:
        tolerance = DEFAULT_PTS_NEAREST_TOLERANCE_NS
    return min(tolerance, MAX_PTS_NEAREST_TOLERANCE_NS)


def _row_uuid(row: dict[str, Any]) -> str | None:
    for key in ("frame_uuid", "uuid"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _nearest_metadata_frame(
    frame_pts: int,
    pts_values: list[int],
    pts_frames: list[tuple[int, int, dict[str, Any]]],
) -> tuple[int, dict[str, Any]] | None:
    if not pts_values:
        return None
    position = bisect.bisect_left(pts_values, frame_pts)
    candidates = []
    if position < len(pts_values):
        candidates.append(position)
    if position > 0:
        candidates.append(position - 1)
    best: tuple[int, int, dict[str, Any]] | None = None
    for index in candidates:
        candidate_pts, order, frame = pts_frames[index]
        delta = abs(int(candidate_pts) - int(frame_pts))
        current = (delta, order, frame)
        if best is None or current[:2] < best[:2]:
            best = current
    if best is None:
        return None
    return best[0], best[2]


def _mark_annotation_not_displayable(row: dict[str, Any]) -> None:
    row["displayable"] = False
    for key in ("t_ms", "t_s", "clip_frame_index", "matched_metadata_pts"):
        row.pop(key, None)


def trim_video_to_canonical_window(
    *,
    source_video: str,
    output_video: str,
    source_first_pts: int,
    event_pts: int,
    pre_seconds: float,
    post_seconds: float,
) -> dict[str, Any]:
    """Create a canonical video clip using ffmpeg stream copy with reencode fallback."""

    src = Path(source_video)
    dst = Path(output_video)
    dst.parent.mkdir(parents=True, exist_ok=True)
    target_duration = _positive_float(pre_seconds, 5.0) + _positive_float(post_seconds, 5.0)
    start_s = max(0.0, (int(event_pts) - int(source_first_pts)) * PTS_TIME_BASE_SECONDS - _positive_float(pre_seconds, 5.0))
    ffmpeg = _ffmpeg_exe()
    result = {
        "canonical_trim_enabled": True,
        "source_video": str(src),
        "output_video": str(dst),
        "trim_start_seconds": round(start_s, 9),
        "trim_duration_seconds": round(target_duration, 9),
        "method": "",
        "fallback_used": False,
        "error": "",
        "bounded": True,
    }
    if not src.is_file():
        result.update(method="none", error="source_video_missing")
        return result
    if ffmpeg is None:
        shutil.copy2(src, dst)
        result.update(method="copy_no_ffmpeg", fallback_used=True, error="ffmpeg_unavailable")
        return result

    reencode_cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(src),
        "-ss",
        f"{start_s:.9f}",
        "-t",
        f"{target_duration:.9f}",
        "-map",
        "0:v:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(dst),
    ]
    ok, err = _run_ffmpeg(reencode_cmd)
    if ok and dst.is_file() and dst.stat().st_size > 0:
        result["method"] = "trim_reencode"
        return result

    copy_cmd = [
        ffmpeg,
        "-y",
        "-ss",
        f"{start_s:.9f}",
        "-i",
        str(src),
        "-t",
        f"{target_duration:.9f}",
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    ok, err2 = _run_ffmpeg(copy_cmd)
    if ok and dst.is_file() and dst.stat().st_size > 0:
        result.update(method="trim_copy_fallback", fallback_used=True, error=err)
        return result
    shutil.copy2(src, dst)
    result.update(method="copy_fallback", fallback_used=True, error=err2 or err)
    return result


def load_metadata_rows(path: str | Path) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.is_file():
        return []
    text = file_path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        for key in ("frames", "metadata", "rows"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [parsed]
    return []


def write_metadata_rows(path: str | Path, rows: list[dict[str, Any]]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )


def _row_pts(row: dict[str, Any]) -> int | None:
    return _int_or_none(row.get("pts")) if _int_or_none(row.get("pts")) is not None else _int_or_none(row.get("frame_pts"))


def _ffmpeg_exe() -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _run_ffmpeg(cmd: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except Exception as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[-500:]
    return True, ""


def _positive_float(value: Any, default: float) -> float:
    parsed = _float_or_none(value)
    if parsed is None or parsed <= 0:
        return float(default)
    return float(parsed)


def _float_or_none(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
