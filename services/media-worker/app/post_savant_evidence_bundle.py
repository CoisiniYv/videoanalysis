"""Package post-Savant video-file-sink output as a production evidence bundle."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
SCHEMA_VERSION = "2.0-c2"
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
    time_domain_crop_applied: bool = False,
    crop_video_to_time_window: bool = False,
    evidence_capture_mode: str | None = None,
    event_style_replay_job_passed: bool | None = None,
    replay_event_flow_status: str | None = None,
    workaround_used: bool | None = None,
    workaround_reason: str | None = None,
    replay_timing_metadata: dict[str, Any] | None = None,
    video_integrity_required: bool = False,
    decoded_frame_count_reader: Callable[[Path], int] | None = None,
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
    raise RuntimeError("decoded_video_frame_count_unavailable")


def _select_time_domain_frames(
    frames: list[dict[str, Any]],
    *,
    requested_start_pts: int | None,
    requested_end_pts: int | None,
    event_frame_pts: int | None,
    enabled: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not enabled:
        return frames, {
            "requested_start_pts": requested_start_pts,
            "requested_end_pts": requested_end_pts,
            "event_frame_pts": event_frame_pts,
            "time_domain_crop_applied": False,
        }
    if requested_start_pts is None or requested_end_pts is None:
        raise ValueError("requested_start_pts_and_requested_end_pts_required")
    if requested_end_pts <= requested_start_pts:
        raise ValueError("requested_end_pts_must_be_after_requested_start_pts")
    selected = [
        frame
        for frame in frames
        if _frame_pts(frame) is not None
        and requested_start_pts <= int(_frame_pts(frame) or 0) <= requested_end_pts
    ]
    if not selected:
        raise ValueError("time_domain_crop_selected_zero_metadata_frames")
    actual_start_pts = int(_frame_pts(selected[0]) or 0)
    actual_end_pts = int(_frame_pts(selected[-1]) or 0)
    return selected, {
        "requested_start_pts": requested_start_pts,
        "requested_end_pts": requested_end_pts,
        "event_frame_pts": event_frame_pts,
        "actual_start_pts": actual_start_pts,
        "actual_end_pts": actual_end_pts,
        "requested_duration_s": _pts_duration_s(requested_start_pts, requested_end_pts),
        "actual_duration_s": _pts_duration_s(actual_start_pts, actual_end_pts),
        "time_domain_crop_applied": True,
        "source_metadata_frame_count": len(frames),
        "cropped_metadata_frame_count": len(selected),
    }


def _copy_or_crop_video(
    *,
    source_video_path: Path,
    output_video_path: Path,
    source_frames: list[dict[str, Any]],
    time_window: dict[str, Any],
    copy_video: bool,
    crop_video_to_time_window: bool,
) -> dict[str, Any]:
    if not crop_video_to_time_window:
        if copy_video:
            shutil.copy2(source_video_path, output_video_path)
            method = "copy"
        else:
            if output_video_path.exists():
                output_video_path.unlink()
            output_video_path.symlink_to(source_video_path)
            method = "symlink"
        return {
            "method": method,
            "crop_video_to_time_window": False,
            "source_video_path": str(source_video_path),
        }

    if not time_window.get("time_domain_crop_applied"):
        raise ValueError("crop_video_to_time_window_requires_time_domain_crop")
    if not source_frames:
        raise ValueError("source_frames_required_for_video_crop")
    first_pts = _frame_pts(source_frames[0])
    requested_start_pts = _number_or_none(time_window.get("requested_start_pts"))
    requested_end_pts = _number_or_none(time_window.get("requested_end_pts"))
    if None in (first_pts, requested_start_pts, requested_end_pts):
        raise ValueError("video_crop_pts_unavailable")
    start_seconds = max(0.0, (float(requested_start_pts) - float(first_pts)) / 1_000_000_000.0)
    duration_seconds = max(0.0, (float(requested_end_pts) - float(requested_start_pts)) / 1_000_000_000.0)
    if duration_seconds <= 0:
        raise ValueError("video_crop_duration_must_be_positive")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{start_seconds:.9f}",
        "-i",
        str(source_video_path),
        "-t",
        f"{duration_seconds:.9f}",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(output_video_path),
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    log_path = output_video_path.with_name("video_crop_ffmpeg.log")
    log_path.write_text(completed.stderr or "", encoding="utf-8")
    if completed.returncode != 0 or not output_video_path.is_file() or output_video_path.stat().st_size <= 0:
        raise RuntimeError(f"video_time_domain_crop_failed:{completed.returncode}")
    return {
        "method": "ffmpeg_time_domain_transcode",
        "crop_video_to_time_window": True,
        "diagnostic_remux_or_transcode": False,
        "source_video_path": str(source_video_path),
        "start_seconds": start_seconds,
        "duration_seconds": duration_seconds,
        "ffmpeg_log_path": str(log_path),
    }


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


def _pts_duration_s(start_pts: int | float, end_pts: int | float) -> float:
    return max(0.0, (float(end_pts) - float(start_pts)) / 1_000_000_000.0)


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
        "legacy_fallback_allowed": False,
        "legacy_used_for_visual_binding": False,
        "fallback_used": False,
        "allow_db_annotation_fallback": False,
        "allow_legacy_annotation_fallback": False,
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
            _c2_2_limitation(limitation) for limitation in limitations
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
        summary.update(_c2_replay_timing_summary_fields(replay_timing_metadata))
    summary.update(_c2_event_summary_fields(event_metadata))
    return summary


def _c2_replay_timing_summary_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "requested_start_pts",
        "requested_end_pts",
        "requested_duration_s",
        "actual_start_pts",
        "actual_end_pts",
        "actual_duration_s",
        "replay_anchor_keyframe",
        "replay_anchor_pts",
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


def _c2_event_summary_fields(event_metadata: dict[str, Any]) -> dict[str, Any]:
    if not event_metadata:
        return {}
    allowed = (
        "replay_source_kind",
        "c2_3b_record_request_id",
        "c2_3b_source_event_id",
        "c2_3b_event_type",
        "c2_3b_camera_id",
        "c2_3b_source_id",
        "c2_3b_frame_pts",
        "c2_3b_frame_num",
        "evidence_topology",
        "annotation_source_policy",
        "allow_db_annotation_fallback",
        "allow_legacy_annotation_fallback",
        "replay_stored_stream_id",
        "replay_resulting_stream_id",
        "requested_start_pts",
        "requested_end_pts",
        "requested_duration_s",
        "actual_start_pts",
        "actual_end_pts",
        "actual_duration_s",
        "replay_anchor_keyframe",
        "replay_anchor_pts",
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


def _c2_2_limitation(limitation: Any) -> str:
    text = str(limitation)
    if text == "watchlist_trigger_identity_binding_not_verified_in_c2_1":
        return "watchlist_trigger_identity_binding_not_verified_in_c2_2r"
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
    max_fps: str | None,
    min_fps: str | None,
    fps_gating_applied: bool | None,
    source_input_fps_estimate: float | None,
) -> dict[str, Any]:
    metadata_fps = _metadata_fps_estimate(native_frames)
    decoded_fps = _decoded_video_fps_estimate(video_path, decoded_video_frame_count)
    if source_input_fps_estimate is None:
        source_input_fps_estimate = metadata_fps or decoded_fps
    if fps_gating_applied is None:
        fps_gating_applied = _env_bool("MAX_FPS_CONTROL", default=True)
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
        completed = subprocess.run(
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
        completed = subprocess.run(
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
