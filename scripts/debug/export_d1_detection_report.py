#!/usr/bin/env python3
"""Export the D1 RTSP detection-only run report.

The D1 runtime writes behavior events to ``security.events`` and face
observations to ``security.face_observations``. This exporter reads those Redis
streams, filters the fixed D1 source, and writes report artifacts under
``manual-inspection/d1_15min_detection_latest``. It intentionally does not
create or reference clips/evidence bundles.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


FIXED_RTSP_URI = "rtsp://10.37.57.112:8554/live/1080movie"
DEFAULT_SOURCE_ID = "d1_rtsp_15min"
DEFAULT_CAMERA_ID = "cam_d1_rtsp_15min"
DEFAULT_OUTPUT_DIR = "manual-inspection/d1_15min_detection_latest"
BEHAVIOR_EVENT_TYPES = {
    "intrusion",
    "loitering",
    "crowd_gathering",
    "running",
    "chasing",
    "fall",
    "wall_climb_suspicious",
}
GALLERY_EVENT_TYPES = {"watchlist_hit", "live_search_hit", "gallery_match"}


@dataclass(frozen=True)
class BBoxConversion:
    raw: Any
    xyxy: list[float] | None
    source_format: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _safe_json_loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _json_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def bbox_to_xyxy(raw: Any, *, source_hint: str = "person") -> BBoxConversion:
    """Convert known bbox shapes to xyxy without guessing unknown formats."""
    if isinstance(raw, dict):
        keys = {"x", "y", "width", "height"}
        if keys.issubset(raw):
            x = _safe_float(raw.get("x"))
            y = _safe_float(raw.get("y"))
            w = _safe_float(raw.get("width"))
            h = _safe_float(raw.get("height"))
            if None not in (x, y, w, h):
                return BBoxConversion(raw, [x, y, x + w, y + h], "xywh")
        return BBoxConversion(raw, None, "object_unknown")

    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        vals = [_safe_float(v) for v in raw]
        if all(v is not None for v in vals):
            a, b, c, d = [float(v) for v in vals if v is not None]
            if source_hint == "face":
                return BBoxConversion(raw, [a - c / 2.0, b - d / 2.0, a + c / 2.0, b + d / 2.0], "cxcywh")
            return BBoxConversion(raw, None, "list_unknown")
        return BBoxConversion(raw, None, "list_unknown")

    return BBoxConversion(raw, None, "unavailable")


def _read_redis_stream(redis_url: str, stream: str) -> list[tuple[str, dict[str, str]]]:
    import redis

    client = redis.Redis.from_url(redis_url, decode_responses=True)
    entries = client.xrange(stream, min="-", max="+")
    return [(str(stream_id), dict(fields)) for stream_id, fields in entries]


def _stream_payloads(
    entries: Iterable[tuple[str, dict[str, str]]],
) -> list[tuple[str, dict[str, Any]]]:
    payloads: list[tuple[str, dict[str, Any]]] = []
    for stream_id, fields in entries:
        data = _safe_json_loads(fields.get("data"))
        if data:
            payloads.append((stream_id, data))
    return payloads


def collect_behavior_events(
    entries: Iterable[tuple[str, dict[str, str]]],
    *,
    source_id: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for stream_id, event in _stream_payloads(entries):
        if event.get("source_id") != source_id:
            continue
        if event.get("event_type") not in BEHAVIOR_EVENT_TYPES:
            continue

        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
        bbox_conv = bbox_to_xyxy(payload.get("bbox"), source_hint="person")
        event_ts_ms = (
            _safe_int(event.get("event_ts_ms"))
            or _safe_int(event.get("end_ts_ms"))
            or _safe_int(event.get("start_ts_ms"))
            or 0
        )

        events.append(
            {
                "stream_id": stream_id,
                "event_id": event.get("event_id") or event.get("id"),
                "source_event_id": event.get("source_event_id"),
                "camera_id": event.get("camera_id"),
                "source_id": event.get("source_id"),
                "event_type": event.get("event_type"),
                "track_id": str(event.get("track_id") or ""),
                "event_ts_ms": event_ts_ms,
                "frame_uuid": event.get("frame_uuid") or media.get("frame_uuid"),
                "keyframe_uuid": event.get("keyframe_uuid") or media.get("keyframe_uuid"),
                "bbox_raw": bbox_conv.raw,
                "bbox_xyxy": bbox_conv.xyxy,
                "bbox_source_format": bbox_conv.source_format,
                "confidence": _safe_float(event.get("confidence")),
                "zone_id": payload.get("zone_id") or event.get("zone"),
                "rule_name": payload.get("rule") or event.get("rule_name"),
                "bbox_source": payload.get("bbox_source"),
                "keypoint_confidence": payload.get("keypoint_confidence"),
            }
        )
    return events


def aggregate_people_tracks(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        track_id = str(event.get("track_id") or "unavailable")
        grouped.setdefault(track_id, []).append(event)

    tracks: list[dict[str, Any]] = []
    for track_id, items in grouped.items():
        ordered = sorted(items, key=lambda e: int(e.get("event_ts_ms") or 0))
        confidences = [c for c in (_safe_float(e.get("confidence")) for e in ordered) if c is not None]
        first = ordered[0]
        last = ordered[-1]
        first_ts = int(first.get("event_ts_ms") or 0)
        last_ts = int(last.get("event_ts_ms") or 0)
        tracks.append(
            {
                "track_id": track_id,
                "first_seen_ts_ms": first_ts,
                "last_seen_ts_ms": last_ts,
                "duration_ms": max(last_ts - first_ts, 0),
                "event_count": len(ordered),
                "max_confidence": max(confidences) if confidences else None,
                "mean_confidence": mean(confidences) if confidences else None,
                "last_bbox": last.get("bbox_xyxy"),
                "first_frame_uuid": first.get("frame_uuid"),
                "last_frame_uuid": last.get("frame_uuid"),
                "events": ordered,
            }
        )
    return sorted(tracks, key=lambda t: (-int(t["event_count"]), str(t["track_id"])))


def collect_face_observations(
    entries: Iterable[tuple[str, dict[str, str]]],
    *,
    source_id: str,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for stream_id, obs in _stream_payloads(entries):
        if obs.get("source_id") != source_id:
            continue
        payload = obs.get("payload") if isinstance(obs.get("payload"), dict) else {}
        media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
        bbox_conv = bbox_to_xyxy(obs.get("face_bbox"), source_hint="face")
        embedding = obs.get("embedding")
        embedding_status = "present" if isinstance(embedding, list) and embedding else "unavailable"

        observations.append(
            {
                "stream_id": stream_id,
                "observation_id": obs.get("observation_id") or obs.get("id"),
                "source_observation_id": obs.get("source_observation_id"),
                "camera_id": obs.get("camera_id"),
                "source_id": obs.get("source_id"),
                "track_id": str(obs.get("track_id") or ""),
                "timestamp_ms": _safe_int(obs.get("timestamp_ms")) or 0,
                "frame_uuid": media.get("frame_uuid"),
                "face_bbox_raw": obs.get("face_bbox"),
                "face_bbox_xyxy": bbox_conv.xyxy,
                "face_bbox_source_format": bbox_conv.source_format,
                "face_confidence": _safe_float(obs.get("face_confidence")),
                "landmarks": obs.get("landmarks"),
                "quality": _safe_float(obs.get("quality")),
                "embedding_status": embedding_status,
                "embedding_norm": _safe_float(obs.get("embedding_norm")),
            }
        )
    return observations


def aggregate_face_tracks(observations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        track_id = str(obs.get("track_id") or "unavailable")
        grouped.setdefault(track_id, []).append(obs)

    tracks: list[dict[str, Any]] = []
    for track_id, items in grouped.items():
        ordered = sorted(items, key=lambda o: int(o.get("timestamp_ms") or 0))
        first = ordered[0]
        last = ordered[-1]
        best = max(
            ordered,
            key=lambda o: _safe_float(o.get("quality")) if _safe_float(o.get("quality")) is not None else -1.0,
        )
        tracks.append(
            {
                "track_id": track_id,
                "face_observation_count": len(ordered),
                "first_face_ts_ms": int(first.get("timestamp_ms") or 0),
                "last_face_ts_ms": int(last.get("timestamp_ms") or 0),
                "best_quality": best.get("quality"),
                "best_face_bbox": best.get("face_bbox_xyxy"),
                "best_frame_uuid": best.get("frame_uuid"),
            }
        )
    return sorted(tracks, key=lambda t: (-int(t["face_observation_count"]), str(t["track_id"])))


def collect_gallery_hits(
    entries: Iterable[tuple[str, dict[str, str]]],
    *,
    source_id: str,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for stream_id, event in _stream_payloads(entries):
        if event.get("source_id") != source_id:
            continue
        event_type = event.get("event_type")
        if event_type not in GALLERY_EVENT_TYPES:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
        person = payload.get("person") if isinstance(payload.get("person"), dict) else {}
        match = payload.get("match") if isinstance(payload.get("match"), dict) else {}
        hits.append(
            {
                "stream_id": stream_id,
                "hit_event_id": event.get("event_id") or event.get("id"),
                "source_event_id": event.get("source_event_id"),
                "event_type": event_type,
                "person_id": event.get("person_id") or person.get("person_id"),
                "external_person_id": person.get("external_person_id"),
                "person_name": person.get("person_name") or person.get("name"),
                "similarity": _safe_float(match.get("similarity") or payload.get("similarity")),
                "match_score": _safe_float(match.get("match_score") or payload.get("match_score")),
                "threshold": _safe_float(match.get("threshold") or payload.get("threshold")),
                "source_observation_id": payload.get("source_observation_id")
                or match.get("source_observation_id"),
                "track_id": str(event.get("track_id") or payload.get("track_id") or ""),
                "frame_uuid": event.get("frame_uuid") or media.get("frame_uuid"),
                "timestamp_ms": _safe_int(event.get("event_ts_ms")) or 0,
            }
        )
    return hits


def quality_field_status(
    behavior_events: list[dict[str, Any]],
    face_observations: list[dict[str, Any]],
    gallery_hits: list[dict[str, Any]],
) -> dict[str, str]:
    return {
        "person_confidence_available": "available"
        if any(e.get("confidence") is not None for e in behavior_events)
        else "unavailable",
        "keypoint_confidence_available": "available"
        if any(e.get("keypoint_confidence") is not None for e in behavior_events)
        else "unavailable",
        "track_id_available": "available"
        if any(e.get("track_id") for e in behavior_events)
        else "unavailable",
        "bbox_source_available": "available"
        if any(e.get("bbox_source") for e in behavior_events)
        else "unavailable",
        "face_confidence_available": "available"
        if any(o.get("face_confidence") is not None for o in face_observations)
        else "unavailable",
        "landmarks_available": "available"
        if any(o.get("landmarks") for o in face_observations)
        else "unavailable",
        "face_quality_available": "available"
        if any(o.get("quality") is not None for o in face_observations)
        else "unavailable",
        "embedding_norm_available": "available"
        if any(o.get("embedding_norm") is not None for o in face_observations)
        else "unavailable",
        "gallery_similarity_available": "available"
        if any((h.get("similarity") is not None or h.get("match_score") is not None) for h in gallery_hits)
        else "unavailable",
    }


def build_summary(
    *,
    source_id: str,
    camera_id: str,
    input_uri: str,
    duration_seconds: int,
    started_at: str,
    ended_at: str,
    behavior_events: list[dict[str, Any]],
    people_tracks: list[dict[str, Any]],
    face_observations: list[dict[str, Any]],
    face_tracks: list[dict[str, Any]],
    gallery_hits: list[dict[str, Any]],
    quality_fields: dict[str, str],
) -> dict[str, Any]:
    watchlist_count = sum(1 for hit in gallery_hits if hit.get("event_type") == "watchlist_hit")
    live_search_count = sum(1 for hit in gallery_hits if hit.get("event_type") == "live_search_hit")
    gallery_match_count = sum(1 for hit in gallery_hits if hit.get("event_type") == "gallery_match")
    gallery_available = bool(gallery_hits)
    return {
        "schema_version": "1.0",
        "phase": "D1",
        "input_type": "rtsp",
        "input_uri": input_uri,
        "duration_seconds": duration_seconds,
        "duration_minutes": duration_seconds / 60.0,
        "source_id": source_id,
        "camera_id": camera_id,
        "recording_enabled": False,
        "clip_generated": False,
        "annotated_clip_generated": False,
        "evidence_bundle_generated": False,
        "local_file_used": False,
        "test_video_used": False,
        "second_rtsp_pull": False,
        "source_extraction_fallback": False,
        "clip_worker_started": False,
        "media_worker_started": False,
        "video_file_sink_started": False,
        "behavior_event_count": len(behavior_events),
        "people_track_count": len(people_tracks),
        "face_observation_count": len(face_observations),
        "face_track_count": len(face_tracks),
        "gallery_hit_count": len(gallery_hits),
        "watchlist_hit_count": watchlist_count,
        "live_search_hit_count": live_search_count,
        "gallery_match_count": gallery_match_count,
        "gallery_recognition_available": gallery_available,
        "gallery_recognition_reason": ""
        if gallery_available
        else "no_watchlist_hit_or_live_search_hit_in_run",
        "quality_fields": quality_fields,
        "started_at": started_at,
        "ended_at": ended_at,
    }


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _json_cell(row.get(field)) for field in fieldnames})


def write_outputs(
    *,
    output_dir: Path,
    summary: dict[str, Any],
    behavior_events: list[dict[str, Any]],
    people_tracks: list[dict[str, Any]],
    face_observations: list[dict[str, Any]],
    face_tracks: list[dict[str, Any]],
    gallery_hits: list[dict[str, Any]],
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in (
        "report.md",
        "summary.json",
        "people_tracks.csv",
        "people_tracks.json",
        "face_observations.csv",
        "face_observations.json",
        "gallery_hits.csv",
        "gallery_hits.json",
    ):
        try:
            (output_dir / stale).unlink()
        except FileNotFoundError:
            pass

    people_json = {
        "schema_version": "1.0",
        "source_id": summary["source_id"],
        "camera_id": summary["camera_id"],
        "tracks": people_tracks,
        "events": behavior_events,
    }
    face_json = {
        "schema_version": "1.0",
        "source_id": summary["source_id"],
        "camera_id": summary["camera_id"],
        "observations": face_observations,
        "tracks": face_tracks,
    }

    _write_json(output_dir / "summary.json", summary)
    _write_json(output_dir / "people_tracks.json", people_json)
    _write_json(output_dir / "face_observations.json", face_json)

    _write_csv(
        output_dir / "people_tracks.csv",
        people_tracks,
        [
            "track_id",
            "first_seen_ts_ms",
            "last_seen_ts_ms",
            "duration_ms",
            "event_count",
            "max_confidence",
            "mean_confidence",
            "last_bbox",
            "first_frame_uuid",
            "last_frame_uuid",
        ],
    )
    _write_csv(
        output_dir / "face_observations.csv",
        face_observations,
        [
            "observation_id",
            "source_observation_id",
            "camera_id",
            "source_id",
            "track_id",
            "timestamp_ms",
            "frame_uuid",
            "face_bbox_raw",
            "face_bbox_xyxy",
            "face_confidence",
            "landmarks",
            "quality",
            "embedding_status",
            "embedding_norm",
        ],
    )

    if gallery_hits:
        _write_json(output_dir / "gallery_hits.json", {"schema_version": "1.0", "hits": gallery_hits})
        _write_csv(
            output_dir / "gallery_hits.csv",
            gallery_hits,
            [
                "hit_event_id",
                "source_event_id",
                "event_type",
                "person_id",
                "external_person_id",
                "person_name",
                "similarity",
                "match_score",
                "threshold",
                "source_observation_id",
                "track_id",
                "frame_uuid",
                "timestamp_ms",
            ],
        )

    report = render_report(summary, output_dir)
    (output_dir / "report.md").write_text(report, encoding="utf-8")

    paths = {
        "report.md": str(output_dir / "report.md"),
        "summary.json": str(output_dir / "summary.json"),
        "people_tracks.csv": str(output_dir / "people_tracks.csv"),
        "people_tracks.json": str(output_dir / "people_tracks.json"),
        "face_observations.csv": str(output_dir / "face_observations.csv"),
        "face_observations.json": str(output_dir / "face_observations.json"),
    }
    if gallery_hits:
        paths["gallery_hits.csv"] = str(output_dir / "gallery_hits.csv")
        paths["gallery_hits.json"] = str(output_dir / "gallery_hits.json")
    return paths


def render_report(summary: dict[str, Any], output_dir: Path) -> str:
    q = summary["quality_fields"]
    gallery_line = (
        f"有，命中 {summary['gallery_hit_count']} 条"
        if summary["gallery_recognition_available"]
        else f"没有，原因：{summary['gallery_recognition_reason']}"
    )
    return f"""# D1 15分钟 RTSP 检测结果报告

