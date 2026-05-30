"""Runtime frame metadata probe for R3.3A0.

The probe is environment-gated and side-effect free for event semantics. It
only introspects the actual frame object visible to the pyfunc and writes a
small JSON sample for the first N frames per source.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_OUTPUT_ROOT = "/data/video-analytics/media/debug/r3_3a0_frame_uuid_probe"
DEFAULT_MAX_FRAMES = 20

PROBED_ATTRS = (
    "uuid",
    "frame_uuid",
    "previous_keyframe_uuid",
    "keyframe_uuid",
    "pts",
    "dts",
    "duration",
    "frame_num",
    "source_id",
    "time_base",
    "metadata",
    "buf_pts",
    "ntp_timestamp",
    "batch_id",
    "framerate",
)

NESTED_OBJECT_ATTRS = (
    "video_frame",
    "_video_frame",
    "frame_meta",
    "metadata",
)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUTHY


def _safe_str(value: Any, *, limit: int = 500) -> str:
    try:
        text = str(value)
    except Exception as exc:
        text = f"<str failed: {type(exc).__name__}>"
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _safe_repr(value: Any, *, limit: int = 500) -> str:
    try:
        text = repr(value)
    except Exception as exc:
        text = f"<repr failed: {type(exc).__name__}>"
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value[:50]]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in list(value.items())[:50]}
    return _safe_repr(value)


def _get_attr(obj: Any, name: str) -> Any:
    try:
        if hasattr(obj, name):
            return _jsonable(getattr(obj, name))
    except Exception as exc:
        return f"<read failed: {type(exc).__name__}: {_safe_str(exc, limit=160)}>"
    return None


def _safe_raw_attr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _to_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = str(value)
    except Exception:
        return None
    if text in ("", "None", "null"):
        return None
    return text


def _to_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _to_time_base(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return f"{int(value[0])}/{int(value[1])}"
    except Exception:
        pass
    return _to_optional_str(value)


def extract_frame_anchor_metadata(frame_meta: Any) -> dict[str, Any]:
    """Extract production-safe frame anchor metadata from a Savant frame.

    This helper is intentionally small and non-diagnostic. It never calls
    ``dir()`` or ``repr()`` and never raises; missing fields are returned as
    ``None``. Prefer the nested Savant ``VideoFrame`` because R3.3A0 proved it
    exposes ``uuid`` in the current runtime.
    """
    anchor = {
        "frame_uuid": None,
        "keyframe_uuid": None,
        "previous_keyframe_uuid": None,
        "frame_pts": None,
        "frame_dts": None,
        "duration": None,
        "frame_num": None,
        "ntp_timestamp": None,
        "time_base": None,
        "source_id": None,
        "metadata_source": "not_available",
    }

    try:
        video_frame = _safe_raw_attr(frame_meta, "video_frame")
        if video_frame is None:
            video_frame = _safe_raw_attr(frame_meta, "_video_frame")

        pyds_frame_meta = _safe_raw_attr(frame_meta, "frame_meta")

        if video_frame is not None:
            previous_keyframe_uuid = _to_optional_str(
                _safe_raw_attr(video_frame, "previous_keyframe_uuid")
            )
            direct_keyframe_uuid = _to_optional_str(
                _safe_raw_attr(video_frame, "keyframe_uuid")
            )
            anchor.update(
                {
                    "frame_uuid": _to_optional_str(
                        _safe_raw_attr(video_frame, "uuid")
                    ),
                    "keyframe_uuid": direct_keyframe_uuid or previous_keyframe_uuid,
                    "previous_keyframe_uuid": previous_keyframe_uuid,
                    "frame_pts": _to_optional_int(_safe_raw_attr(video_frame, "pts")),
                    "frame_dts": _to_optional_int(_safe_raw_attr(video_frame, "dts")),
                    "duration": _to_optional_int(
                        _safe_raw_attr(video_frame, "duration")
                    ),
                    "time_base": _to_time_base(
                        _safe_raw_attr(video_frame, "time_base")
                    ),
                    "source_id": _to_optional_str(
                        _safe_raw_attr(video_frame, "source_id")
                    ),
                    "metadata_source": "video_frame",
                }
            )

        if anchor["frame_uuid"] is None:
            anchor["frame_uuid"] = _to_optional_str(
                _safe_raw_attr(frame_meta, "frame_uuid")
            ) or _to_optional_str(_safe_raw_attr(frame_meta, "uuid"))

        if anchor["keyframe_uuid"] is None:
            anchor["keyframe_uuid"] = _to_optional_str(
                _safe_raw_attr(frame_meta, "keyframe_uuid")
            )

        if anchor["previous_keyframe_uuid"] is None:
            anchor["previous_keyframe_uuid"] = _to_optional_str(
                _safe_raw_attr(frame_meta, "previous_keyframe_uuid")
            )

        if anchor["frame_pts"] is None:
            anchor["frame_pts"] = _to_optional_int(_safe_raw_attr(frame_meta, "pts"))
        if anchor["frame_dts"] is None:
            anchor["frame_dts"] = _to_optional_int(_safe_raw_attr(frame_meta, "dts"))
        if anchor["duration"] is None:
            anchor["duration"] = _to_optional_int(
                _safe_raw_attr(frame_meta, "duration")
            )
        if anchor["frame_num"] is None:
            anchor["frame_num"] = _to_optional_int(
                _safe_raw_attr(frame_meta, "frame_num")
            )
        if anchor["time_base"] is None:
            anchor["time_base"] = _to_time_base(_safe_raw_attr(frame_meta, "time_base"))
        if anchor["source_id"] is None:
            anchor["source_id"] = _to_optional_str(_safe_raw_attr(frame_meta, "source_id"))

        if pyds_frame_meta is not None:
            if anchor["ntp_timestamp"] is None:
                anchor["ntp_timestamp"] = _to_optional_int(
                    _safe_raw_attr(pyds_frame_meta, "ntp_timestamp")
                )
            if anchor["frame_pts"] is None:
                anchor["frame_pts"] = _to_optional_int(
                    _safe_raw_attr(pyds_frame_meta, "buf_pts")
                )
            if anchor["frame_num"] is None:
                anchor["frame_num"] = _to_optional_int(
                    _safe_raw_attr(pyds_frame_meta, "frame_num")
                )
            if anchor["metadata_source"] == "not_available":
                anchor["metadata_source"] = "pyds_frame_meta"

        if anchor["ntp_timestamp"] is None:
            anchor["ntp_timestamp"] = _to_optional_int(
                _safe_raw_attr(frame_meta, "ntp_timestamp")
            )
        if anchor["metadata_source"] == "not_available" and frame_meta is not None:
            anchor["metadata_source"] = "frame_meta"
    except Exception:
        return anchor

    return anchor


def _public_dir(obj: Any) -> list[str]:
    try:
        names = dir(obj)
    except Exception:
        return []
    public = [name for name in names if not name.startswith("__")]
    return sorted(public)[:400]


def _object_type(obj: Any) -> str:
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def _inspect_object(obj: Any) -> dict[str, Any]:
    attrs = {name: _get_attr(obj, name) for name in PROBED_ATTRS}
    return {
        "object_type": _object_type(obj),
        "object_repr": _safe_repr(obj),
        "available_attrs": _public_dir(obj),
        "uuid": attrs.get("uuid") or attrs.get("frame_uuid"),
        "frame_uuid": attrs.get("frame_uuid"),
        "previous_keyframe_uuid": attrs.get("previous_keyframe_uuid"),
        "keyframe_uuid": attrs.get("keyframe_uuid"),
        "pts": attrs.get("pts"),
        "dts": attrs.get("dts"),
        "duration": attrs.get("duration"),
        "frame_num": attrs.get("frame_num"),
        "source_id": attrs.get("source_id"),
        "time_base": attrs.get("time_base"),
        "metadata": attrs.get("metadata"),
        "probed_attrs": attrs,
    }


def _inspect_nested_objects(obj: Any) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for name in NESTED_OBJECT_ATTRS:
        try:
            if not hasattr(obj, name):
                nested[name] = {"available": False}
                continue
            value = getattr(obj, name)
        except Exception as exc:
            nested[name] = {
                "available": True,
                "read_error": f"{type(exc).__name__}: {_safe_str(exc, limit=160)}",
            }
            continue
        if value is None:
            nested[name] = {"available": True, "is_null": True}
            continue
        nested[name] = {"available": True, **_inspect_object(value)}
    return nested


def _safe_source_id(source_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_id.strip())
    return clean or "unknown"


class FrameUuidRuntimeProbe:
    """Collect first-N per-source frame object introspection samples."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        output_root: str | None = None,
        max_frames: int | None = None,
    ) -> None:
        self.enabled = (
            env_flag("R3_3A0_FRAME_UUID_PROBE_ENABLED")
            if enabled is None
            else bool(enabled)
        )
        self.output_root = Path(
            output_root
            or os.getenv("R3_3A0_PROBE_OUTPUT_ROOT")
            or DEFAULT_OUTPUT_ROOT
        )
        self.max_frames = int(
            max_frames
            if max_frames is not None
            else os.getenv("R3_3A0_PROBE_MAX_FRAMES", str(DEFAULT_MAX_FRAMES))
        )
        self._counts: dict[str, int] = {}

    def should_probe(self, source_id: str) -> bool:
        if not self.enabled:
            return False
        return self._counts.get(source_id, 0) < self.max_frames

    def collect(
        self,
        frame_meta: Any,
        *,
        timestamp_ms_used_by_event: int | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        source_id = str(_get_attr(frame_meta, "source_id") or "")
        inspected = _inspect_object(frame_meta)
        attrs = inspected["probed_attrs"]
        nested = _inspect_nested_objects(frame_meta)
        video_frame = nested.get("video_frame") or {}
        return {
            "source_id": source_id,
            "frame_object_type": inspected["object_type"],
            "frame_object_repr": inspected["object_repr"],
            "available_attrs": inspected["available_attrs"],
            "uuid": attrs.get("uuid") or attrs.get("frame_uuid"),
            "frame_uuid": attrs.get("frame_uuid"),
            "previous_keyframe_uuid": attrs.get("previous_keyframe_uuid"),
            "keyframe_uuid": attrs.get("keyframe_uuid"),
            "video_frame_object_type": video_frame.get("object_type"),
            "video_frame_uuid": video_frame.get("uuid"),
            "video_frame_previous_keyframe_uuid": video_frame.get("previous_keyframe_uuid"),
            "video_frame_keyframe_uuid": video_frame.get("keyframe_uuid"),
            "nested_objects": nested,
            "pts": attrs.get("pts"),
            "dts": attrs.get("dts"),
            "duration": attrs.get("duration"),
            "frame_num": attrs.get("frame_num"),
            "source_id_attr": attrs.get("source_id"),
            "time_base": attrs.get("time_base"),
            "metadata": attrs.get("metadata"),
            "buf_pts": attrs.get("buf_pts"),
            "ntp_timestamp": attrs.get("ntp_timestamp"),
            "batch_id": attrs.get("batch_id"),
            "framerate": attrs.get("framerate"),
            "timestamp_ms_used_by_event": timestamp_ms_used_by_event,
            "probed_attrs": attrs,
            "notes": notes,
        }

    def probe(
        self,
        frame_meta: Any,
        *,
        timestamp_ms_used_by_event: int | None = None,
        notes: str = "",
    ) -> dict[str, Any] | None:
        source_id = str(_get_attr(frame_meta, "source_id") or "unknown")
        if not self.should_probe(source_id):
            return None

        current = self._counts.get(source_id, 0) + 1
        self._counts[source_id] = current
        sample = self.collect(
            frame_meta,
            timestamp_ms_used_by_event=timestamp_ms_used_by_event,
            notes=notes,
        )

        try:
            source_dir = self.output_root / _safe_source_id(source_id)
            source_dir.mkdir(parents=True, exist_ok=True)
            path = source_dir / f"frame_{current:06d}.json"
            path.write_text(json.dumps(sample, indent=2, sort_keys=True), encoding="utf-8")
            sample["probe_output_path"] = str(path)
        except Exception as exc:
            sample["probe_write_error"] = f"{type(exc).__name__}: {_safe_str(exc, limit=240)}"

        print(
            "stage=r3_3a0_frame_uuid_probe "
            f"source_id={source_id} "
            f"frame_num={sample.get('frame_num')} "
            f"frame_object_type={sample.get('frame_object_type')} "
            f"has_uuid={sample.get('uuid') is not None} "
            f"has_previous_keyframe_uuid={sample.get('previous_keyframe_uuid') is not None} "
            f"has_keyframe_uuid={sample.get('keyframe_uuid') is not None} "
            f"video_frame_object_type={sample.get('video_frame_object_type')} "
            f"has_video_frame_uuid={sample.get('video_frame_uuid') is not None} "
            f"has_pts={sample.get('pts') is not None} "
            f"has_frame_num={sample.get('frame_num') is not None} "
            f"output={sample.get('probe_output_path', '')} "
            f"write_error={sample.get('probe_write_error', '')}",
            flush=True,
        )
        return sample
