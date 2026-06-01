"""Read-only evidence bundle indexing and parsing helpers.

The viewer is intentionally file-based. It reads bundle directories under a
configured evidence root and never follows user input outside that root.
"""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any


SAFE_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
RAW_CLIP_PREFERRED_NAMES = (
    "raw_clip.mp4",
    "raw_clip.mov",
    "raw_clip.webm",
    "raw_clip.mkv",
)


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
        actual = values.get(key)
        if actual is None or expected.lower() not in str(actual).lower():
            return False
    return True


def bundle_summary(bundle_dir: Path) -> dict[str, Any]:
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
    annotations_path = bundle_dir / "annotations.jsonl"
    return {
        "event_id": event.get("event_id") or summary.get("event_id") or bundle_dir.name,
        "event_type": event.get("event_type") or summary.get("event_type"),
        "source_id": event.get("source_id") or summary.get("source_id"),
        "camera_id": event.get("camera_id") or summary.get("camera_id"),
        "raw_clip_available": raw_clip is not None,
        "raw_clip_name": raw_clip.name if raw_clip else None,
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
    candidates = [path for path in root.iterdir() if path.is_dir()]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)

    matched: list[dict[str, Any]] = []
    warnings: list[str] = []
    for bundle_dir in candidates:
        if not SAFE_EVENT_ID_RE.fullmatch(bundle_dir.name):
            warnings.append(f"skipped_unsafe_bundle_name:{bundle_dir.name}")
            continue
        metadata, metadata_warnings = load_json_object(bundle_dir / "metadata.json")
        summary, summary_warnings = load_json_object(bundle_dir / "summary.json")
        if not _matches_filters(bundle_dir, metadata, summary, filters):
            continue
        item = bundle_summary(bundle_dir)
        item["warnings"] = sorted(
            set(item.get("warnings", []) + metadata_warnings + summary_warnings)
        )
        matched.append(item)

    start = max(0, offset)
    end = start + max(1, limit)
    return {
        "bundles": matched[start:end],
        "total": len(matched),
        "limit": limit,
        "offset": start,
        "warnings": warnings,
    }


def bundle_manifest(evidence_root: Path, event_id: str) -> dict[str, Any]:
    bundle_dir = ensure_bundle_dir(evidence_root, event_id)
    metadata, metadata_warnings = load_json_object(bundle_dir / "metadata.json")
    summary, summary_warnings = load_json_object(bundle_dir / "summary.json")
    raw_clip = discover_raw_clip(bundle_dir, metadata)
    available_files = sorted(path.name for path in bundle_dir.iterdir() if path.is_file())
    warnings = metadata_warnings + summary_warnings
    if raw_clip is None:
        warnings.append("raw_clip_missing")
    return {
        "event_id": event_id,
        "metadata": metadata,
        "summary": summary,
        "raw_clip_url": f"/api/bundles/{event_id}/media/raw_clip"
        if raw_clip is not None
        else None,
        "raw_clip_name": raw_clip.name if raw_clip else None,
        "annotations_url": f"/api/bundles/{event_id}/annotations",
        "sink_metadata_url": f"/api/bundles/{event_id}/sink-metadata",
        "available_files": available_files,
        "warnings": warnings,
    }


def normalize_bbox(
    bbox: dict[str, Any],
    *,
    width: float,
    height: float,
) -> dict[str, float] | None:
    values = bbox.get("values")
    if not isinstance(values, list) or len(values) < 4:
        return None
    try:
        v0, v1, v2, v3 = [float(value) for value in values[:4]]
    except (TypeError, ValueError):
        return None

    fmt = str(bbox.get("format") or "cxcywh").lower()
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


def object_label(obj: dict[str, Any], annotation: dict[str, Any]) -> str:
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
        color = style.get("bbox_color") or "#9E9E9E"
        if str(color).upper() in {"#D50000", "#FF0000", "#E53935", "#FF1744"}:
            color = "#9E9E9E"
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
