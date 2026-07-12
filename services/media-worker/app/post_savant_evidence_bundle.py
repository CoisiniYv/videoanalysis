"""Package post-Savant video-file-sink output as a production evidence bundle."""

from __future__ import annotations

import json
import os
import re
import resource
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.subprocess_control import run_managed_subprocess
from app.post_savant_metadata_annotation_builder import (
    ANNOTATION_SOURCE,
    PRODUCTION_TIMELINE_DOMAIN,
    SIDECAR_ANNOTATIONS_FILE,
    SIDECAR_SUMMARY_FILE,
    build_post_savant_annotation_sidecar,
    load_native_metadata,
)
from app.post_savant_video_integrity import inspect_video_integrity


RAW_CLIP_FILE = "raw_clip.mov"
SINK_METADATA_FILE = "sink_metadata.json"
SUMMARY_FILE = "summary.json"
SCHEMA_VERSION = os.getenv("EVIDENCE_SCHEMA_VERSION", "2.0-midterm")
EVIDENCE_TOPOLOGY = "post_savant_replay"
TRIM_LIMITATIONS = (
    "metadata_frame_count_exceeds_decoded_video_frames",
    "sidecar_trimmed_to_playable_frame_count",
)
TIMELINE_COUNTS_MATCH = "frame_counts_match"
TIMELINE_SPARSE_SIDECAR = "metadata_time_aligned_sparse_sidecar"
TIMELINE_NEEDS_MAPPING = "needs_visual_or_time_mapping_verification"


@dataclass(frozen=True)
class EvidenceBundleResult:
    input_dir: Path
    output_dir: Path
    raw_clip_path: Path
    sink_metadata_path: Path
    production_sidecar_path: Path
    sidecar_summary_path: Path
    summary_path: Path
    summary: dict[str, Any]
    report: dict[str, Any]


