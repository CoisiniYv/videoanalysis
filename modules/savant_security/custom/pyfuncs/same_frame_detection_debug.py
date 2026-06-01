"""SameFrameDetectionDebugPyFunc — C1F.1c same-frame pose + face summary export.

Reads person (YOLO26-pose + nvtracker) and face (YOLOv8-Face + association)
metadata from the same frame and writes a lightweight JSONL summary.

Environment gate: C1F1_SAME_FRAME_DEBUG_ENABLED=1

Output: /data/video-analytics/artifacts/c1f1/same_frame_pose_face_summary.jsonl

HARDENED: This pyfunc must NEVER cause the Savant pipeline to stop.
- No file I/O in on_start() (causes GStreamer pipeline stop).
- File operations are lazy: initialized on first process_frame().
- Every import, metadata read, JSON write is wrapped in try/except.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_OUTPUT_DIR = "/data/video-analytics/artifacts/c1f1"
DEFAULT_OUTPUT_FILE = "same_frame_pose_face_summary.jsonl"
DEFAULT_MAX_FRAMES = 0  # 0 = unlimited


def _env_flag(name: str, default: bool = False) -> bool:
    try:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in TRUTHY
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        s = str(value)
        return s if s and s != "None" else None
    except Exception:
        return None


def _read_attr(obj: Any, namespace: str, name: str) -> Any:
    """Read a single attribute from an object, return None on failure."""
    try:
        attr = obj.get_attr_meta(namespace, name)
        if attr is not None:
            return getattr(attr, "value", None)
    except Exception:
        pass
    return None


def _to_xyxy(bbox: Any) -> list[float] | None:
    """Convert Savant bbox (xc, yc, w, h) to [x1, y1, x2, y2]."""
    if bbox is None:
        return None
    try:
        xc = float(bbox.x)
        yc = float(bbox.y)
        w = float(bbox.width)
        h = float(bbox.height)
        return [round(xc - w / 2, 2), round(yc - h / 2, 2),
                round(xc + w / 2, 2), round(yc + h / 2, 2)]
    except Exception:
        return None


def _extract_anchor_safe(frame_meta: Any) -> dict[str, Any]:
    """Lazy-import and call extract_frame_anchor_metadata, never raise."""
    try:
        from custom.services.frame_anchor_metadata import (
            extract_frame_anchor_metadata,
        )
        return extract_frame_anchor_metadata(frame_meta)
    except Exception as exc:
        return {"metadata_source": f"import_error: {type(exc).__name__}"}


class SameFrameDetectionDebugPyFunc(NvDsPyFuncPlugin):
    """Export same-frame pose + face summary as JSONL for C1F.1c.

    HARDENED: every method is wrapped in try/except. File I/O is lazy
    (first process_frame, not on_start). This pyfunc must never cause
    the pipeline to stop.
    """

    def __init__(self, log_every_n_frames: int = 30, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._log_interval = max(int(log_every_n_frames), 1)
        self._enabled = _env_flag("C1F1_SAME_FRAME_DEBUG_ENABLED")
        self._output_dir = Path(
            os.getenv("C1F1_SAME_FRAME_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)
        )
        self._output_file = os.getenv(
            "C1F1_SAME_FRAME_OUTPUT_FILE", DEFAULT_OUTPUT_FILE
        )
        self._max_frames = int(
            os.getenv("C1F1_SAME_FRAME_MAX_FRAMES", str(DEFAULT_MAX_FRAMES))
        )
        self._frame_count = 0
        self._written = 0
        self._fh: Any = None
        self._file_initialized = False

    def _ensure_file(self) -> None:
        """Lazy file init — called from process_frame, NOT on_start.

        on_start() cannot do file I/O in Savant: it causes the GStreamer
        pipeline to stop. All file operations must be deferred to the
        first process_frame() call.
        """
        if self._file_initialized:
            return
        self._file_initialized = True
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            output_path = self._output_dir / self._output_file
            self._fh = output_path.open("a", encoding="utf-8")
            print(
                f"[same_frame_debug] enabled output={output_path}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[same_frame_debug] file_init_error={type(exc).__name__}: {exc}",
                flush=True,
            )
            self._enabled = False

    def on_stop(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def process_frame(self, buffer: Any, frame_meta: Any) -> None:
        """Top-level wrapper — never raise."""
        if not self._enabled:
            return
        try:
            self._ensure_file()
            self._process_frame_inner(buffer, frame_meta)
        except Exception as exc:
            print(
                f"[same_frame_debug] process_error={type(exc).__name__}: {exc}",
                flush=True,
            )

    def _process_frame_inner(self, buffer: Any, frame_meta: Any) -> None:
        self._frame_count += 1

        if self._max_frames > 0 and self._written >= self._max_frames:
            return

        # Safe object enumeration
        person_objects: list[Any] = []
        face_objects: list[Any] = []
        try:
            for o in frame_meta.objects:
                try:
                    label = getattr(o, "label", "")
                    if label == "person":
                        person_objects.append(o)
                    elif label == "face":
                        face_objects.append(o)
                except Exception:
                    pass
        except Exception:
            pass

        record = self._build_record_safe(frame_meta, person_objects, face_objects)
        self._write_record_safe(record)

    def _build_record_safe(
        self,
        frame_meta: Any,
        person_objects: list,
        face_objects: list,
    ) -> dict[str, Any]:
        """Build record — every field access is individually guarded."""
        anchor = _extract_anchor_safe(frame_meta)

        # Timestamp
        timestamp_ms = None
        try:
            pts = anchor.get("frame_pts")
            if pts is not None:
                tb = anchor.get("time_base")
                if tb and "/" in str(tb):
                    num, den = str(tb).split("/")
                    timestamp_ms = int(int(pts) * int(num) / int(den) / 1_000_000)
                else:
                    timestamp_ms = int(int(pts) / 1_000_000)
        except Exception:
            pass

        # Persons
        persons = []
        for obj in person_objects:
            try:
                persons.append(self._extract_person_safe(obj))
            except Exception:
                pass

        # Faces + association
        faces = []
        matched_pairs = []
        unmatched_face_count = 0

        for obj in face_objects:
            try:
                face_info = self._extract_face_safe(obj)
                faces.append(face_info)
            except Exception:
                pass

            try:
                track_id = _read_attr(
                    obj, "face_person_associator", "person_track_id"
                )
                assoc_score = _read_attr(
                    obj, "face_person_associator", "association_score"
                )
                assoc_method = _read_attr(
                    obj, "face_person_associator", "association_method"
                )

                if track_id is not None and _safe_int(track_id) > 0:
                    matched_pairs.append({
                        "track_id": str(track_id),
                        "association_method": _safe_str(assoc_method),
                        "association_score": round(_safe_float(assoc_score), 4),
                    })
                else:
                    unmatched_face_count += 1
            except Exception:
                unmatched_face_count += 1

        # Reason
        reason_if_no_match = None
        if not matched_pairs:
            if not face_objects and not person_objects:
                reason_if_no_match = "no_persons_and_no_faces"
            elif not face_objects:
                reason_if_no_match = "no_faces_detected"
            elif not person_objects:
                reason_if_no_match = "no_persons_detected"
            else:
                reason_if_no_match = "no_geometric_match_found"

        source_id = None
        try:
            source_id = anchor.get("source_id") or getattr(
                frame_meta, "source_id", None
            )
        except Exception:
            pass

        return {
            "source_id": _safe_str(source_id),
            "camera_id": _safe_str(getattr(frame_meta, "source_id", None)),
            "frame_uuid": _safe_str(anchor.get("frame_uuid")),
            "frame_num": anchor.get("frame_num"),
            "frame_pts": anchor.get("frame_pts"),
            "timestamp_ms": timestamp_ms,
            "pose": {
                "person_count": len(persons),
                "persons": persons,
            },
            "face": {
                "face_count": len(faces),
                "faces": faces,
            },
            "association": {
                "matched_face_person_pairs": matched_pairs,
                "unmatched_face_count": unmatched_face_count,
                "reason_if_no_match": reason_if_no_match,
            },
        }

    def _extract_person_safe(self, obj: Any) -> dict[str, Any]:
        """Extract person fields — each guarded."""
        track_id = None
        bbox = None
        conf = 0.0
        kp_count = 0
        visible_kp = 0
        mean_kp_conf = 0.0

        try:
            track_id = _read_attr(obj, "tracker", "track_id")
            if track_id is None:
                track_id = _read_attr(obj, "nvtracker", "track_id")
        except Exception:
            pass

        try:
            bbox = _to_xyxy(getattr(obj, "bbox", None))
        except Exception:
            pass

        try:
            conf = _safe_float(getattr(obj, "confidence", 0.0))
        except Exception:
            pass

        try:
            kp_attr = _read_attr(obj, "yolo26_pose", "keypoints")
            if kp_attr is None:
                kp_attr = _read_attr(obj, "custom", "keypoints")
            if kp_attr is not None and hasattr(kp_attr, "__iter__"):
                kp_list = list(kp_attr)
                kp_count = (
                    len(kp_list) // 3 if len(kp_list) >= 3 else len(kp_list)
                )
                if len(kp_list) >= 3 and len(kp_list) % 3 == 0:
                    confs = [kp_list[i + 2] for i in range(0, len(kp_list), 3)]
                    visible_kp = sum(1 for c in confs if c > 0.01)
                    mean_kp_conf = sum(confs) / len(confs) if confs else 0.0
        except Exception:
            pass

        return {
            "track_id": _safe_str(track_id),
            "bbox": bbox,
            "confidence": round(conf, 4),
            "keypoints_count": kp_count,
            "visible_keypoint_count": visible_kp,
            "mean_keypoint_confidence": round(mean_kp_conf, 4),
        }

    def _extract_face_safe(self, obj: Any) -> dict[str, Any]:
        """Extract face fields — each guarded."""
        bbox = None
        conf = 0.0
        lm_count = 0

        try:
            bbox = _to_xyxy(getattr(obj, "bbox", None))
        except Exception:
            pass

        try:
            conf = _safe_float(getattr(obj, "confidence", 0.0))
        except Exception:
            pass

        try:
            lm_attr = _read_attr(obj, "yolov8_face", "landmarks")
            if lm_attr is not None and hasattr(lm_attr, "__iter__"):
                lm_list = list(lm_attr)
                lm_count = (
                    len(lm_list) // 2 if len(lm_list) >= 2 else len(lm_list)
                )
        except Exception:
            pass

        return {
            "bbox": bbox,
            "confidence": round(conf, 4),
            "landmarks_count": lm_count,
        }

    def _write_record_safe(self, record: dict[str, Any]) -> None:
        """Write JSONL record — never raise."""
        if self._fh is None:
            return
        try:
            self._fh.write(
                json.dumps(
                    record, default=str, sort_keys=True, separators=(",", ":")
                )
            )
            self._fh.write("\n")
            self._fh.flush()
            self._written += 1
        except Exception as exc:
            print(
                f"[same_frame_debug] write_error={type(exc).__name__}: {exc}",
                flush=True,
            )

        # Log first few records
        if self._written <= 3:
            try:
                print(
                    f"[same_frame_debug] frame={self._frame_count} "
                    f"frame_num={record.get('frame_num')} "
                    f"persons={record['pose']['person_count']} "
                    f"faces={record['face']['face_count']} "
                    f"matched="
                    f"{len(record['association']['matched_face_person_pairs'])} "
                    f"reason={record['association']['reason_if_no_match']}",
                    flush=True,
                )
            except Exception:
                pass
