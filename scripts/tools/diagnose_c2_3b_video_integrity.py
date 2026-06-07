#!/usr/bin/env python3
"""Diagnose C2.3B event-style Replay raw clip video integrity.

This tool is intentionally offline. It only reads existing evidence bundles,
sink output, and prior diagnosis artifacts. It does not call Redis,
PostgreSQL, FastAPI, Docker, or Replay, and it never modifies the original
evidence files.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import shutil
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


RAW_CLIP_CANDIDATES = ("raw_clip.mov", "raw_clip.mp4", "video.mov", "video.mp4")
SIDECAR_FILE = "annotations.frame_cache.identity.jsonl"
SUMMARY_FILE = "summary.json"
SINK_METADATA_FILE = "sink_metadata.json"

RESULT_FAIL = "FAIL_C2_3B_V_REPLAY_VIDEO_INTEGRITY_BLOCKED"
RESULT_PARTIAL_COMPAT = "PARTIAL_C2_3B_V_PLAYER_OR_REMUX_COMPATIBILITY_SUSPECTED"
RESULT_PARTIAL_DATA = "PARTIAL_C2_3B_V_INSUFFICIENT_VIDEO_DIAGNOSTIC_DATA"
RESULT_PASS_DIAGNOSIS = "PASS_C2_3B_V_VIDEO_INTEGRITY_DIAGNOSIS_READY"

FAILED_REPLAY_PAYLOAD_CANDIDATES = (
    Path("/tmp/c2_3b_frame125_20260607T180146-replay-job.json"),
)

FAILED_SAMPLE_SECONDS = (0, 1, 3, 5, 8, 10, 15, 20, 30, 40)
SUCCESS_SAMPLE_SECONDS = (0, 1, 3, 5, 8, 10)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose C2.3B event-style Replay video integrity."
    )
    parser.add_argument("--success-bundle", required=True, type=Path)
    parser.add_argument("--failed-bundle", required=True, type=Path)
    parser.add_argument("--failed-sink-output", required=True, type=Path)
    parser.add_argument("--time-domain-diagnosis", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--failed-replay-payload", type=Path, default=None)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    output_dir = args.output_dir.resolve(strict=False)
    if output_dir.exists():
        if not args.overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        if output_dir.is_dir():
            shutil.rmtree(output_dir)
        else:
            output_dir.unlink()
    output_dir.mkdir(parents=True)
    debug_dir = output_dir / "debug"
    debug_dir.mkdir()
    remux_dir = output_dir / "diagnostic_remux"
    remux_dir.mkdir()
    frames_dir = output_dir / "frames"
    frames_dir.mkdir()

    tool_status = {
        "ffprobe_available": shutil.which("ffprobe") is not None,
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
    }

    success = inspect_bundle(args.success_bundle, role="success")
    failed = inspect_bundle(args.failed_bundle, role="failed")
    failed_sink = inspect_sink_output(args.failed_sink_output)
    time_domain = read_json_if_exists(args.time_domain_diagnosis)
    replay_payload = load_replay_payload(args.failed_replay_payload, failed)

    write_json(output_dir / "raw_ffprobe_success.json", success["raw_ffprobe"])
    write_json(output_dir / "raw_ffprobe_failed.json", failed["raw_ffprobe"])
    write_json(debug_dir / "success_summary.json", success["summary"])
    write_json(debug_dir / "failed_summary.json", failed["summary"])
    write_json(debug_dir / "time_domain_diagnosis.json", time_domain)
    if replay_payload:
        write_json(debug_dir / "failed_replay_payload.json", replay_payload)

    success_decode = run_decode_check(success["raw_clip_path"], output_dir / "decode_errors_success.log")
    failed_decode = run_decode_check(failed["raw_clip_path"], output_dir / "decode_errors_failed.log")

    success["decode"] = success_decode
    failed["decode"] = failed_decode

    write_packet_csv(output_dir / "packet_summary_success.csv", success["packets"])
    write_packet_csv(output_dir / "packet_summary_failed.csv", failed["packets"])
    write_keyframe_csv(output_dir / "keyframe_summary_success.csv", success["frames"])
    write_keyframe_csv(output_dir / "keyframe_summary_failed.csv", failed["frames"])

    success_sheet = make_contact_sheet(
        video_path=success["raw_clip_path"],
        seconds=sample_seconds_for_case(success, SUCCESS_SAMPLE_SECONDS),
        output_path=output_dir / "success_contact_sheet.jpg",
        frames_dir=frames_dir,
        label_prefix="success",
    )
    failed_sheet = make_contact_sheet(
        video_path=failed["raw_clip_path"],
        seconds=sample_seconds_for_case(failed, FAILED_SAMPLE_SECONDS, include_last=True),
        output_path=output_dir / "failed_contact_sheet.jpg",
        frames_dir=frames_dir,
        label_prefix="failed",
    )

    remux = run_remux_and_transcode(
        failed_video=failed["raw_clip_path"],
        remux_dir=remux_dir,
    )

    diagnosis = build_diagnosis(
        success_bundle=args.success_bundle,
        failed_bundle=args.failed_bundle,
        failed_sink_output=args.failed_sink_output,
        output_dir=output_dir,
        success=success,
        failed=failed,
        failed_sink=failed_sink,
        time_domain=time_domain,
        replay_payload=replay_payload,
        tool_status=tool_status,
        remux=remux,
    )
    write_json(output_dir / "video_integrity_diagnosis.json", diagnosis)
    write_comparison_csv(output_dir / "comparison_video_integrity.csv", diagnosis)
    write_review_html(
        output_dir / "failed_video_integrity_review.html",
        diagnosis=diagnosis,
        success_sheet=success_sheet,
        failed_sheet=failed_sheet,
        failed_samples=failed_sheet.get("samples", []),
        success_samples=success_sheet.get("samples", []),
    )
    write_report(output_dir / "report.md", diagnosis)
    write_solution_proposal(output_dir / "solution_proposal.md", diagnosis)

    print(json.dumps({
        "result_marker": diagnosis["result_marker"],
        "output_dir": str(output_dir),
        "report": str(output_dir / "report.md"),
        "solution_proposal": str(output_dir / "solution_proposal.md"),
        "diagnosis": str(output_dir / "video_integrity_diagnosis.json"),
        "review_html": str(output_dir / "failed_video_integrity_review.html"),
    }, indent=2, sort_keys=True))
    return 0


def inspect_bundle(bundle_dir: Path, *, role: str) -> dict[str, Any]:
    bundle_dir = bundle_dir.resolve(strict=False)
    raw_clip_path = find_raw_clip(bundle_dir)
    summary_path = bundle_dir / SUMMARY_FILE
    metadata_path = bundle_dir / SINK_METADATA_FILE
    sidecar_path = bundle_dir / SIDECAR_FILE

    summary = read_json(summary_path)
    metadata_rows = load_json_or_jsonl(metadata_path)
    sidecar_rows = read_jsonl(sidecar_path)
    metadata_stats = metadata_frame_stats(native_video_frames(metadata_rows))
    sidecar_stats = sidecar_frame_stats(sidecar_rows)
    raw_ffprobe = ffprobe_container_stream(raw_clip_path)
    frames = ffprobe_frames(raw_clip_path)
    packets = ffprobe_packets(raw_clip_path)
    video_stats = video_integrity_stats(raw_ffprobe, frames, packets)

    return {
        "role": role,
        "bundle_dir": str(bundle_dir),
        "raw_clip_path": raw_clip_path,
        "summary_path": str(summary_path),
        "metadata_path": str(metadata_path),
        "sidecar_path": str(sidecar_path),
        "summary": summary,
        "metadata": metadata_stats,
        "sidecar": sidecar_stats,
        "raw_ffprobe": raw_ffprobe,
        "frames": frames,
        "packets": packets,
        "video": video_stats,
    }


def inspect_sink_output(sink_dir: Path) -> dict[str, Any]:
    sink_dir = sink_dir.resolve(strict=False)
    video_path = find_raw_clip(sink_dir)
    metadata_path = sink_dir / "metadata.json"
    metadata_rows = load_json_or_jsonl(metadata_path)
    return {
        "sink_dir": str(sink_dir),
        "video_path": str(video_path),
        "metadata_path": str(metadata_path),
        "metadata": metadata_frame_stats(native_video_frames(metadata_rows)),
        "raw_ffprobe": ffprobe_container_stream(video_path),
    }


def build_diagnosis(
    *,
    success_bundle: Path,
    failed_bundle: Path,
    failed_sink_output: Path,
    output_dir: Path,
    success: dict[str, Any],
    failed: dict[str, Any],
    failed_sink: dict[str, Any],
    time_domain: dict[str, Any],
    replay_payload: dict[str, Any],
    tool_status: dict[str, Any],
    remux: dict[str, Any],
) -> dict[str, Any]:
    replay = replay_request_summary(replay_payload, failed)
    success_case = condensed_case(success)
    failed_case = condensed_case(failed)
    remux_summary = condensed_remux(remux)

    missing_tools = not tool_status.get("ffprobe_available") or not tool_status.get("ffmpeg_available")
    failed_decode_problem = failed_case["decode_error_count"] > 0
    failed_decode_signal = bool(
        failed_case["decode_signal_counts"].get("missing_reference")
        or failed_case["decode_signal_counts"].get("invalid_nal")
        or failed_case["decode_signal_counts"].get("non_existing_pps_sps")
        or failed_case["decode_signal_counts"].get("corrupt_packet_or_frame")
        or failed_case["decode_signal_counts"].get("concealing_errors")
    )
    first_frame_not_keyframe = failed_case.get("first_frame_keyframe") is False
    pts_discontinuity = bool(failed_case.get("pts_jump_detected"))
    dts_discontinuity = bool(failed_case.get("dts_jump_detected"))
    frame_count_unsafe = bool(
        (time_domain.get("diagnosis") or {}).get("frame_count_duration_mismatch")
        or time_domain.get("result_marker") == "FAIL_C2_3B_R_FRAME_COUNT_WINDOW_UNSAFE"
    )

    blocking_reasons: list[str] = []
    if frame_count_unsafe:
        blocking_reasons.append("stop_condition.frame_count was used as an unsafe duration proxy")
    if failed_decode_problem:
        blocking_reasons.append("failed raw_clip emitted ffmpeg decode warnings/errors")
    if failed_decode_signal:
        blocking_reasons.append("decode log contains missing reference, invalid NAL, or corruption signals")
    if first_frame_not_keyframe:
        blocking_reasons.append("failed raw_clip first decoded frame is not a keyframe")
    if pts_discontinuity or dts_discontinuity:
        blocking_reasons.append("failed raw_clip has PTS/DTS jump or discontinuity signals")
    if failed_case.get("decoded_frames") != failed_case.get("metadata_rows"):
        blocking_reasons.append("failed decoded frame count does not match metadata row count")

    if missing_tools:
        result_marker = RESULT_PARTIAL_DATA
    elif blocking_reasons:
        result_marker = RESULT_FAIL
    elif failed_decode_problem == 0 and remux_summary.get("remux_decode_error_count") == 0:
        result_marker = RESULT_PARTIAL_COMPAT
    else:
        result_marker = RESULT_PASS_DIAGNOSIS

    raw_clip_decode_integrity_failed = None if missing_tools else bool(failed_decode_problem or failed_decode_signal)
    likely_causes = likely_root_causes(
        frame_count_unsafe=frame_count_unsafe,
        failed_decode_signal=failed_decode_signal,
        first_frame_not_keyframe=first_frame_not_keyframe,
        pts_discontinuity=pts_discontinuity or dts_discontinuity,
        remux=remux,
    )

    return {
        "result_marker": result_marker,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "success_bundle": str(success_bundle),
        "failed_bundle": str(failed_bundle),
        "failed_sink_output": str(failed_sink_output),
        "output_dir": str(output_dir),
        "tool_status": tool_status,
        "success_video": success_case,
        "failed_video": failed_case,
        "failed_sink": {
            "metadata_rows": failed_sink.get("metadata", {}).get("metadata_frame_count"),
            "pts_duration_s": failed_sink.get("metadata", {}).get("pts_duration_s"),
            "source_id": failed_sink.get("metadata", {}).get("source_id"),
        },
        "replay_request": replay,
        "time_domain_diagnosis": {
            "path": str(output_dir / "debug" / "time_domain_diagnosis.json"),
            "result_marker": time_domain.get("result_marker"),
            "frame_count_window_unsafe": frame_count_unsafe,
            "failed_effective_video_fps": (time_domain.get("failed") or {}).get("effective_video_fps"),
            "failed_effective_metadata_fps": (time_domain.get("failed") or {}).get("effective_metadata_fps"),
        },
        "remux_transcode": remux_summary,
        "diagnosis": {
            "frame_count_window_unsafe": frame_count_unsafe,
            "raw_clip_decode_integrity_failed": raw_clip_decode_integrity_failed,
            "failed_video_missing_initial_keyframe_or_parameter_sets": bool(
                failed_decode_signal or first_frame_not_keyframe
            ),
            "pts_or_dts_discontinuity_detected": bool(pts_discontinuity or dts_discontinuity),
            "event_style_replay_video_not_production_ready": result_marker == RESULT_FAIL,
            "failed_clip_manual_visual_status": "manual_review_reported_severe_artifacts",
            "likely_root_causes": likely_causes,
            "blocking_reasons": blocking_reasons,
        },
        "recommendation": {
            "next_phase": "C2.3B-R2",
            "strategy": (
                "repair event evidence as a time-domain Replay/media finalization flow, "
                "then gate production on raw_clip decode integrity, decoded/metadata timeline "
                "reconciliation, and C2.3Q visual audit"
            ),
        },
    }


def condensed_case(case: dict[str, Any]) -> dict[str, Any]:
    video = case.get("video") or {}
    metadata = case.get("metadata") or {}
    sidecar = case.get("sidecar") or {}
    summary = case.get("summary") or {}
    decode = case.get("decode") or {}
    decoded_frames = video.get("decoded_frames")
    duration = video.get("duration_s")
    return {
        "raw_clip_path": str(case.get("raw_clip_path")),
        "duration_s": duration,
        "decoded_frames": decoded_frames,
        "metadata_rows": metadata.get("metadata_frame_count"),
        "sidecar_rows": sidecar.get("sidecar_frame_count"),
        "metadata_pts_duration_s": metadata.get("pts_duration_s"),
        "effective_video_fps": effective_fps(decoded_frames, duration),
        "effective_metadata_fps": effective_fps(metadata.get("metadata_frame_count"), metadata.get("pts_duration_s")),
        "avg_frame_rate": video.get("avg_frame_rate"),
        "r_frame_rate": video.get("r_frame_rate"),
        "codec_name": video.get("codec_name"),
        "codec_tag_string": video.get("codec_tag_string"),
        "pix_fmt": video.get("pix_fmt"),
        "width": video.get("width"),
        "height": video.get("height"),
        "time_base": video.get("time_base"),
        "start_time": video.get("start_time"),
        "bit_rate": video.get("bit_rate"),
        "extradata_size": video.get("extradata_size"),
        "is_avc": video.get("is_avc"),
        "nal_length_size": video.get("nal_length_size"),
        "keyframe_count": video.get("keyframe_count"),
        "first_frame_keyframe": video.get("first_frame_keyframe"),
        "first_keyframe_pts_time": video.get("first_keyframe_pts_time"),
        "pict_type_counts": video.get("pict_type_counts"),
        "packet_count": video.get("packet_count"),
        "frame_count_from_show_frames": video.get("frame_count_from_show_frames"),
        "pts_monotonic": video.get("pts_monotonic"),
        "dts_monotonic": video.get("dts_monotonic"),
        "pts_jump_detected": video.get("pts_jump_detected"),
        "dts_jump_detected": video.get("dts_jump_detected"),
        "negative_timestamp_count": video.get("negative_timestamp_count"),
        "duplicate_pts_count": video.get("duplicate_pts_count"),
        "packet_duration_distribution": video.get("packet_duration_distribution"),
        "decode_error_count": decode.get("decode_error_count", 0),
        "decode_signal_counts": decode.get("signal_counts", {}),
        "decode_command": decode.get("command"),
        "trim_occurred": bool(summary.get("trim_occurred")),
        "production_ready": bool(summary.get("production_ready")),
        "timeline_reconciliation_status": summary.get("timeline_reconciliation_status"),
        "fallback_used": bool(summary.get("fallback_used")),
        "legacy_used_for_visual_binding": bool(summary.get("legacy_used_for_visual_binding")),
        "annotation_source_kind": summary.get("annotation_source_kind"),
    }


def condensed_remux(remux: dict[str, Any]) -> dict[str, Any]:
    return {
        "remux_path": remux.get("remux_path"),
        "remux_returncode": remux.get("remux_returncode"),
        "remux_decode_error_count": (remux.get("remux_decode") or {}).get("decode_error_count"),
        "remux_decode_signal_counts": (remux.get("remux_decode") or {}).get("signal_counts"),
        "transcode_path": remux.get("transcode_path"),
        "transcode_returncode": remux.get("transcode_returncode"),
        "transcode_decode_error_count": (remux.get("transcode_decode") or {}).get("decode_error_count"),
        "transcode_decode_signal_counts": (remux.get("transcode_decode") or {}).get("signal_counts"),
        "diagnostic_only": True,
        "production_source_preserved": True,
    }


def likely_root_causes(
    *,
    frame_count_unsafe: bool,
    failed_decode_signal: bool,
    first_frame_not_keyframe: bool,
    pts_discontinuity: bool,
    remux: dict[str, Any],
) -> list[str]:
    causes: list[str] = []
    if frame_count_unsafe:
        causes.append("fixed frame_count window on low-cadence event-style Replay output")
    if first_frame_not_keyframe:
        causes.append("Replay output does not begin at an independently decodable keyframe")
    if failed_decode_signal:
        causes.append("bitstream decode context/reference data missing or corrupt in failed raw_clip")
    if pts_discontinuity:
        causes.append("PTS/DTS discontinuity or large timestamp jumps in failed raw_clip")
    remux_decode = (remux.get("remux_decode") or {}).get("decode_error_count")
    transcode_decode = (remux.get("transcode_decode") or {}).get("decode_error_count")
    if remux_decode and transcode_decode == 0:
        causes.append("transcode can produce a clean diagnostic file, but source evidence is still not production-safe")
    if not causes:
        causes.append("manual visual artifact report needs human review against generated contact sheets")
    return causes


def ffprobe_container_stream(video_path: Path) -> dict[str, Any]:
    if shutil.which("ffprobe") is None:
        return {"error": "ffprobe_missing", "video_path": str(video_path)}
    command = [
        "ffprobe",
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        (
            "format=duration,size,bit_rate,format_name,start_time:"
            "stream=index,codec_name,codec_tag_string,codec_type,width,height,pix_fmt,"
            "r_frame_rate,avg_frame_rate,time_base,start_time,duration,nb_frames,"
            "nb_read_frames,bit_rate,extradata_size,is_avc,nal_length_size"
        ),
        "-of",
        "json",
        str(video_path),
    ]
    return run_json_command(command, video_path)


def ffprobe_frames(video_path: Path) -> dict[str, Any]:
    if shutil.which("ffprobe") is None:
        return {"error": "ffprobe_missing", "frames": []}
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        (
            "frame=key_frame,pict_type,best_effort_timestamp_time,pkt_dts_time,"
            "pkt_pts_time,pkt_duration_time,coded_picture_number,display_picture_number"
        ),
        "-of",
        "json",
        str(video_path),
    ]
    return run_json_command(command, video_path)


def ffprobe_packets(video_path: Path) -> dict[str, Any]:
    if shutil.which("ffprobe") is None:
        return {"error": "ffprobe_missing", "packets": []}
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_packets",
        "-show_entries",
        "packet=pts,pts_time,dts,dts_time,duration,duration_time,size,pos,flags",
        "-of",
        "json",
        str(video_path),
    ]
    return run_json_command(command, video_path)


def run_json_command(command: list[str], video_path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        return {
            "error": "command_failed",
            "video_path": str(video_path),
            "command": command,
            "stderr": exc.stderr[-4000:],
        }
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "error": f"json_decode_failed:{exc}",
            "video_path": str(video_path),
            "command": command,
            "stderr": completed.stderr[-4000:],
        }
    data["video_path"] = str(video_path)
    return data


def video_integrity_stats(raw_ffprobe: dict[str, Any], frames_data: dict[str, Any], packets_data: dict[str, Any]) -> dict[str, Any]:
    stream = first_stream(raw_ffprobe)
    fmt = raw_ffprobe.get("format") if isinstance(raw_ffprobe.get("format"), dict) else {}
    frames = frames_data.get("frames") if isinstance(frames_data.get("frames"), list) else []
    packets = packets_data.get("packets") if isinstance(packets_data.get("packets"), list) else []

    pts_values = [float_value(packet.get("pts_time")) for packet in packets if float_value(packet.get("pts_time")) is not None]
    dts_values = [float_value(packet.get("dts_time")) for packet in packets if float_value(packet.get("dts_time")) is not None]
    packet_durations = [
        float_value(packet.get("duration_time"))
        for packet in packets
        if float_value(packet.get("duration_time")) is not None
    ]
    keyframes = [
        frame for frame in frames
        if int_or_none(frame.get("key_frame")) == 1
    ]
    pict_types = Counter(str(frame.get("pict_type") or "unknown") for frame in frames)
    frame_pts_values = [
        frame_pts_time(frame)
        for frame in frames
        if frame_pts_time(frame) is not None
    ]
    duration = first_numeric(fmt.get("duration"), stream.get("duration"))
    decoded_frames = int_or_none(stream.get("nb_read_frames")) or int_or_none(stream.get("nb_frames")) or len(frames)

    return {
        "duration_s": duration,
        "stream_duration_s": float_value(stream.get("duration")),
        "format_duration_s": float_value(fmt.get("duration")),
        "start_time": first_value(stream.get("start_time"), fmt.get("start_time")),
        "codec_name": stream.get("codec_name"),
        "codec_tag_string": stream.get("codec_tag_string"),
        "pix_fmt": stream.get("pix_fmt"),
        "width": int_or_none(stream.get("width")),
        "height": int_or_none(stream.get("height")),
        "r_frame_rate": stream.get("r_frame_rate"),
        "avg_frame_rate": stream.get("avg_frame_rate"),
        "time_base": stream.get("time_base"),
        "nb_frames": int_or_none(stream.get("nb_frames")),
        "nb_read_frames": int_or_none(stream.get("nb_read_frames")),
        "decoded_frames": decoded_frames,
        "bit_rate": first_value(stream.get("bit_rate"), fmt.get("bit_rate")),
        "extradata_size": int_or_none(stream.get("extradata_size")),
        "is_avc": stream.get("is_avc"),
        "nal_length_size": stream.get("nal_length_size"),
        "packet_count": len(packets),
        "frame_count_from_show_frames": len(frames),
        "keyframe_count": len(keyframes),
        "first_frame_keyframe": (
            int_or_none(frames[0].get("key_frame")) == 1 if frames else None
        ),
        "first_keyframe_pts_time": frame_pts_time(keyframes[0]) if keyframes else None,
        "keyframe_pts_times": [frame_pts_time(frame) for frame in keyframes[:20]],
        "pict_type_counts": dict(pict_types),
        "pts_monotonic": monotonic(pts_values),
        "dts_monotonic": monotonic(dts_values),
        "pts_jump_detected": jump_detected(pts_values),
        "dts_jump_detected": jump_detected(dts_values),
        "pts_jump_samples": jump_samples(pts_values),
        "dts_jump_samples": jump_samples(dts_values),
        "negative_timestamp_count": sum(1 for value in pts_values + dts_values if value < 0),
        "duplicate_pts_count": len(pts_values) - len(set(pts_values)),
        "packet_duration_distribution": duration_distribution(packet_durations),
        "frame_pts_start": frame_pts_values[0] if frame_pts_values else None,
        "frame_pts_end": frame_pts_values[-1] if frame_pts_values else None,
    }


def run_decode_check(video_path: Path, log_path: Path) -> dict[str, Any]:
    if shutil.which("ffmpeg") is None:
        log_path.write_text("ffmpeg_missing\n", encoding="utf-8")
        return {
            "command": None,
            "returncode": None,
            "decode_error_count": 1,
            "signal_counts": {"ffmpeg_missing": 1},
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
    log_path.write_text(stderr, encoding="utf-8")
    lines = [line for line in stderr.splitlines() if line.strip()]
    return {
        "command": " ".join(command),
        "returncode": completed.returncode,
        "decode_error_count": len(lines),
        "signal_counts": categorize_decode_log(lines),
        "line_samples": lines[:30],
    }


def categorize_decode_log(lines: list[str]) -> dict[str, int]:
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
        if "dts" in lower and ("invalid" in lower or "non" in lower):
            categories["dts_warning"] += 1
        if "pts" in lower and ("invalid" in lower or "non" in lower):
            categories["pts_warning"] += 1
        if not any(
            token in lower
            for token in (
                "missing reference",
                "reference picture missing",
                "non-existing pps",
                "sps",
                "pps",
                "invalid nal",
                "nal unit",
                "corrupt",
                "error while decoding",
                "concealing",
                "non monoton",
                "non-monoton",
                "dts",
                "pts",
            )
        ):
            categories["other"] += 1
    return dict(categories)


def run_remux_and_transcode(*, failed_video: Path, remux_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if shutil.which("ffmpeg") is None:
        return {"error": "ffmpeg_missing"}

    remux_path = remux_dir / "remux_copy.mov"
    remux_command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(failed_video),
        "-c",
        "copy",
        str(remux_path),
    ]
    remux_completed = subprocess.run(remux_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    (remux_dir / "remux_command.log").write_text(remux_completed.stderr or "", encoding="utf-8")
    result["remux_path"] = str(remux_path)
    result["remux_returncode"] = remux_completed.returncode
    if remux_path.exists():
        result["remux_decode"] = run_decode_check(remux_path, remux_dir / "remux_decode_errors.log")
    else:
        (remux_dir / "remux_decode_errors.log").write_text("remux output missing\n", encoding="utf-8")
        result["remux_decode"] = {"decode_error_count": 1, "signal_counts": {"remux_output_missing": 1}}

    transcode_path = remux_dir / "transcode_h264.mp4"
    transcode_command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(failed_video),
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(transcode_path),
    ]
    transcode_completed = subprocess.run(transcode_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    (remux_dir / "transcode_command.log").write_text(transcode_completed.stderr or "", encoding="utf-8")
    result["transcode_path"] = str(transcode_path)
    result["transcode_returncode"] = transcode_completed.returncode
    if transcode_path.exists():
        result["transcode_decode"] = run_decode_check(transcode_path, remux_dir / "transcode_decode_errors.log")
    else:
        (remux_dir / "transcode_decode_errors.log").write_text("transcode output missing\n", encoding="utf-8")
        result["transcode_decode"] = {"decode_error_count": 1, "signal_counts": {"transcode_output_missing": 1}}

    return result


def make_contact_sheet(
    *,
    video_path: Path,
    seconds: list[float],
    output_path: Path,
    frames_dir: Path,
    label_prefix: str,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    extracted_paths: list[Path] = []
    for second in seconds:
        name = f"{label_prefix}_{safe_second_label(second)}.jpg"
        frame_path = frames_dir / name
        extraction = extract_frame(video_path, second, frame_path)
        samples.append({
            "second": second,
            "path": str(frame_path),
            "status": extraction["status"],
            "ffmpeg_stderr": extraction.get("stderr", "")[-500:],
        })
        extracted_paths.append(frame_path)

    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:
        output_path.write_text(f"PIL missing: {exc}\n", encoding="utf-8")
        return {"path": str(output_path), "samples": samples, "error": f"PIL_missing:{exc}"}

    tiles: list[Image.Image] = []
    for sample in samples:
        path = Path(sample["path"])
        if path.is_file() and path.stat().st_size > 0:
            try:
                image = Image.open(path).convert("RGB")
            except Exception:
                image = placeholder_image(f"{label_prefix} {sample['second']}s\nopen failed")
        else:
            image = placeholder_image(f"{label_prefix} {sample['second']}s\ndecode_failed")
        image.thumbnail((320, 180))
        tile = Image.new("RGB", (340, 230), "white")
        tile.paste(image, ((340 - image.width) // 2, 8))
        draw = ImageDraw.Draw(tile)
        font = ImageFont.load_default()
        label = f"{label_prefix} t={sample['second']:.3f}s {sample['status']}"
        draw.text((10, 195), label, fill=(0, 0, 0), font=font)
        tiles.append(tile)

    cols = min(3, max(1, len(tiles)))
    rows = math.ceil(len(tiles) / cols)
    sheet = Image.new("RGB", (cols * 340, rows * 230), (238, 238, 238))
    for index, tile in enumerate(tiles):
        x = (index % cols) * 340
        y = (index // cols) * 230
        sheet.paste(tile, (x, y))
    sheet.save(output_path, quality=90)
    return {"path": str(output_path), "samples": samples}


def extract_frame(video_path: Path, second: float, frame_path: Path) -> dict[str, Any]:
    if shutil.which("ffmpeg") is None:
        return {"status": "ffmpeg_missing", "stderr": ""}
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{max(second, 0):.6f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(frame_path),
    ]
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ok = completed.returncode == 0 and frame_path.exists() and frame_path.stat().st_size > 0
    return {
        "status": "ok" if ok else "decode_failed",
        "returncode": completed.returncode,
        "stderr": completed.stderr or "",
    }


def write_review_html(
    path: Path,
    *,
    diagnosis: dict[str, Any],
    success_sheet: dict[str, Any],
    failed_sheet: dict[str, Any],
    failed_samples: list[dict[str, Any]],
    success_samples: list[dict[str, Any]],
) -> None:
    failed = diagnosis["failed_video"]
    success = diagnosis["success_video"]
    decode = failed.get("decode_signal_counts") or {}
    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(sample.get('second')))}</td>"
        f"<td>{html.escape(sample.get('status', ''))}</td>"
        f"<td><a href='{html.escape(rel(path, Path(sample.get('path', ''))))}'>frame</a></td>"
        "</tr>"
        for sample in failed_samples
    )
    success_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(sample.get('second')))}</td>"
        f"<td>{html.escape(sample.get('status', ''))}</td>"
        f"<td><a href='{html.escape(rel(path, Path(sample.get('path', ''))))}'>frame</a></td>"
        "</tr>"
        for sample in success_samples
    )
    content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>C2.3B Video Integrity Review</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; color: #111; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
    .card {{ border: 1px solid #ccc; border-radius: 6px; padding: 14px; }}
    img {{ max-width: 100%; height: auto; border: 1px solid #ddd; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
    th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
    code {{ background: #f2f2f2; padding: 1px 3px; }}
  </style>
</head>
<body>
  <h1>C2.3B Video Integrity Review</h1>
  <div class="card">
    <p><strong>Result marker:</strong> <code>{html.escape(diagnosis['result_marker'])}</code></p>
    <p><strong>Failed decode error count:</strong> {failed.get('decode_error_count')}</p>
    <p><strong>Failed decode signals:</strong> <code>{html.escape(json.dumps(decode, sort_keys=True))}</code></p>
    <p><strong>Manual visual status:</strong> {html.escape(diagnosis['diagnosis'].get('failed_clip_manual_visual_status', 'manual_review_required'))}</p>
  </div>
  <div class="grid">
    <div class="card">
      <h2>Success Contact Sheet</h2>
      <p>duration={success.get('duration_s')}s decoded={success.get('decoded_frames')} first_keyframe={success.get('first_frame_keyframe')}</p>
      <img src="{html.escape(rel(path, Path(success_sheet.get('path', ''))))}" alt="success contact sheet">
      <table><tr><th>Second</th><th>Status</th><th>Frame</th></tr>{success_rows}</table>
    </div>
    <div class="card">
      <h2>Failed Contact Sheet</h2>
      <p>duration={failed.get('duration_s')}s decoded={failed.get('decoded_frames')} first_keyframe={failed.get('first_frame_keyframe')}</p>
      <img src="{html.escape(rel(path, Path(failed_sheet.get('path', ''))))}" alt="failed contact sheet">
      <table><tr><th>Second</th><th>Status</th><th>Frame</th></tr>{rows}</table>
    </div>
  </div>
  <h2>How to Review</h2>
  <p>Compare the failed frames against the success sheet. Look for block corruption,
  missing reference artifacts, repeated stale content, large visual jumps, and whether
  corruption clusters near the Replay start, event offset, or late window.</p>
</body>
</html>
"""
    path.write_text(content, encoding="utf-8")