def build_post_savant_evidence_bundle(
    *,
    input_dir: Path,
    output_dir: Path,
    copy_video: bool = True,
    trim_sidecar_to_video: bool = True,
    overwrite: bool = False,
    max_fps: str | None = None,
    min_fps: str | None = None,
    fps_gating_applied: bool | None = None,
    source_input_fps_estimate: float | None = None,
    event_metadata: dict[str, Any] | None = None,
    requested_start_pts: int | None = None,
    requested_end_pts: int | None = None,
    event_frame_pts: int | None = None,
    event_frame_uuid: str | None = None,
    start_window_frame_uuid: str | None = None,
    post_window_frame_uuid: str | None = None,
    time_domain_crop_applied: bool = False,
    crop_video_to_time_window: bool = False,
    evidence_capture_mode: str | None = None,
    event_style_replay_job_passed: bool | None = None,
    replay_event_flow_status: str | None = None,
    workaround_used: bool | None = None,
    workaround_reason: str | None = None,
    replay_timing_metadata: dict[str, Any] | None = None,
    video_integrity_required: bool = False,
    materialization_timeout_s: float | None = None,
    decoded_frame_count_reader: Callable[[Path], int] | None = None,
    decoded_video_duration_s: float | None = None,
) -> EvidenceBundleResult:
    """Create a standard evidence bundle from video-file-sink output."""

    input_dir = input_dir.resolve(strict=False)
    output_dir = output_dir.resolve(strict=False)
    video_path = _find_required(input_dir, ("video.mov", "raw_clip.mov"), "video")
    metadata_path = _find_required(input_dir, ("metadata.json", "sink_metadata.json"), "metadata")
    input_native_frames = [frame for frame in load_native_metadata(metadata_path) if _is_native_frame(frame)]
    native_frames, time_window = _select_time_domain_frames(
        input_native_frames,
        requested_start_pts=requested_start_pts,
        requested_end_pts=requested_end_pts,
        event_frame_pts=event_frame_pts,
        event_frame_uuid=event_frame_uuid,
        start_window_frame_uuid=start_window_frame_uuid,
        post_window_frame_uuid=post_window_frame_uuid,
        enabled=time_domain_crop_applied,
    )

    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        if output_dir.is_dir():
            shutil.rmtree(output_dir)
        else:
            output_dir.unlink()

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_clip_path = output_dir / RAW_CLIP_FILE
    sink_metadata_path = output_dir / SINK_METADATA_FILE
    video_crop = _copy_or_crop_video(
        source_video_path=video_path,
        output_video_path=raw_clip_path,
        source_frames=input_native_frames,
        time_window=time_window,
        copy_video=copy_video,
        crop_video_to_time_window=crop_video_to_time_window,
        materialization_timeout_s=materialization_timeout_s,
    )
    if time_domain_crop_applied:
        _write_jsonl(sink_metadata_path, native_frames)
    else:
        shutil.copy2(metadata_path, sink_metadata_path)

    decoded_frame_count = (
        decoded_frame_count_reader(raw_clip_path)
        if decoded_frame_count_reader is not None
        else read_decoded_video_frame_count(raw_clip_path)
    )
    if decoded_frame_count <= 0:
        raise ValueError(f"decoded video frame count must be > 0: {decoded_frame_count}")

    original_metadata_frame_count = len(native_frames)
    sidecar_max_frames = (
        min(original_metadata_frame_count, decoded_frame_count)
        if trim_sidecar_to_video
        else original_metadata_frame_count
    )
    trim_occurred = original_metadata_frame_count > sidecar_max_frames
    timeline_reconciliation_status = _timeline_reconciliation_status(
        original_metadata_frame_count=original_metadata_frame_count,
        decoded_video_frame_count=decoded_frame_count,
    )
    extra_limitations = list(TRIM_LIMITATIONS) if trim_occurred else []
    if (
        original_metadata_frame_count > 0
        and decoded_frame_count > 0
        and original_metadata_frame_count > decoded_frame_count
    ):
        for limitation in TRIM_LIMITATIONS:
            if limitation not in extra_limitations:
                extra_limitations.append(limitation)

    production_sidecar_path = output_dir / SIDECAR_ANNOTATIONS_FILE
    sidecar_summary_path = output_dir / SIDECAR_SUMMARY_FILE
    sidecar_result = build_post_savant_annotation_sidecar(
        metadata_path=sink_metadata_path,
        output_jsonl_path=production_sidecar_path,
        summary_path=sidecar_summary_path,
        max_frames=sidecar_max_frames,
        extra_limitations=extra_limitations,
    )
    sidecar_summary = sidecar_result.summary
    video_integrity = (
        inspect_video_integrity(
            raw_clip_path,
            decode_log_path=output_dir / "video_integrity_decode_errors.log",
            requested_duration_s=_requested_duration_s(time_window),
            decoded_video_frame_count=decoded_frame_count,
            sidecar_frame_count=len(sidecar_result.rows),
            trim_occurred=trim_occurred,
            time_domain_crop_applied=time_domain_crop_applied,
        )
        if video_integrity_required
        else None
    )
    summary = _bundle_summary(
        sidecar_summary=sidecar_summary,
        source_metadata_frame_count=len(input_native_frames),
        original_metadata_frame_count=original_metadata_frame_count,
        decoded_video_frame_count=decoded_frame_count,
        sidecar_frame_count=len(sidecar_result.rows),
        trim_occurred=trim_occurred,
        timeline_reconciliation_status=timeline_reconciliation_status,
        fps=_fps_summary(
            native_frames=native_frames,
            video_path=video_path,
            decoded_video_frame_count=decoded_frame_count,
            decoded_video_duration_s=decoded_video_duration_s,
            max_fps=max_fps,
            min_fps=min_fps,
            fps_gating_applied=fps_gating_applied,
            source_input_fps_estimate=source_input_fps_estimate,
        ),
        event_metadata=event_metadata,
        time_window=time_window,
        video_crop=video_crop,
        video_integrity=video_integrity,
        video_integrity_required=video_integrity_required,
        time_domain_crop_applied=time_domain_crop_applied,
        evidence_capture_mode=evidence_capture_mode,
        event_style_replay_job_passed=event_style_replay_job_passed,
        replay_event_flow_status=replay_event_flow_status,
        workaround_used=workaround_used,
        workaround_reason=workaround_reason,
        replay_timing_metadata=replay_timing_metadata,
    )
    _validate_bundle_summary(summary)
    _write_json(output_dir / SUMMARY_FILE, summary)
    _write_json(sidecar_summary_path, summary)
    report = _build_report(
        input_dir=input_dir,
        output_dir=output_dir,
        raw_clip_path=raw_clip_path,
        sink_metadata_path=sink_metadata_path,
        production_sidecar_path=production_sidecar_path,
        sidecar_summary_path=sidecar_summary_path,
        summary_path=output_dir / SUMMARY_FILE,
        summary=summary,
        trim_occurred=trim_occurred,
    )
    return EvidenceBundleResult(
        input_dir=input_dir,
        output_dir=output_dir,
        raw_clip_path=raw_clip_path,
        sink_metadata_path=sink_metadata_path,
        production_sidecar_path=production_sidecar_path,
        sidecar_summary_path=sidecar_summary_path,
        summary_path=output_dir / SUMMARY_FILE,
        summary=summary,
        report=report,
    )


def read_decoded_video_frame_count(video_path: Path) -> int:
    ffprobe_count = _read_frame_count_ffprobe(video_path)
    if ffprobe_count is not None:
        return ffprobe_count
    cv2_count = _read_frame_count_cv2(video_path)
    if cv2_count is not None:
        return cv2_count
    ffmpeg_count = _read_frame_count_ffmpeg_decode(video_path)
    if ffmpeg_count is not None:
        return ffmpeg_count
    raise RuntimeError("decoded_video_frame_count_unavailable")


