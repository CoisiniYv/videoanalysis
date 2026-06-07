#!/usr/bin/env python3
"""Diagnose C2.3B Replay event-window time-domain behavior.

This tool is intentionally offline. It only reads existing evidence bundles,
sink output, summaries, sidecars, metadata, and optional saved Replay payloads.
It does not call Redis, PostgreSQL, FastAPI, Docker, or Replay.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import statistics
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


SIDECAR_FILE = "annotations.frame_cache.identity.jsonl"
SUMMARY_FILE = "summary.json"
SINK_METADATA_FILE = "sink_metadata.json"
RAW_CLIP_CANDIDATES = ("raw_clip.mov", "raw_clip.mp4", "video.mov", "video.mp4")
RESULT_FRAME_COUNT_UNSAFE = "FAIL_C2_3B_R_FRAME_COUNT_WINDOW_UNSAFE"
RESULT_INSUFFICIENT = "PARTIAL_C2_3B_R_INSUFFICIENT_RUNTIME_DATA"
RESULT_ALTERNATIVE = "PARTIAL_C2_3B_R_ALTERNATIVE_ROOT_CAUSE_FOUND"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose C2.3B Replay frame_count/time-domain mismatch."
    )
    parser.add_argument("--success-bundle", required=True, type=Path)
    parser.add_argument("--failed-bundle", required=True, type=Path)
    parser.add_argument("--failed-sink-output", required=True, type=Path)
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

    success = inspect_bundle(args.success_bundle, role="success")
    failed = inspect_bundle(args.failed_bundle, role="failed")
    failed_sink = inspect_sink_output(args.failed_sink_output)
    replay_payload = load_replay_payload(args.failed_replay_payload, args.failed_bundle)

    raw_success_ffprobe = success.get("raw_ffprobe") or {}
    raw_failed_ffprobe = failed.get("raw_ffprobe") or {}
    write_json(output_dir / "raw_ffprobe_success.json", raw_success_ffprobe)
    write_json(output_dir / "raw_ffprobe_failed.json", raw_failed_ffprobe)
    write_json(debug_dir / "success_summary.json", success.get("summary") or {})
    write_json(debug_dir / "failed_summary.json", failed.get("summary") or {})
    write_json(debug_dir / "success_metadata_stats.json", success.get("metadata") or {})
    write_json(debug_dir / "failed_metadata_stats.json", failed.get("metadata") or {})
    if replay_payload:
        write_json(debug_dir / "failed_replay_payload.json", replay_payload)

    write_pts_delta_csv(output_dir / "pts_delta_success.csv", success.get("metadata") or {})
    write_pts_delta_csv(output_dir / "pts_delta_failed.csv", failed.get("metadata") or {})

    diagnosis = build_diagnosis(
        success_bundle=args.success_bundle,
        failed_bundle=args.failed_bundle,
        failed_sink_output=args.failed_sink_output,
        success=success,
        failed=failed,
        failed_sink=failed_sink,
        replay_payload=replay_payload,
        output_dir=output_dir,
    )
    write_json(output_dir / "diagnosis.json", diagnosis)
    write_comparison_csv(output_dir / "comparison.csv", diagnosis)
    write_frame_count_vs_duration(output_dir / "frame_count_vs_duration.md", diagnosis)
    write_report(output_dir / "report.md", diagnosis)

    print(json.dumps({
        "result_marker": diagnosis["result_marker"],
        "output_dir": str(output_dir),
        "report": str(output_dir / "report.md"),
        "diagnosis": str(output_dir / "diagnosis.json"),
    }, indent=2, sort_keys=True))
    return 0


def inspect_bundle(bundle_dir: Path, *, role: str) -> dict[str, Any]:
    bundle_dir = bundle_dir.resolve(strict=False)
    summary_path = bundle_dir / SUMMARY_FILE
    sidecar_path = bundle_dir / SIDECAR_FILE
    metadata_path = bundle_dir / SINK_METADATA_FILE
    raw_clip_path = find_raw_clip(bundle_dir)

    summary = read_json(summary_path)
    sidecar_rows = read_jsonl(sidecar_path)
    metadata_rows = load_json_or_jsonl(metadata_path)
    native_frames = native_video_frames(metadata_rows)
    metadata_stats = metadata_frame_stats(native_frames)
    sidecar_stats = sidecar_frame_stats(sidecar_rows)
    ffprobe = ffprobe_video(raw_clip_path)
    video_stats = video_stats_from_ffprobe(ffprobe)

    return {
        "role": role,
        "bundle_dir": str(bundle_dir),
        "raw_clip_path": str(raw_clip_path),
        "summary_path": str(summary_path),
        "sidecar_path": str(sidecar_path),
        "metadata_path": str(metadata_path),
        "summary": summary,
        "raw_ffprobe": ffprobe,
        "video": video_stats,
        "metadata": metadata_stats,
        "sidecar": sidecar_stats,
    }


def inspect_sink_output(sink_dir: Path) -> dict[str, Any]:
    sink_dir = sink_dir.resolve(strict=False)
    metadata_path = sink_dir / "metadata.json"
    video_path = find_raw_clip(sink_dir)
    rows = load_json_or_jsonl(metadata_path)
    frames = native_video_frames(rows)
    return {
        "sink_dir": str(sink_dir),
        "metadata_path": str(metadata_path),
        "video_path": str(video_path),
        "raw_ffprobe": ffprobe_video(video_path),
        "metadata": metadata_frame_stats(frames),
    }


def load_replay_payload(path: Path | None, failed_bundle: Path) -> dict[str, Any]:
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    metadata_path = failed_bundle / "metadata.json"
    if metadata_path.is_file():
        try:
            metadata = read_json(metadata_path)
            request = (
                metadata.get("replay", {})
                .get("replay_job_request")
            )
            if isinstance(request, dict):
                return request
        except Exception:
            pass
    for candidate in candidates:
        if candidate.is_file():
            return read_json(candidate)
    return {}


def build_diagnosis(
    *,
    success_bundle: Path,
    failed_bundle: Path,
    failed_sink_output: Path,
    success: dict[str, Any],
    failed: dict[str, Any],
    failed_sink: dict[str, Any],
    replay_payload: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    success_summary = success.get("summary") or {}
    failed_summary = failed.get("summary") or {}
    success_meta = success.get("metadata") or {}
    failed_meta = failed.get("metadata") or {}
    success_video = success.get("video") or {}
    failed_video = failed.get("video") or {}
    replay_request = replay_request_summary(replay_payload)

    success_duration = numeric(success_video.get("format_duration_s")) or numeric(success_video.get("stream_duration_s"))
    failed_duration = numeric(failed_video.get("format_duration_s")) or numeric(failed_video.get("stream_duration_s"))
    success_pts_duration = numeric(success_meta.get("pts_duration_s"))
    failed_pts_duration = numeric(failed_meta.get("pts_duration_s"))
    success_metadata_fps = effective_fps(success_meta.get("metadata_frame_count"), success_pts_duration)
    failed_metadata_fps = effective_fps(failed_meta.get("metadata_frame_count"), failed_pts_duration)
    success_video_fps = effective_fps(success_video.get("decoded_frame_count"), success_duration)
    failed_video_fps = effective_fps(failed_video.get("decoded_frame_count"), failed_duration)

    frame_count = replay_request.get("stop_condition_frame_count")
    frame_count_used = frame_count is not None
    failed_time_expanded = bool(
        failed_duration is not None
        and failed_duration > 15
        and failed_meta.get("metadata_frame_count") == 240
    )
    frame_count_duration_mismatch = bool(
        failed_duration is not None
        and frame_count == 240
        and abs(failed_duration - 10.0) > 2.0
    )
    success_matches_10s = bool(
        success_duration is not None
        and 8.0 <= success_duration <= 12.0
        and success_meta.get("metadata_frame_count") == 240
    )

    if (
        frame_count_used
        and frame_count_duration_mismatch
        and failed_time_expanded
        and success_matches_10s
    ):
        result_marker = RESULT_FRAME_COUNT_UNSAFE
        likely_root_cause = (
            "C2.3B used stop_condition.frame_count=240 as a duration proxy, "
            "but the event-style Replay output cadence is about "
            f"{failed_metadata_fps:.2f} FPS, so 240 metadata frames span about "
            f"{failed_pts_duration:.2f}s instead of 10s."
        )
        recommended_repair = "time_domain_window"
    elif missing_critical_data(success, failed, replay_request):
        result_marker = RESULT_INSUFFICIENT
        likely_root_cause = "critical runtime data is missing"
        recommended_repair = "collect_missing_runtime_data"
    else:
        result_marker = RESULT_ALTERNATIVE
        likely_root_cause = (
            "frame_count window mismatch was not conclusively established; "
            "inspect video-file-sink encoding, Replay PTS continuity, and ffprobe data."
        )
        recommended_repair = "alternative_root_cause_investigation"

    recommendations = [
        "C2.3B-R2 should use a time-domain evidence window based on event_ts/frame_pts and pre/post seconds.",
        "Prefer Replay time stop conditions or explicit start/end timestamps if the Replay API supports them.",
        "If Replay must over-export, clip both video and metadata by PTS/timestamp together before finalizing.",
        "Do not treat fixed frame_count=240 as a 10 second production evidence window.",
        "Do not pass by trimming metadata rows to decoded frame count when the timeline is unverified.",
        "Do not enter C2.4 identity binding until C2.3B runtime evidence timing is stable.",
    ]

    return {
        "result_marker": result_marker,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "success_bundle": str(success_bundle),
        "failed_bundle": str(failed_bundle),
        "failed_sink_output": str(failed_sink_output),
        "output_dir": str(output_dir),
        "success": condensed_case(
            success,
            metadata_fps=success_metadata_fps,
            video_fps=success_video_fps,
            duration=success_duration,
        ),
        "failed": condensed_case(
            failed,
            metadata_fps=failed_metadata_fps,
            video_fps=failed_video_fps,
            duration=failed_duration,
        ),
        "failed_sink": failed_sink,
        "replay_request": replay_request,
        "diagnosis": {
            "frame_count_used_as_duration_proxy": frame_count_used,
            "frame_count_duration_mismatch": frame_count_duration_mismatch,
            "failed_clip_looks_time_expanded": failed_time_expanded,
            "success_240_rows_looks_10s": success_matches_10s,
            "likely_root_cause": likely_root_cause,
            "recommended_repair": recommended_repair,
        },
        "recommendations": recommendations,
    }


def condensed_case(
    case: dict[str, Any],
    *,
    metadata_fps: float | None,
    video_fps: float | None,
    duration: float | None,
) -> dict[str, Any]:
    summary = case.get("summary") or {}
    metadata = case.get("metadata") or {}
    sidecar = case.get("sidecar") or {}
    video = case.get("video") or {}
    return {
        "bundle_dir": case.get("bundle_dir"),
        "raw_clip_path": case.get("raw_clip_path"),
        "video_duration_s": duration,
        "format_duration_s": video.get("format_duration_s"),
        "stream_duration_s": video.get("stream_duration_s"),
        "decoded_frame_count": video.get("decoded_frame_count"),
        "metadata_frame_count": metadata.get("metadata_frame_count"),
        "sidecar_frame_count": sidecar.get("sidecar_frame_count"),
        "effective_video_fps": video_fps,
        "effective_metadata_fps": metadata_fps,
        "pts_start": metadata.get("pts_start"),
        "pts_end": metadata.get("pts_end"),
        "pts_duration_s": metadata.get("pts_duration_s"),
        "pts_delta_ms_distribution": metadata.get("pts_delta_ms_distribution"),
        "large_pts_delta_count": metadata.get("large_pts_delta_count"),
        "duplicate_frame_pts_count": metadata.get("duplicate_frame_pts_count"),
        "non_monotonic_frame_pts_count": metadata.get("non_monotonic_frame_pts_count"),
        "source_id": metadata.get("source_id"),
        "first_frame_uuid": metadata.get("first_frame_uuid"),
        "last_frame_uuid": metadata.get("last_frame_uuid"),
        "original_metadata_frame_count": summary.get("original_metadata_frame_count"),
        "decoded_video_frame_count": summary.get("decoded_video_frame_count"),
        "summary_sidecar_frame_count": summary.get("sidecar_frame_count"),
        "trim_occurred": summary.get("trim_occurred"),
        "timeline_reconciliation_status": summary.get("timeline_reconciliation_status"),
        "production_ready": summary.get("production_ready"),
        "fallback_used": bool(summary.get("fallback_used", False)),
        "legacy_used_for_visual_binding": bool(summary.get("legacy_used_for_visual_binding", False)),
        "annotation_source_kind": summary.get("annotation_source_kind") or "production_sidecar",
        "evidence_topology": summary.get("evidence_topology"),
        "replay_source_kind": summary.get("replay_source_kind"),
        "object_counts": summary.get("object_counts"),
        "keypoints_count": summary.get("keypoints_count"),
        "face_landmarks_count": summary.get("face_landmarks_count"),
        "sidecar_object_count_distribution": sidecar.get("object_count_distribution"),
        "sidecar_empty_frame_count": sidecar.get("empty_frame_count"),
        "sidecar_trimmed_by_summary": bool(summary.get("trim_occurred") or summary.get("sidecar_trimmed")),
    }


def replay_request_summary(payload: dict[str, Any]) -> dict[str, Any]:
    configuration = payload.get("configuration") if isinstance(payload, dict) else {}
    if not isinstance(configuration, dict):
        configuration = {}
    stop_condition = payload.get("stop_condition") if isinstance(payload, dict) else {}
    if not isinstance(stop_condition, dict):
        stop_condition = {}
    offset = payload.get("offset") if isinstance(payload, dict) else {}
    if not isinstance(offset, dict):
        offset = {}
    labels = configuration.get("labels") if isinstance(configuration, dict) else {}
    if not isinstance(labels, dict):
        labels = {}
    return {
        "anchor_keyframe": payload.get("anchor_keyframe"),
        "offset_seconds": numeric(offset.get("seconds")),
        "stop_condition_frame_count": int(stop_condition["frame_count"])
        if "frame_count" in stop_condition and numeric(stop_condition.get("frame_count")) is not None
        else None,
        "stop_condition_ts_delta_sec": (
            stop_condition.get("ts_delta_sec", {}).get("max_delta_sec")
            if isinstance(stop_condition.get("ts_delta_sec"), dict)
            else None
        ),
        "stop_condition": stop_condition,
        "stored_stream_id": configuration.get("stored_stream_id"),
        "resulting_stream_id": configuration.get("resulting_stream_id"),
        "send_metadata_only": configuration.get("send_metadata_only"),
        "ts_sync": configuration.get("ts_sync"),
        "min_duration": configuration.get("min_duration"),
        "max_duration": configuration.get("max_duration"),
        "ts_discrepancy_fix_duration": configuration.get("ts_discrepancy_fix_duration"),
        "labels": labels,
    }


def metadata_frame_stats(frames: list[dict[str, Any]]) -> dict[str, Any]:
    pts_values = [int(frame["pts"]) for frame in frames if numeric(frame.get("pts")) is not None]
    timestamp_values = [
        int(frame["timestamp_ms"]) for frame in frames if numeric(frame.get("timestamp_ms")) is not None
    ]
    deltas = [
        pts_values[index + 1] - pts_values[index]
        for index in range(len(pts_values) - 1)
    ]
    duplicate_count = len(pts_values) - len(set(pts_values))
    non_monotonic_count = sum(1 for delta in deltas if delta <= 0)
    large_deltas = [
        {
            "index": index,
            "delta_ns": delta,
            "delta_s": delta / 1_000_000_000.0,
            "from_pts": pts_values[index],
            "to_pts": pts_values[index + 1],
        }
        for index, delta in enumerate(deltas)
        if delta > 100_000_000
    ]
    source_ids = Counter(str(frame.get("source_id")) for frame in frames if frame.get("source_id") is not None)
    object_counts = [len(frame.get("objects") or []) for frame in frames]
    keyframes = [
        {"index": index, "uuid": frame.get("uuid"), "pts": frame.get("pts")}
        for index, frame in enumerate(frames)
        if frame.get("keyframe") is True
    ]
    first = frames[0] if frames else {}
    last = frames[-1] if frames else {}
    return {
        "metadata_frame_count": len(frames),
        "first_frame_index": 0 if frames else None,
        "last_frame_index": len(frames) - 1 if frames else None,
        "pts_start": pts_values[0] if pts_values else None,
        "pts_end": pts_values[-1] if pts_values else None,
        "pts_duration_s": (
            (pts_values[-1] - pts_values[0]) / 1_000_000_000.0
            if len(pts_values) >= 2
            else None
        ),
        "pts_delta_count": len(deltas),
        "pts_delta_min_ms": min(deltas) / 1_000_000.0 if deltas else None,
        "pts_delta_max_ms": max(deltas) / 1_000_000.0 if deltas else None,
        "pts_delta_mean_ms": statistics.mean(deltas) / 1_000_000.0 if deltas else None,
        "pts_delta_median_ms": statistics.median(deltas) / 1_000_000.0 if deltas else None,
        "pts_delta_ms_distribution": delta_distribution(deltas),
        "pts_deltas": [
            {
                "index": index,
                "from_pts": pts_values[index],
                "to_pts": pts_values[index + 1],
                "delta_ns": delta,
                "delta_ms": delta / 1_000_000.0,
            }
            for index, delta in enumerate(deltas)
        ],
        "large_pts_deltas": large_deltas,
        "large_pts_delta_count": len(large_deltas),
        "duplicate_frame_pts_count": duplicate_count,
        "non_monotonic_frame_pts_count": non_monotonic_count,
        "timestamp_ms_start": timestamp_values[0] if timestamp_values else None,
        "timestamp_ms_end": timestamp_values[-1] if timestamp_values else None,
        "timestamp_ms_duration_s": (
            (timestamp_values[-1] - timestamp_values[0]) / 1000.0
            if len(timestamp_values) >= 2
            else None
        ),
        "source_id": source_ids.most_common(1)[0][0] if source_ids else None,
        "source_id_counts": dict(source_ids),
        "first_frame_uuid": first.get("uuid") or first.get("frame_uuid"),
        "last_frame_uuid": last.get("uuid") or last.get("frame_uuid"),
        "first_frame_sample": frame_sample(first),
        "last_frame_sample": frame_sample(last),
        "object_count_distribution": dict(Counter(object_counts)),
        "frames_with_objects_count": sum(1 for count in object_counts if count > 0),
        "empty_frame_count": sum(1 for count in object_counts if count == 0),
        "keyframe_count": len(keyframes),
        "keyframe_samples": keyframes[:10],
    }


def sidecar_frame_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pts_values = [int(row["frame_pts"]) for row in rows if numeric(row.get("frame_pts")) is not None]
    object_counts = [len(row.get("objects") or []) for row in rows]
    type_counts: Counter[str] = Counter()
    keypoints_count = 0
    landmarks_count = 0
    for row in rows:
        for obj in row.get("objects") or []:
            if not isinstance(obj, dict):
                continue
            object_type = str(obj.get("object_type") or obj.get("label") or "unknown")
            type_counts[object_type] += 1
            pose = obj.get("pose") if isinstance(obj.get("pose"), dict) else {}
            keypoints_count += len(pose.get("keypoints") or []) if isinstance(pose, dict) else 0
            landmarks = obj.get("landmarks") if isinstance(obj.get("landmarks"), dict) else {}
            landmarks_count += len(landmarks.get("points") or []) if isinstance(landmarks, dict) else 0
    return {
        "sidecar_frame_count": len(rows),
        "first_frame_index": rows[0].get("frame_index") if rows else None,
        "last_frame_index": rows[-1].get("frame_index") if rows else None,
        "first_frame_pts": pts_values[0] if pts_values else None,
        "last_frame_pts": pts_values[-1] if pts_values else None,
        "object_count_distribution": dict(Counter(object_counts)),
        "empty_frame_count": sum(1 for count in object_counts if count == 0),
        "frames_with_objects_count": sum(1 for count in object_counts if count > 0),
        "object_counts": dict(type_counts),
        "person_count": type_counts.get("person", 0),
        "face_count": type_counts.get("face", 0),
        "known_face_count": type_counts.get("known_face", 0),
        "keypoints_count": keypoints_count,
        "face_landmarks_count": landmarks_count,
    }


def ffprobe_video(video_path: Path) -> dict[str, Any]:
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
            "stream=index,codec_name,codec_type,width,height,r_frame_rate,"
            "avg_frame_rate,time_base,start_time,duration,nb_frames,nb_read_frames"
        ),
        "-of",
        "json",
        str(video_path),
    ]
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
            "error": "ffprobe_failed",
            "video_path": str(video_path),
            "stderr": exc.stderr[-1000:],
        }
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"ffprobe_json_error:{exc}", "video_path": str(video_path)}
    data["video_path"] = str(video_path)
    return data


def video_stats_from_ffprobe(ffprobe: dict[str, Any]) -> dict[str, Any]:
    streams = ffprobe.get("streams")
    stream = streams[0] if isinstance(streams, list) and streams else {}
    fmt = ffprobe.get("format") if isinstance(ffprobe.get("format"), dict) else {}
    decoded = int_or_none(stream.get("nb_read_frames")) or int_or_none(stream.get("nb_frames"))
    format_duration = numeric(fmt.get("duration"))
    stream_duration = numeric(stream.get("duration"))
    return {
        "decoded_frame_count": decoded,
        "format_duration_s": format_duration,
        "stream_duration_s": stream_duration,
        "r_frame_rate": stream.get("r_frame_rate"),
        "avg_frame_rate": stream.get("avg_frame_rate"),
        "time_base": stream.get("time_base"),
        "codec_name": stream.get("codec_name"),
        "start_time": stream.get("start_time") or fmt.get("start_time"),
        "nb_frames": stream.get("nb_frames"),
        "nb_read_frames": stream.get("nb_read_frames"),
    }


def write_comparison_csv(path: Path, diagnosis: dict[str, Any]) -> None:
    fields = [
        "case",
        "duration_s",
        "decoded_frames",
        "metadata_rows",
        "sidecar_rows",
        "effective_video_fps",
        "effective_metadata_fps",
        "pts_duration_s",
        "trim_occurred",
        "production_ready",
        "timeline_reconciliation_status",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for name in ("success", "failed"):
            case = diagnosis[name]
            writer.writerow({
                "case": name,
                "duration_s": case.get("video_duration_s"),
                "decoded_frames": case.get("decoded_frame_count"),
                "metadata_rows": case.get("metadata_frame_count"),
                "sidecar_rows": case.get("sidecar_frame_count"),
                "effective_video_fps": case.get("effective_video_fps"),
                "effective_metadata_fps": case.get("effective_metadata_fps"),
                "pts_duration_s": case.get("pts_duration_s"),
                "trim_occurred": case.get("trim_occurred"),
                "production_ready": case.get("production_ready"),
                "timeline_reconciliation_status": case.get("timeline_reconciliation_status"),
            })


def write_pts_delta_csv(path: Path, metadata: dict[str, Any]) -> None:
    fields = ["index", "from_pts", "to_pts", "delta_ns", "delta_ms"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in metadata.get("pts_deltas") or []:
            writer.writerow({field: row.get(field) for field in fields})


def write_frame_count_vs_duration(path: Path, diagnosis: dict[str, Any]) -> None:
    success = diagnosis["success"]
    failed = diagnosis["failed"]
    replay = diagnosis["replay_request"]
    lines = [
        "# Frame Count vs Duration",
        "",
        f"Replay stop_condition.frame_count: `{replay.get('stop_condition_frame_count')}`",
        f"Replay offset.seconds: `{replay.get('offset_seconds')}`",
        f"Replay anchor_keyframe: `{replay.get('anchor_keyframe')}`",
        "",
        "| Case | Metadata Rows | Decoded Frames | Video Duration s | PTS Duration s | Effective Metadata FPS | Effective Video FPS |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        comparison_row("success", success),
        comparison_row("failed", failed),
        "",
        "Conclusion: fixed frame_count is not a safe duration proxy when the Replay output cadence changes.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(path: Path, diagnosis: dict[str, Any]) -> None:
    success = diagnosis["success"]
    failed = diagnosis["failed"]
    replay = diagnosis["replay_request"]
    diag = diagnosis["diagnosis"]
    lines = [
        "# C2.3B-R Replay Event Window Time-Domain Diagnosis",
        "",
        f"Result marker: `{diagnosis['result_marker']}`",
        "",
        "## Inputs",
        "",
        f"- Success bundle: `{diagnosis['success_bundle']}`",
        f"- Failed bundle: `{diagnosis['failed_bundle']}`",
        f"- Failed sink output: `{diagnosis['failed_sink_output']}`",
        "",
        "## Comparison",
        "",
        "| Case | Duration s | Decoded Frames | Metadata Rows | Sidecar Rows | Metadata PTS Duration s | Effective Metadata FPS | Effective Video FPS | Trim | Production Ready |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        report_row("C2.3A success", success),
        report_row("C2.3B failed", failed),
        "",
        "## Required Answers",
        "",
        f"1. C2.3B 240 metadata rows correspond to `{fmt(failed.get('pts_duration_s'))}` seconds by metadata PTS.",
        f"2. C2.3B raw_clip duration is `{fmt(failed.get('video_duration_s'))}` seconds.",
        f"3. The C2.3A success sample 240 rows correspond to `{fmt(success.get('pts_duration_s'))}` seconds by metadata PTS.",
        (
            "4. Yes, cadence differs: "
            f"success metadata FPS is `{fmt(success.get('effective_metadata_fps'))}`, "
            f"failed metadata FPS is `{fmt(failed.get('effective_metadata_fps'))}`."
        ),
        (
            "5. No, `stop_condition.frame_count=240` is not safe as a 10 second evidence window. "
            f"In this failed run it produced about `{fmt(failed.get('video_duration_s'))}` seconds of video."
        ),
        (
            "6. Reason: frame_count controls how many Replay output frames are emitted, "
            "but the event-style resulting stream cadence is lower and has PTS gaps. "
            "Therefore 240 output frames no longer imply 10 seconds."
        ),
        "7. The evidence window should be expressed in time domain: event time/frame_pts plus pre/post seconds, with video and metadata clipped together by PTS.",
        (
            "8. This does not primarily look like ordinary inference frame dropping. "
            "The failed sink still has 240 metadata VideoFrame rows, but the raw clip decodes only 238 frames and spans much longer time."
        ),
        (
            "9. Lower effective cadence is involved, but production evidence must remain a 10 second time window. "
            "Reduced inference cadence must not make the evidence video 41 seconds long."
        ),
        "10. C2.3B-R2 should repair the Replay/media finalization contract with a time-domain window before C2.4 starts.",
        "",
        "## Replay Request",
        "",
        f"- stored_stream_id: `{replay.get('stored_stream_id')}`",
        f"- resulting_stream_id: `{replay.get('resulting_stream_id')}`",
        f"- anchor_keyframe: `{replay.get('anchor_keyframe')}`",
        f"- offset.seconds: `{replay.get('offset_seconds')}`",
        f"- stop_condition.frame_count: `{replay.get('stop_condition_frame_count')}`",
        f"- stop_condition.ts_delta_sec: `{replay.get('stop_condition_ts_delta_sec')}`",
        "",
        "## Diagnosis",
        "",
        f"- frame_count_used_as_duration_proxy: `{diag.get('frame_count_used_as_duration_proxy')}`",
        f"- frame_count_duration_mismatch: `{diag.get('frame_count_duration_mismatch')}`",
        f"- failed_clip_looks_time_expanded: `{diag.get('failed_clip_looks_time_expanded')}`",
        f"- likely_root_cause: {diag.get('likely_root_cause')}",
        "",
        "## Recommended C2.3B-R2 Repair",
        "",
        "Preferred: Time-domain Replay window.",
        "",
        "- Express evidence request as event_ts/frame_pts plus pre_seconds/post_seconds.",
        "- Prefer Replay time stop conditions or explicit start/end timestamps if supported.",
        "- Finalizer should align metadata frame_pts and decoded frame PTS, not row index alone.",
        "- If Replay must over-export, crop video and metadata together by PTS/timestamp.",
        "",
        "Fallback option: dynamic frame_count from measured Replay cadence, with measured_fps recorded in summary. This is weaker than time-domain clipping.",
        "",
        "Not recommended:",
        "",
        "- fixed `frame_count=240` for 10 seconds",
        "- assuming source FPS means 10 seconds equals 240 output frames",
        "- naive trim when decoded and metadata counts differ",
        "- PostgreSQL annotation fallback",
        "- legacy sidecar/viewer fallback",
        "- C2.4 identity binding before this timing issue is repaired",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def report_row(name: str, case: dict[str, Any]) -> str:
    return (
        f"| {name} | {fmt(case.get('video_duration_s'))} | {case.get('decoded_frame_count')} | "
        f"{case.get('metadata_frame_count')} | {case.get('sidecar_frame_count')} | "
        f"{fmt(case.get('pts_duration_s'))} | {fmt(case.get('effective_metadata_fps'))} | "
        f"{fmt(case.get('effective_video_fps'))} | {case.get('trim_occurred')} | {case.get('production_ready')} |"
    )


def comparison_row(name: str, case: dict[str, Any]) -> str:
    return (
        f"| {name} | {case.get('metadata_frame_count')} | {case.get('decoded_frame_count')} | "
        f"{fmt(case.get('video_duration_s'))} | {fmt(case.get('pts_duration_s'))} | "
        f"{fmt(case.get('effective_metadata_fps'))} | {fmt(case.get('effective_video_fps'))} |"
    )


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


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
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


def frame_sample(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": frame.get("source_id"),
        "uuid": frame.get("uuid") or frame.get("frame_uuid"),
        "pts": frame.get("pts") or frame.get("frame_pts"),
        "timestamp_ms": frame.get("timestamp_ms"),
        "keyframe": frame.get("keyframe"),
        "object_count": len(frame.get("objects") or []),
    }


def delta_distribution(deltas: list[int]) -> dict[str, int]:
    buckets: Counter[str] = Counter()
    for delta in deltas:
        buckets[f"{round(delta / 1_000_000.0, 3):.3f}"] += 1
    return dict(buckets.most_common(20))


def effective_fps(frame_count: Any, duration_s: Any) -> float | None:
    frame_count_i = int_or_none(frame_count)
    duration_f = numeric(duration_s)
    if frame_count_i is None or duration_f is None or duration_f <= 0:
        return None
    return frame_count_i / duration_f


def missing_critical_data(success: dict[str, Any], failed: dict[str, Any], replay: dict[str, Any]) -> bool:
    return any(
        value is None
        for value in (
            success.get("video", {}).get("decoded_frame_count"),
            failed.get("video", {}).get("decoded_frame_count"),
            success.get("metadata", {}).get("metadata_frame_count"),
            failed.get("metadata", {}).get("metadata_frame_count"),
            replay.get("stop_condition_frame_count"),
        )
    )


def numeric(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fmt(value: Any) -> str:
    number = numeric(value)
    if number is None:
        return "null"
    return f"{number:.3f}"


if __name__ == "__main__":
    raise SystemExit(main())