def write_report(path: Path, diagnosis: dict[str, Any]) -> None:
    success = diagnosis["success_video"]
    failed = diagnosis["failed_video"]
    replay = diagnosis["replay_request"]
    remux = diagnosis["remux_transcode"]
    diag = diagnosis["diagnosis"]
    lines = [
        "# C2.3B-V Replay Resulting Stream Video Integrity Diagnosis",
        "",
        f"Result marker: `{diagnosis['result_marker']}`",
        "",
        "## Inputs",
        "",
        f"- Success bundle: `{diagnosis['success_bundle']}`",
        f"- Failed bundle: `{diagnosis['failed_bundle']}`",
        f"- Failed sink output: `{diagnosis['failed_sink_output']}`",
        "",
        "## Success vs Failed Video",
        "",
        "| Case | Duration s | Decoded Frames | Metadata Rows | Sidecar Rows | Avg FPS | Keyframes | First Frame Keyframe | Decode Lines | PTS Monotonic | DTS Monotonic | PTS Jump |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- | --- | --- |",
        video_row("C2.3A success", success),
        video_row("C2.3B failed", failed),
        "",
        "## Required Answers",
        "",
        f"1. C2.3B failed video ffmpeg decode error/warning line count: `{failed.get('decode_error_count')}`. Signals: `{json.dumps(failed.get('decode_signal_counts'), sort_keys=True)}`.",
        f"2. C2.3B failed first decoded frame keyframe: `{failed.get('first_frame_keyframe')}`.",
        f"3. SPS/PPS/reference-frame evidence: `{sps_pps_answer(failed)}`.",
        f"4. PTS monotonic: `{failed.get('pts_monotonic')}`; DTS monotonic: `{failed.get('dts_monotonic')}`; PTS jump detected: `{failed.get('pts_jump_detected')}`; DTS jump detected: `{failed.get('dts_jump_detected')}`.",
        f"5. Most likely visual-artifact class: `{artifact_classification(diagnosis)}`.",
        f"6. Remux copy decode line count: `{remux.get('remux_decode_error_count')}`. Remux is diagnostic only.",
        f"7. Transcode decode line count: `{remux.get('transcode_decode_error_count')}`. Transcode is diagnostic only.",
        "8. Remux/transcode must not be used to make this C2.3B evidence pass: they do not prove the original production raw_clip is trustworthy, and they can hide bitstream/timing defects.",
        (
            "9. Key differences: the success clip is about "
            f"`{fmt(success.get('duration_s'))}`s at `{fmt(success.get('effective_video_fps'))}` FPS with "
            f"`{success.get('decoded_frames')}` decoded frames; the failed clip is about "
            f"`{fmt(failed.get('duration_s'))}`s at `{fmt(failed.get('effective_video_fps'))}` FPS with "
            f"`{failed.get('decoded_frames')}` decoded frames and `{failed.get('metadata_rows')}` metadata rows."
        ),
        (
            "10. C2.3B-R2 can start only as a repair phase that fixes the time-domain request and adds raw video integrity gates; "
            "this failed event-style output is not production evidence."
        ),
        (
            "11. If the next run still has decode/keyframe/PTS issues after time-domain repair, Replay anchor/keyframe selection "
            "or video-file-sink packetization must be fixed before C2.4."
        ),
        "",
        "## Replay Payload",
        "",
        f"- stored_stream_id: `{replay.get('stored_stream_id')}`",
        f"- resulting_stream_id: `{replay.get('resulting_stream_id')}`",
        f"- anchor_keyframe: `{replay.get('anchor_keyframe')}`",
        f"- offset.seconds: `{replay.get('offset_seconds')}`",
        f"- stop_condition.frame_count: `{replay.get('stop_condition_frame_count')}`",
        "",
        "## Diagnosis",
        "",
        f"- frame_count_window_unsafe: `{diag.get('frame_count_window_unsafe')}`",
        f"- raw_clip_decode_integrity_failed: `{diag.get('raw_clip_decode_integrity_failed')}`",
        f"- failed_video_missing_initial_keyframe_or_parameter_sets: `{diag.get('failed_video_missing_initial_keyframe_or_parameter_sets')}`",
        f"- pts_or_dts_discontinuity_detected: `{diag.get('pts_or_dts_discontinuity_detected')}`",
        f"- event_style_replay_video_not_production_ready: `{diag.get('event_style_replay_video_not_production_ready')}`",
        "",
        "Blocking reasons:",
        "",
        *[f"- {reason}" for reason in diag.get("blocking_reasons") or ["none"]],
        "",
        "Likely root causes:",
        "",
        *[f"- {cause}" for cause in diag.get("likely_root_causes") or ["none"]],
        "",
        "## Human Review",
        "",
        "Open `failed_video_integrity_review.html` and compare the failed contact sheet against the success sheet. The script does not auto-classify visual artifacts; it provides sampled frames and decode/timeline evidence for manual confirmation.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_solution_proposal(path: Path, diagnosis: dict[str, Any]) -> None:
    lines = [
        "# C2.3B-R2 Repair Proposal",
        "",
        "## Scheme 0: Immediate Prohibitions",
        "",
        "- Do not keep using `stop_condition.frame_count=240` to mean 10 seconds.",
        "- Do not use trim to hide decoded/metadata frame-count mismatch.",
        "- Do not use PostgreSQL or DB-window fallback to fill production evidence annotations.",
        "- Do not use legacy `annotations.jsonl` fallback.",
        "- Do not claim known-face identity before C2.4 identity binding.",
        "- Do not treat diagnostic remux/transcode outputs as production evidence.",
        "",
        "## Scheme 1: Replay Job Request Repair",
        "",
        "- Express evidence requests as a time-domain window: event frame_pts/event_ts plus pre/post seconds.",
        "- Replay must start from the nearest independently decodable keyframe before the requested start.",
        "- Offset must not cause the resulting stream to begin at a non-keyframe or without SPS/PPS/reference context.",
        "- If Replay only supports anchor keyframe plus offset, choose an anchor at least `pre_seconds + keyframe_margin` before the event.",
        "- Stop using fixed frame count. Prefer Replay time-based stop conditions or explicit start/end timestamps.",
        "- If time stop is unavailable, measure effective Replay cadence, over-export conservatively, then crop by PTS.",
        "- Avoid crossing loop/replay-buffer boundaries unless Replay can preserve decode context and monotonic timestamps.",
        "- Summary must record anchor_keyframe, start_pts, end_pts, requested_window, actual_window, offset, stop condition, and resulting stream id.",
        "",
        "## Scheme 2: video-file-sink / media-worker Production Gate",
        "",
        "- Add an ffprobe/ffmpeg decode integrity gate before `production_ready=true`.",
        "- `decoded_video_frame_count` must match or be explicitly reconciled with metadata and sidecar in the same time domain.",
        "- `trim_occurred=true` should default to `production_ready=false` unless time-based visual reconciliation is proven.",
        "- Non-zero ffmpeg decode warnings/errors should set `production_ready=false` for C2 production evidence.",
        "- First-frame/keyframe/timestamp abnormalities should set `production_ready=false`.",
        "- If remux/transcode is offered later, preserve the original raw clip and record `raw_clip_integrity_status`, `remux_used`, `transcode_used`, and `production_source_preserved`.",
        "- Do not silently replace `raw_clip.mov` with a repaired diagnostic copy.",
        "",
        "## Scheme 3: C2.3B-R2 Minimal Implementation Steps",
        "",
        "- Isolate current failed C2.3B WIP before editing.",
        "- Keep the contract fields: `replay_source_kind`, `evidence_topology`, no DB fallback, no legacy fallback.",
        "- Change the smoke/request builder so it no longer requests `frame_count=240` as the primary duration control.",
        "- Add a time-domain request builder using frame_pts/event_ts plus pre/post seconds.",
        "- Add a raw video integrity gate in the finalizer path.",
        "- Run C2.3Q audit only after the raw clip passes timeline and decode integrity gates.",
        "- Pass C2.3B only when raw_clip decode is clean, timeline reconciliation is clean, and C2.3Q visual audit passes.",
        "",
        "## Scheme 4: If event-style Replay remains unstable",
        "",
        "- Do not generate production evidence directly from the unstable event-style resulting stream.",
        "- As a temporary C2.3B workaround, use the stable FPS-gated post-Savant sink output and perform PTS-based video+metadata crop together.",
        "- Alternative: keep a post-Savant continuous short-cache sink, then let media-worker cut by PTS into the target evidence window.",
        "- This is a workaround and should be documented as a deviation from the Replay-service target until Replay event output is repaired.",
        "",
        "## Next Phase",
        "",
        f"Recommended next phase: `{diagnosis['recommendation']['next_phase']}`.",
        "",
        diagnosis["recommendation"]["strategy"],
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_comparison_csv(path: Path, diagnosis: dict[str, Any]) -> None:
    fields = [
        "case",
        "duration_s",
        "decoded_frames",
        "metadata_rows",
        "sidecar_rows",
        "avg_frame_rate",
        "effective_video_fps",
        "keyframe_count",
        "first_frame_keyframe",
        "decode_error_count",
        "pts_monotonic",
        "dts_monotonic",
        "pts_jump_detected",
        "dts_jump_detected",
        "production_ready",
        "trim_occurred",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for name in ("success_video", "failed_video"):
            case = diagnosis[name]
            writer.writerow({
                "case": name,
                **{field: case.get(field) for field in fields if field != "case"},
            })


def write_packet_csv(path: Path, packets_data: dict[str, Any]) -> None:
    fields = ["index", "pts_time", "dts_time", "duration_time", "flags", "size", "pos"]
    packets = packets_data.get("packets") if isinstance(packets_data.get("packets"), list) else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, packet in enumerate(packets):
            row = {"index": index}
            for field in fields[1:]:
                row[field] = packet.get(field)
            writer.writerow(row)


def write_keyframe_csv(path: Path, frames_data: dict[str, Any]) -> None:
    fields = ["index", "pts_time", "pkt_dts_time", "key_frame", "pict_type", "pkt_duration_time"]
    frames = frames_data.get("frames") if isinstance(frames_data.get("frames"), list) else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, frame in enumerate(frames):
            if int_or_none(frame.get("key_frame")) != 1:
                continue
            writer.writerow({
                "index": index,
                "pts_time": frame_pts_time(frame),
                "pkt_dts_time": frame.get("pkt_dts_time"),
                "key_frame": frame.get("key_frame"),
                "pict_type": frame.get("pict_type"),
                "pkt_duration_time": frame.get("pkt_duration_time"),
            })


def load_replay_payload(path: Path | None, failed: dict[str, Any]) -> dict[str, Any]:
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    candidates.extend(FAILED_REPLAY_PAYLOAD_CANDIDATES)
    metadata_path = Path(str(failed.get("bundle_dir", ""))) / "metadata.json"
    if metadata_path.is_file():
        try:
            metadata = read_json(metadata_path)
            request = (metadata.get("replay") or {}).get("replay_job_request")
            if isinstance(request, dict):
                return request
        except Exception:
            pass
    for candidate in candidates:
        if candidate.is_file():
            return read_json(candidate)
    return {}


def replay_request_summary(payload: dict[str, Any], failed: dict[str, Any]) -> dict[str, Any]:
    stop = payload.get("stop_condition") if isinstance(payload.get("stop_condition"), dict) else {}
    offset = payload.get("offset") if isinstance(payload.get("offset"), dict) else {}
    labels = ((payload.get("configuration") or {}).get("labels") or {}) if isinstance(payload.get("configuration"), dict) else {}
    summary = failed.get("summary") or {}
    return {
        "anchor_keyframe": payload.get("anchor_keyframe"),
        "offset_seconds": offset.get("seconds") or offset.get("secs"),
        "stop_condition_frame_count": stop.get("frame_count"),
        "stop_condition_ts_delta_sec": stop.get("ts_delta_sec") or stop.get("seconds"),
        "resulting_stream_id": labels.get("resulting_stream_id") or summary.get("replay_resulting_stream_id"),
        "stored_stream_id": payload.get("source_id") or labels.get("stored_stream_id") or summary.get("c2_3b_source_id"),
        "labels": labels,
    }


def metadata_frame_stats(frames: list[dict[str, Any]]) -> dict[str, Any]:
    pts_values = [int(frame["pts"]) for frame in frames if numeric(frame.get("pts")) is not None]
    deltas = [pts_values[index + 1] - pts_values[index] for index in range(len(pts_values) - 1)]
    source_ids = Counter(str(frame.get("source_id")) for frame in frames if frame.get("source_id") is not None)
    return {
        "metadata_frame_count": len(frames),
        "pts_start": pts_values[0] if pts_values else None,
        "pts_end": pts_values[-1] if pts_values else None,
        "pts_duration_s": ((pts_values[-1] - pts_values[0]) / 1_000_000_000.0 if len(pts_values) >= 2 else None),
        "pts_delta_min_ms": min(deltas) / 1_000_000.0 if deltas else None,
        "pts_delta_max_ms": max(deltas) / 1_000_000.0 if deltas else None,
        "pts_delta_mean_ms": (sum(deltas) / len(deltas) / 1_000_000.0 if deltas else None),
        "duplicate_frame_pts_count": len(pts_values) - len(set(pts_values)),
        "non_monotonic_frame_pts_count": sum(1 for delta in deltas if delta <= 0),
        "source_id": source_ids.most_common(1)[0][0] if source_ids else None,
    }


def sidecar_frame_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    type_counts: Counter[str] = Counter()
    keypoints_count = 0
    landmarks_count = 0
    for row in rows:
        for obj in row.get("objects") or []:
            object_type = str(obj.get("object_type") or obj.get("label") or "unknown")
            type_counts[object_type] += 1
            pose = obj.get("pose") if isinstance(obj.get("pose"), dict) else {}
            if isinstance(pose, dict):
                keypoints_count += len(pose.get("keypoints") or [])
            landmarks = obj.get("landmarks") if isinstance(obj.get("landmarks"), dict) else {}
            if isinstance(landmarks, dict):
                landmarks_count += len(landmarks.get("points") or [])
    return {
        "sidecar_frame_count": len(rows),
        "object_counts": dict(type_counts),
        "keypoints_count": keypoints_count,
        "face_landmarks_count": landmarks_count,
    }


def native_video_frames(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if isinstance(row, dict)
        and (
            row.get("type") == "VideoFrame"
            or "pts" in row
            or "frame_pts" in row
            or "objects" in row
        )
        and row.get("pts") is not None
    ]


def find_raw_clip(directory: Path) -> Path:
    for name in RAW_CLIP_CANDIDATES:
        path = directory / name
        if path.is_file():
            return path
    raise FileNotFoundError(f"raw clip not found in {directory}")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"error": "missing", "path": str(path)}
    return read_json(path)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def load_json_or_jsonl(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("frames", "metadata", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def first_stream(raw_ffprobe: dict[str, Any]) -> dict[str, Any]:
    streams = raw_ffprobe.get("streams")
    return streams[0] if isinstance(streams, list) and streams else {}


def first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def first_numeric(*values: Any) -> float | None:
    for value in values:
        parsed = float_value(value)
        if parsed is not None:
            return parsed
    return None


def numeric(value: Any) -> float | None:
    return float_value(value)


def float_value(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def effective_fps(frame_count: Any, duration_s: Any) -> float | None:
    frames = int_or_none(frame_count)
    duration = float_value(duration_s)
    if frames is None or duration is None or duration <= 0:
        return None
    return frames / duration


def frame_pts_time(frame: dict[str, Any]) -> float | None:
    return first_numeric(
        frame.get("best_effort_timestamp_time"),
        frame.get("pkt_pts_time"),
        frame.get("pkt_dts_time"),
    )


def monotonic(values: list[float]) -> bool | None:
    if len(values) < 2:
        return None
    return all(values[index + 1] >= values[index] for index in range(len(values) - 1))


def jump_detected(values: list[float]) -> bool:
    return bool(jump_samples(values))


def jump_samples(values: list[float]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    if len(values) < 3:
        return samples
    deltas = [values[index + 1] - values[index] for index in range(len(values) - 1)]
    positive = [delta for delta in deltas if delta > 0]
    if not positive:
        return samples
    median_delta = sorted(positive)[len(positive) // 2]
    threshold = max(median_delta * 5, 1.0)
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


def duration_distribution(values: list[float]) -> dict[str, int]:
    buckets: Counter[str] = Counter()
    for value in values:
        buckets[f"{value:.6f}"] += 1
    return dict(buckets.most_common(20))


def sample_seconds_for_case(case: dict[str, Any], base_seconds: tuple[int, ...], *, include_last: bool = False) -> list[float]:
    duration = float_value((case.get("video") or {}).get("duration_s"))
    seconds = [float(second) for second in base_seconds if duration is None or second <= duration]
    if include_last and duration is not None and duration > 0:
        last = max(0.0, duration - 0.1)
        if all(abs(last - second) > 0.25 for second in seconds):
            seconds.append(last)
    return seconds


def placeholder_image(text: str):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (320, 180), (250, 245, 238))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    draw.multiline_text((12, 72), text, fill=(80, 50, 40), font=font, spacing=4)
    return image


def safe_second_label(second: float) -> str:
    return f"{second:.3f}".replace(".", "_").replace("-", "m") + "s"


def rel(from_file: Path, target: Path) -> str:
    try:
        return str(target.resolve(strict=False).relative_to(from_file.parent.resolve(strict=False)))
    except ValueError:
        return str(target)


def fmt(value: Any) -> str:
    parsed = float_value(value)
    if parsed is None:
        return "null"
    return f"{parsed:.3f}"


def video_row(name: str, case: dict[str, Any]) -> str:
    return (
        f"| {name} | {fmt(case.get('duration_s'))} | {case.get('decoded_frames')} | "
        f"{case.get('metadata_rows')} | {case.get('sidecar_rows')} | {fmt(case.get('effective_video_fps'))} | "
        f"{case.get('keyframe_count')} | {case.get('first_frame_keyframe')} | "
        f"{case.get('decode_error_count')} | {case.get('pts_monotonic')} | "
        f"{case.get('dts_monotonic')} | {case.get('pts_jump_detected')} |"
    )


def sps_pps_answer(failed: dict[str, Any]) -> str:
    signals = failed.get("decode_signal_counts") or {}
    if signals.get("non_existing_pps_sps"):
        return "decode log contains SPS/PPS warnings"
    if signals.get("missing_reference"):
        return "decode log contains missing-reference warnings"
    if failed.get("first_frame_keyframe") is False:
        return "first frame is not a keyframe, so initial decode context is suspect"
    return "no explicit SPS/PPS or missing-reference warning was captured"


def artifact_classification(diagnosis: dict[str, Any]) -> str:
    failed = diagnosis["failed_video"]
    signals = failed.get("decode_signal_counts") or {}
    if signals.get("missing_reference") or signals.get("non_existing_pps_sps") or failed.get("first_frame_keyframe") is False:
        return "decode context missing or Replay started from a non-independent point"
    if failed.get("pts_jump_detected") or failed.get("dts_jump_detected"):
        return "PTS/DTS or container timeline discontinuity"
    if failed.get("decode_error_count", 0) > 0:
        return "bitstream/container decode warnings"
    return "manual review required; ffmpeg did not expose a clear corruption signature"


if __name__ == "__main__":
    raise SystemExit(main())
