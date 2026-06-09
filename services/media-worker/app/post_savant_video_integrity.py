"""Raw video integrity gate for post-Savant evidence bundles."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


INTEGRITY_PASS = "pass"
INTEGRITY_WARNING = "warning"
INTEGRITY_FAIL = "fail"
DEFAULT_LARGE_PACKET_DURATION_THRESHOLD_S = 1.0
DEFAULT_FIRST_KEYFRAME_GRACE_S = 0.5
DEFAULT_REQUESTED_DURATION_SLACK_S = 0.75


def inspect_video_integrity(
    video_path: Path,
    *,
    decode_log_path: Path | None = None,
    requested_duration_s: float | None = None,
    decoded_video_frame_count: int | None = None,
    sidecar_frame_count: int | None = None,
    trim_occurred: bool = False,
    time_domain_crop_applied: bool = False,
    large_packet_duration_threshold_s: float = DEFAULT_LARGE_PACKET_DURATION_THRESHOLD_S,
    first_keyframe_grace_s: float = DEFAULT_FIRST_KEYFRAME_GRACE_S,
    requested_duration_slack_s: float = DEFAULT_REQUESTED_DURATION_SLACK_S,
) -> dict[str, Any]:
    """Inspect *video_path* and return the production gate result.

    The gate intentionally combines decode output with packet/keyframe/timeline
    checks. A clean ffmpeg decode log alone is not sufficient for production
    evidence because event-style Replay can emit visually corrupt but decodable
    streams when the first independent decode point is far from the clip start.
    """

    video_path = video_path.resolve(strict=False)
    container = _ffprobe_container(video_path)
    frames = _ffprobe_frames(video_path)
    packets = _ffprobe_packets(video_path)
    decode = _ffmpeg_decode(video_path, decode_log_path)
    stats = _summarize_video(
        container=container,
        frames=frames,
        packets=packets,
        decode=decode,
        requested_duration_s=requested_duration_s,
        decoded_video_frame_count=decoded_video_frame_count,
        sidecar_frame_count=sidecar_frame_count,
        trim_occurred=trim_occurred,
        time_domain_crop_applied=time_domain_crop_applied,
    )
    return evaluate_video_integrity(
        stats,
        large_packet_duration_threshold_s=large_packet_duration_threshold_s,
        first_keyframe_grace_s=first_keyframe_grace_s,
        requested_duration_slack_s=requested_duration_slack_s,
    )


def evaluate_video_integrity(
    stats: dict[str, Any],
    *,
    large_packet_duration_threshold_s: float = DEFAULT_LARGE_PACKET_DURATION_THRESHOLD_S,
    first_keyframe_grace_s: float = DEFAULT_FIRST_KEYFRAME_GRACE_S,
    requested_duration_slack_s: float = DEFAULT_REQUESTED_DURATION_SLACK_S,
) -> dict[str, Any]:
    """Evaluate pre-collected video stats as a production gate."""

    failures: list[str] = []
    warnings: list[str] = []
    decoded = _int_or_none(stats.get("decoded_frame_count"))
    duration = _float_or_none(stats.get("duration_s"))
    sidecar = _int_or_none(stats.get("sidecar_frame_count"))
    requested_duration = _float_or_none(stats.get("requested_duration_s"))
    decode_errors = int(_int_or_none(stats.get("decode_error_count")) or 0)
    first_frame_keyframe = stats.get("first_frame_keyframe")
    first_keyframe_pts = _float_or_none(stats.get("first_keyframe_pts_time"))
    max_packet_duration = _float_or_none(stats.get("max_packet_duration_s"))
    time_domain_crop_applied = bool(stats.get("time_domain_crop_applied"))

    if decode_errors > 0:
        failures.append("decode_error_count_gt_zero")
    if decoded is None or decoded <= 0:
        failures.append("decoded_frame_count_unavailable_or_zero")
    if duration is None or duration <= 0:
        failures.append("duration_unavailable_or_zero")
    if first_frame_keyframe is False:
        if first_keyframe_pts is None or first_keyframe_pts > first_keyframe_grace_s:
            failures.append("first_frame_not_keyframe_and_first_keyframe_far_from_start")
        else:
            warnings.append("first_frame_not_keyframe_but_keyframe_near_start")
    if stats.get("pts_monotonic") is False:
        failures.append("pts_not_monotonic")
    if stats.get("dts_monotonic") is False:
        failures.append("dts_not_monotonic")
    if stats.get("pts_large_jump_detected") is True:
        failures.append("pts_large_jump_detected")
    if stats.get("dts_large_jump_detected") is True:
        failures.append("dts_large_jump_detected")
    if (
        max_packet_duration is not None
        and max_packet_duration > large_packet_duration_threshold_s
    ):
        failures.append("max_packet_duration_exceeds_threshold")
    if (
        requested_duration is not None
        and requested_duration > 0
        and duration is not None
        and abs(duration - requested_duration) > requested_duration_slack_s
        and not bool(stats.get("over_exported"))
    ):
        failures.append("duration_differs_from_requested_window")
    if sidecar is not None and decoded is not None and sidecar > decoded:
        failures.append("sidecar_frame_count_exceeds_decoded_frame_count")
    if bool(stats.get("trim_occurred")) and not time_domain_crop_applied:
        failures.append("trim_occurred_without_declared_time_domain_crop")

    integrity_status = INTEGRITY_FAIL if failures else INTEGRITY_WARNING if warnings else INTEGRITY_PASS
    result = {
        **stats,
        "integrity_status": integrity_status,
        "production_gate_passed": integrity_status == INTEGRITY_PASS,
        "failure_reasons": failures,
        "warning_reasons": warnings,
        "large_packet_duration_threshold_s": large_packet_duration_threshold_s,
        "first_keyframe_grace_s": first_keyframe_grace_s,
        "requested_duration_slack_s": requested_duration_slack_s,
    }
    return result


def _summarize_video(
    *,
    container: dict[str, Any],
    frames: dict[str, Any],
    packets: dict[str, Any],
    decode: dict[str, Any],
    requested_duration_s: float | None,
    decoded_video_frame_count: int | None,
    sidecar_frame_count: int | None,
    trim_occurred: bool,
    time_domain_crop_applied: bool,
) -> dict[str, Any]:
    stream = _first_stream(container)
    fmt = container.get("format") if isinstance(container.get("format"), dict) else {}
    frame_rows = frames.get("frames") if isinstance(frames.get("frames"), list) else []
    packet_rows = packets.get("packets") if isinstance(packets.get("packets"), list) else []
    packet_pts_values = [
        _float_or_none(packet.get("pts_time"))
        for packet in packet_rows
        if _float_or_none(packet.get("pts_time")) is not None
    ]
    dts_values = [
        _float_or_none(packet.get("dts_time"))
        for packet in packet_rows
        if _float_or_none(packet.get("dts_time")) is not None
    ]
    packet_durations = [
        _float_or_none(packet.get("duration_time"))
        for packet in packet_rows
        if _float_or_none(packet.get("duration_time")) is not None
    ]
    keyframes = [
        frame for frame in frame_rows
        if _int_or_none(frame.get("key_frame")) == 1
    ]
    frame_pts_values = [
        _frame_pts_time(frame)
        for frame in frame_rows
        if _frame_pts_time(frame) is not None
    ]
    duration = _first_float(fmt.get("duration"), stream.get("duration"))
    decoded = (
        decoded_video_frame_count
        if decoded_video_frame_count is not None
        else _int_or_none(stream.get("nb_read_frames"))
        or _int_or_none(stream.get("nb_frames"))
        or len(frame_rows)
    )
    return {
        "duration_s": duration,
        "decoded_frame_count": decoded,
        "avg_frame_rate": stream.get("avg_frame_rate"),
        "r_frame_rate": stream.get("r_frame_rate"),
        "nb_frames": _int_or_none(stream.get("nb_frames")),
        "nb_read_frames": _int_or_none(stream.get("nb_read_frames")),
        "codec_name": stream.get("codec_name"),
        "width": _int_or_none(stream.get("width")),
        "height": _int_or_none(stream.get("height")),
        "keyframe_count": len(keyframes),
        "first_frame_keyframe": (
            _int_or_none(frame_rows[0].get("key_frame")) == 1
            if frame_rows
            else None
        ),
        "first_keyframe_pts_time": _frame_pts_time(keyframes[0]) if keyframes else None,
        "pts_monotonic": _monotonic(frame_pts_values),
        "packet_pts_monotonic": _monotonic(packet_pts_values),
        "dts_monotonic": _monotonic(dts_values),
        "pts_large_jump_detected": bool(_jump_samples(frame_pts_values)),
        "packet_pts_large_jump_detected": bool(_jump_samples(packet_pts_values)),
        "dts_large_jump_detected": bool(_jump_samples(dts_values)),
        "pts_large_jump_samples": _jump_samples(frame_pts_values),
        "packet_pts_large_jump_samples": _jump_samples(packet_pts_values),
        "dts_large_jump_samples": _jump_samples(dts_values),
        "max_packet_duration_s": max(packet_durations) if packet_durations else None,
        "packet_duration_distribution": _duration_distribution(packet_durations),
        "decode_error_count": int(decode.get("decode_error_count") or 0),
        "decode_error_log_path": decode.get("decode_error_log_path"),
        "decode_error_sample": decode.get("decode_error_sample") or [],
        "decode_signal_counts": decode.get("decode_signal_counts") or {},
        "requested_duration_s": requested_duration_s,
        "sidecar_frame_count": sidecar_frame_count,
        "trim_occurred": bool(trim_occurred),
        "time_domain_crop_applied": bool(time_domain_crop_applied),
    }


def _ffprobe_container(video_path: Path) -> dict[str, Any]:
    if shutil.which("ffprobe") is None:
        return {"error": "ffprobe_missing", "video_path": str(video_path)}
    return _run_json_command([
        "ffprobe",
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        (
            "format=duration,size,bit_rate,format_name,start_time:"
            "stream=index,codec_name,codec_type,width,height,r_frame_rate,"
            "avg_frame_rate,time_base,start_time,duration,nb_frames,"
            "nb_read_frames,bit_rate"
        ),
        "-of",
        "json",
        str(video_path),
    ], video_path)


def _ffprobe_frames(video_path: Path) -> dict[str, Any]:
    if shutil.which("ffprobe") is None:
        return {"error": "ffprobe_missing", "frames": []}
    return _run_json_command([
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        (
            "frame=key_frame,pict_type,best_effort_timestamp_time,"
            "pkt_dts_time,pkt_pts_time,pkt_duration_time"
        ),
        "-of",
        "json",
        str(video_path),
    ], video_path)


def _ffprobe_packets(video_path: Path) -> dict[str, Any]:
    if shutil.which("ffprobe") is None:
        return {"error": "ffprobe_missing", "packets": []}
    return _run_json_command([
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,dts_time,duration_time,flags,size,pos",
        "-of",
        "json",
        str(video_path),
    ], video_path)


def _run_json_command(command: list[str], video_path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        return {
            "error": "command_failed",
            "video_path": str(video_path),
            "stderr": getattr(exc, "stderr", "")[-1000:],
        }
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"json_decode_failed:{exc}", "video_path": str(video_path)}
    data["video_path"] = str(video_path)
    return data


def _ffmpeg_decode(video_path: Path, decode_log_path: Path | None) -> dict[str, Any]:
    if shutil.which("ffmpeg") is None:
        if decode_log_path is not None:
            decode_log_path.write_text("ffmpeg_missing\n", encoding="utf-8")
        return {
            "decode_error_count": 1,
            "decode_error_log_path": str(decode_log_path) if decode_log_path else None,
            "decode_error_sample": ["ffmpeg_missing"],
            "decode_signal_counts": {"ffmpeg_missing": 1},
        }
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "warning",
        "-i",
        str(video_path),
        "-f",
        "null",
        "-",
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr = completed.stderr or ""
    if decode_log_path is not None:
        decode_log_path.parent.mkdir(parents=True, exist_ok=True)
        decode_log_path.write_text(stderr, encoding="utf-8")
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if completed.returncode != 0 and not lines:
        lines = [f"ffmpeg_returncode={completed.returncode}"]
    return {
        "decode_error_count": len(lines),
        "decode_error_log_path": str(decode_log_path) if decode_log_path else None,
        "decode_error_sample": lines[:20],
        "decode_signal_counts": _categorize_decode_log(lines),
    }


def _categorize_decode_log(lines: list[str]) -> dict[str, int]:
    categories: Counter[str] = Counter()
    for line in lines:
        lower = line.lower()
        if "missing reference" in lower or "reference picture missing" in lower:
            categories["missing_reference"] += 1
        if "non-existing pps" in lower or "sps" in lower or "pps" in lower:
            categories["non_existing_pps_sps"] += 1
        if "invalid nal" in lower or "nal unit" in lower:
            categories["invalid_nal"] += 1
        if "corrupt" in lower or "error while decoding" in lower:
            categories["corrupt_packet_or_frame"] += 1
        if "concealing" in lower:
            categories["concealing_errors"] += 1
        if "non monoton" in lower or "non-monoton" in lower:
            categories["non_monotonic_timestamp"] += 1
        if not categories:
            categories["other"] += 1
    return dict(categories)


def _first_stream(container: dict[str, Any]) -> dict[str, Any]:
    streams = container.get("streams")
    return streams[0] if isinstance(streams, list) and streams else {}


def _first_float(*values: Any) -> float | None:
    for value in values:
        parsed = _float_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, "", "N/A"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, "", "N/A"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _frame_pts_time(frame: dict[str, Any]) -> float | None:
    return _first_float(
        frame.get("best_effort_timestamp_time"),
        frame.get("pkt_pts_time"),
        frame.get("pkt_dts_time"),
    )


def _monotonic(values: list[float]) -> bool | None:
    if len(values) < 2:
        return None
    return all(values[index + 1] >= values[index] for index in range(len(values) - 1))


def _jump_samples(values: list[float]) -> list[dict[str, Any]]:
    if len(values) < 3:
        return []
    deltas = [values[index + 1] - values[index] for index in range(len(values) - 1)]
    positive = [delta for delta in deltas if delta > 0]
    if not positive:
        return []
    median_delta = sorted(positive)[len(positive) // 2]
    threshold = max(median_delta * 5, DEFAULT_LARGE_PACKET_DURATION_THRESHOLD_S)
    samples: list[dict[str, Any]] = []
    for index, delta in enumerate(deltas):
        if delta < 0 or delta > threshold:
            samples.append({
                "index": index,
                "from": values[index],
                "to": values[index + 1],
                "delta": delta,
                "median_delta": median_delta,
            })
        if len(samples) >= 20:
            break
    return samples


def _duration_distribution(values: list[float]) -> dict[str, int]:
    buckets: Counter[str] = Counter()
    for value in values:
        buckets[f"{value:.6f}"] += 1
    return dict(buckets.most_common(20))
