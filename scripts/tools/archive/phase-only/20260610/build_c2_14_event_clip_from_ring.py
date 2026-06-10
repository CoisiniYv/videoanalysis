#!/usr/bin/env python3
"""Build C2.14 RTSP event evidence from a managed segment ring."""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(MEDIA_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(MEDIA_WORKER_ROOT))

from app.post_savant_metadata_annotation_builder import (  # type: ignore  # noqa: E402
    SIDECAR_ANNOTATIONS_FILE,
    build_post_savant_annotation_sidecar,
)
from app.post_savant_video_integrity import inspect_video_integrity  # type: ignore  # noqa: E402
from scripts.tools import manage_c2_14_rtsp_segment_ring as ring  # noqa: E402


SCHEMA_VERSION = "1.0-c2.14-event-clip"
DEFAULT_RING_ROOT = ring.DEFAULT_RING_ROOT
DEFAULT_SOURCE_ID = ring.DEFAULT_SOURCE_ID
DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
RESULT_PASS = "PASS_C2_14B_RTSP_EVENT_CLIP_BUILDER_READY"
RESULT_PARTIAL = "PARTIAL_C2_14B_RUNTIME_WRITER_GAP"
RESULT_FAIL = "FAIL_C2_14_RTSP_SEGMENT_RING_BLOCKED"


def run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def int_or_none(value: Any) -> int | None:
    return ring.int_or_none(value)


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def event_identity(event: dict[str, Any]) -> str:
    return str(
        first_present(
            event.get("event_id"),
            event.get("source_event_id"),
            f"{event.get('event_type') or 'event'}:{event.get('source_id') or ''}:{event.get('frame_pts') or event.get('event_ts_ms') or ''}",
        )
    )


def event_anchor(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    return {
        "source_id": str(first_present(event.get("source_id"), media.get("source_id"), DEFAULT_SOURCE_ID)),
        "frame_pts": int_or_none(first_present(event.get("frame_pts"), media.get("frame_pts"), media.get("pts"))),
        "event_ts_ms": int_or_none(first_present(event.get("event_ts_ms"), event.get("timestamp_ms"), media.get("timestamp_ms"))),
        "frame_uuid": first_present(event.get("frame_uuid"), media.get("frame_uuid")),
    }


def filter_metadata_frames(frames: list[dict[str, Any]], window: dict[str, Any]) -> list[dict[str, Any]]:
    return [item["frame"] for item in filter_metadata_frames_with_indices(frames, window)]


def filter_metadata_frames_with_indices(frames: list[dict[str, Any]], window: dict[str, Any]) -> list[dict[str, Any]]:
    basis = window["basis"]
    start = int(window["requested_start"])
    end = int(window["requested_end"])
    selected = []
    for frame_index, frame in enumerate(frames):
        if basis == "pts":
            value = ring.frame_pts(frame)
        else:
            value = ring.frame_timestamp_ms(frame)
        if value is None:
            continue
        if start <= value <= end:
            selected.append({"frame_index": frame_index, "frame": frame, "time_value": value})
    return selected


def concatenate_or_copy_raw_clip(segments: list[dict[str, Any]], output_path: Path) -> dict[str, Any]:
    video_paths = [Path(str(segment.get("video_path") or "")) for segment in segments]
    if not video_paths:
        return {"status": "failed", "reason": "no_video_segments"}
    if len(video_paths) == 1:
        shutil.copy2(video_paths[0], output_path)
        return {
            "status": "copied_single_segment",
            "raw_clip_path": str(output_path),
            "segment_video_paths": [str(video_paths[0])],
            "time_domain_crop_applied": False,
        }
    if shutil.which("ffmpeg") is None:
        return {"status": "failed", "reason": "ffmpeg_missing_for_multi_segment_concat"}
    list_path = output_path.parent / "concat_segments.txt"
    list_path.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in video_paths),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(output_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        return {"status": "failed", "reason": "ffmpeg_concat_failed", "stderr": proc.stderr[-1000:]}
    return {
        "status": "concatenated_segments",
        "raw_clip_path": str(output_path),
        "segment_video_paths": [str(path) for path in video_paths],
        "time_domain_crop_applied": False,
    }


def crop_window_clip_from_segment(
    *,
    segment: dict[str, Any],
    selected_indices: list[int],
    output_path: Path,
    requested_duration_s: float,
) -> dict[str, Any]:
    if not selected_indices:
        return {"status": "failed", "reason": "no_frames_selected_for_window"}
    if shutil.which("ffmpeg") is None:
        return {"status": "failed", "reason": "ffmpeg_missing_for_window_crop"}
    start_index = min(selected_indices)
    end_index = max(selected_indices)
    expected_count = end_index - start_index + 1
    if expected_count != len(selected_indices):
        return {
            "status": "failed",
            "reason": "non_contiguous_frame_window",
            "selected_frame_count": len(selected_indices),
            "start_frame_index": start_index,
            "end_frame_index": end_index,
        }
    if requested_duration_s <= 0:
        return {"status": "failed", "reason": "requested_duration_not_positive"}
    output_fps = len(selected_indices) / requested_duration_s
    filter_expr = f"select=between(n\\,{start_index}\\,{end_index}),setpts=N/({output_fps:.8f}*TB)"
    video_path = Path(str(segment.get("video_path") or ""))
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vf",
            filter_expr,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            f"{output_fps:.8f}",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "status": "failed",
            "reason": "ffmpeg_window_crop_failed",
            "stderr": proc.stderr[-1000:],
            "start_frame_index": start_index,
            "end_frame_index": end_index,
        }
    return {
        "status": "reencoded_single_segment_window",
        "raw_clip_path": str(output_path),
        "segment_video_paths": [str(video_path)],
        "time_domain_crop_applied": True,
        "source_frame_start_index": start_index,
        "source_frame_end_index": end_index,
        "selected_frame_count": len(selected_indices),
        "requested_duration_s": requested_duration_s,
        "output_fps": output_fps,
        "codec": "libx264",
    }


