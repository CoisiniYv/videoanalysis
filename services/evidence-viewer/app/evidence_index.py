"""Read-only evidence bundle indexing and parsing helpers.

The viewer is intentionally file-based. It reads bundle directories under a
configured evidence root and never follows user input outside that root.
"""

from __future__ import annotations

import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


SAFE_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
RAW_CLIP_PREFERRED_NAMES = (
    "raw_clip.mp4",
    "raw_clip.mov",
    "raw_clip.webm",
    "raw_clip.mkv",
)
RAW_CLIP_UNAVAILABLE_MATERIALIZATION_STATUSES = {
    "manifest_ready",
    "materialization_pending",
    "materializing",
    "materialization_deferred",
    "materialization_failed",
    "materialization_expired",
}
EPOCH_MS_MIN = 946684800000
EPOCH_MS_MAX = 4102444800000
EVENT_CATEGORY_TYPES = {
    "identity": {"watchlist_hit", "live_search_hit"},
    "perimeter": {"intrusion", "wall_climb_suspicious"},
    "behavior": {"loitering", "running", "fall"},
    "crowd": {"crowd_gathering"},
}
_BUNDLE_DOC_CACHE_MAX = 20000
_BUNDLE_DOC_CACHE: dict[
    str,
    tuple[tuple[int, int, int, int], dict[str, Any], dict[str, Any], list[str]],
] = {}


class EvidencePathError(ValueError):
    """Raised when an event id or bundle path is outside the evidence root."""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_root(evidence_root: Path) -> Path:
    return evidence_root.resolve(strict=False)


def safe_bundle_dir(evidence_root: Path, event_id: str) -> Path:
    """Resolve an event bundle directory and reject traversal/symlink escapes."""

    if not event_id or event_id in {".", ".."}:
        raise EvidencePathError("invalid event_id")
    if "/" in event_id or "\\" in event_id:
        raise EvidencePathError("event_id must be a single path segment")
    if not SAFE_EVENT_ID_RE.fullmatch(event_id):
        raise EvidencePathError("event_id contains unsafe characters")

    root = resolve_root(evidence_root)
    candidate = (root / event_id).resolve(strict=False)
    if not _is_relative_to(candidate, root):
        raise EvidencePathError("event_id resolves outside evidence root")
    return candidate


def ensure_bundle_dir(evidence_root: Path, event_id: str) -> Path:
    bundle_dir = safe_bundle_dir(evidence_root, event_id)
    if not bundle_dir.is_dir():
        raise FileNotFoundError(f"evidence bundle not found: {event_id}")
    return bundle_dir


def safe_child_path(bundle_dir: Path, filename: str) -> Path:
    if "/" in filename or "\\" in filename or filename in {"", ".", ".."}:
        raise EvidencePathError("unsafe file name")
    base = bundle_dir.resolve(strict=False)
    child = (base / filename).resolve(strict=False)
    if not _is_relative_to(child, base):
        raise EvidencePathError("file resolves outside bundle")
    return child