def _select_time_domain_frames(
    frames: list[dict[str, Any]],
    *,
    requested_start_pts: int | None,
    requested_end_pts: int | None,
    event_frame_pts: int | None,
    event_frame_uuid: str | None = None,
    start_window_frame_uuid: str | None = None,
    post_window_frame_uuid: str | None = None,
    enabled: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    anchor_uuids = _frame_uuid_anchors(
        event_frame_uuid=event_frame_uuid,
        start_window_frame_uuid=start_window_frame_uuid,
        post_window_frame_uuid=post_window_frame_uuid,
    )
    if not enabled:
        return frames, {
            "requested_start_pts": requested_start_pts,
            "requested_end_pts": requested_end_pts,
            "event_frame_pts": event_frame_pts,
            "event_frame_uuid": event_frame_uuid,
            "start_window_frame_uuid": start_window_frame_uuid,
            "post_window_frame_uuid": post_window_frame_uuid,
            "time_domain_crop_applied": False,
        }
    if requested_start_pts is None or requested_end_pts is None:
        raise ValueError("requested_start_pts_and_requested_end_pts_required")
    if requested_end_pts <= requested_start_pts:
        raise ValueError("requested_end_pts_must_be_after_requested_start_pts")
    candidates = _time_domain_window_candidates(
        frames,
        requested_start_pts=requested_start_pts,
        requested_end_pts=requested_end_pts,
        event_frame_pts=event_frame_pts,
        anchor_uuids=anchor_uuids,
    )
    if not candidates:
        raise ValueError("time_domain_crop_selected_zero_metadata_frames")
    candidate = max(candidates, key=_time_domain_candidate_sort_key)
    selected = [frame for _index, frame in candidate["selected"]]
    actual_start_pts = int(_frame_pts(selected[0]) or 0)
    actual_end_pts = int(_frame_pts(selected[-1]) or 0)
    actual_start_index = int(candidate["selected"][0][0])
    actual_end_index = int(candidate["selected"][-1][0])
    matched_anchor_uuid = candidate.get("matched_anchor_uuid")
    segment_first_pts = int(candidate["segment_first_pts"])
    segment_last_pts = int(candidate["segment_last_pts"])
    return selected, {
        "requested_start_pts": requested_start_pts,
        "requested_end_pts": requested_end_pts,
        "event_frame_pts": event_frame_pts,
        "event_frame_uuid": event_frame_uuid,
        "start_window_frame_uuid": start_window_frame_uuid,
        "post_window_frame_uuid": post_window_frame_uuid,
        "actual_start_pts": actual_start_pts,
        "actual_end_pts": actual_end_pts,
        "requested_duration_s": _pts_duration_s(requested_start_pts, requested_end_pts),
        "actual_duration_s": _pts_duration_s(actual_start_pts, actual_end_pts),
        "time_domain_crop_applied": True,
        "source_metadata_frame_count": len(frames),
        "cropped_metadata_frame_count": len(selected),
        "time_domain_selection_strategy": (
            "frame_uuid_contiguous_segment"
            if matched_anchor_uuid
            else "latest_contiguous_pts_segment"
        ),
        "frame_uuid_anchor_found": bool(matched_anchor_uuid),
        "frame_uuid_anchor_used": matched_anchor_uuid,
        "frame_uuid_anchor_candidates": anchor_uuids,
        "crop_segment_start_index": int(candidate["segment_start_index"]),
        "crop_segment_end_index": int(candidate["segment_end_index"]),
        "crop_segment_first_pts": segment_first_pts,
        "crop_segment_last_pts": segment_last_pts,
        "actual_start_index": actual_start_index,
        "actual_end_index": actual_end_index,
        "source_metadata_pts_discontinuities": max(0, len(_pts_contiguous_segments(frames)) - 1),
        "candidate_contiguous_segments": len(candidates),
    }


def _copy_or_crop_video(
    *,
    source_video_path: Path,
    output_video_path: Path,
    source_frames: list[dict[str, Any]],
    time_window: dict[str, Any],
    copy_video: bool,
    crop_video_to_time_window: bool,
    materialization_timeout_s: float | None = None,
) -> dict[str, Any]:
    input_bytes = _file_size_or_none(source_video_path)
    source_metadata_frame_count = len(source_frames or [])
    source_metadata_duration_seconds = _source_metadata_duration_seconds(source_frames)
    if not crop_video_to_time_window:
        started = time.monotonic()
        if copy_video:
            method = _publish_raw_clip_copy_or_link(source_video_path, output_video_path)
        else:
            if output_video_path.exists():
                output_video_path.unlink()
            output_video_path.symlink_to(source_video_path)
            method = "symlink"
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "measurement_schema_version": "phase0-materialization-v1",
            "method": method,
            "materialization_mode": "copy" if copy_video else "symlink",
            "crop_video_to_time_window": False,
            "source_video_path": str(source_video_path),
            "input_bytes": input_bytes,
            "input_duration_seconds": source_metadata_duration_seconds,
            "output_bytes": _file_size_or_none(output_video_path),
            "materialization_elapsed_ms": elapsed_ms,
            "source_metadata_frame_count": source_metadata_frame_count,
            "source_metadata_duration_seconds": source_metadata_duration_seconds,
        }

    if not time_window.get("time_domain_crop_applied"):
        raise ValueError("crop_video_to_time_window_requires_time_domain_crop")
    if not source_frames:
        raise ValueError("source_frames_required_for_video_crop")
    requested_start_pts = _number_or_none(time_window.get("requested_start_pts"))
    requested_end_pts = _number_or_none(time_window.get("requested_end_pts"))
    actual_start_pts = _number_or_none(time_window.get("actual_start_pts"))
    segment_first_pts = _number_or_none(time_window.get("crop_segment_first_pts"))
    first_pts = segment_first_pts if segment_first_pts is not None else _frame_pts(source_frames[0])
    if None in (first_pts, requested_start_pts, requested_end_pts):
        raise ValueError("video_crop_pts_unavailable")
    crop_start_pts = actual_start_pts if actual_start_pts is not None else requested_start_pts
    start_seconds = max(0.0, (float(crop_start_pts) - float(first_pts)) / 1_000_000_000.0)
    duration_seconds = max(0.0, (float(requested_end_pts) - float(requested_start_pts)) / 1_000_000_000.0)
    if duration_seconds <= 0:
        raise ValueError("video_crop_duration_must_be_positive")
    ffmpeg_exe = _ffmpeg_executable()
    video_filter = (
        "setpts=PTS-STARTPTS,"
        f"trim=start={start_seconds:.9f}:duration={duration_seconds:.9f},"
        "setpts=PTS-STARTPTS"
    )
    command = [
        ffmpeg_exe,
        "-hide_banner",
        "-nostdin",
        *_ffmpeg_thread_args(),
        "-y",
        "-i",
        str(source_video_path),
        "-vf",
        video_filter,
        "-t",
        f"{duration_seconds:.9f}",
        "-an",
        "-c:v",
        "libx264",
        *_ffmpeg_x264_tuning_args(),
        "-pix_fmt",
        "yuv420p",
        str(output_video_path),
    ]
    ffmpeg_started = time.monotonic()
    child_cpu_before = _child_cpu_seconds()
    timeout = _positive_timeout_or_none(materialization_timeout_s)
    try:
        completed = run_managed_subprocess(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output_video_path.unlink(missing_ok=True)
        log_path = output_video_path.with_name("video_crop_ffmpeg.log")
        timeout_stderr = exc.stderr or ""
        if isinstance(timeout_stderr, bytes):
            timeout_stderr = timeout_stderr.decode("utf-8", errors="replace")
        log_path.write_text(timeout_stderr, encoding="utf-8")
        raise RuntimeError(
            f"video_time_domain_crop_failed:timeout:{timeout:g}s"
        ) from exc
    ffmpeg_elapsed_ms = int((time.monotonic() - ffmpeg_started) * 1000)
    child_cpu_after = _child_cpu_seconds()
    ffmpeg_child_cpu_seconds = (
        round(max(0.0, child_cpu_after - child_cpu_before), 6)
        if child_cpu_before is not None and child_cpu_after is not None
        else None
    )
    log_path = output_video_path.with_name("video_crop_ffmpeg.log")
    log_path.write_text(completed.stderr or "", encoding="utf-8")
    if completed.returncode != 0 or not output_video_path.is_file() or output_video_path.stat().st_size <= 0:
        raise RuntimeError(f"video_time_domain_crop_failed:{completed.returncode}")
    decode_probe_started = time.monotonic()
    try:
        decoded_frame_count = read_decoded_video_frame_count(output_video_path)
    except Exception as exc:
        output_video_path.unlink(missing_ok=True)
        raise RuntimeError(
            "video_time_domain_crop_failed:decoded_frame_count_unavailable"
        ) from exc
    decoded_probe_elapsed_ms = int((time.monotonic() - decode_probe_started) * 1000)
    if decoded_frame_count <= 0:
        output_video_path.unlink(missing_ok=True)
        raise RuntimeError("video_time_domain_crop_failed:decoded_frame_count_zero")
    return {
        "measurement_schema_version": "phase0-materialization-v1",
        "method": "ffmpeg_segment_normalized_transcode",
        "materialization_mode": "baseline_crop",
        "crop_video_to_time_window": True,
        "ffmpeg_executable": ffmpeg_exe,
        "ffmpeg_returncode": int(completed.returncode),
        "ffmpeg_timeout_s": timeout,
        "ffmpeg_elapsed_ms": ffmpeg_elapsed_ms,
        "ffmpeg_child_cpu_seconds": ffmpeg_child_cpu_seconds,
        "ffmpeg_stderr_bytes": len((completed.stderr or "").encode("utf-8", errors="replace")),
        "decoded_frame_count": int(decoded_frame_count),
        "decoded_frame_count_probe_elapsed_ms": decoded_probe_elapsed_ms,
        "input_bytes": input_bytes,
        "input_duration_seconds": source_metadata_duration_seconds,
        "output_bytes": _file_size_or_none(output_video_path),
        "materialization_elapsed_ms": ffmpeg_elapsed_ms,
        "source_metadata_frame_count": source_metadata_frame_count,
        "source_metadata_duration_seconds": source_metadata_duration_seconds,
        "diagnostic_remux_or_transcode": False,
        "source_video_path": str(source_video_path),
        "start_seconds": start_seconds,
        "duration_seconds": duration_seconds,
        "timeline_basis": "contiguous_metadata_segment",
        "crop_segment_first_pts": int(first_pts),
        "crop_start_pts": int(crop_start_pts),
        "ffmpeg_filter": video_filter,
        "ffmpeg_log_path": str(log_path),
    }


def _publish_raw_clip_copy_or_link(source_video_path: Path, output_video_path: Path) -> str:
    output_video_path.parent.mkdir(parents=True, exist_ok=True)
    mode = os.getenv("MEDIA_WORKER_RAW_CLIP_PUBLISH_MODE", "hardlink").strip().lower()
    if mode in {"hardlink", "link", "auto", ""}:
        try:
            output_video_path.unlink(missing_ok=True)
            os.link(source_video_path, output_video_path)
            return "hardlink"
        except OSError:
            if output_video_path.exists():
                try:
                    if os.path.samefile(source_video_path, output_video_path):
                        return "hardlink"
                except OSError:
                    pass
            if mode in {"hardlink", "link"}:
                logger_mode = "hardlink_failed_falling_back_to_copy"
            else:
                logger_mode = "auto_hardlink_failed_falling_back_to_copy"
            # The caller has no logger in this utility module; preserve the
            # fast path where possible and silently use a normal copy when the
            # source and destination live on different filesystems.
            _ = logger_mode
    shutil.copy2(source_video_path, output_video_path)
    return "copy"


def _file_size_or_none(path: Path) -> int | None:
    try:
        return int(path.stat().st_size)
    except OSError:
        return None


def _child_cpu_seconds() -> float | None:
    try:
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    except (OSError, AttributeError):
        return None
    return float(usage.ru_utime + usage.ru_stime)


def _source_metadata_duration_seconds(frames: list[dict[str, Any]]) -> float | None:
    pts_values = [
        int(pts)
        for frame in frames
        if (pts := _frame_pts(frame)) is not None
    ]
    if len(pts_values) < 2:
        return None
    return round((max(pts_values) - min(pts_values)) / 1_000_000_000.0, 9)


def _positive_timeout_or_none(value: float | None) -> float | None:
    try:
        timeout = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def _ffmpeg_executable() -> str:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]
    except Exception as exc:
        raise FileNotFoundError("ffmpeg executable unavailable") from exc
    try:
        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:
        raise FileNotFoundError("imageio_ffmpeg executable unavailable") from exc