def build_window_raw_clip(
    *,
    selected_segments: list[dict[str, Any]],
    selected_by_segment: list[dict[str, Any]],
    output_path: Path,
    requested_duration_s: float,
) -> dict[str, Any]:
    non_empty = [item for item in selected_by_segment if item["selected_indices"]]
    if len(selected_segments) == 1 and len(non_empty) == 1:
        return crop_window_clip_from_segment(
            segment=selected_segments[0],
            selected_indices=non_empty[0]["selected_indices"],
            output_path=output_path,
            requested_duration_s=requested_duration_s,
        )
    return {
        "status": "failed",
        "reason": "multi_segment_window_crop_not_implemented",
        "selected_segment_count": len(selected_segments),
        "segments_with_selected_frames": len(non_empty),
    }


def event_frame_in_filtered_metadata(event: dict[str, Any], frames: list[dict[str, Any]]) -> bool:
    anchor = event_anchor(event)
    frame_pts = anchor.get("frame_pts")
    frame_uuid = anchor.get("frame_uuid")
    for frame in frames:
        if frame_uuid and ring.frame_uuid(frame) == frame_uuid:
            return True
        if frame_pts is not None and ring.frame_pts(frame) == frame_pts:
            return True
    return False


def evaluate_clip_acceptance(
    *,
    raw_clip_exists: bool,
    metadata_exists: bool,
    sidecar_exists: bool,
    selected_segment_window_covers_event: bool,
    decoded_video_frame_count: int | None,
    sidecar_frame_count: int | None,
    video_integrity_pass: bool,
    fallback_used: bool,
    legacy_used_for_visual_binding: bool,
    db_window_fallback_used: bool,
    event_style_replay_job_passed: bool,
) -> dict[str, Any]:
    failures: list[str] = []
    if not raw_clip_exists:
        failures.append("raw_clip_missing")
    if not metadata_exists:
        failures.append("metadata_missing")
    if not sidecar_exists:
        failures.append("sidecar_missing")
    if not selected_segment_window_covers_event:
        failures.append("selected_segment_window_does_not_cover_event")
    if not video_integrity_pass:
        failures.append("video_integrity_failed")
    if fallback_used:
        failures.append("fallback_used")
    if legacy_used_for_visual_binding:
        failures.append("legacy_used_for_visual_binding")
    if db_window_fallback_used:
        failures.append("db_window_fallback_used")
    if event_style_replay_job_passed:
        failures.append("event_style_replay_job_passed_must_be_false")
    if decoded_video_frame_count is None or decoded_video_frame_count <= 0:
        failures.append("decoded_video_frame_count_missing")
    if sidecar_frame_count is None or sidecar_frame_count <= 0:
        failures.append("sidecar_frame_count_missing")
    if decoded_video_frame_count is not None and sidecar_frame_count is not None and decoded_video_frame_count != sidecar_frame_count:
        failures.append("decoded_sidecar_frame_count_mismatch")
    return {
        "passed": not failures,
        "failure_reasons": failures,
        "fallback_used": fallback_used,
        "legacy_used_for_visual_binding": legacy_used_for_visual_binding,
        "db_window_fallback_used": db_window_fallback_used,
        "event_style_replay_job_passed": event_style_replay_job_passed,
    }