def load_json_object(path: Path) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if not path.is_file():
        return {}, [f"missing:{path.name}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, [f"invalid_json:{path.name}:{exc.lineno}:{exc.msg}"]
    if not isinstance(data, dict):
        return {}, [f"not_object:{path.name}"]
    return data, warnings


def _file_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (-1, -1)
    except OSError:
        return (-2, -2)
    return (stat.st_mtime_ns, stat.st_size)


def load_bundle_index_docs(bundle_dir: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    metadata_path = bundle_dir / "metadata.json"
    summary_path = bundle_dir / "summary.json"
    signature = (*_file_signature(metadata_path), *_file_signature(summary_path))
    cache_key = str(bundle_dir)
    cached = _BUNDLE_DOC_CACHE.get(cache_key)
    if cached and cached[0] == signature:
        return cached[1], cached[2], cached[3]

    metadata, metadata_warnings = load_json_object(metadata_path)
    summary, summary_warnings = load_json_object(summary_path)
    warnings = metadata_warnings + summary_warnings
    if len(_BUNDLE_DOC_CACHE) > _BUNDLE_DOC_CACHE_MAX:
        _BUNDLE_DOC_CACHE.clear()
    _BUNDLE_DOC_CACHE[cache_key] = (signature, metadata, summary, warnings)
    return metadata, summary, warnings


def _read_yaml_doc(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return {}


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_jsonl_records(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not path.is_file():
        return records, [f"missing:{path.name}"]

    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                warnings.append(f"invalid_jsonl:{path.name}:{line_no}:{exc.msg}")
                continue
            if not isinstance(data, dict):
                warnings.append(f"non_object_jsonl:{path.name}:{line_no}")
                continue
            records.append(data)
    return records, warnings


def parse_json_or_jsonl_records(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], [f"missing:{path.name}"]

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return [], []

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return parse_jsonl_records(path)

    if isinstance(data, list):
        warnings = [
            f"non_object_json_array_item:{path.name}:{idx}"
            for idx, item in enumerate(data)
            if not isinstance(item, dict)
        ]
        return [item for item in data if isinstance(item, dict)], warnings
    if isinstance(data, dict):
        return [data], []
    return [], [f"unsupported_json:{path.name}"]


def first_sink_frame_pts(records: list[dict[str, Any]]) -> int | None:
    for record in records:
        pts = record.get("pts")
        try:
            return int(pts)
        except (TypeError, ValueError):
            continue
    return None


def _metadata_raw_clip_names(metadata: dict[str, Any]) -> list[str]:
    names: list[str] = []
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    for key in ("raw_clip_name", "raw_clip_path"):
        value = media.get(key)
        if not isinstance(value, str) or not value:
            continue
        name = Path(value).name
        if name.startswith("raw_clip.") and name not in names:
            names.append(name)
    return names


def discover_raw_clip(
    bundle_dir: Path, metadata: dict[str, Any] | None = None
) -> Path | None:
    metadata = metadata or {}
    names = _metadata_raw_clip_names(metadata)
    for name in RAW_CLIP_PREFERRED_NAMES:
        if name not in names:
            names.append(name)

    for name in names:
        try:
            candidate = safe_child_path(bundle_dir, name)
        except EvidencePathError:
            continue
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate

    for candidate in sorted(bundle_dir.glob("raw_clip.*")):
        resolved = candidate.resolve(strict=False)
        if (
            _is_relative_to(resolved, bundle_dir.resolve(strict=False))
            and candidate.is_file()
            and candidate.stat().st_size > 0
        ):
            return candidate
    return None


def media_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mov":
        return "video/quicktime"
    if suffix == ".webm":
        return "video/webm"
    if suffix == ".mkv":
        return "video/x-matroska"
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _contains_person(bundle_dir: Path, metadata: dict[str, Any], needle: str) -> bool:
    lowered = needle.lower()
    if lowered in json.dumps(metadata, sort_keys=True).lower():
        return True
    annotations_path = bundle_dir / "annotations.jsonl"
    if not annotations_path.is_file():
        return False
    try:
        with annotations_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                if lowered in raw.lower():
                    return True
    except OSError:
        return False
    return False


def _matches_filters(
    bundle_dir: Path,
    metadata: dict[str, Any],
    summary: dict[str, Any],
    filters: dict[str, str | None],
) -> bool:
    event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
    status = metadata.get("status") if isinstance(metadata.get("status"), dict) else {}
    values = {
        "event_type": event.get("event_type") or summary.get("event_type"),
        "source_id": event.get("source_id") or summary.get("source_id"),
        "camera_id": event.get("camera_id") or summary.get("camera_id"),
        "event_id": event.get("event_id") or bundle_dir.name,
        "clip_status": status.get("clip_status") or summary.get("clip_status"),
    }
    for key, expected in filters.items():
        if not expected:
            continue
        if key == "person":
            if not _contains_person(bundle_dir, metadata, expected):
                return False
            continue
        if key == "event_category":
            if expected == "all":
                continue
            actual_type = values.get("event_type")
            if not actual_type or str(actual_type) not in EVENT_CATEGORY_TYPES.get(expected, set()):
                return False
            continue
        actual = values.get(key)
        if actual is None or expected.lower() not in str(actual).lower():
            return False
    return True


def _iso_from_epoch_ms(value: Any) -> str | None:
    try:
        raw = str(value).strip()
        if not raw:
            return None
        epoch_ms = int(float(raw))
    except (TypeError, ValueError):
        return None
    if EPOCH_MS_MIN <= epoch_ms <= EPOCH_MS_MAX:
        return (
            datetime.fromtimestamp(epoch_ms / 1000, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return None


def _first_epoch_ms_from_text(value: Any) -> tuple[str | None, str | None]:
    text = str(value or "")
    for match in reversed(re.findall(r"\d{10,13}", text)):
        iso_value = _iso_from_epoch_ms(match)
        if iso_value:
            return iso_value, match
    return None, None


def alarm_machine_time(metadata: dict[str, Any], summary: dict[str, Any]) -> tuple[str | None, str | None]:
    event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
    candidates = (
        ("event.alarm_machine_time", event.get("alarm_machine_time")),
        ("event.created_at", event.get("created_at")),
        ("metadata.alarm_machine_time", metadata.get("alarm_machine_time")),
        ("metadata.created_at", metadata.get("created_at")),
        ("summary.alarm_machine_time", summary.get("alarm_machine_time")),
        ("summary.event_created_at", summary.get("event_created_at")),
    )
    for source, value in candidates:
        if value:
            return str(value), source

    for source, value in (
        ("event.event_ts_ms", event.get("event_ts_ms")),
        ("event.timestamp_ms", event.get("timestamp_ms")),
    ):
        iso_value = _iso_from_epoch_ms(value)
        if iso_value:
            return iso_value, source

    iso_value, _raw = _first_epoch_ms_from_text(event.get("source_event_id"))
    if iso_value:
        return iso_value, "event.source_event_id"

    return None, None


def load_camera_name_lookup(
    *,
    camera_config_path: Path | None = None,
    sources_config_path: Path | None = None,
) -> dict[str, str]:
    lookup: dict[str, str] = {}
    cameras_doc = _read_yaml_doc(camera_config_path)
    cameras = cameras_doc.get("cameras") if isinstance(cameras_doc, dict) else {}
    if isinstance(cameras, dict):
        for camera_id, camera in cameras.items():
            if not isinstance(camera, dict):
                continue
            name = _text_or_none(camera.get("name"))
            if not name:
                continue
            camera_id_text = str(camera_id)
            source_id = _text_or_none(camera.get("source_id"))
            lookup[f"camera_id:{camera_id_text}"] = name
            if source_id:
                lookup[f"source_id:{source_id}"] = name

    sources_doc = _read_yaml_doc(sources_config_path)
    sources = sources_doc.get("sources") if isinstance(sources_doc, dict) else {}
    if isinstance(sources, dict):
        for camera_key, source in sources.items():
            if not isinstance(source, dict):
                continue
            camera_name = _text_or_none(source.get("camera_name"))
            camera_id = _text_or_none(source.get("camera_id")) or str(camera_key)
            source_id = _text_or_none(source.get("source_id"))
            if camera_name:
                lookup.setdefault(f"camera_id:{camera_id}", camera_name)
                if source_id:
                    lookup.setdefault(f"source_id:{source_id}", camera_name)
            elif camera_id and source_id and f"camera_id:{camera_id}" in lookup:
                lookup.setdefault(f"source_id:{source_id}", lookup[f"camera_id:{camera_id}"])
    return lookup


def camera_name_for_bundle(
    metadata: dict[str, Any],
    summary: dict[str, Any],
    camera_name_lookup: dict[str, str] | None = None,
) -> str | None:
    event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
    camera = metadata.get("camera") if isinstance(metadata.get("camera"), dict) else {}
    direct = (
        _text_or_none(event.get("camera_name"))
        or _text_or_none(metadata.get("camera_name"))
        or _text_or_none(summary.get("camera_name"))
        or _text_or_none(camera.get("name"))
    )
    if direct:
        return direct
    lookup = camera_name_lookup or {}
    camera_id = _text_or_none(event.get("camera_id")) or _text_or_none(summary.get("camera_id"))
    source_id = _text_or_none(event.get("source_id")) or _text_or_none(summary.get("source_id"))
    if camera_id and lookup.get(f"camera_id:{camera_id}"):
        return lookup[f"camera_id:{camera_id}"]
    if source_id and lookup.get(f"source_id:{source_id}"):
        return lookup[f"source_id:{source_id}"]
    return None


def bundle_summary(
    bundle_dir: Path,
    *,
    camera_name_lookup: dict[str, str] | None = None,
) -> dict[str, Any]:
    metadata, metadata_warnings = load_json_object(bundle_dir / "metadata.json")
    summary, summary_warnings = load_json_object(bundle_dir / "summary.json")
    event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
    status = metadata.get("status") if isinstance(metadata.get("status"), dict) else {}
    annotations_meta = (
        metadata.get("annotations")
        if isinstance(metadata.get("annotations"), dict)
        else {}
    )
    raw_clip = discover_raw_clip(bundle_dir, metadata)
    materialization = materialization_summary(metadata, summary, raw_clip=raw_clip)
    raw_clip_playable = raw_clip is not None and not materialization.get(
        "raw_clip_unavailable_reason"
    )
    annotations_path = bundle_dir / "annotations.jsonl"
    alarm_time, alarm_time_source = alarm_machine_time(metadata, summary)
    event_id = event.get("event_id") or summary.get("event_id") or bundle_dir.name
    camera_name = camera_name_for_bundle(metadata, summary, camera_name_lookup)
    return {
        "event_id": event_id,
        "event_type": event.get("event_type") or summary.get("event_type"),
        "source_id": event.get("source_id") or summary.get("source_id"),
        "camera_id": event.get("camera_id") or summary.get("camera_id"),
        "camera_name": camera_name,
        "alarm_machine_time": alarm_time,
        "alarm_machine_time_source": alarm_time_source,
        "raw_clip_available": raw_clip_playable,
        "raw_clip_name": raw_clip.name if raw_clip else None,
        "raw_clip_url": f"/api/bundles/{event_id}/media/raw_clip"
        if raw_clip_playable
        else None,
        **materialization,
        "annotations_available": annotations_path.is_file(),
        "annotation_lines": summary.get("annotation_lines"),
        "clip_status": status.get("clip_status") or summary.get("clip_status"),
        "frontend_overlay_required": annotations_meta.get(
            "frontend_overlay_required", summary.get("frontend_overlay_required")
        ),
        "matched_objects": summary.get("matched_objects"),
        "unknown_objects": summary.get("unknown_objects"),
        "created_at": metadata.get("created_at") or event.get("created_at"),
        "warnings": metadata_warnings + summary_warnings,
    }


def scan_bundles(
    evidence_root: Path,
    *,
    filters: dict[str, str | None] | None = None,
    limit: int = 200,
    offset: int = 0,
    camera_name_lookup: dict[str, str] | None = None,
    materialize_all_matches: bool = False,
) -> dict[str, Any]:
    root = resolve_root(evidence_root)
    if not root.is_dir():
        return {
            "bundles": [],
            "total": 0,
            "limit": limit,
            "offset": offset,
            "warnings": [f"evidence_root_missing:{root}"],
        }

    filters = filters or {}
    active_filters = {key: value for key, value in filters.items() if value and not (key == "event_category" and value == "all")}
    candidates = [path for path in root.iterdir() if path.is_dir()]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)

    if not active_filters and not materialize_all_matches:
        safe_candidates: list[Path] = []
        warnings: list[str] = []
        for bundle_dir in candidates:
            if SAFE_EVENT_ID_RE.fullmatch(bundle_dir.name):
                safe_candidates.append(bundle_dir)
            else:
                warnings.append(f"skipped_unsafe_bundle_name:{bundle_dir.name}")
        start = max(0, offset)
        end = start + max(1, limit)
        return {
            "bundles": [
                bundle_summary(bundle_dir, camera_name_lookup=camera_name_lookup)
                for bundle_dir in safe_candidates[start:end]
            ],
            "total": len(safe_candidates),
            "limit": limit,
            "offset": start,
            "warnings": warnings,
        }

    page_dirs: list[tuple[Path, dict[str, Any], dict[str, Any], list[str]]] = []
    matched: list[dict[str, Any]] = []
    warnings: list[str] = []
    matched_count = 0
    start = max(0, offset)
    end = start + max(1, limit)
    for bundle_dir in candidates:
        if not SAFE_EVENT_ID_RE.fullmatch(bundle_dir.name):
            warnings.append(f"skipped_unsafe_bundle_name:{bundle_dir.name}")
            continue
        metadata, summary, load_warnings = load_bundle_index_docs(bundle_dir)
        if not _matches_filters(bundle_dir, metadata, summary, filters):
            continue
        if materialize_all_matches or start <= matched_count < end:
            page_dirs.append(
                (
                    bundle_dir,
                    metadata,
                    summary,
                    load_warnings,
                )
            )
        matched_count += 1

    for bundle_dir, _metadata, _summary, load_warnings in page_dirs:
        item = bundle_summary(bundle_dir, camera_name_lookup=camera_name_lookup)
        item["warnings"] = sorted(set(item.get("warnings", []) + load_warnings))
        matched.append(item)

    return {
        "bundles": matched if materialize_all_matches else matched[: max(1, limit)],
        "total": matched_count,
        "limit": limit,
        "offset": start,
        "warnings": warnings,
    }


def bundle_manifest(
    evidence_root: Path,
    event_id: str,
    *,
    camera_name_lookup: dict[str, str] | None = None,
) -> dict[str, Any]:
    bundle_dir = ensure_bundle_dir(evidence_root, event_id)
    metadata, metadata_warnings = load_json_object(bundle_dir / "metadata.json")
    summary, summary_warnings = load_json_object(bundle_dir / "summary.json")
    raw_clip = discover_raw_clip(bundle_dir, metadata)
    materialization = materialization_summary(metadata, summary, raw_clip=raw_clip)
    raw_clip_playable = raw_clip is not None and not materialization.get(
        "raw_clip_unavailable_reason"
    )
    available_files = sorted(path.name for path in bundle_dir.iterdir() if path.is_file())
    warnings = metadata_warnings + summary_warnings
    if raw_clip is None:
        warnings.append("raw_clip_missing")
    elif not raw_clip_playable:
        warnings.append(str(materialization["raw_clip_unavailable_reason"]))
    alarm_time, alarm_time_source = alarm_machine_time(metadata, summary)
    camera_name = camera_name_for_bundle(metadata, summary, camera_name_lookup)
    return {
        "event_id": event_id,
        "camera_name": camera_name,
        "alarm_machine_time": alarm_time,
        "alarm_machine_time_source": alarm_time_source,
        "metadata": metadata,
        "summary": summary,
        "raw_clip_url": f"/api/bundles/{event_id}/media/raw_clip"
        if raw_clip_playable
        else None,
        "raw_clip_name": raw_clip.name if raw_clip else None,
        **materialization,
        "annotations_url": f"/api/bundles/{event_id}/annotations",
        "sink_metadata_url": f"/api/bundles/{event_id}/sink-metadata",
        "available_files": available_files,
        "warnings": warnings,
    }


def materialization_summary(
    metadata: dict[str, Any],
    summary: dict[str, Any],
    *,
    raw_clip: Path | None,
) -> dict[str, Any]:
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    status_doc = metadata.get("status") if isinstance(metadata.get("status"), dict) else {}
    materialization_status = _text_or_none(
        media.get("materialization_status")
        or status_doc.get("materialization_status")
        or summary.get("materialization_status")
    )
    if not materialization_status and raw_clip is not None:
        materialization_status = "materialized"
    materialization_reason = _text_or_none(
        media.get("materialization_reason")
        or media.get("materialization_defer_reason")
        or media.get("materialization_failure_reason")
        or media.get("materialization_expired_reason")
        or summary.get("materialization_reason")
    )
    return {
        "materialization_status": materialization_status,
        "materialization_reason": materialization_reason,
        "materialization_deadline_at": _text_or_none(
            media.get("materialization_deadline_at")
            or summary.get("materialization_deadline_at")
        ),
        "quota_decision": media.get("quota_decision")
        if isinstance(media.get("quota_decision"), dict)
        else {},
        "degrade_decision": media.get("degrade_decision")
        if isinstance(media.get("degrade_decision"), dict)
        else {},
        "raw_clip_unavailable_reason": raw_clip_unavailable_reason_for_status(
            materialization_status
        ),
    }


def raw_clip_unavailable_reason_for_status(status: str | None) -> str | None:
    text = _text_or_none(status)
    if text in RAW_CLIP_UNAVAILABLE_MATERIALIZATION_STATUSES:
        return f"raw_clip_unavailable:{text}"
    return None


def normalize_bbox(
    bbox: dict[str, Any],
    *,
    width: float,
    height: float,
) -> dict[str, float] | None:
    values = bbox.get("values")
    fmt = str(bbox.get("format") or "cxcywh").lower()
    if not isinstance(values, list):
        if isinstance(bbox.get("xyxy"), list):
            values = bbox.get("xyxy")
            fmt = "xyxy"
        elif isinstance(bbox.get("xywh"), list):
            values = bbox.get("xywh")
            fmt = "xywh"
        elif isinstance(bbox.get("cxcywh"), list):
            values = bbox.get("cxcywh")
            fmt = "cxcywh"
    if not isinstance(values, list) or len(values) < 4:
        return None
    try:
        v0, v1, v2, v3 = [float(value) for value in values[:4]]
    except (TypeError, ValueError):
        return None

    if "cxcywh" in fmt:
        x1, y1, x2, y2 = v0 - v2 / 2, v1 - v3 / 2, v0 + v2 / 2, v1 + v3 / 2
    elif "xyxy" in fmt:
        x1, y1, x2, y2 = v0, v1, v2, v3
    elif fmt == "xywh" or "xywh" in fmt:
        x1, y1, x2, y2 = v0, v1, v0 + v2, v1 + v3
    else:
        return None

    x1 = min(max(0.0, x1), width)
    y1 = min(max(0.0, y1), height)
    x2 = min(max(0.0, x2), width)
    y2 = min(max(0.0, y2), height)
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def overlay_objects_for_annotation(annotation: dict[str, Any]) -> list[dict[str, Any]]:
    """Return drawable overlay objects for one annotation record."""
    objects: list[dict[str, Any]] = []
    raw_objects = annotation.get("objects")
    if isinstance(raw_objects, list):
        objects.extend(obj for obj in raw_objects if isinstance(obj, dict))

    if annotation.get("record_type") != "object_annotation":
        return objects

    obj: dict[str, Any] = {}
    for key in (
        "object_type",
        "annotation_role",
        "track_id",
        "bbox",
        "identity",
        "label",
        "action",
        "style",
        "landmarks",
        "pose",
    ):
        if key in annotation:
            obj[key] = annotation[key]
    if obj:
        objects.append(obj)
    return objects


def annotation_time_seconds(
    annotation: dict[str, Any], first_video_frame_pts: int | None
) -> tuple[float | None, str]:
    frame_pts = annotation.get("frame_pts")
    if first_video_frame_pts is not None and frame_pts is not None:
        try:
            return (int(frame_pts) - int(first_video_frame_pts)) / 1_000_000_000, "frame_pts"
        except (TypeError, ValueError):
            pass
    time_offset_ms = annotation.get("time_offset_ms")
    if time_offset_ms is not None:
        try:
            return float(time_offset_ms) / 1000.0, "time_offset_ms_fallback"
        except (TypeError, ValueError):
            pass
    return None, "missing_time"


def object_is_matched(obj: dict[str, Any]) -> bool:
    identity = obj.get("identity") if isinstance(obj.get("identity"), dict) else {}
    return bool(
        identity.get("status") == "matched"
        or identity.get("match_status") == "above_threshold"
        or identity.get("external_person_id")
        or identity.get("person_id") is not None
    )


def object_is_behavior_event(obj: dict[str, Any]) -> bool:
    label = obj.get("label") if isinstance(obj.get("label"), dict) else {}
    action = obj.get("action") if isinstance(obj.get("action"), dict) else {}
    return bool(
        obj.get("object_type") == "person"
        and (
            obj.get("annotation_role") == "behavior_event"
            or label.get("kind") == "behavior_event"
            or action.get("event_type") == "intrusion"
            or action.get("status") == "event_triggered"
        )
    )


def object_is_person_context(obj: dict[str, Any]) -> bool:
    return bool(
        obj.get("object_type") == "person"
        and (
            obj.get("annotation_role") == "person_context"
            or _as_role_style_reason(obj) == "person_detection"
        )
    )


def _as_role_style_reason(obj: dict[str, Any]) -> str:
    style = obj.get("style") if isinstance(obj.get("style"), dict) else {}
    return str(style.get("reason") or "")


def object_visible_by_default(obj: dict[str, Any]) -> bool:
    return (
        object_is_person_context(obj)
        or object_is_behavior_event(obj)
        or object_is_matched(obj)
    )


def object_label(obj: dict[str, Any], annotation: dict[str, Any]) -> str:
    if object_is_person_context(obj):
        track = obj.get("track_id") or ""
        return f"Person | track {track}" if track else "Person"

    if object_is_behavior_event(obj):
        label = obj.get("label") if isinstance(obj.get("label"), dict) else {}
        text = label.get("text")
        if text:
            return str(text)
        action = obj.get("action") if isinstance(obj.get("action"), dict) else {}
        event_type = str(action.get("event_type") or "Intrusion")
        return event_type.replace("_", " ").title()

    identity = obj.get("identity") if isinstance(obj.get("identity"), dict) else {}
    track = obj.get("track_id") or ""
    ts = annotation.get("timestamp_ms") or ""
    if not object_is_matched(obj):
        parts = ["Unknown face"]
        if track:
            parts.append(f"track {track}")
        if ts:
            parts.append(f"ts {ts}")
        return " | ".join(parts)

    name = (
        identity.get("display_name")
        or identity.get("external_person_id")
        or "Matched face"
    )
    similarity = identity.get("similarity")
    try:
        sim_text = f"{float(similarity):.3f}"
    except (TypeError, ValueError):
        sim_text = "n/a"
    parts = [f"{name} {sim_text}"]
    if track:
        parts.append(f"track {track}")
    if ts:
        parts.append(f"ts {ts}")
    return " | ".join(parts)


def object_style(obj: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    style = obj.get("style") if isinstance(obj.get("style"), dict) else {}
    identity = obj.get("identity") if isinstance(obj.get("identity"), dict) else {}
    warnings: list[str] = []

    if object_is_person_context(obj):
        color = style.get("bbox_color") or style.get("color") or "#00C853"
        return (
            {
                "bbox_color": color,
                "label_color": style.get("label_color") or color,
                "line_width": style.get("line_width") or 2,
                "priority": style.get("priority") if style.get("priority") is not None else "context",
                "reason": style.get("reason") or "person_detection",
            },
            warnings,
        )

    if object_is_behavior_event(obj):
        color = style.get("bbox_color") or "#FF6D00"
        action = obj.get("action") if isinstance(obj.get("action"), dict) else {}
        reason = style.get("reason") or action.get("event_type") or "behavior_event"
        priority = style.get("priority") if style.get("priority") is not None else "warning"
        return (
            {
                "bbox_color": color,
                "label_color": style.get("label_color") or color,
                "line_width": style.get("line_width") or 3,
                "priority": priority,
                "reason": reason,
            },
            warnings,
        )

    status = identity.get("status")
    match_status = identity.get("match_status")
    is_matched = object_is_matched(obj)
    is_low_similarity = status == "low_similarity_candidate" or match_status in {
        "below_threshold",
        "low_similarity_candidate",
    }

    if is_matched:
        color = style.get("bbox_color") or "#D50000"
        reason = style.get("reason") or "identity_match"
        priority = style.get("priority") if style.get("priority") is not None else 50
    elif is_low_similarity:
        color = style.get("bbox_color") if style.get("bbox_color") not in {None, "#D50000"} else "#FFD166"
        reason = "low_similarity_candidate"
        priority = style.get("priority") if style.get("priority") is not None else 30
    else:
        color = style.get("bbox_color") or "#00B0FF"
        if str(color).upper() in {"#D50000", "#FF0000", "#E53935", "#FF1744"}:
            color = "#00B0FF"
            warnings.append("unknown_style_overridden_from_event_alert")
        reason = "unknown_face"
        priority = style.get("priority") if style.get("priority") is not None else 10

    return (
        {
            "bbox_color": color,
            "label_color": style.get("label_color") if is_matched else color,
            "line_width": style.get("line_width") or 2,
            "priority": priority,
            "reason": reason,
        },
        warnings,
    )
