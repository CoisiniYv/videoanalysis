"""R3.2A metadata.json writer for event evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def build_metadata(
    *,
    event: dict[str, Any],
    media_status: str,
    snapshot_status: str,
    clip_status: str,
    metadata_status: str,
    snapshot_path: str | None,
    raw_clip_path: str | None,
    metadata_path: str,
    capture: dict[str, Any],
    clip: dict[str, Any] | None,
    overlay: dict[str, Any],
) -> dict[str, Any]:
    """Build the R3.2A/R3.2B metadata.json object."""
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    return {
        "event": {
            "event_id": str(event.get("id", "")),
            "source_event_id": event.get("source_event_id", ""),
            "event_type": event.get("event_type", ""),
            "algorithm_type": event.get("algorithm_type", ""),
            "camera_id": event.get("camera_id", ""),
            "source_id": event.get("source_id", ""),
            "track_id": str(event.get("track_id", "") or ""),
            "person_id": event.get("person_id"),
            "severity": event.get("severity", ""),
            "confidence": float(event.get("confidence", 0.0) or 0.0),
            "start_ts_ms": int(event.get("start_ts_ms", 0) or 0),
            "end_ts_ms": event.get("end_ts_ms"),
        },
        "evidence": {
            "media_status": media_status,
            "snapshot_status": snapshot_status,
            "clip_status": clip_status,
            "metadata_status": metadata_status,
            "snapshot_path": snapshot_path,
            "raw_clip_path": raw_clip_path,
            "annotated_clip_path": None,
            "metadata_path": metadata_path,
            "clip_error_message": (clip or {}).get("clip_error_message"),
        },
        "capture": _jsonable(capture),
        "clip": _jsonable(clip or {}),
        "overlay": _jsonable(overlay),
        "payload": _jsonable(payload),
    }


def write_metadata_file(path: str, metadata: dict[str, Any]) -> None:
    """Write metadata JSON atomically enough for smoke/operator usage."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(output)