def build_clip_from_ring(
    *,
    event: dict[str, Any],
    ring_root: Path,
    source_id: str | None,
    pre_seconds: float,
    post_seconds: float,
    output_dir: Path,
) -> dict[str, Any]:
    anchor = event_anchor(event)
    resolved_source_id = source_id or str(anchor["source_id"])
    window = ring.find_window(
        ring_root=ring_root,
        source_id=resolved_source_id,
        center_pts=anchor["frame_pts"],
        center_timestamp_ms=None if anchor["frame_pts"] is not None else anchor["event_ts_ms"],
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    if window["status"] != "pass":
        summary = {
            "schema_version": SCHEMA_VERSION,
            "phase": "C2.14",
            "evidence_capture_mode": "rtsp_segment_ring",
            "event_identity": event_identity(event),
            "source_id": resolved_source_id,
            "status": "partial",
            "reason": window.get("reason") or "no_ring_window_coverage",
            "selected_segment_window_covers_event": False,
            "fallback_used": False,
            "legacy_used_for_visual_binding": False,
            "db_window_fallback_used": False,
            "event_style_replay_job_passed": False,
            "visual_evidence_required_for_evidence_pass": True,
            "acceptance": {"passed": False, "failure_reasons": [window.get("reason") or "no_ring_window_coverage"]},
        }
        write_json(output_dir / "summary.json", summary)
        write_json(output_dir / "event.json", event)
        write_json(output_dir / "evidence_join_report.json", window)
        write_json(output_dir / "video_integrity_report.json", {"integrity_status": "not_run", "production_gate_passed": False})
        (output_dir / "operator_rtsp_event_clip_report.html").write_text(render_html(summary, window, {"integrity_status": "not_run"}), encoding="utf-8")
        return {"status": "partial", "reason": summary["reason"], "result_marker": RESULT_PARTIAL, "bundle_path": str(output_dir), "summary": summary}

    selected_segments = window["selected_segments"]
    frames: list[dict[str, Any]] = []
    selected_by_segment: list[dict[str, Any]] = []
    for segment in selected_segments:
        segment_frames = ring.load_native_metadata(Path(str(segment["metadata_path"])))
        indexed_frames = filter_metadata_frames_with_indices(segment_frames, window)
        selected_by_segment.append(
            {
                "segment_id": segment.get("segment_id"),
                "selected_indices": [int(item["frame_index"]) for item in indexed_frames],
                "selected_frame_count": len(indexed_frames),
            }
        )
        frames.extend(item["frame"] for item in indexed_frames)
    filtered_frames = frames
    sink_metadata_path = output_dir / "sink_metadata.json"
    ring.write_jsonl(sink_metadata_path, filtered_frames)
    raw_clip_path = output_dir / "raw_clip.mp4"
    requested_duration_s = float(pre_seconds + post_seconds)
    clip_result = build_window_raw_clip(
        selected_segments=selected_segments,
        selected_by_segment=selected_by_segment,
        output_path=raw_clip_path,
        requested_duration_s=requested_duration_s,
    )
    if clip_result.get("status") == "failed":
        summary = {
            "schema_version": SCHEMA_VERSION,
            "phase": "C2.14",
            "evidence_capture_mode": "rtsp_segment_ring",
            "event_identity": event_identity(event),
            "event_type": event.get("event_type"),
            "source_id": resolved_source_id,
            "camera_id": event.get("camera_id"),
            "event_frame_pts": anchor["frame_pts"],
            "event_ts_ms": anchor["event_ts_ms"],
            "raw_clip": None,
            "sink_metadata": str(sink_metadata_path),
            "sidecar": None,
            "selected_segment_ids": [segment.get("segment_id") for segment in selected_segments],
            "selected_segment_window_covers_event": bool(window["window_covered"] and window["event_frame_located"]),
            "selected_frames_by_segment": selected_by_segment,
            "decoded_video_frame_count": None,
            "sidecar_frame_count": 0,
            "video_integrity_status": "not_run",
            "video_integrity_pass": False,
            "fallback_used": False,
            "legacy_used_for_visual_binding": False,
            "db_window_fallback_used": False,
            "event_style_replay_job_passed": False,
            "visual_evidence_required_for_evidence_pass": True,
            "clip_result": clip_result,
            "acceptance": {
                "passed": False,
                "failure_reasons": [clip_result.get("reason") or "window_clip_build_failed"],
                "fallback_used": False,
                "legacy_used_for_visual_binding": False,
                "db_window_fallback_used": False,
                "event_style_replay_job_passed": False,
            },
            "status": "partial",
            "reason": clip_result.get("reason") or "window_clip_build_failed",
        }
        write_json(output_dir / "summary.json", summary)
        write_json(output_dir / "event.json", event)
        write_json(output_dir / "evidence_join_report.json", window)
        write_json(output_dir / "video_integrity_report.json", {"integrity_status": "not_run", "production_gate_passed": False, "reason": summary["reason"]})
        scan = ring.scan_for_unsafe_payload({"event": event, "summary": summary, "join": window})
        write_json(output_dir / "unsafe_payload_scan.json", scan)
        (output_dir / "operator_rtsp_event_clip_report.html").write_text(render_html(summary, window, {"integrity_status": "not_run"}), encoding="utf-8")
        return {
            "status": "partial",
            "reason": summary["reason"],
            "result_marker": RESULT_PARTIAL if scan["passed"] else RESULT_FAIL,
            "bundle_path": str(output_dir),
            "summary": summary,
            "unsafe_payload_scan": scan,
        }
    sidecar_path = output_dir / SIDECAR_ANNOTATIONS_FILE
    sidecar_summary_path = output_dir / "summary.frame_cache.identity.json"
    sidecar = build_post_savant_annotation_sidecar(
        metadata_path=sink_metadata_path,
        output_jsonl_path=sidecar_path,
        summary_path=sidecar_summary_path,
        extra_limitations=["rtsp_segment_ring_c2_14_mvp"],
    )
    integrity = inspect_video_integrity(
        raw_clip_path,
        decode_log_path=output_dir / "video_integrity_decode_errors.log",
        requested_duration_s=requested_duration_s,
        sidecar_frame_count=len(sidecar.rows),
        trim_occurred=bool(clip_result.get("time_domain_crop_applied")),
        time_domain_crop_applied=bool(clip_result.get("time_domain_crop_applied")),
    )
    event_located_in_metadata = event_frame_in_filtered_metadata(event, filtered_frames)
    acceptance = evaluate_clip_acceptance(
        raw_clip_exists=raw_clip_path.is_file() and raw_clip_path.stat().st_size > 0,
        metadata_exists=sink_metadata_path.is_file() and sink_metadata_path.stat().st_size > 0,
        sidecar_exists=sidecar_path.is_file(),
        selected_segment_window_covers_event=bool(window["window_covered"] and window["event_frame_located"] and event_located_in_metadata),
        decoded_video_frame_count=ring.int_or_none(integrity.get("decoded_frame_count")),
        sidecar_frame_count=len(sidecar.rows),
        video_integrity_pass=bool(integrity.get("production_gate_passed")),
        fallback_used=False,
        legacy_used_for_visual_binding=False,
        db_window_fallback_used=False,
        event_style_replay_job_passed=False,
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "phase": "C2.14",
        "evidence_capture_mode": "rtsp_segment_ring",
        "event_identity": event_identity(event),
        "event_type": event.get("event_type"),
        "source_id": resolved_source_id,
        "camera_id": event.get("camera_id"),
        "event_frame_pts": anchor["frame_pts"],
        "event_ts_ms": anchor["event_ts_ms"],
        "raw_clip": str(raw_clip_path),
        "sink_metadata": str(sink_metadata_path),
        "sidecar": str(sidecar_path),
        "selected_segment_ids": [segment.get("segment_id") for segment in selected_segments],
        "selected_segment_window_covers_event": bool(window["window_covered"] and window["event_frame_located"] and event_located_in_metadata),
        "event_frame_located_in_filtered_metadata": event_located_in_metadata,
        "selected_frames_by_segment": selected_by_segment,
        "decoded_video_frame_count": integrity.get("decoded_frame_count"),
        "sidecar_frame_count": len(sidecar.rows),
        "video_integrity_status": integrity.get("integrity_status"),
        "video_integrity_pass": bool(integrity.get("production_gate_passed")),
        "fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "db_window_fallback_used": False,
        "event_style_replay_job_passed": False,
        "visual_evidence_required_for_evidence_pass": True,
        "clip_result": clip_result,
        "acceptance": acceptance,
        "status": "pass" if acceptance["passed"] else "partial",
    }
    write_json(output_dir / "summary.json", summary)
    write_json(output_dir / "event.json", event)
    write_json(output_dir / "evidence_join_report.json", window)
    write_json(output_dir / "video_integrity_report.json", integrity)
    (output_dir / "operator_rtsp_event_clip_report.html").write_text(render_html(summary, window, integrity), encoding="utf-8")
    scan = ring.scan_for_unsafe_payload({"event": event, "summary": summary, "join": window, "integrity": integrity})
    write_json(output_dir / "unsafe_payload_scan.json", scan)
    marker = RESULT_PASS if acceptance["passed"] and scan["passed"] else RESULT_PARTIAL
    if not scan["passed"]:
        marker = RESULT_FAIL
    return {
        "status": summary["status"],
        "result_marker": marker,
        "bundle_path": str(output_dir),
        "raw_clip": str(raw_clip_path),
        "summary": summary,
        "unsafe_payload_scan": scan,
    }


def render_html(summary: dict[str, Any], join: dict[str, Any], integrity: dict[str, Any]) -> str:
    rows = [
        ("Marker status", summary.get("status")),
        ("Capture mode", summary.get("evidence_capture_mode")),
        ("Source", summary.get("source_id")),
        ("Event", summary.get("event_identity")),
        ("Raw clip", summary.get("raw_clip")),
        ("Window covered", join.get("window_covered")),
        ("Event located", join.get("event_frame_located")),
        ("Video integrity", integrity.get("integrity_status")),
    ]
    body = "\n".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>C2.14 RTSP Event Clip</title></head>
<body>
  <h1>C2.14 RTSP Event Clip</h1>
  <table>{body}</table>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-json", type=Path, required=True)
    parser.add_argument("--ring-root", type=Path, default=DEFAULT_RING_ROOT)
    parser.add_argument("--source-id")
    parser.add_argument("--pre-seconds", type=float, default=5.0)
    parser.add_argument("--post-seconds", type=float, default=5.0)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir or args.evidence_root / f"c2_14_rtsp_event_clip_{run_stamp()}"
    try:
        event = load_json(args.event_json)
        result = build_clip_from_ring(
            event=event,
            ring_root=args.ring_root,
            source_id=args.source_id,
            pre_seconds=args.pre_seconds,
            post_seconds=args.post_seconds,
            output_dir=output_dir,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("result_marker") != RESULT_FAIL else 2
    except Exception as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "result_marker": RESULT_FAIL,
            "error": str(exc),
            "bundle_path": str(output_dir),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
