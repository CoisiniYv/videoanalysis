#!/usr/bin/env python3
"""C2.14A RTSP segment ring index and retention manager.

This tool is deliberately conservative. It can adopt compatible post-Savant
video-file-sink outputs into a managed ring directory, build a segment index,
plan bounded retention, and resolve event-centered windows. It never deletes
outside the ring source root and dry-run is the default operational posture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]

SCHEMA_VERSION = "1.0-c2.14-segment-ring"
DEFAULT_RING_ROOT = Path("/data/video-analytics/media/rtsp-ring")
DEFAULT_SOURCE_ID = "c2_post_savant_fps_probe"
DEFAULT_MEDIA_SOURCE_DIR = Path("/data/video-analytics/media/c2-post-savant-replay-fps-probe")
DEFAULT_TTL_SECONDS = 600
DEFAULT_MAX_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_MIN_KEEP_SECONDS = 120
VIDEO_CANDIDATES = ("video.mp4", "video.mov", "raw_clip.mp4", "raw_clip.mov")
METADATA_CANDIDATES = ("metadata.json", "sink_metadata.json")
FORBIDDEN_IMAGE_KEYS = {
    "image",
    "image_base64",
    "base64",
    "base64_image",
    "crop",
    "crop_bytes",
    "image_bytes",
    "raw_frame",
    "frame_bytes",
    "jpeg",
    "png",
}
FORBIDDEN_VECTOR_KEYS = {
    "embedding",
    "embedding_vector",
    "embedding_values",
    "feature",
    "features",
}


@dataclass(frozen=True)
class SegmentCandidate:
    source_dir: Path
    video_path: Path
    metadata_path: Path
    frames: list[dict[str, Any]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def source_ring_root(ring_root: Path, source_id: str) -> Path:
    require_source_id(source_id)
    return ring_root / source_id


def segments_root(ring_root: Path, source_id: str) -> Path:
    return source_ring_root(ring_root, source_id) / "segments"


def index_path(ring_root: Path, source_id: str) -> Path:
    return source_ring_root(ring_root, source_id) / "segment_index.jsonl"


def require_source_id(source_id: str) -> str:
    value = str(source_id or "").strip()
    if not value:
        raise ValueError("source_id is required")
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError(f"unsafe source_id: {source_id!r}")
    return value


def safe_segment_id(value: str) -> str:
    chars = []
    for char in str(value):
        if char.isalnum() or char in ("-", "_", "."):
            chars.append(char)
        else:
            chars.append("-")
    cleaned = "".join(chars).strip("-._")
    return cleaned[:96] or "segment"


def load_native_metadata(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid metadata JSONL at line {line_number}: {exc}") from exc
            if isinstance(value, dict):
                rows.append(value)
        return rows
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("frames", "metadata", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid index JSONL at line {line_number}: {exc}") from exc
        if isinstance(value, dict):
            rows.append(value)
    return rows


def first_existing(directory: Path, names: Iterable[str]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def find_video_for_metadata(metadata_path: Path) -> Path | None:
    return first_existing(metadata_path.parent, VIDEO_CANDIDATES)


def discover_segment_candidates(source_dir: Path) -> list[SegmentCandidate]:
    if not source_dir.exists():
        return []
    candidates: list[SegmentCandidate] = []
    for metadata_path in sorted(source_dir.rglob("metadata.json")):
        video_path = find_video_for_metadata(metadata_path)
        if video_path is None:
            continue
        try:
            frames = load_native_metadata(metadata_path)
        except Exception:
            continue
        candidates.append(
            SegmentCandidate(
                source_dir=metadata_path.parent,
                video_path=video_path,
                metadata_path=metadata_path,
                frames=frames,
            )
        )
    return candidates


def int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def frame_pts(frame: dict[str, Any]) -> int | None:
    return int_or_none(first_present(frame, "pts", "frame_pts", "source_pts"))


def frame_timestamp_ms(frame: dict[str, Any]) -> int | None:
    direct = int_or_none(first_present(frame, "timestamp_ms", "event_ts_ms"))
    if direct is not None:
        return direct
    pts = frame_pts(frame)
    if pts is not None:
        return int(pts // 1_000_000)
    return None


def frame_uuid(frame: dict[str, Any]) -> str | None:
    value = first_present(frame, "uuid", "frame_uuid")
    text = str(value or "").strip()
    return text or None


def frame_source_id(frame: dict[str, Any]) -> str | None:
    value = frame.get("source_id")
    text = str(value or "").strip()
    return text or None


def frame_keyframe(frame: dict[str, Any]) -> bool:
    return bool(frame.get("keyframe") or frame.get("is_keyframe"))


def first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def segment_stats(candidate: SegmentCandidate, source_id: str) -> dict[str, Any]:
    frames = candidate.frames
    sources = sorted({sid for sid in (frame_source_id(frame) for frame in frames) if sid})
    pts_values = [value for value in (frame_pts(frame) for frame in frames) if value is not None]
    ts_values = [value for value in (frame_timestamp_ms(frame) for frame in frames) if value is not None]
    uuid_values = [value for value in (frame_uuid(frame) for frame in frames) if value]
    keyframe_count = sum(1 for frame in frames if frame_keyframe(frame))
    source_matches = bool(sources) and set(sources) == {source_id}
    time_basis = "pts" if pts_values else "timestamp_ms" if ts_values else "missing"
    return {
        "source_matches": source_matches,
        "metadata_source_ids": sources,
        "time_basis": time_basis,
        "first_frame_pts": min(pts_values) if pts_values else None,
        "last_frame_pts": max(pts_values) if pts_values else None,
        "first_timestamp_ms": min(ts_values) if ts_values else None,
        "last_timestamp_ms": max(ts_values) if ts_values else None,
        "first_frame_uuid": uuid_values[0] if uuid_values else None,
        "last_frame_uuid": uuid_values[-1] if uuid_values else None,
        "frame_count": len(frames),
        "keyframe_count": keyframe_count,
        "metadata_has_time_anchor": bool(pts_values or ts_values),
    }


def candidate_is_compatible(candidate: SegmentCandidate, source_id: str) -> tuple[bool, str, dict[str, Any]]:
    stats = segment_stats(candidate, source_id)
    if not candidate.video_path.is_file() or candidate.video_path.stat().st_size <= 0:
        return False, "video_missing_or_empty", stats
    if not candidate.metadata_path.is_file() or candidate.metadata_path.stat().st_size <= 0:
        return False, "metadata_missing_or_empty", stats
    if not stats["frame_count"]:
        return False, "metadata_has_no_frames", stats
    if not stats["source_matches"]:
        return False, "source_id_mismatch", stats
    if not stats["metadata_has_time_anchor"]:
        return False, "metadata_missing_pts_or_timestamp", stats
    return True, "compatible", stats


def deterministic_segment_id(candidate: SegmentCandidate, stats: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(str(candidate.metadata_path.resolve(strict=False)).encode("utf-8"))
    digest.update(str(candidate.video_path.resolve(strict=False)).encode("utf-8"))
    digest.update(str(stats.get("first_frame_pts")).encode("utf-8"))
    digest.update(str(stats.get("last_frame_pts")).encode("utf-8"))
    stem = safe_segment_id(candidate.source_dir.name)
    return f"{stem}-{digest.hexdigest()[:12]}"


def build_index_row(
    *,
    source_id: str,
    segment_id: str,
    segment_dir: Path,
    video_path: Path,
    metadata_path: Path,
    stats: dict[str, Any],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    created = created_at or utc_now()
    expires = created + timedelta(seconds=int(ttl_seconds))
    row = {
        "schema_version": SCHEMA_VERSION,
        "source_id": source_id,
        "segment_id": segment_id,
        "segment_dir": str(segment_dir),
        "video_path": str(video_path),
        "metadata_path": str(metadata_path),
        "first_frame_pts": stats.get("first_frame_pts"),
        "last_frame_pts": stats.get("last_frame_pts"),
        "first_timestamp_ms": stats.get("first_timestamp_ms"),
        "last_timestamp_ms": stats.get("last_timestamp_ms"),
        "first_frame_uuid": stats.get("first_frame_uuid"),
        "last_frame_uuid": stats.get("last_frame_uuid"),
        "frame_count": int(stats.get("frame_count") or 0),
        "keyframe_count": int(stats.get("keyframe_count") or 0),
        "size_bytes": directory_size(segment_dir),
        "created_at": isoformat(created),
        "expires_at": isoformat(expires),
        "evidence_refs": [],
        "cleanup_eligible": True,
        "completed": True,
        "time_basis": stats.get("time_basis"),
    }
    failures = validate_segment_index_row(row)
    if failures:
        raise ValueError(f"invalid segment index row {segment_id}: {failures}")
    return row


def validate_segment_index_row(row: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if not row.get("source_id"):
        failures.append("source_id_required")
    if not row.get("segment_id"):
        failures.append("segment_id_required")
    if not row.get("video_path"):
        failures.append("video_path_required")
    if not row.get("metadata_path"):
        failures.append("metadata_path_required")
    has_pts = row.get("first_frame_pts") is not None and row.get("last_frame_pts") is not None
    has_ts = row.get("first_timestamp_ms") is not None and row.get("last_timestamp_ms") is not None
    if not has_pts and not has_ts:
        failures.append("first_last_pts_or_timestamps_required")
    if int_or_none(row.get("frame_count")) is None or int(row.get("frame_count") or 0) <= 0:
        failures.append("frame_count_required")
    return failures


def adopt_candidate_into_ring(
    *,
    candidate: SegmentCandidate,
    source_id: str,
    ring_root: Path,
    ttl_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    compatible, reason, stats = candidate_is_compatible(candidate, source_id)
    if not compatible:
        return {
            "status": "skipped",
            "reason": reason,
            "source_dir": str(candidate.source_dir),
            "metadata_source_ids": stats.get("metadata_source_ids"),
            "frame_count": stats.get("frame_count"),
        }
    segment_id = deterministic_segment_id(candidate, stats)
    segment_dir = segments_root(ring_root, source_id) / segment_id
    segment_dir.mkdir(parents=True, exist_ok=True)
    video_dst = segment_dir / ("video.mp4" if candidate.video_path.suffix.lower() == ".mp4" else "video.mov")
    metadata_dst = segment_dir / "metadata.json"
    shutil.copy2(candidate.video_path, video_dst)
    shutil.copy2(candidate.metadata_path, metadata_dst)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_id": source_id,
        "segment_id": segment_id,
        "status": "complete",
        "created_by": "manage_c2_14_rtsp_segment_ring.py",
        "source_video_path": str(candidate.video_path),
        "source_metadata_path": str(candidate.metadata_path),
        "video_path": str(video_dst),
        "metadata_path": str(metadata_dst),
        "stats": stats,
    }
    write_json(segment_dir / "segment_manifest.json", manifest)
    row = build_index_row(
        source_id=source_id,
        segment_id=segment_id,
        segment_dir=segment_dir,
        video_path=video_dst,
        metadata_path=metadata_dst,
        stats=stats,
        ttl_seconds=ttl_seconds,
        created_at=now,
    )
    return {"status": "indexed", "reason": reason, "row": row, "manifest_path": str(segment_dir / "segment_manifest.json")}


def index_existing(
    *,
    source_dir: Path,
    ring_root: Path,
    source_id: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    overwrite_index: bool = True,
) -> dict[str, Any]:
    require_source_id(source_id)
    ring_source = source_ring_root(ring_root, source_id)
    ring_source.mkdir(parents=True, exist_ok=True)
    candidates = discover_segment_candidates(source_dir)
    indexed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    now = utc_now()
    for candidate in candidates:
        result = adopt_candidate_into_ring(
            candidate=candidate,
            source_id=source_id,
            ring_root=ring_root,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        if result.get("status") == "indexed":
            indexed.append(result["row"])
        else:
            skipped.append(result)
    if overwrite_index:
        write_jsonl(index_path(ring_root, source_id), indexed)
    else:
        existing = read_jsonl(index_path(ring_root, source_id))
        merged = merge_index_rows(existing + indexed)
        write_jsonl(index_path(ring_root, source_id), merged)
        indexed = merged
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "index-existing",
        "source_id": source_id,
        "source_dir": str(source_dir),
        "ring_root": str(ring_root),
        "ring_source_root": str(ring_source),
        "index_path": str(index_path(ring_root, source_id)),
        "candidate_count": len(candidates),
        "compatible_segments_found": len([row for row in indexed if row.get("source_id") == source_id]),
        "index_row_count": len(indexed),
        "indexed_segment_ids": [row["segment_id"] for row in indexed],
        "skipped_count": len(skipped),
        "skipped": skipped[:50],
    }


def merge_index_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        sid = str(row.get("segment_id") or "")
        if sid:
            by_id[sid] = row
    return sorted(by_id.values(), key=lambda row: (row.get("first_frame_pts") is None, row.get("first_frame_pts") or row.get("first_timestamp_ms") or 0, row.get("segment_id") or ""))


def inspect_config(*, ring_root: Path, source_id: str) -> dict[str, Any]:
    compose = ROOT / "infra" / "docker-compose.c2-post-savant-replay-poc.yml"
    replay_config = ROOT / "modules" / "savant_replay" / "config.c2_post_savant_replay_poc.json"
    compose_text = compose.read_text(encoding="utf-8") if compose.exists() else ""
    replay_payload = json.loads(replay_config.read_text(encoding="utf-8")) if replay_config.exists() else {}
    sink_env = docker_env("c2-poc-video-file-sink")
    chunk_size = sink_env.get("CHUNK_SIZE") or env_from_compose_text(compose_text, "CHUNK_SIZE") or "0"
    dir_location = sink_env.get("DIR_LOCATION") or env_from_compose_text(compose_text, "DIR_LOCATION") or ""
    ring_source = source_ring_root(ring_root, source_id)
    rows = read_jsonl(index_path(ring_root, source_id))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "inspect",
        "input_type": "rtsp",
        "source_id": source_id,
        "ring_root": str(ring_root),
        "ring_source_root": str(ring_source),
        "segment_index_path": str(index_path(ring_root, source_id)),
        "segment_index_exists": index_path(ring_root, source_id).exists(),
        "segment_index_row_count": len(rows),
        "video_file_sink": {
            "chunk_size": str(chunk_size),
            "dir_location": str(dir_location),
            "can_configure_chunk_size_gt_zero": True,
            "chunk_idx_supported_by_official_adapter": True,
            "current_chunk_size_is_ring_ready": str(chunk_size) not in ("", "0"),
            "current_dir_location_uses_chunk_idx": "%chunk_idx" in str(dir_location),
        },
        "replay_storage": {
            "config_path": str(replay_config),
            "data_expiration_ttl": nested(replay_payload, "storage", "rocksdb", "data_expiration_ttl"),
            "compaction_period": nested(replay_payload, "storage", "rocksdb", "compaction_period"),
            "scope": "replay_rocksdb_only_not_video_file_sink_or_evidence",
        },
        "answers": {
            "video_file_sink_can_configure_chunk_size_gt_zero": True,
            "chunk_idx_can_be_used_for_segment_path": True,
            "metadata_can_locate_event_frame_when_source_and_pts_or_uuid_match": True,
            "cleanup_exists_before_c2_14": False,
            "ring_manager_owned_directories": [str(ring_source)],
        },
    }


def env_from_compose_text(text: str, key: str) -> str | None:
    marker = f"{key}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(marker):
            return stripped.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def docker_env(container_name: str) -> dict[str, str]:
    try:
        proc = subprocess.run(
            ["docker", "inspect", container_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    if not payload:
        return {}
    env_items = (((payload[0] or {}).get("Config") or {}).get("Env") or [])
    env: dict[str, str] = {}
    for item in env_items:
        if "=" in item:
            key, value = item.split("=", 1)
            env[key] = value
    return env


def nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def active_segment_ids(rows: list[dict[str, Any]]) -> set[str]:
    completed_rows = [row for row in rows if row.get("completed", True)]
    if not completed_rows:
        return set()
    newest = max(
        completed_rows,
        key=lambda row: (
            parse_iso_datetime(row.get("created_at")) or datetime.fromtimestamp(0, timezone.utc),
            str(row.get("segment_id") or ""),
        ),
    )
    return {str(newest.get("segment_id") or "")}


def retention_plan(
    *,
    ring_root: Path,
    source_id: str,
    ttl_seconds: int,
    max_bytes: int,
    min_keep_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    require_source_id(source_id)
    current_time = now or utc_now()
    ring_source = source_ring_root(ring_root, source_id)
    seg_root = segments_root(ring_root, source_id)
    rows = read_jsonl(index_path(ring_root, source_id))
    active_ids = active_segment_ids(rows)
    total_size = sum(directory_size(Path(str(row.get("segment_dir") or ""))) for row in rows)
    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    unsafe_skipped: list[dict[str, Any]] = []
    for row in rows:
        segment_id = str(row.get("segment_id") or "")
        segment_dir = Path(str(row.get("segment_dir") or ""))
        reasons: list[str] = []
        if not is_relative_to(segment_dir, seg_root):
            unsafe_skipped.append({"segment_id": segment_id, "segment_dir": str(segment_dir), "reason": "outside_ring_segments_root"})
            continue
        if not (segment_dir / "segment_manifest.json").is_file():
            reasons.append("manifest_missing")
        if segment_id in active_ids:
            reasons.append("current_active_segment")
        if row.get("evidence_refs"):
            reasons.append("evidence_referenced")
        if not bool(row.get("cleanup_eligible")):
            reasons.append("cleanup_eligible_false")
        if not bool(row.get("completed", True)):
            reasons.append("segment_not_complete")
        created = parse_iso_datetime(row.get("created_at")) or datetime.fromtimestamp(0, timezone.utc)
        expires = parse_iso_datetime(row.get("expires_at")) or (created + timedelta(seconds=ttl_seconds))
        age_s = (current_time - created).total_seconds()
        expired = current_time >= expires and age_s >= min_keep_seconds
        row_size = directory_size(segment_dir)
        item = {
            "segment_id": segment_id,
            "segment_dir": str(segment_dir),
            "created_at": row.get("created_at"),
            "expires_at": row.get("expires_at"),
            "age_seconds": age_s,
            "size_bytes": row_size,
            "expired": expired,
        }
        if reasons:
            skipped.append({**item, "skip_reasons": reasons})
            continue
        eligible.append(item)
    eligible.sort(key=lambda item: (parse_iso_datetime(item.get("created_at")) or datetime.fromtimestamp(0, timezone.utc), item.get("segment_id") or ""))
    delete_by_ttl = [item for item in eligible if item["expired"]]
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    projected_size = total_size
    for item in delete_by_ttl:
        selected.append({**item, "delete_reason": "ttl_expired"})
        selected_ids.add(str(item["segment_id"]))
        projected_size -= int(item.get("size_bytes") or 0)
    if max_bytes > 0 and projected_size > max_bytes:
        for item in eligible:
            if str(item["segment_id"]) in selected_ids:
                continue
            selected.append({**item, "delete_reason": "max_bytes_exceeded"})
            selected_ids.add(str(item["segment_id"]))
            projected_size -= int(item.get("size_bytes") or 0)
            if projected_size <= max_bytes:
                break
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "retention-plan",
        "source_id": source_id,
        "ring_root": str(ring_root),
        "ring_source_root": str(ring_source),
        "index_path": str(index_path(ring_root, source_id)),
        "ttl_seconds": ttl_seconds,
        "max_bytes": max_bytes,
        "min_keep_seconds": min_keep_seconds,
        "total_size_bytes": total_size,
        "projected_size_bytes": max(projected_size, 0),
        "index_row_count": len(rows),
        "active_segment_ids": sorted(active_ids),
        "delete_candidates": selected,
        "delete_candidate_count": len(selected),
        "skipped_segments": skipped,
        "unsafe_rows_skipped": unsafe_skipped,
        "unsafe_deletion_target_count": 0,
    }


def apply_retention_plan(plan: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    deleted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    ring_source = Path(str(plan.get("ring_source_root") or ""))
    segments = ring_source / "segments"
    for item in plan.get("delete_candidates") or []:
        segment_dir = Path(str(item.get("segment_dir") or ""))
        if not is_relative_to(segment_dir, segments):
            errors.append({"segment_dir": str(segment_dir), "error": "outside_ring_segments_root"})
            continue
        if dry_run:
            continue
        try:
            shutil.rmtree(segment_dir)
            deleted.append(item)
        except Exception as exc:
            errors.append({"segment_dir": str(segment_dir), "error": str(exc)})
    return {
        **plan,
        "mode": "retention-dry-run" if dry_run else "retention-apply",
        "dry_run": dry_run,
        "deleted_count": len(deleted),
        "deleted_segments": deleted,
        "delete_error_count": len(errors),
        "delete_errors": errors,
    }


def find_window(
    *,
    ring_root: Path,
    source_id: str,
    center_pts: int | None,
    center_timestamp_ms: int | None,
    pre_seconds: float,
    post_seconds: float,
) -> dict[str, Any]:
    rows = read_jsonl(index_path(ring_root, source_id))
    if center_pts is None and center_timestamp_ms is None:
        raise ValueError("center_pts or center_timestamp_ms is required")
    if center_pts is not None:
        start = int(center_pts - pre_seconds * 1_000_000_000)
        end = int(center_pts + post_seconds * 1_000_000_000)
        basis = "pts"
        first_key = "first_frame_pts"
        last_key = "last_frame_pts"
    else:
        start = int(center_timestamp_ms - pre_seconds * 1000)  # type: ignore[operator]
        end = int(center_timestamp_ms + post_seconds * 1000)  # type: ignore[operator]
        basis = "timestamp_ms"
        first_key = "first_timestamp_ms"
        last_key = "last_timestamp_ms"
    selected = []
    for row in rows:
        first = int_or_none(row.get(first_key))
        last = int_or_none(row.get(last_key))
        if first is None or last is None:
            continue
        if last >= start and first <= end:
            selected.append(row)
    selected.sort(key=lambda row: int_or_none(row.get(first_key)) or 0)
    covered = bool(selected) and min(int(row[first_key]) for row in selected if row.get(first_key) is not None) <= start and max(int(row[last_key]) for row in selected if row.get(last_key) is not None) >= end
    event_located = bool(selected) and any(
        (int_or_none(row.get(first_key)) or 0) <= (center_pts if basis == "pts" else center_timestamp_ms) <= (int_or_none(row.get(last_key)) or -1)  # type: ignore[operator]
        for row in selected
    )
    reason = None
    if not selected:
        reason = "no_segment_overlap"
    elif not covered:
        reason = "segment_window_gap"
    elif not event_located:
        reason = "event_frame_not_located"
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "find-window",
        "source_id": source_id,
        "ring_root": str(ring_root),
        "basis": basis,
        "center_pts": center_pts,
        "center_timestamp_ms": center_timestamp_ms,
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
        "requested_start": start,
        "requested_end": end,
        "selected_segment_count": len(selected),
        "selected_segments": selected,
        "window_covered": bool(covered),
        "event_frame_located": bool(event_located),
        "status": "pass" if covered and event_located else "partial",
        "reason": reason,
    }


def scan_for_unsafe_payload(payload: Any) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = str(key).lower()
                child_path = f"{path}.{key}" if path else str(key)
                if lowered in FORBIDDEN_IMAGE_KEYS:
                    hits.append({"path": child_path, "kind": "image_or_crop_bytes"})
                if lowered in FORBIDDEN_VECTOR_KEYS and looks_like_vector(child):
                    hits.append({"path": child_path, "kind": "embedding_vector"})
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(payload, "")
    return {
        "passed": not hits,
        "unsafe_hit_count": len(hits),
        "hits": hits[:50],
        "payload_has_embedding": any(hit["kind"] == "embedding_vector" for hit in hits),
        "payload_has_image_bytes": any(hit["kind"] == "image_or_crop_bytes" for hit in hits),
    }


def looks_like_vector(value: Any) -> bool:
    return isinstance(value, list) and len(value) >= 16 and all(isinstance(item, (int, float)) for item in value[:16])


def print_result(result: dict[str, Any], json_output: Path | None = None) -> None:
    if json_output:
        write_json(json_output, result)
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_inspect(args: argparse.Namespace) -> dict[str, Any]:
    return inspect_config(ring_root=args.ring_root, source_id=args.source_id)


def cmd_index_existing(args: argparse.Namespace) -> dict[str, Any]:
    return index_existing(
        source_dir=args.source_dir,
        ring_root=args.ring_root,
        source_id=args.source_id,
        ttl_seconds=args.ttl_seconds,
        overwrite_index=not args.append_index,
    )


def cmd_retention(args: argparse.Namespace, *, dry_run: bool) -> dict[str, Any]:
    plan = retention_plan(
        ring_root=args.ring_root,
        source_id=args.source_id,
        ttl_seconds=args.ttl_seconds,
        max_bytes=args.max_bytes,
        min_keep_seconds=args.min_keep_seconds,
    )
    return apply_retention_plan(plan, dry_run=dry_run)


def cmd_find_window(args: argparse.Namespace) -> dict[str, Any]:
    return find_window(
        ring_root=args.ring_root,
        source_id=args.source_id,
        center_pts=args.center_pts,
        center_timestamp_ms=args.center_timestamp_ms,
        pre_seconds=args.pre_seconds,
        post_seconds=args.post_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ring-root", type=Path, default=DEFAULT_RING_ROOT)
        p.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
        p.add_argument("--json-output", type=Path)

    inspect_p = sub.add_parser("inspect")
    add_common(inspect_p)

    index_p = sub.add_parser("index-existing")
    add_common(index_p)
    index_p.add_argument("--source-dir", type=Path, required=True)
    index_p.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    index_p.add_argument("--append-index", action="store_true")

    dry_p = sub.add_parser("retention-dry-run")
    add_common(dry_p)
    dry_p.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    dry_p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    dry_p.add_argument("--min-keep-seconds", type=int, default=DEFAULT_MIN_KEEP_SECONDS)

    apply_p = sub.add_parser("retention-apply")
    add_common(apply_p)
    apply_p.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    apply_p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    apply_p.add_argument("--min-keep-seconds", type=int, default=DEFAULT_MIN_KEEP_SECONDS)

    find_p = sub.add_parser("find-window")
    add_common(find_p)
    find_p.add_argument("--center-pts", type=int)
    find_p.add_argument("--center-timestamp-ms", type=int)
    find_p.add_argument("--pre-seconds", type=float, default=5.0)
    find_p.add_argument("--post-seconds", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.mode == "inspect":
            result = cmd_inspect(args)
        elif args.mode == "index-existing":
            result = cmd_index_existing(args)
        elif args.mode == "retention-dry-run":
            result = cmd_retention(args, dry_run=True)
        elif args.mode == "retention-apply":
            result = cmd_retention(args, dry_run=False)
        elif args.mode == "find-window":
            result = cmd_find_window(args)
        else:
            raise ValueError(f"unsupported mode: {args.mode}")
        result["unsafe_payload_scan"] = scan_for_unsafe_payload(result)
        print_result(result, args.json_output)
        return 0
    except Exception as exc:
        error = {
            "schema_version": SCHEMA_VERSION,
            "mode": getattr(args, "mode", "unknown"),
            "status": "error",
            "error": str(exc),
            "result_marker": "FAIL_C2_14_RTSP_SEGMENT_RING_BLOCKED",
        }
        print_result(error, getattr(args, "json_output", None))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