## 运行范围

- 阶段：D1
- 实际运行时长：{summary['duration_seconds']} 秒（{summary['duration_minutes']:.1f} 分钟）
- 输入类型：rtsp
- 输入地址：{summary['input_uri']}
- source_id：{summary['source_id']}
- camera_id：{summary['camera_id']}
- 开始时间：{summary['started_at']}
- 结束时间：{summary['ended_at']}

## 检测结果

- 人体行为事件数：{summary['behavior_event_count']}
- 人体 track 数：{summary['people_track_count']}
- face observation 数：{summary['face_observation_count']}
- gallery/watchlist/live_search 命中：{gallery_line}
- watchlist_hit 数：{summary['watchlist_hit_count']}
- live_search_hit 数：{summary['live_search_hit_count']}

## 录像边界

- 是否生成 clip：no
- 是否生成 evidence bundle：no
- 是否启动 clip-worker：no
- 是否启动 media-worker：no
- 是否启动 video-file-sink：no
- 是否使用第二路 RTSP：no
- 是否使用本地文件或测试视频：no
- 是否使用 source extraction fallback：no

## 字段可见性

- person bbox confidence：field_status = {q['person_confidence_available']}
- keypoint confidence：field_status = {q['keypoint_confidence_available']}
- person track_id：field_status = {q['track_id_available']}
- bbox_source：field_status = {q['bbox_source_available']}
- face bbox confidence：field_status = {q['face_confidence_available']}
- landmarks：field_status = {q['landmarks_available']}
- face quality：field_status = {q['face_quality_available']}
- embedding_norm：field_status = {q['embedding_norm_available']}
- gallery similarity：field_status = {q['gallery_similarity_available']}

