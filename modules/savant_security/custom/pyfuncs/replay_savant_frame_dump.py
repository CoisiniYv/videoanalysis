"""Debug-only Replay -> Savant frame image dump.

This pyfunc is disabled by default. When explicitly enabled, it writes the
Savant runtime frame image for selected frame UUIDs/PTS values so the frame can
be compared against Replay raw media at the same UUID/PTS.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

try:
    from savant.deepstream.pyfunc import NvDsPyFuncPlugin
except Exception:  # pragma: no cover - dependency-light unit tests.
    class NvDsPyFuncPlugin:  # type: ignore[no-redef]
        def __init__(self, **_kwargs: Any) -> None:
            pass


TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_OUTPUT_ROOT = "/data/video-analytics/media/debug/runtime_frame_dump"


def _env_flag(name: str, default: bool = False) -> bool:
    try:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in TRUTHY
    except Exception:
        return default


def _env_first(names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        try:
            value = os.getenv(name)
            if value is not None:
                return value
        except Exception:
            pass
    return default


def _env_csv_set(name: str) -> set[str]:
    try:
        value = os.getenv(name, "")
        return {item.strip() for item in value.split(",") if item.strip()}
    except Exception:
        return set()


def _env_int_set(name: str) -> set[int]:
    out: set[int] = set()
    for item in _env_csv_set(name):
        try:
            out.add(int(item))
        except Exception:
            pass
    return out


def _env_csv_set_first(names: tuple[str, ...]) -> set[str]:
    return {item.strip() for item in _env_first(names).split(",") if item.strip()}


def _env_int_set_first(names: tuple[str, ...]) -> set[int]:
    out: set[int] = set()
    for item in _env_csv_set_first(names):
        try:
            out.add(int(item))
        except Exception:
            pass
    return out


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = str(value)
        return text if text and text != "None" else None
    except Exception:
        return None


def _safe_path_component(value: Any) -> str:
    text = _safe_str(value) or "unknown"
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)[:160]


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def _extract_anchor_safe(frame_meta: Any) -> dict[str, Any]:
    try:
        from custom.services.frame_anchor_metadata import extract_frame_anchor_metadata

        return extract_frame_anchor_metadata(frame_meta)
    except Exception as exc:
        return {"metadata_source": f"import_error:{type(exc).__name__}"}


class ReplaySavantFrameDumpPyFunc(NvDsPyFuncPlugin):
    """Write selected Savant runtime frames to disk for alignment diagnostics."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._enabled = _env_flag("REPLAY_SAVANT_FRAME_DUMP_ENABLED")
        self._target_uuids = _env_csv_set_first(
            ("REPLAY_SAVANT_FRAME_DUMP_TARGET_UUIDS",)
        )
        self._target_pts = _env_int_set_first(
            ("REPLAY_SAVANT_FRAME_DUMP_TARGET_PTS",)
        )
        self._output_root = Path(
            _env_first(
                ("REPLAY_SAVANT_FRAME_DUMP_ROOT",),
                DEFAULT_OUTPUT_ROOT,
            )
        )
        self._max_frames = max(
            0,
            _safe_int(
                _env_first(
                    ("REPLAY_SAVANT_FRAME_DUMP_MAX_FRAMES",),
                    "20",
                ),
                20,
            ),
        )
        self._written = 0
        print(
            "component=replay_savant_frame_dump_init "
            f"enabled={self._enabled} target_uuid_count={len(self._target_uuids)} "
            f"target_pts_count={len(self._target_pts)} max_frames={self._max_frames} "
            f"output_root={self._output_root}",
            flush=True,
        )

    def process_frame(self, buffer: Any, frame_meta: Any) -> None:
        if not self._enabled:
            return
        try:
            self._process_frame_inner(buffer, frame_meta)
        except Exception as exc:
            print(
                "component=replay_savant_frame_dump "
                f"status=error error={type(exc).__name__}: {exc}",
                flush=True,
            )

    def _process_frame_inner(self, buffer: Any, frame_meta: Any) -> None:
        if self._max_frames and self._written >= self._max_frames:
            return
        anchor = _extract_anchor_safe(frame_meta)
        frame_uuid = _safe_str(anchor.get("frame_uuid"))
        frame_pts = _safe_int(anchor.get("frame_pts"), -1)
        target_by_uuid = bool(frame_uuid and frame_uuid in self._target_uuids)
        target_by_pts = bool(frame_pts >= 0 and frame_pts in self._target_pts)
        has_targets = bool(self._target_uuids or self._target_pts)
        if has_targets and not (target_by_uuid or target_by_pts):
            return

        source_id = _safe_str(anchor.get("source_id")) or _safe_str(
            getattr(frame_meta, "source_id", None)
        ) or "unknown"
        frame_name = frame_uuid or f"frame_{_safe_int(anchor.get('frame_num'), self._written):06d}"
        out_dir = self._output_root / _safe_path_component(source_id)
        jpg_path = out_dir / f"{_safe_path_component(frame_name)}.jpg"
        json_path = jpg_path.with_suffix(".json")

        import cv2  # type: ignore
        import numpy as np  # type: ignore
        import pyds  # type: ignore

        out_dir.mkdir(parents=True, exist_ok=True)
        batch_id = _safe_int(getattr(frame_meta, "batch_id", 0), 0)
        surface = pyds.get_nvds_buf_surface(hash(buffer), batch_id)
        image = np.array(surface, copy=True)
        if image.ndim == 3 and image.shape[2] == 4:
            bgr = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
        elif image.ndim == 3 and image.shape[2] == 3:
            bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        else:
            bgr = image
        if not cv2.imwrite(str(jpg_path), bgr):
            raise RuntimeError("cv2_imwrite_returned_false")

        sidecar = {
            "source_id": source_id,
            "camera_id": _safe_str(getattr(frame_meta, "source_id", None)) or source_id,
            "frame_uuid": frame_uuid,
            "keyframe_uuid": _safe_str(anchor.get("keyframe_uuid")),
            "previous_keyframe_uuid": _safe_str(anchor.get("previous_keyframe_uuid")),
            "frame_num": anchor.get("frame_num"),
            "frame_pts": anchor.get("frame_pts"),
            "keyframe_pts": anchor.get("keyframe_pts"),
            "frame_dts": anchor.get("frame_dts"),
            "duration": anchor.get("duration"),
            "time_base": anchor.get("time_base"),
            "image_width": int(bgr.shape[1]) if hasattr(bgr, "shape") and len(bgr.shape) > 1 else 0,
            "image_height": int(bgr.shape[0]) if hasattr(bgr, "shape") else 0,
            "image_sha256": _sha256_file(jpg_path),
            "image_path": str(jpg_path),
            "dump_source": "savant_runtime_frame",
            "created_by": "debug_only_runtime_frame_dump",
            "capture_backend": "pyds.get_nvds_buf_surface",
            "capture_batch_id": batch_id,
            "capture_component": "replay_savant_frame_dump",
            "target_match": {"uuid": target_by_uuid, "pts": target_by_pts},
            "schema_version": "1.0",
        }
        json_path.write_text(
            json.dumps(sidecar, default=str, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._written += 1
        print(
            "component=replay_savant_frame_dump "
            f"status=written source_id={source_id} frame_uuid={frame_uuid} "
            f"frame_pts={anchor.get('frame_pts')} image_path={jpg_path}",
            flush=True,
        )