def _ffmpeg_thread_args() -> list[str]:
    try:
        thread_limit = int(os.getenv("MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT", "0"))
    except ValueError:
        thread_limit = 0
    if thread_limit <= 0:
        return []
    return ["-threads", str(thread_limit)]


def _ffmpeg_x264_tuning_args() -> list[str]:
    args: list[str] = []
    preset = os.getenv("MEDIA_WORKER_FFMPEG_X264_PRESET", "ultrafast").strip()
    if preset:
        args.extend(["-preset", preset])
    args.extend(_ffmpeg_thread_args())
    return args


def _requested_duration_s(time_window: dict[str, Any]) -> float | None:
    value = _number_or_none(time_window.get("requested_duration_s"))
    return float(value) if value is not None else None


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _frame_pts(frame: dict[str, Any]) -> int | None:
    value = _number_or_none(frame.get("pts") if frame.get("pts") is not None else frame.get("frame_pts"))
    return int(value) if value is not None else None


def _frame_uuid(frame: dict[str, Any]) -> str:
    return str(frame.get("frame_uuid") or frame.get("uuid") or "").strip()


def _frame_uuid_anchors(
    *,
    event_frame_uuid: str | None,
    start_window_frame_uuid: str | None,
    post_window_frame_uuid: str | None,
) -> list[str]:
    anchors: list[str] = []
    for value in (event_frame_uuid, start_window_frame_uuid, post_window_frame_uuid):
        text = str(value or "").strip()
        if text and text not in anchors:
            anchors.append(text)
    return anchors


