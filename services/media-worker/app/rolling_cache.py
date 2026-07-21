"""Rolling-cache segment lookup and fast evidence materialization."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from fractions import Fraction
from glob import escape as glob_escape
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from app.post_savant_metadata_annotation_builder import load_native_metadata
from app.subprocess_control import run_managed_subprocess


PTS_TIME_BASE = 1_000_000_000
# Keep rolling admission aligned with the DB-backed finalizer's existing
# POST_SAVANT_WINDOW_EDGE_SLACK_SEC default (0.75s). A stricter upstream value
# creates permanent retries for clips the canonical finalizer would accept.
DEFAULT_COVERAGE_SLACK_NS = 750_000_000
DEFAULT_INTERNAL_ROW_GAP_SLACK_NS = 1_000_000_000
DEFAULT_INTERNAL_SEGMENT_GAP_SLACK_NS = 2_000_000_000
VIDEO_NAMES = ("video.mov", "raw_clip.mov", "video.mp4", "raw_clip.mp4", "video.mkv")
MIN_VALID_VIDEO_BYTES = 1024
DEFAULT_DURATION_REPAIR_SLACK_S = 1.0


class RollingCacheError(RuntimeError):
    """Base error for rolling-cache materialization failures."""


class RollingCacheCoverageMiss(RollingCacheError):
    """Raised when rolling cache has no complete-enough coverage for a request."""


@dataclass(frozen=True)
class RollingSegment:
    segment_id: str
    source_id: str
    runtime_epoch_id: str
    directory: Path
    video_path: Path
    metadata_path: Path
    first_pts: int
    last_pts: int
    frame_count: int
    size_bytes: int
    # Compact source-clock bounds let event-to-mux mapping select the one or
    # two relevant metadata files instead of parsing the whole retention
    # window.  Older/legacy segment producers may leave them unset.
    source_first_pts: int | None = None
    source_last_pts: int | None = None


@dataclass(frozen=True)
class RollingMaterializationResult:
    sink_dir: Path
    video_path: Path
    metadata_path: Path
    metadata_payload: dict[str, Any]
    segment_ids: tuple[str, ...]
    requested_start_pts: int
    requested_end_pts: int
    actual_start_pts: int
    actual_end_pts: int
    selected_frame_count: int
    materialization_ms: int
    metadata_publish_ms: int
    metadata_bytes: int
    ffmpeg_command: tuple[str, ...]
    immutable_probe: dict[str, Any]


CommandRunner = Callable[[list[str], Path], None]
DurationProbe = Callable[[Path], float | None]
FrameRateProbe = Callable[[Path], float | None]
RowLoader = Callable[[RollingSegment], list[dict[str, Any]]]


def find_segments(
    root: str | Path,
    *,
    source_id: str,
    runtime_epoch_id: str = "",
) -> list[RollingSegment]:
    """Return indexed rolling-cache segments for a source.

    The writer MVP can be either manifest-backed or plain video-file-sink
    directories. For resilience this function scans finalized ``metadata.json``
    files and derives segment boundaries from native frame PTS.
    """

    root_path = Path(root)
    if not root_path.exists():
        return []
    search_roots = _candidate_source_roots(
        root_path,
        source_id=source_id,
        runtime_epoch_id=runtime_epoch_id,
    )
    segments: list[RollingSegment] = []
    seen: set[Path] = set()
    for source_root in search_roots:
        if not source_root.exists():
            continue
        for metadata_path in source_root.rglob("metadata.json"):
            if metadata_path in seen or _is_materialized_intermediate(metadata_path):
                continue
            seen.add(metadata_path)
            segment = _segment_from_metadata(
                metadata_path,
                source_id=source_id,
                runtime_epoch_id=runtime_epoch_id,
            )
            if segment is not None:
                segments.append(segment)
    return sorted(segments, key=lambda segment: (segment.first_pts, segment.last_pts))


def materialize_window(
    *,
    root: str | Path,
    output_root: str | Path,
    event_id: str,
    source_id: str,
    requested_start_pts: int,
    requested_end_pts: int,
    runtime_epoch_id: str = "",
    labels: dict[str, Any] | None = None,
    segments: Iterable[RollingSegment] | None = None,
    ffmpeg: str | None = None,
    command_runner: CommandRunner | None = None,
    duration_probe: DurationProbe | None = None,
    frame_rate_probe: FrameRateProbe | None = None,
    row_loader: RowLoader | None = None,
    coverage_slack_ns: int = DEFAULT_COVERAGE_SLACK_NS,
    allow_partial: bool = False,
) -> RollingMaterializationResult:
    """Copy/remux rolling segments into a sink-like directory for finalization."""

    started = time.monotonic()
    if requested_end_pts <= requested_start_pts:
        raise RollingCacheCoverageMiss("invalid_requested_pts_window")
    original_requested_start_pts = int(requested_start_pts)
    original_requested_end_pts = int(requested_end_pts)
    segments = list(segments) if segments is not None else find_segments(
        root,
        source_id=source_id,
        runtime_epoch_id=runtime_epoch_id,
    )
    mapped_window = _map_source_window_to_mux(
        segments,
        requested_start_pts=original_requested_start_pts,
        requested_end_pts=original_requested_end_pts,
        labels=labels or {},
        row_loader=row_loader,
    )
    if mapped_window is not None:
        requested_start_pts, requested_end_pts, event_mux_pts = mapped_window
    else:
        event_mux_pts = _int_or_none((labels or {}).get("event_frame_pts"))
    selected = overlapping_segments(
        segments,
        requested_start_pts=requested_start_pts,
        requested_end_pts=requested_end_pts,
    )
    if not selected:
        raise RollingCacheCoverageMiss("no_overlapping_segments")
    selected.sort(key=lambda segment: (segment.first_pts, segment.last_pts))
    internal_segment_gap_limit_ns = max(
        coverage_slack_ns,
        DEFAULT_INTERNAL_SEGMENT_GAP_SLACK_NS,
    )
    internal_gaps = [
        (previous.segment_id, current.segment_id, current.first_pts - previous.last_pts)
        for previous, current in zip(selected, selected[1:])
        if current.first_pts - previous.last_pts > internal_segment_gap_limit_ns
    ]
    if internal_gaps:
        previous_id, current_id, gap_ns = max(
            internal_gaps,
            key=lambda item: item[2],
        )
        raise RollingCacheCoverageMiss(
            "rolling_cache_requested_window_internal_gap:"
            f"previous_segment={previous_id}:next_segment={current_id}:"
            f"internal_gap_ns={gap_ns}"
        )
    segment_start_pts = min(segment.first_pts for segment in selected)
    segment_end_pts = max(segment.last_pts for segment in selected)
    output_start_pts = max(segment_start_pts, requested_start_pts)
    output_end_pts = min(segment_end_pts, requested_end_pts)
    if output_end_pts <= output_start_pts:
        raise RollingCacheCoverageMiss("rolling_cache_requested_window_not_covered")
    start_gap_ns = max(0, segment_start_pts - requested_start_pts)
    end_gap_ns = max(0, requested_end_pts - segment_end_pts)
    event_frame_pts = event_mux_pts
    if (
        event_frame_pts is not None
        and (event_frame_pts < segment_start_pts or event_frame_pts > segment_end_pts)
    ):
        raise RollingCacheCoverageMiss("rolling_cache_event_frame_not_covered")
    coverage_status = (
        "covered"
        if start_gap_ns <= coverage_slack_ns and end_gap_ns <= coverage_slack_ns
        else "partial"
    )
    if coverage_status != "covered" and not allow_partial:
        gaps = []
        if start_gap_ns > coverage_slack_ns:
            gaps.append(f"pre_gap_ns={start_gap_ns}")
        if end_gap_ns > coverage_slack_ns:
            gaps.append(f"post_gap_ns={end_gap_ns}")
        detail = ",".join(gaps) or "window_gap"
        raise RollingCacheCoverageMiss(
            f"rolling_cache_requested_window_not_fully_covered:{detail}"
        )
    canonical_clip = coverage_status == "covered"

    selected_rows = _select_rows(
        selected,
        output_start_pts,
        output_end_pts,
        row_loader=row_loader,
    )
    _validate_selected_row_coverage(
        selected_rows,
        requested_start_pts=output_start_pts,
        requested_end_pts=output_end_pts,
        coverage_slack_ns=coverage_slack_ns,
        allow_partial=allow_partial,
    )

    epoch = runtime_epoch_id or selected[0].runtime_epoch_id or "unknown-epoch"
    sink_dir = Path(output_root) / "midterm" / "epochs" / epoch / "materialized" / event_id
    if sink_dir.exists():
        shutil.rmtree(sink_dir)
    sink_dir.mkdir(parents=True, exist_ok=True)
    video_path = sink_dir / "video.mov"
    metadata_path = sink_dir / "metadata.json"

    ffmpeg_bin = ffmpeg or shutil.which("ffmpeg") or "ffmpeg"
    concat_path = sink_dir / "segments.concat.txt"
    concat_path.write_text(
        "".join(f"file '{_concat_escape(segment.video_path)}'\n" for segment in selected),
        encoding="utf-8",
    )
    trim_start_s = max(0.0, (output_start_pts - segment_start_pts) / PTS_TIME_BASE)
    output_duration_s = max(
        0.001,
        (output_end_pts - output_start_pts) / PTS_TIME_BASE,
    )
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-ss",
        _seconds_arg(trim_start_s),
        "-t",
        _seconds_arg(output_duration_s),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(video_path),
    ]
    runner = command_runner or _default_command_runner
    executed_command = command
    try:
        runner(command, sink_dir / "rolling_cache_ffmpeg.log")
    except RollingCacheError as exc:
        retry_command = _concat_copy_retry_command(
            ffmpeg_bin=ffmpeg_bin,
            concat_path=concat_path,
            trim_start_s=trim_start_s,
            output_duration_s=output_duration_s,
            video_path=video_path,
        )
        video_path.unlink(missing_ok=True)
        try:
            runner(retry_command, sink_dir / "rolling_cache_ffmpeg_retry.log")
        except RollingCacheError as retry_exc:
            raise RollingCacheError(
                f"{retry_exc}; initial_attempt={_compact_error_text(str(exc))}"
            ) from retry_exc
        executed_command = retry_command
    min_output_bytes = 1 if command_runner is not None else MIN_VALID_VIDEO_BYTES
    if not video_path.is_file() or video_path.stat().st_size < min_output_bytes:
        retry_command = _concat_copy_retry_command(
            ffmpeg_bin=ffmpeg_bin,
            concat_path=concat_path,
            trim_start_s=trim_start_s,
            output_duration_s=output_duration_s,
            video_path=video_path,
        )
        video_path.unlink(missing_ok=True)
        runner(retry_command, sink_dir / "rolling_cache_ffmpeg_retry.log")
        executed_command = retry_command
    if not video_path.is_file() or video_path.stat().st_size < min_output_bytes:
        raise RollingCacheError("rolling_cache_ffmpeg_output_missing")

    observed_duration_s: float | None = None
    observed_frame_rate_fps: float | None = None
    duration_repair_attempted = False
    duration_repair_status = "not_checked"
    probe = duration_probe or (
        _probe_video_duration_seconds if command_runner is None else None
    )
    fps_probe = frame_rate_probe or (
        _probe_video_frame_rate if command_runner is None else None
    )
    if probe is not None:
        observed_duration_s = probe(video_path)
        observed_frame_rate_fps = fps_probe(video_path) if fps_probe is not None else None
        duration_repair_status = "copy_duration_ok"
        copy_duration_short = _duration_below_expected(
            observed_duration_s, output_duration_s
        )
        copy_frame_rate_low = _frame_rate_below_expected(observed_frame_rate_fps)
        if copy_duration_short or copy_frame_rate_low:
            duration_repair_attempted = True
            duration_repair_status = (
                "transcode_retry_duration_short"
                if copy_duration_short
                else "transcode_retry_frame_rate_low"
            )
            retry_command = _concat_transcode_retry_command(
                ffmpeg_bin=ffmpeg_bin,
                concat_path=concat_path,
                trim_start_s=trim_start_s,
                output_duration_s=output_duration_s,
                video_path=video_path,
            )
            video_path.unlink(missing_ok=True)
            runner(retry_command, sink_dir / "rolling_cache_ffmpeg_transcode_retry.log")
            executed_command = retry_command
            if not video_path.is_file() or video_path.stat().st_size < min_output_bytes:
                raise RollingCacheError("rolling_cache_transcode_output_missing")
            observed_duration_s = probe(video_path)
            observed_frame_rate_fps = (
                fps_probe(video_path) if fps_probe is not None else None
            )
            if _duration_below_expected(observed_duration_s, output_duration_s):
                duration_repair_status = "duration_still_short"
                # Endpoint-only metadata can look complete while an
                # async-finalized middle fragment is not indexed yet. Treat a
                # still-short decode as retryable coverage, not a terminal
                # ffmpeg failure; the next attempt refreshes the segment index.
                raise RollingCacheCoverageMiss(
                    "rolling_cache_output_duration_short:"
                    f"expected_s={output_duration_s:.3f}:"
                    f"observed_s={observed_duration_s if observed_duration_s is not None else 'unknown'}"
                )
            if _frame_rate_below_expected(observed_frame_rate_fps):
                duration_repair_status = "frame_rate_still_low"
                raise RollingCacheCoverageMiss(
                    "rolling_cache_output_frame_rate_low:"
                    f"minimum_fps={_minimum_output_frame_rate_fps():.3f}:"
                    f"observed_fps={observed_frame_rate_fps if observed_frame_rate_fps is not None else 'unknown'}"
                )
            duration_repair_status = "transcode_retry_duration_ok"

    materialization_ms = int((time.monotonic() - started) * 1000)
    output_identity = _file_identity(video_path)
    immutable_probe = {
        "schema_version": "rolling-cache-immutable-probe-v1",
        "status": "ready" if observed_duration_s is not None else "unavailable",
        "duration_s": observed_duration_s,
        "identity": output_identity,
        "observed_at_epoch_ns": time.time_ns(),
    }
    metadata_publish_started = time.monotonic()
    label_doc = {
        **{str(k): str(v) for k, v in (labels or {}).items() if v is not None},
        "event_id": event_id,
        "source_id": source_id,
        "runtime_epoch_id": epoch,
        "requested_start_pts": str(original_requested_start_pts),
        "requested_end_pts": str(original_requested_end_pts),
        "effective_start_pts": str(original_requested_start_pts),
        "actual_start_pts": str(original_requested_start_pts),
        "actual_end_pts": str(original_requested_end_pts),
        "rolling_cache_mux_requested_start_pts": str(requested_start_pts),
        "rolling_cache_mux_requested_end_pts": str(requested_end_pts),
        "rolling_cache_mux_event_pts": str(event_mux_pts or ""),
        "pre_window_truncated": "true" if start_gap_ns > coverage_slack_ns else "false",
        "post_window_truncated": "true" if end_gap_ns > coverage_slack_ns else "false",
        "rolling_cache_enabled": "true",
        "materialization_mode": "rolling_cache_copy",
        "canonical_clip": "true" if canonical_clip else "false",
        "time_domain_crop_applied": "true",
        "requested_duration_s": _seconds_arg(
            (original_requested_end_pts - original_requested_start_pts)
            / PTS_TIME_BASE
        ),
    }
    metadata = {
        "event_id": event_id,
        "job_id": f"rolling-cache-event-{event_id}",
        "source_id": source_id,
        "stored_stream_id": source_id,
        "resulting_stream_id": f"rolling-cache-event-{event_id}",
        "runtime_epoch_id": epoch,
        "labels": label_doc,
        "configuration": {
            "labels": label_doc,
            "stored_stream_id": source_id,
            "resulting_stream_id": f"rolling-cache-event-{event_id}",
        },
        "rolling_cache": {
            "schema_version": "rolling-cache-materialized-v1",
            "materialization_mode": "rolling_cache_copy",
            "canonical_clip": canonical_clip,
            "requested_start_pts": original_requested_start_pts,
            "requested_end_pts": original_requested_end_pts,
            "requested_duration_s": (
                original_requested_end_pts - original_requested_start_pts
            ) / PTS_TIME_BASE,
            "actual_start_pts": original_requested_start_pts,
            "actual_end_pts": original_requested_end_pts,
            "mux_requested_start_pts": requested_start_pts,
            "mux_requested_end_pts": requested_end_pts,
            "mux_event_pts": event_mux_pts,
            "mux_actual_start_pts": output_start_pts,
            "mux_actual_end_pts": output_end_pts,
            "segment_start_pts": segment_start_pts,
            "segment_end_pts": segment_end_pts,
            "trim_start_s": trim_start_s,
            "output_duration_s": output_duration_s,
            "time_domain_crop_applied": True,
            "start_gap_ns": start_gap_ns,
            "end_gap_ns": end_gap_ns,
            "coverage_slack_ns": coverage_slack_ns,
            "coverage_status": coverage_status,
            "pre_window_truncated": start_gap_ns > coverage_slack_ns,
            "post_window_truncated": end_gap_ns > coverage_slack_ns,
            "segment_ids": [segment.segment_id for segment in selected],
            "segment_paths": [str(segment.video_path) for segment in selected],
            "selected_frame_count": len(selected_rows),
            "materialization_ms": materialization_ms,
            "probed_output_duration_s": observed_duration_s,
            "probed_output_frame_rate_fps": observed_frame_rate_fps,
            "immutable_probe": immutable_probe,
            "duration_repair_attempted": duration_repair_attempted,
            "duration_repair_status": duration_repair_status,
        },
        "frames": selected_rows,
    }
    _write_json(metadata_path, metadata)
    metadata_publish_ms = int(
        round((time.monotonic() - metadata_publish_started) * 1000)
    )
    metadata_bytes = int(metadata_path.stat().st_size)
    return RollingMaterializationResult(
        sink_dir=sink_dir,
        video_path=video_path,
        metadata_path=metadata_path,
        metadata_payload=metadata,
        segment_ids=tuple(segment.segment_id for segment in selected),
        requested_start_pts=original_requested_start_pts,
        requested_end_pts=original_requested_end_pts,
        actual_start_pts=original_requested_start_pts,
        actual_end_pts=original_requested_end_pts,
        selected_frame_count=len(selected_rows),
        materialization_ms=materialization_ms,
        metadata_publish_ms=metadata_publish_ms,
        metadata_bytes=metadata_bytes,
        ffmpeg_command=tuple(executed_command),
        immutable_probe=immutable_probe,
    )


def overlapping_segments(
    segments: Iterable[RollingSegment],
    *,
    requested_start_pts: int,
    requested_end_pts: int,
) -> list[RollingSegment]:
    """Return the immutable segment subset intersecting a requested window."""

    return [
        segment
        for segment in segments
        if segment.last_pts >= requested_start_pts
        and segment.first_pts <= requested_end_pts
    ]


def _concat_copy_retry_command(
    *,
    ffmpeg_bin: str,
    concat_path: Path,
    trim_start_s: float,
    output_duration_s: float,
    video_path: Path,
) -> list[str]:
    """Retry copy-remux with input-side seek for boundary-sensitive MOV cuts."""

    return [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-ss",
        _seconds_arg(trim_start_s),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-t",
        _seconds_arg(output_duration_s),
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(video_path),
    ]


def _concat_transcode_retry_command(
    *,
    ffmpeg_bin: str,
    concat_path: Path,
    trim_start_s: float,
    output_duration_s: float,
    video_path: Path,
) -> list[str]:
    """Retry with decode/encode when copy-remux cuts short at segment boundaries."""

    return [
        ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        *_ffmpeg_thread_args(),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-vf",
        (
            f"trim=start={trim_start_s:.9f}:duration={output_duration_s:.9f},"
            "setpts=PTS-STARTPTS"
        ),
        "-t",
        f"{output_duration_s:.9f}",
        "-an",
        "-c:v",
        "libx264",
        *_ffmpeg_x264_tuning_args(),
        "-pix_fmt",
        "yuv420p",
        str(video_path),
    ]


def _probe_video_duration_seconds(path: Path) -> float | None:
    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    completed = run_managed_subprocess(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        value = float(completed.stdout.strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _probe_video_frame_rate(path: Path) -> float | None:
    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    completed = run_managed_subprocess(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        value = float(Fraction(completed.stdout.strip()))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def _duration_below_expected(
    observed_duration_s: float | None,
    expected_duration_s: float,
) -> bool:
    if observed_duration_s is None:
        return False
    return observed_duration_s < max(0.0, expected_duration_s - DEFAULT_DURATION_REPAIR_SLACK_S)


def _minimum_output_frame_rate_fps() -> float:
    try:
        return max(float(os.getenv("ROLLING_CACHE_MIN_OUTPUT_FPS", "23.5")), 0.0)
    except ValueError:
        return 23.5


def _frame_rate_below_expected(observed_frame_rate_fps: float | None) -> bool:
    minimum = _minimum_output_frame_rate_fps()
    return (
        observed_frame_rate_fps is not None
        and minimum > 0
        and observed_frame_rate_fps < minimum
    )


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


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _seconds_arg(value: float) -> str:
    return f"{max(0.0, value):.6f}".rstrip("0").rstrip(".") or "0"


def _candidate_source_roots(
    root: Path,
    *,
    source_id: str,
    runtime_epoch_id: str,
) -> list[Path]:
    roots: list[Path] = []
    bases = [root]
    if root.name != "midterm":
        bases.append(root / "midterm")
    for base in bases:
        if runtime_epoch_id:
            roots.append(base / "epochs" / runtime_epoch_id / source_id)
            roots.extend(
                base.glob(f"epochs/{glob_escape(runtime_epoch_id)}/{glob_escape(source_id)}*")
            )
            roots.append(base / source_id)
            roots.extend(base.glob(f"{glob_escape(source_id)}*"))
            continue
        roots.extend(base.glob(f"epochs/*/{source_id}"))
        roots.extend(base.glob(f"epochs/*/{glob_escape(source_id)}*"))
        roots.append(base / source_id)
        roots.extend(base.glob(f"{glob_escape(source_id)}*"))
    unique: list[Path] = []
    seen: set[Path] = set()
    for item in roots:
        resolved = item.resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            unique.append(item)
    return unique


def _segment_from_metadata(
    metadata_path: Path,
    *,
    source_id: str,
    runtime_epoch_id: str,
) -> RollingSegment | None:
    try:
        rows = load_native_metadata(metadata_path)
    except Exception:
        return None
    pts_values = [pts for pts in (_row_pts(row) for row in rows) if pts is not None]
    if not pts_values:
        return None
    source_pts_values = [
        pts for pts in (_source_row_pts(row) for row in rows) if pts is not None
    ]
    video_path = _find_video(metadata_path.parent)
    if video_path is None:
        return None
    epoch = runtime_epoch_id or _runtime_epoch_from_path(metadata_path)
    stat = video_path.stat()
    return RollingSegment(
        segment_id=_segment_id(metadata_path.parent),
        source_id=source_id,
        runtime_epoch_id=epoch,
        directory=metadata_path.parent,
        video_path=video_path,
        metadata_path=metadata_path,
        first_pts=int(min(pts_values)),
        last_pts=int(max(pts_values)),
        frame_count=len(pts_values),
        size_bytes=int(stat.st_size),
        source_first_pts=(
            int(min(source_pts_values)) if source_pts_values else None
        ),
        source_last_pts=(
            int(max(source_pts_values)) if source_pts_values else None
        ),
    )


def _find_video(directory: Path) -> Path | None:
    for name in VIDEO_NAMES:
        candidate = directory / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    for pattern in ("*.mov", "*.mp4", "*.mkv", "*.webm"):
        for candidate in directory.glob(pattern):
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    return None


def _select_rows(
    segments: Iterable[RollingSegment],
    start_pts: int,
    end_pts: int,
    *,
    row_loader: RowLoader | None = None,
) -> list[dict[str, Any]]:
    frame_rows: list[tuple[int, int, dict[str, Any]]] = []
    auxiliary_rows: list[dict[str, Any]] = []
    sequence = 0
    for segment in segments:
        segment_rows = (
            row_loader(segment)
            if row_loader is not None
            else load_native_metadata(segment.metadata_path)
        )
        for row in segment_rows:
            pts = _row_pts(row)
            if pts is None:
                auxiliary_rows.append(row)
            elif start_pts <= pts <= end_pts:
                frame_rows.append((pts, sequence, row))
            sequence += 1
    strictly_increasing: list[dict[str, Any]] = []
    last_pts: int | None = None
    for pts, _sequence, row in sorted(
        frame_rows,
        key=lambda item: (item[0], item[1]),
    ):
        if last_pts is not None and pts <= last_pts:
            continue
        strictly_increasing.append(row)
        last_pts = pts
    return strictly_increasing + auxiliary_rows


def _map_source_window_to_mux(
    segments: Iterable[RollingSegment],
    *,
    requested_start_pts: int,
    requested_end_pts: int,
    labels: dict[str, Any],
    row_loader: RowLoader | None,
) -> tuple[int, int, int] | None:
    """Anchor a wall-clock event window onto the stable rolling mux clock."""

    event_source_pts = _int_or_none(labels.get("event_frame_pts"))
    if event_source_pts is None:
        return None
    event_uuid = str(
        labels.get("event_frame_uuid") or labels.get("frame_uuid") or ""
    )
    segment_list = list(segments)
    candidate_segments = _source_clock_candidate_segments(
        segment_list,
        event_source_pts=event_source_pts,
    )
    best_delta: int | None = None
    best_mux_pts: int | None = None
    for segment in candidate_segments:
        rows = (
            row_loader(segment)
            if row_loader is not None
            else load_native_metadata(segment.metadata_path)
        )
        for row in rows:
            mux_pts = _mux_row_pts(row)
            source_pts = _source_row_pts(row)
            if mux_pts is None or source_pts is None:
                continue
            row_uuid = str(row.get("uuid") or row.get("frame_uuid") or "")
            if event_uuid and row_uuid == event_uuid:
                best_delta = 0
                best_mux_pts = mux_pts
                break
            delta = abs(source_pts - event_source_pts)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_mux_pts = mux_pts
        if best_delta == 0:
            break
    if best_mux_pts is None:
        return None
    pre_ns = max(0, event_source_pts - requested_start_pts)
    post_ns = max(0, requested_end_pts - event_source_pts)
    return best_mux_pts - pre_ns, best_mux_pts + post_ns, best_mux_pts


def _source_clock_candidate_segments(
    segments: Iterable[RollingSegment],
    *,
    event_source_pts: int,
    limit: int = 3,
) -> list[RollingSegment]:
    """Return the nearest compact source-clock ranges for one event.

    Rolling mux PTS and source/event PTS are intentionally separate clock
    domains.  Every indexed segment already has to parse its metadata once;
    retaining only the min/max source PTS makes later event mapping O(logical
    candidates) instead of O(all segments in the retention window).

    Legacy segments without source bounds retain the previous full-scan
    behavior.  This is a compatibility path, not the production hot path.
    """

    segment_list = list(segments)
    bounded: list[tuple[int, int, int, RollingSegment]] = []
    for sequence, segment in enumerate(segment_list):
        first = _int_or_none(getattr(segment, "source_first_pts", None))
        last = _int_or_none(getattr(segment, "source_last_pts", None))
        if first is None or last is None:
            continue
        low, high = sorted((first, last))
        if low <= event_source_pts <= high:
            distance = 0
        else:
            distance = min(abs(event_source_pts - low), abs(event_source_pts - high))
        bounded.append((distance, low, sequence, segment))
    if not bounded:
        return segment_list
    bounded.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in bounded[: max(1, int(limit))]]


def _validate_selected_row_coverage(
    rows: Iterable[dict[str, Any]],
    *,
    requested_start_pts: int,
    requested_end_pts: int,
    coverage_slack_ns: int,
    allow_partial: bool,
) -> None:
    """Reject endpoint-complete segments whose frame rows contain a hole."""

    if allow_partial:
        return
    pts_values = sorted(
        pts for pts in (_row_pts(row) for row in rows) if pts is not None
    )
    if not pts_values:
        raise RollingCacheCoverageMiss("rolling_cache_selected_rows_missing")
    pre_gap_ns = max(0, pts_values[0] - requested_start_pts)
    post_gap_ns = max(0, requested_end_pts - pts_values[-1])
    if pre_gap_ns > coverage_slack_ns or post_gap_ns > coverage_slack_ns:
        raise RollingCacheCoverageMiss(
            "rolling_cache_selected_rows_endpoint_gap:"
            f"pre_gap_ns={pre_gap_ns}:post_gap_ns={post_gap_ns}"
        )
    frame_gaps_ns = [
        current - previous for previous, current in zip(pts_values, pts_values[1:])
    ]
    largest_gap_ns = max(frame_gaps_ns, default=0)
    sorted_gaps = sorted(gap for gap in frame_gaps_ns if gap > 0)
    median_gap_ns = (
        sorted_gaps[len(sorted_gaps) // 2] if sorted_gaps else 0
    )
    # Source adapters intentionally stamp RTSP arrivals in the wall-clock
    # domain. Under concurrent scheduling those rows can arrive in a burst
    # after a sub-second pause even though the encoded 24fps stream and frame
    # rows are complete. Endpoint coverage remains strict; this internal-only
    # tolerance distinguishes that scheduling jitter from a missing ~4s
    # rolling fragment.
    internal_gap_limit_ns = max(
        coverage_slack_ns,
        DEFAULT_INTERNAL_ROW_GAP_SLACK_NS,
        median_gap_ns * 8,
    )
    if largest_gap_ns > internal_gap_limit_ns:
        raise RollingCacheCoverageMiss(
            "rolling_cache_selected_rows_internal_gap:"
            f"internal_gap_ns={largest_gap_ns}:"
            f"gap_limit_ns={internal_gap_limit_ns}"
        )


def _file_identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _row_pts(row: dict[str, Any]) -> int | None:
    value = row.get("rolling_cache_mux_pts")
    if value is None:
        value = row.get("pts")
    if value is None:
        value = row.get("frame_pts")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mux_row_pts(row: dict[str, Any]) -> int | None:
    value = row.get("rolling_cache_mux_pts")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _source_row_pts(row: dict[str, Any]) -> int | None:
    value = row.get("pts")
    if value is None:
        value = row.get("frame_pts")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _runtime_epoch_from_path(path: Path) -> str:
    parts = path.parts
    for index, part in enumerate(parts[:-1]):
        if part == "epochs" and index + 1 < len(parts):
            return parts[index + 1]
    return ""


def _segment_id(directory: Path) -> str:
    return directory.name or str(abs(hash(str(directory))))


def _is_materialized_intermediate(path: Path) -> bool:
    return "materialized" in path.parts


def _concat_escape(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def _default_command_runner(command: list[str], log_path: Path) -> None:
    with log_path.open("wb") as log_fh:
        completed = run_managed_subprocess(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if completed.returncode != 0:
        tail = _read_log_tail(log_path)
        suffix = f" tail={tail}" if tail else ""
        raise RollingCacheError(f"ffmpeg_failed:{completed.returncode}{suffix}")


def _read_log_tail(path: Path, *, max_bytes: int = 2048) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = fh.read()
    except OSError:
        return ""
    return _compact_error_text(data.decode("utf-8", errors="replace"))


def _compact_error_text(value: str, *, max_chars: int = 700) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[-max_chars:]


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
