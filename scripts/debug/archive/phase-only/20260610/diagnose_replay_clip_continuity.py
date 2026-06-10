#!/usr/bin/env python3
"""Read-only C1E Replay evidence continuity diagnostic.

The script inspects an existing evidence bundle. It does not pull RTSP,
modify the database, write media files, or attempt to repair clips.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any


DECODE_ERROR_MARKERS = (
    "corrupt",
    "concealing",
    "decode_slice",
    "error while decoding",
    "invalid data",
    "missing reference",
    "non-existing",
    "no frame",
    "mmco",
)

DEFAULT_MEDIA_HOST_ROOT = Path("/data/video-analytics/media")

DEFAULT_LOG_CONTAINERS = {
    "source_adapter": "c1-official-source-adapter",
    "replay_service": "c1-official-replay-service",
    "video_file_sink": "c1-official-video-file-sink",
    "clip_worker": "c1-official-clip-worker",
    "media_worker": "c1-official-media-worker",
}

LOG_KEYWORDS = (
    "drop",
    "dropped",
    "queue",
    "buffer",
    "hwm",
    "late",
    "timeout",
    "gap",
    "dts",
    "pts",
    "decode",
    "warning",
    "error",
    "reconnect",
    "rtsp",
    "tcp",
    "udp",
    "packet",
    "frame",
    "received",
    "stored",
    "forwarded",
    "sending",
    "adding",
)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        frames = data.get("frames")
        if isinstance(frames, list):
            return [item for item in frames if isinstance(item, dict)]
        return [data]

    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _container_media_to_host(path: str | None) -> Path | None:
    if not path:
        return None
    if path.startswith("/media/"):
        return DEFAULT_MEDIA_HOST_ROOT / path.removeprefix("/media/")
    return Path(path)


def _find_raw_clip(evidence_dir: Path, metadata: dict[str, Any]) -> Path | None:
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    from_metadata = _container_media_to_host(media.get("raw_clip_path"))
    if from_metadata and from_metadata.is_file():
        return from_metadata
    for name in ("raw_clip.mov", "raw_clip.mp4", "raw_clip.webm", "raw_clip.mkv"):
        candidate = evidence_dir / name
        if candidate.is_file():
            return candidate
    for candidate in sorted(evidence_dir.glob("raw_clip.*")):
        if candidate.is_file():
            return candidate
    return None


def _find_sink_metadata(evidence_dir: Path, metadata: dict[str, Any]) -> Path:
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    from_metadata = _container_media_to_host(media.get("sink_metadata_path"))
    if from_metadata and from_metadata.is_file():
        return from_metadata
    return evidence_dir / "sink_metadata.json"


def _to_float(value: Any) -> float | None:
    try:
        if value in ("N/A", ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        if value in ("N/A", ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _median_positive(values: list[float]) -> float | None:
    positive = [value for value in values if value > 0]
    if not positive:
        return None
    return float(statistics.median(positive))


def _infer_expected_delta(values: list[tuple[int, float]]) -> float | None:
    deltas = [
        values[index][1] - values[index - 1][1]
        for index in range(1, len(values))
        if values[index][1] - values[index - 1][1] > 0
    ]
    if not deltas:
        return None
    median = _median_positive(deltas)
    if median is None:
        return None
    filtered = [delta for delta in deltas if delta <= median * 1.25]
    return _median_positive(filtered) or median


def _frame_context(frame: dict[str, Any] | None) -> dict[str, Any]:
    if not frame:
        return {}
    context: dict[str, Any] = {}
    for key in (
        "pict_type",
        "key_frame",
        "coded_picture_number",
        "display_picture_number",
        "frame_num",
    ):
        if key in frame:
            context[key] = frame.get(key)
    return context


def _detect_gaps(
    records: list[dict[str, Any]],
    field: str,
    *,
    expected_delta: float | None = None,
    threshold: float = 1.5,
    non_monotonic: bool = False,
) -> tuple[float | None, list[dict[str, Any]]]:
    values: list[tuple[int, float]] = []
    for index, record in enumerate(records):
        value = _to_float(record.get(field))
        if value is not None:
            values.append((index, value))
    if len(values) < 2:
        return expected_delta, []

    expected = expected_delta or _infer_expected_delta(values)
    if expected is None or expected <= 0:
        return expected, []

    gaps: list[dict[str, Any]] = []
    for value_index in range(1, len(values)):
        prev_index, prev_value = values[value_index - 1]
        current_index, current_value = values[value_index]
        delta = current_value - prev_value
        is_gap = delta > expected * threshold
        is_non_monotonic = non_monotonic and delta <= 0
        if not is_gap and not is_non_monotonic:
            continue
        gaps.append(
            {
                "index": current_index,
                "prev_index": prev_index,
                "field": field,
                "prev_value": prev_value,
                "value": current_value,
                "delta": delta,
                "expected_delta": expected,
                "kind": "non_monotonic" if is_non_monotonic else "gap",
                "prev_context": _frame_context(records[prev_index]),
                "context": _frame_context(records[current_index]),
            }
        )
    return expected, gaps


def _detect_frame_num_jumps(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jumps: list[dict[str, Any]] = []
    previous_index: int | None = None
    previous_num: int | None = None
    for index, record in enumerate(records):
        frame_num = _to_int(record.get("frame_num"))
        if frame_num is None:
            continue
        if previous_num is not None and frame_num != previous_num + 1:
            jumps.append(
                {
                    "index": index,
                    "prev_index": previous_index,
                    "prev_frame_num": previous_num,
                    "frame_num": frame_num,
                    "delta": frame_num - previous_num,
                }
            )
        previous_index = index
        previous_num = frame_num
    return jumps


def _run(args: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _inspect_container_env(docker: str, container: str) -> dict[str, str]:
    code, stdout, _stderr = _run(
        [docker, "inspect", container],
        timeout=20,
    )
    if code != 0:
        return {}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, list) or not data:
        return {}
    env_items = ((data[0].get("Config") or {}).get("Env") or [])
    result: dict[str, str] = {}
    for item in env_items:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, value = item.split("=", 1)
        result[key] = value
    return result


def _inspect_container_command(docker: str, container: str) -> dict[str, Any]:
    code, stdout, _stderr = _run(
        [docker, "inspect", container],
        timeout=20,
    )
    if code != 0:
        return {}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, list) or not data:
        return {}
    config = data[0].get("Config") or {}
    return {
        "entrypoint": config.get("Entrypoint") or [],
        "cmd": config.get("Cmd") or [],
        "image": config.get("Image") or "",
    }


def _keyword_log_summary(
    docker: str,
    container: str,
    *,
    since: str,
    sample_limit: int,
) -> dict[str, Any]:
    code, stdout, stderr = _run(
        [docker, "logs", "--since", since, container],
        timeout=30,
    )
    if code != 0:
        return {
            "available": False,
            "error": stderr.strip() or f"docker logs exited with status {code}",
            "keyword_count": 0,
            "samples": [],
        }
    lines = [line.strip() for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
    matched = [
        line
        for line in lines
        if any(keyword in line.lower() for keyword in LOG_KEYWORDS)
    ]
    return {
        "available": True,
        "error": "",
        "keyword_count": len(matched),
        "samples": matched[:sample_limit],
    }


def _ffprobe_frames(raw_clip: Path) -> tuple[list[dict[str, Any]], str]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return [], "ffprobe unavailable"
    args = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        (
            "frame=key_frame,pict_type,best_effort_timestamp_time,pkt_dts_time,"
            "coded_picture_number,display_picture_number"
        ),
        "-of",
        "json",
        str(raw_clip),
    ]
    code, stdout, stderr = _run(args, timeout=120)
    if code != 0:
        return [], stderr.strip() or f"ffprobe exited with status {code}"
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return [], f"ffprobe JSON parse failed: {exc}"
    frames = data.get("frames") if isinstance(data, dict) else []
    if not isinstance(frames, list):
        return [], "ffprobe did not return frame list"
    return [frame for frame in frames if isinstance(frame, dict)], stderr.strip()


def _decode_probe(raw_clip: Path) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    result = {
        "tool": ffmpeg,
        "returncode": None,
        "error_count": None,
        "error_sample": [],
        "stderr_sample": [],
        "probe_error": "",
    }
    if not ffmpeg:
        result["probe_error"] = "ffmpeg unavailable"
        return result
    args = [ffmpeg, "-hide_banner", "-nostdin", "-i", str(raw_clip), "-f", "null", "-"]
    code, _stdout, stderr = _run(args, timeout=120)
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    error_lines = [
        line
        for line in lines
        if any(marker in line.lower() for marker in DECODE_ERROR_MARKERS)
    ]
    if code != 0 and not error_lines:
        error_lines = lines[:5]
    result.update(
        {
            "returncode": code,
            "error_count": len(error_lines),
            "error_sample": error_lines[:5],
            "stderr_sample": lines[:10],
            "probe_error": "" if code == 0 else f"ffmpeg exited with status {code}",
        }
    )
    return result


def _keyframe_positions(records: list[dict[str, Any]]) -> list[int]:
    positions: list[int] = []
    for index, record in enumerate(records):
        key_frame = record.get("key_frame", record.get("keyframe"))
        if key_frame is True or key_frame == 1 or key_frame == "1":
            positions.append(index)
    return positions


def _metadata_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    status = metadata.get("status") if isinstance(metadata.get("status"), dict) else {}
    validation = (
        media.get("clip_validation") if isinstance(media.get("clip_validation"), dict) else {}
    )
    replay = metadata.get("replay") if isinstance(metadata.get("replay"), dict) else {}
    return {
        "event_id": (metadata.get("event") or {}).get("event_id")
        if isinstance(metadata.get("event"), dict)
        else "",
        "raw_clip_duration": media.get("raw_clip_duration"),
        "expected_duration_seconds": media.get("expected_duration_seconds"),
        "duration_probe_status": media.get("duration_probe_status"),
        "clip_validation_ok": validation.get("ok"),
        "clip_validation_decode_error_count": validation.get("decode_error_count"),
        "clip_validation_decode_error_sample": validation.get("decode_error_sample") or [],
        "clip_status": status.get("clip_status"),
        "replay_job_id": replay.get("replay_job_id"),
        "anchor_keyframe_uuid": replay.get("anchor_keyframe_uuid"),
        "offset_seconds": replay.get("offset_seconds"),
        "stop_condition": replay.get("stop_condition"),
        "stored_stream_id": replay.get("stored_stream_id"),
        "resulting_stream_id": replay.get("resulting_stream_id"),
    }


def _aligned(raw_gaps: list[dict[str, Any]], sink_gaps: list[dict[str, Any]]) -> str:
    if not raw_gaps or not sink_gaps:
        return "unknown"
    for raw_gap in raw_gaps:
        raw_index = int(raw_gap.get("index", -1000))
        for sink_gap in sink_gaps:
            sink_index = int(sink_gap.get("index", -2000))
            if abs(raw_index - sink_index) <= 3:
                return "yes"
    return "no"


def _diagnosis(
    *,
    sink_dts_gaps: list[dict[str, Any]],
    sink_frame_jumps: list[dict[str, Any]],
    raw_gaps: list[dict[str, Any]],
    decode_error_count: int | None,
) -> str:
    has_decode_failure = bool(decode_error_count and decode_error_count > 0)
    if sink_dts_gaps or sink_frame_jumps:
        return "upstream_gap"
    if has_decode_failure and not sink_dts_gaps and not sink_frame_jumps:
        return "sink_muxer_suspect"
    if raw_gaps and not sink_dts_gaps:
        return "sink_muxer_suspect"
    return "unknown"


def _print_value(key: str, value: Any) -> None:
    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, sort_keys=True)
    else:
        rendered = "" if value is None else str(value)
    print(f"{key}={rendered}")


def diagnose(
    evidence_dir: Path,
    sample_limit: int,
    *,
    include_docker_logs: bool = False,
    docker: str = "docker",
    log_since: str = "30m",
) -> int:
    metadata_path = evidence_dir / "metadata.json"
    metadata = _load_json(metadata_path)
    sink_metadata_path = _find_sink_metadata(evidence_dir, metadata)
    raw_clip = _find_raw_clip(evidence_dir, metadata)
    sink_records = _load_records(sink_metadata_path)
    raw_frames: list[dict[str, Any]] = []
    ffprobe_error = ""
    decode = {
        "tool": None,
        "returncode": None,
        "error_count": None,
        "error_sample": [],
        "stderr_sample": [],
        "probe_error": "raw clip not found",
    }

    if raw_clip is not None:
        raw_frames, ffprobe_error = _ffprobe_frames(raw_clip)
        decode = _decode_probe(raw_clip)

    raw_dts_expected, raw_dts_gaps = _detect_gaps(
        raw_frames,
        "pkt_dts_time",
        threshold=1.5,
        non_monotonic=True,
    )
    raw_pts_expected, raw_pts_gaps = _detect_gaps(
        raw_frames,
        "best_effort_timestamp_time",
        threshold=1.5,
        non_monotonic=True,
    )

    sink_durations = [
        float(record["duration"])
        for record in sink_records
        if isinstance(record.get("duration"), (int, float)) and record.get("duration", 0) > 0
    ]
    expected_sink_delta = _median_positive(sink_durations)
    sink_dts_expected, sink_dts_gaps = _detect_gaps(
        sink_records,
        "dts",
        expected_delta=expected_sink_delta,
        threshold=1.5,
        non_monotonic=True,
    )
    _sink_pts_expected, sink_pts_order_anomalies = _detect_gaps(
        sink_records,
        "pts",
        expected_delta=expected_sink_delta,
        threshold=1.5,
        non_monotonic=True,
    )
    pts_sorted_records = sorted(
        [record for record in sink_records if _to_float(record.get("pts")) is not None],
        key=lambda record: float(record["pts"]),
    )
    _sink_pts_sorted_expected, sink_pts_sorted_gaps = _detect_gaps(
        pts_sorted_records,
        "pts",
        expected_delta=expected_sink_delta,
        threshold=1.5,
        non_monotonic=False,
    )
    sink_frame_jumps = _detect_frame_num_jumps(sink_records)

    raw_gap_candidates = raw_dts_gaps or raw_pts_gaps
    alignment = _aligned(raw_gap_candidates, sink_dts_gaps)
    diagnosis = _diagnosis(
        sink_dts_gaps=sink_dts_gaps,
        sink_frame_jumps=sink_frame_jumps,
        raw_gaps=raw_gap_candidates,
        decode_error_count=decode.get("error_count"),
    )

    metadata_info = _metadata_summary(metadata)

    print("C1E Replay Clip Continuity Diagnosis")
    _print_value("evidence_dir", evidence_dir)
    _print_value("metadata_json", metadata_path if metadata_path.is_file() else "")
    _print_value("sink_metadata_json", sink_metadata_path if sink_metadata_path.is_file() else "")
    _print_value("raw_clip", raw_clip or "")
    for key, value in metadata_info.items():
        _print_value(key, value)
    _print_value("ffprobe_frame_error", ffprobe_error)
    _print_value("decode_probe_tool", decode.get("tool"))
    _print_value("decode_probe_returncode", decode.get("returncode"))
    _print_value("decode_probe_error_count", decode.get("error_count"))
    _print_value("decode_probe_error_sample", decode.get("error_sample"))
    _print_value("decode_probe_error", decode.get("probe_error"))
    _print_value("raw_clip_frame_count", len(raw_frames))
    _print_value("raw_clip_keyframe_positions", _keyframe_positions(raw_frames))
    _print_value("raw_clip_pkt_dts_expected_delta_seconds", raw_dts_expected)
    _print_value("raw_clip_pkt_dts_gap_count", len(raw_dts_gaps))
    _print_value("raw_clip_pkt_dts_gap_samples", raw_dts_gaps[:sample_limit])
    _print_value("raw_clip_best_effort_pts_expected_delta_seconds", raw_pts_expected)
    _print_value("raw_clip_best_effort_pts_gap_count", len(raw_pts_gaps))
    _print_value("raw_clip_best_effort_pts_gap_samples", raw_pts_gaps[:sample_limit])
    _print_value("sink_metadata_frame_count", len(sink_records))
    _print_value("sink_metadata_keyframe_positions", _keyframe_positions(sink_records))
    _print_value("sink_metadata_dts_expected_delta_ns", sink_dts_expected)
    _print_value("sink_metadata_dts_gap_count", len(sink_dts_gaps))
    _print_value("sink_metadata_dts_gap_samples", sink_dts_gaps[:sample_limit])
    _print_value("sink_metadata_pts_order_anomaly_count", len(sink_pts_order_anomalies))
    _print_value(
        "sink_metadata_pts_order_anomaly_samples",
        sink_pts_order_anomalies[:sample_limit],
    )
    _print_value("sink_metadata_pts_sorted_gap_count", len(sink_pts_sorted_gaps))
    _print_value("sink_metadata_pts_sorted_gap_samples", sink_pts_sorted_gaps[:sample_limit])
    _print_value("sink_metadata_frame_num_jump_count", len(sink_frame_jumps))
    _print_value("sink_metadata_frame_num_jump_samples", sink_frame_jumps[:sample_limit])
    _print_value("raw_clip_gap_aligns_with_sink_metadata_gap", alignment)
    _print_value("diagnosis", diagnosis)

    if include_docker_logs:
        for label, container in DEFAULT_LOG_CONTAINERS.items():
            env = _inspect_container_env(docker, container)
            command = _inspect_container_command(docker, container)
            summary = _keyword_log_summary(
                docker,
                container,
                since=log_since,
                sample_limit=sample_limit,
            )
            _print_value(f"log_{label}_container", container)
            _print_value(f"log_{label}_command", command)
            if label == "source_adapter":
                _print_value("source_adapter_env_RTSP_TRANSPORT", env.get("RTSP_TRANSPORT"))
                _print_value("source_adapter_env_RTSP_URI", env.get("RTSP_URI"))
                _print_value("source_adapter_env_LOCATION", env.get("LOCATION"))
                _print_value("source_adapter_env_ZMQ_ENDPOINT", env.get("ZMQ_ENDPOINT"))
            _print_value(f"log_{label}_available", summary["available"])
            _print_value(f"log_{label}_keyword_count", summary["keyword_count"])
            _print_value(f"log_{label}_samples", summary["samples"])
            _print_value(f"log_{label}_error", summary["error"])
    return 0 if raw_clip is not None and metadata_path.is_file() else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--sample-limit", type=int, default=10)
    parser.add_argument(
        "--include-docker-logs",
        action="store_true",
        help="Also summarize keyword-matched logs from the C1 official containers.",
    )
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--log-since", default="30m")
    args = parser.parse_args()

    evidence_dir = args.evidence_dir.resolve()
    if not evidence_dir.is_dir():
        print(f"evidence_dir_not_found={evidence_dir}", file=sys.stderr)
        return 1
    return diagnose(
        evidence_dir,
        max(args.sample_limit, 1),
        include_docker_logs=args.include_docker_logs,
        docker=args.docker,
        log_since=args.log_since,
    )


if __name__ == "__main__":
    raise SystemExit(main())