def _pts_contiguous_segments(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    current: list[tuple[int, dict[str, Any]]] = []
    current_start = 0
    previous_pts: int | None = None
    for index, frame in enumerate(frames):
        pts = _frame_pts(frame)
        if pts is None:
            if current:
                segments.append(
                    {
                        "start_index": current_start,
                        "end_index": current[-1][0],
                        "frames": current,
                    }
                )
                current = []
            previous_pts = None
            continue
        if current and previous_pts is not None and pts <= previous_pts:
            segments.append(
                {
                    "start_index": current_start,
                    "end_index": current[-1][0],
                    "frames": current,
                }
            )
            current = []
        if not current:
            current_start = index
        current.append((index, frame))
        previous_pts = pts
    if current:
        segments.append(
            {
                "start_index": current_start,
                "end_index": current[-1][0],
                "frames": current,
            }
        )
    return segments


def _time_domain_window_candidates(
    frames: list[dict[str, Any]],
    *,
    requested_start_pts: int,
    requested_end_pts: int,
    event_frame_pts: int | None,
    anchor_uuids: list[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    anchor_set = set(anchor_uuids)
    for segment in _pts_contiguous_segments(frames):
        segment_frames = segment["frames"]
        if not segment_frames:
            continue
        selected = [
            (index, frame)
            for index, frame in segment_frames
            if (pts := _frame_pts(frame)) is not None
            and requested_start_pts <= int(pts) <= requested_end_pts
        ]
        if not selected:
            continue
        pts_values = [int(_frame_pts(frame) or 0) for _index, frame in segment_frames]
        matched_anchor_uuid = ""
        if anchor_set:
            for _index, frame in segment_frames:
                candidate_uuid = _frame_uuid(frame)
                if candidate_uuid in anchor_set:
                    matched_anchor_uuid = candidate_uuid
                    break
        segment_first_pts = pts_values[0]
        segment_last_pts = pts_values[-1]
        candidates.append(
            {
                "segment_start_index": int(segment["start_index"]),
                "segment_end_index": int(segment["end_index"]),
                "segment_first_pts": segment_first_pts,
                "segment_last_pts": segment_last_pts,
                "selected": selected,
                "selected_frame_count": len(selected),
                "matched_anchor_uuid": matched_anchor_uuid,
                "event_pts_inside_segment": (
                    event_frame_pts is not None
                    and segment_first_pts <= int(event_frame_pts) <= segment_last_pts
                ),
            }
        )
    return candidates


def _time_domain_candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        1 if candidate.get("matched_anchor_uuid") else 0,
        1 if candidate.get("event_pts_inside_segment") else 0,
        int(candidate.get("selected_frame_count") or 0),
        int(candidate.get("segment_start_index") or 0),
    )


def _pts_duration_s(start_pts: int | float, end_pts: int | float) -> float:
    return max(0.0, (float(end_pts) - float(start_pts)) / 1_000_000_000.0)


def _read_frame_count_ffprobe(video_path: Path) -> int | None:
    try:
        completed = run_managed_subprocess(
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
    try:
        import cv2  # type: ignore[import-not-found]
    except Exception:
        return None
    capture = cv2.VideoCapture(str(video_path))
    try:
        if not capture.isOpened():
            return None
        value = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        return value if value > 0 else None
    finally:
        capture.release()


def _read_frame_count_imageio_ffmpeg(video_path: Path) -> int | None:
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        frames, _duration = imageio_ffmpeg.count_frames_and_secs(str(video_path))
    except Exception:
        return None
    value = _int_or_none(frames)
    return value if value is not None and value > 0 else None


def _read_frame_count_ffmpeg_decode(video_path: Path) -> int | None:
    try:
        ffmpeg_exe = _ffmpeg_executable()
    except FileNotFoundError:
        return None
    try:
        completed = run_managed_subprocess(
            [
                ffmpeg_exe,
                "-hide_banner",
                "-nostdin",
                *_ffmpeg_thread_args(),
                "-i",
                str(video_path),
                "-map",
                "0:v:0",
                "-f",
                "null",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    stderr = completed.stderr or ""
    matches = re.findall(r"frame=\s*(\d+)", stderr)
    if not matches:
        return None
    value = _int_or_none(matches[-1])
    return value if value is not None and value > 0 else None


def _find_required(input_dir: Path, names: tuple[str, ...], label: str) -> Path:
    for name in names:
        path = input_dir / name
        if path.is_file():
            return path
    raise FileNotFoundError(f"{label} file not found in {input_dir}: {', '.join(names)}")


def _bundle_summary(
    *,
    sidecar_summary: dict[str, Any],
    source_metadata_frame_count: int,
    original_metadata_frame_count: int,
    decoded_video_frame_count: int,
    sidecar_frame_count: int,
    trim_occurred: bool,
    timeline_reconciliation_status: str,
    fps: dict[str, Any],
    event_metadata: dict[str, Any] | None = None,
    time_window: dict[str, Any] | None = None,
    video_crop: dict[str, Any] | None = None,
    video_integrity: dict[str, Any] | None = None,
    video_integrity_required: bool = False,
    time_domain_crop_applied: bool = False,
    evidence_capture_mode: str | None = None,
    event_style_replay_job_passed: bool | None = None,
    replay_event_flow_status: str | None = None,
    workaround_used: bool | None = None,
    workaround_reason: str | None = None,
    replay_timing_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    limitations = list(sidecar_summary.get("limitations") or [])
    if trim_occurred:
        for limitation in TRIM_LIMITATIONS:
            if limitation not in limitations:
                limitations.append(limitation)
    object_counts = _dict(sidecar_summary.get("object_counts"))
    base_ready = (
        sidecar_frame_count > 0
        and int(sidecar_summary.get("frames_with_objects_count") or 0) > 0
    )
    video_gate_passed = (
        True
        if video_integrity is None
        else bool(video_integrity.get("production_gate_passed"))
    )
    production_ready = (
        base_ready
        and timeline_reconciliation_status in (TIMELINE_COUNTS_MATCH, TIMELINE_SPARSE_SIDECAR)
        and video_gate_passed
    )
    annotation_status = "complete" if production_ready else "no_post_savant_objects"
    if base_ready and not production_ready:
        annotation_status = (
            "video_integrity_failed"
            if video_integrity is not None and not video_gate_passed
            else "timeline_reconciliation_unverified"
        )
    visual_evidence_status = (
        "verified_same_stream_metadata" if production_ready else "unverified"
    )
    visual_binding_reason = (
        "verified_same_stream_metadata"
        if production_ready
        else timeline_reconciliation_status
        if base_ready
        else "no_post_savant_objects"
    )
    event_metadata = _dict(event_metadata)
    summary = {
        **sidecar_summary,
        "schema_version": SCHEMA_VERSION,
        "evidence_topology": EVIDENCE_TOPOLOGY,
        "sidecar_type": "production",
        "timeline_domain": PRODUCTION_TIMELINE_DOMAIN,
        "annotation_status": annotation_status,
        "annotation_source": ANNOTATION_SOURCE,
        "production_ready": production_ready,
        "canonical_clip": production_ready,
        "visual_evidence_status": visual_evidence_status,
        "visual_binding_status": "verified" if production_ready else "unverified",
        "visual_binding_reason": visual_binding_reason,
        "evidence_visual_status": "verified" if production_ready else "unverified",
        "fallback_used": False,
        "raw_video_binding": "continuous_replay_video",
        "annotation_binding": "pts_time_offset_sidecar",
        "raw_clip_path": RAW_CLIP_FILE,
        "sink_metadata_path": SINK_METADATA_FILE,
        "production_sidecar_path": SIDECAR_ANNOTATIONS_FILE,
        "source_metadata_frame_count": source_metadata_frame_count,
        "original_metadata_frame_count": original_metadata_frame_count,
        "decoded_video_frame_count": decoded_video_frame_count,
        "sidecar_frame_count": sidecar_frame_count,
        "frame_count": sidecar_frame_count,
        "sidecar_trimmed": trim_occurred,
        "trim_occurred": trim_occurred,
        "timeline_reconciliation_status": timeline_reconciliation_status,
        "time_domain_crop_applied": bool(time_domain_crop_applied),
        "video_integrity_required": bool(video_integrity_required),
        "video_integrity": video_integrity,
        "video_crop": video_crop or {},
        "time_window": time_window or {},
        "fps": fps,
        "object_counts": {
            "person": int(object_counts.get("person") or 0),
            "face": int(object_counts.get("face") or 0),
            "known_face": int(object_counts.get("known_face") or 0),
        },
        "limitations": [
            _normalize_limitation(limitation) for limitation in limitations
        ],
    }
    if evidence_capture_mode is not None:
        summary["evidence_capture_mode"] = evidence_capture_mode
    if event_style_replay_job_passed is not None:
        summary["event_style_replay_job_passed"] = bool(event_style_replay_job_passed)
    if replay_event_flow_status is not None:
        summary["replay_event_flow_status"] = replay_event_flow_status
    if workaround_used is not None:
        summary["workaround_used"] = bool(workaround_used)
    if workaround_reason is not None:
        summary["workaround_reason"] = workaround_reason
    if replay_timing_metadata:
        summary.update(_replay_timing_summary_fields(replay_timing_metadata))
    summary.update(_event_summary_fields(event_metadata))
    return summary


def _replay_timing_summary_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "requested_start_pts",
        "original_requested_start_pts",
        "effective_start_pts",
        "requested_end_pts",
        "requested_duration_s",
        "requested_pre_window_seconds",
        "effective_pre_window_seconds",
        "pre_window_truncated_seconds",
        "pre_window_truncated",
        "pre_window_policy",
        "actual_start_pts",
        "actual_end_pts",
        "actual_duration_s",
        "evidence_anchor_strategy",
        "event_frame_uuid",
        "event_frame_pts",
        "anchor_keyframe_uuid",
        "anchor_keyframe_pts",
        "post_window_frame_uuid",
        "post_window_frame_pts",
        "post_window_proof_used",
        "post_window_cross_session_proof_used",
        "frame_domain_session_policy",
        "frame_domain_proof_method",
        "start_window_frame_uuid",
        "start_window_frame_pts",
        "start_window_stream_session_id",
        "post_window_stream_session_id",
        "start_window_coverage_used",
        "crop_reason",
        "fail_closed_reason",
        "replay_offset_seconds",
        "replay_stop_strategy",
        "over_exported",
        "window_crosses_loop_or_replay_boundary",
    )
    return {
        key: metadata[key]
        for key in allowed
        if key in metadata and metadata[key] is not None
    }


def _event_summary_fields(event_metadata: dict[str, Any]) -> dict[str, Any]:
    if not event_metadata:
        return {}
    allowed = (
        "replay_source_kind",
        "legacy_record_request_id",
        "legacy_source_event_id",
        "legacy_event_type",
        "legacy_camera_id",
        "legacy_source_id",
        "legacy_frame_pts",
        "legacy_frame_num",
        "evidence_topology",
        "annotation_source_policy",
        "replay_stored_stream_id",
        "replay_resulting_stream_id",
        "requested_start_pts",
        "original_requested_start_pts",
        "effective_start_pts",
        "requested_end_pts",
        "requested_duration_s",
        "requested_pre_window_seconds",
        "effective_pre_window_seconds",
        "pre_window_truncated_seconds",
        "pre_window_truncated",
        "pre_window_policy",
        "actual_start_pts",
        "actual_end_pts",
        "actual_duration_s",
        "evidence_anchor_strategy",
        "event_frame_uuid",
        "event_frame_pts",
        "anchor_keyframe_uuid",
        "anchor_keyframe_pts",
        "post_window_frame_uuid",
        "post_window_frame_pts",
        "post_window_proof_used",
        "post_window_cross_session_proof_used",
        "frame_domain_session_policy",
        "frame_domain_proof_method",
        "start_window_frame_uuid",
        "start_window_frame_pts",
        "start_window_stream_session_id",
        "post_window_stream_session_id",
        "start_window_coverage_used",
        "crop_reason",
        "fail_closed_reason",
        "replay_offset_seconds",
        "replay_stop_strategy",
        "over_exported",
        "time_domain_crop_applied",
    )
    return {
        key: event_metadata[key]
        for key in allowed
        if key in event_metadata and event_metadata[key] is not None
    }


def _normalize_limitation(limitation: Any) -> str:
    text = str(limitation)
    if text == "watchlist_trigger_identity_binding_not_verified":
        return "watchlist_trigger_identity_binding_not_verified"
    return text


def _timeline_reconciliation_status(
    *,
    original_metadata_frame_count: int,
    decoded_video_frame_count: int,
) -> str:
    if original_metadata_frame_count == decoded_video_frame_count:
        return TIMELINE_COUNTS_MATCH
    if 0 < original_metadata_frame_count < decoded_video_frame_count:
        return TIMELINE_SPARSE_SIDECAR
    return TIMELINE_NEEDS_MAPPING


def _fps_summary(
    *,
    native_frames: list[dict[str, Any]],
    video_path: Path,
    decoded_video_frame_count: int,
    decoded_video_duration_s: float | None = None,
    max_fps: str | None,
    min_fps: str | None,
    fps_gating_applied: bool | None,
    source_input_fps_estimate: float | None,
) -> dict[str, Any]:
    metadata_fps = _metadata_fps_estimate(native_frames)
    decoded_fps = (
        decoded_video_frame_count / decoded_video_duration_s
        if decoded_video_duration_s and decoded_video_duration_s > 0
        else _decoded_video_fps_estimate(video_path, decoded_video_frame_count)
    )
    if source_input_fps_estimate is None:
        source_input_fps_estimate = metadata_fps or decoded_fps
    if fps_gating_applied is None:
        fps_gating_applied = _env_bool(
            "INGRESS_FPS_GATE_ENABLED",
            default=_env_bool("MAX_FPS_CONTROL", default=False),
        )
    return {
        "source_input_fps_estimate": source_input_fps_estimate,
        "metadata_fps_estimate": metadata_fps,
        "decoded_video_fps_estimate": decoded_fps,
        "max_fps": max_fps or os.environ.get("MAX_FPS") or "8/1",
        "min_fps": min_fps or os.environ.get("MIN_FPS") or "2/1",
        "fps_gating_applied": fps_gating_applied,
    }


def _metadata_fps_estimate(native_frames: list[dict[str, Any]]) -> float | None:
    pts_values = [
        _number_or_none(frame.get("pts") if frame.get("pts") is not None else frame.get("frame_pts"))
        for frame in native_frames
    ]
    pts_values = [value for value in pts_values if value is not None]
    if len(pts_values) < 2:
        return None
    first = pts_values[0]
    last = pts_values[-1]
    if last <= first:
        return None
    return (len(pts_values) - 1) * 1_000_000_000.0 / (last - first)


def _decoded_video_fps_estimate(video_path: Path, decoded_video_frame_count: int) -> float | None:
    duration = _read_video_duration_ffprobe(video_path)
    if duration and duration > 0:
        return decoded_video_frame_count / duration
    return _read_avg_frame_rate_ffprobe(video_path)


def _read_video_duration_ffprobe(video_path: Path) -> float | None:
    try:
        completed = run_managed_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
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
        value = json.loads(completed.stdout).get("format", {}).get("duration")
        return float(value) if value not in (None, "", "N/A") else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _read_avg_frame_rate_ffprobe(video_path: Path) -> float | None:
    try:
        completed = run_managed_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate,r_frame_rate",
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
        streams = json.loads(completed.stdout).get("streams") or []
    except json.JSONDecodeError:
        return None
    if not streams:
        return None
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = _rate_or_none(streams[0].get(key))
        if value:
            return value
    return None


def _rate_or_none(value: Any) -> float | None:
    if not value or value == "0/0":
        return None
    if isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            denominator_float = float(denominator)
            if denominator_float == 0:
                return None
            return float(numerator) / denominator_float
        except ValueError:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _number_or_none(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_bundle_summary(summary: dict[str, Any]) -> None:
    if int(summary.get("sidecar_frame_count") or 0) <= 0:
        raise ValueError("sidecar_generated_zero_rows")
    object_counts = _dict(summary.get("object_counts"))
    person_count = int(object_counts.get("person") or 0)
    face_count = int(object_counts.get("face") or 0)
    if person_count <= 0 and face_count <= 0:
        video_crop = _dict(summary.get("video_crop"))
        if video_crop.get("materialization_mode") == "rolling_cache_copy":
            return
        raise ValueError("no_post_savant_person_or_face_objects_found")


def _build_report(
    *,
    input_dir: Path,
    output_dir: Path,
    raw_clip_path: Path,
    sink_metadata_path: Path,
    production_sidecar_path: Path,
    sidecar_summary_path: Path,
    summary_path: Path,
    summary: dict[str, Any],
    trim_occurred: bool,
) -> dict[str, Any]:
    return {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "raw_clip": str(raw_clip_path),
        "sink_metadata": str(sink_metadata_path),
        "production_sidecar": str(production_sidecar_path),
        "sidecar_summary": str(sidecar_summary_path),
        "summary": str(summary_path),
        "original_metadata_frame_count": summary.get("original_metadata_frame_count"),
        "decoded_video_frame_count": summary.get("decoded_video_frame_count"),
        "sidecar_frame_count": summary.get("sidecar_frame_count"),
        "trim_occurred": trim_occurred,
        "timeline_reconciliation_status": summary.get("timeline_reconciliation_status"),
        "fps": summary.get("fps"),
        "object_counts": summary.get("object_counts"),
        "person_count": _dict(summary.get("object_counts")).get("person"),
        "face_count": _dict(summary.get("object_counts")).get("face"),
        "known_face_count": _dict(summary.get("object_counts")).get("known_face"),
        "keypoints_count": summary.get("keypoints_count"),
        "production_ready": summary.get("production_ready"),
        "annotation_status": summary.get("annotation_status"),
        "limitations": summary.get("limitations"),
    }


def _is_native_frame(frame: Any) -> bool:
    if not isinstance(frame, dict):
        return False
    if frame.get("type") == "VideoFrame":
        return True
    return any(key in frame for key in ("pts", "frame_pts", "uuid", "frame_uuid", "objects"))


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, "N/A", ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