## 语义说明

face_observation 只表示检测到可用于人脸向量处理的人脸观测，不等于 gallery recognition。只有 watchlist_hit、live_search_hit 或 gallery_match 才表示识别命中。

## 输出文件

- report.md：{output_dir / 'report.md'}
- summary.json：{output_dir / 'summary.json'}
- people_tracks.csv：{output_dir / 'people_tracks.csv'}
- people_tracks.json：{output_dir / 'people_tracks.json'}
- face_observations.csv：{output_dir / 'face_observations.csv'}
- face_observations.json：{output_dir / 'face_observations.json'}
"""


def export_report(
    *,
    redis_url: str,
    output_dir: Path,
    source_id: str,
    camera_id: str,
    input_uri: str,
    duration_seconds: int,
    started_at: str,
    ended_at: str,
) -> dict[str, Any]:
    event_entries = _read_redis_stream(redis_url, "security.events")
    face_entries = _read_redis_stream(redis_url, "security.face_observations")

    behavior_events = collect_behavior_events(event_entries, source_id=source_id)
    people_tracks = aggregate_people_tracks(behavior_events)
    face_observations = collect_face_observations(face_entries, source_id=source_id)
    face_tracks = aggregate_face_tracks(face_observations)
    gallery_hits = collect_gallery_hits(event_entries, source_id=source_id)
    quality_fields = quality_field_status(behavior_events, face_observations, gallery_hits)
    summary = build_summary(
        source_id=source_id,
        camera_id=camera_id,
        input_uri=input_uri,
        duration_seconds=duration_seconds,
        started_at=started_at,
        ended_at=ended_at,
        behavior_events=behavior_events,
        people_tracks=people_tracks,
        face_observations=face_observations,
        face_tracks=face_tracks,
        gallery_hits=gallery_hits,
        quality_fields=quality_fields,
    )
    paths = write_outputs(
        output_dir=output_dir,
        summary=summary,
        behavior_events=behavior_events,
        people_tracks=people_tracks,
        face_observations=face_observations,
        face_tracks=face_tracks,
        gallery_hits=gallery_hits,
    )
    return {"summary": summary, "paths": paths}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", default=os.getenv("D1_REDIS_URL", "redis://127.0.0.1:6392/0"))
    parser.add_argument("--output-dir", default=os.getenv("D1_OUTPUT_DIR", DEFAULT_OUTPUT_DIR))
    parser.add_argument("--source-id", default=os.getenv("D1_SOURCE_ID", DEFAULT_SOURCE_ID))
    parser.add_argument("--camera-id", default=os.getenv("D1_CAMERA_ID", DEFAULT_CAMERA_ID))
    parser.add_argument("--input-uri", default=os.getenv("D1_INPUT_URI", FIXED_RTSP_URI))
    parser.add_argument("--duration-seconds", type=int, default=int(os.getenv("D1_DURATION_SECONDS", "900")))
    parser.add_argument("--started-at", default=os.getenv("D1_STARTED_AT", _now_iso()))
    parser.add_argument("--ended-at", default=os.getenv("D1_ENDED_AT", _now_iso()))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = export_report(
        redis_url=args.redis_url,
        output_dir=Path(args.output_dir),
        source_id=args.source_id,
        camera_id=args.camera_id,
        input_uri=args.input_uri,
        duration_seconds=args.duration_seconds,
        started_at=args.started_at,
        ended_at=args.ended_at,
    )
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
