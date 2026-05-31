"""SameFrameDetectionDebugPyFunc — C1F.1c same-frame pose + face summary export.

Reads person (YOLO26-pose + nvtracker) and face (YOLOv8-Face + association)
metadata from the same frame and writes a lightweight JSONL summary.

Environment gate: C1F1_SAME_FRAME_DEBUG_ENABLED=1

Output: /data/video-analytics/artifacts/c1f1/same_frame_pose_face_summary.jsonl

Does NOT:
- output full frame images
- output face crops
- output JPEG/PNG/RAW
- send image bytes via Redis
- generate annotated_clip
- implement watchlist_hit / live_search_hit
- change business event semantics
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.services.frame_anchor_metadata import extract_frame_anchor_metadata

TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_OUTPUT_DIR = "/data/video-analytics/artifacts/c1f1"
DEFAULT_OUTPUT_FILE = "same_frame_pose_face_summary.jsonl"
DEFAULT_MAX_FRAMES = 0  # 0 = unlimited


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUTHY


def _to_xyxy(bbox: Any) -> list[float] | None:
    """Convert Savant bbox (xc, yc, w, h) to [x1, y1, x2, y2]."""
    if bbox is None:
        return None
    try:
        xc = float(bbox.x)
        yc = float(bbox.y)
        w = float(bbox.width)
        h = float(bbox.height)
        return [xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2]
    except Exception:
        return None


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


def _read_attr(obj: Any, namespace: str, name: str) -> Any:
    """Read a single attribute from an object, return None on failure."""
    try:
        attr = obj.get_attr_meta(namespace, name)
        if attr is not None:
            return getattr(attr, "value", None)
    except Exception:
        pass
    return None


class SameFrameDetectionDebugPyFunc(NvDsPyFuncPlugin):
    """Export same-frame pose + face summary as JSONL for C1F.1c."""

    def __init__(
        self,
        *,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        output_file: str = DEFAULT_OUTPUT_FILE,
        max_frames: int = DEFAULT_MAX_FRAMES,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._enabled = _env_flag("C1F1_SAME_FRAME_DEBUG_ENABLED")
        self._output_dir = Path(
            os.getenv("C1F1_SAME_FRAME_OUTPUT_DIR", output_dir)
        )
        self._output_file = os.getenv("C1F1_SAME_FRAME_OUTPUT_FILE", output_file)
        self._max_frames = int(
            os.getenv("C1F1_SAME_FRAME_MAX_FRAMES", str(max_frames))
        )
        self._frame_count = 0
        self._written = 0
        self._fh: Any = None

    def on_start(self) -> None:
        if not self._enabled:
            return
        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / self._output_file
        self._fh = output_path.open("a", encoding="utf-8")
        print(
            f"[same_frame_debug] enabled output={output_path}",
            flush=True,
        )

    def on_stop(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def process_frame(self, buffer: Any, frame_meta: Any) -> None:
        if not self._enabled:
            return
        self._frame_count += 1

        if self._max_frames > 0 and self._written >= self._max_frames:
            return

        objects = list(frame_meta.objects)
        person_objects = [
            o for o in objects if getattr(o, "label", "") == "person"
        ]
        face_objects = [
            o for o in objects if getattr(o, "label", "") == "face"
        ]

        record = self._build_record(frame_meta, person_objects, face_objects)
        self._write_record(record)

    def _build_record(
        self,
        frame_meta: Any,
        person_objects: list,
        face_objects: list,
    ) -> dict[str, Any]:
        anchor = extract_frame_anchor_metadata(frame_meta)

        # Compute timestamp_ms from frame_pts
        timestamp_ms = None
        pts = anchor.get("frame_pts")
        if pts is not None:
            try:
                # PTS is in time_base units; convert to ms
                tb = anchor.get("time_base")
                if tb and "/" in str(tb):
                    num, den = str(tb).split("/")
                    timestamp_ms = int(int(pts) * int(num) / int(den) / 1_000_000)
                else:
                    # Assume nanosecond time base
                    timestamp_ms = int(int(pts) / 1_000_000)
            except Exception:
                pass

        # Build pose summary
        persons = []
        for obj in person_objects:
            track_id = _read_attr(obj, "tracker", "track_id")
            if track_id is None:
                track_id = _read_attr(obj, "nvtracker", "track_id")
            bbox = _to_xyxy(getattr(obj, "bbox", None))
            conf = _safe_float(getattr(obj, "confidence", 0.0))

            # Keypoints
            kp_count = 0
            visible_kp = 0
            mean_kp_conf = 0.0
            try:
                kp_attr = _read_attr(obj, "yolo26_pose", "keypoints")
                if kp_attr is None:
                    kp_attr = _read_attr(obj, "custom", "keypoints")
                if kp_attr is not None:
                    kp_list = list(kp_attr) if hasattr(kp_attr, "__iter__") else []
                    kp_count = len(kp_list) // 3 if len(kp_list) >= 3 else len(kp_list)
                    # Keypoints format: [x, y, conf, x, y, conf, ...]
                    if len(kp_list) >= 3 and len(kp_list) % 3 == 0:
                        confs = [kp_list[i + 2] for i in range(0, len(kp_list), 3)]
                        visible_kp = sum(1 for c in confs if c > 0.01)
                        mean_kp_conf = (
                            sum(confs) / len(confs) if confs else 0.0
                        )
            except Exception:
                pass

            persons.append({
                "track_id": str(track_id) if track_id is not None else None,
                "bbox": bbox,
                "confidence": round(conf, 4),
                "keypoints_count": kp_count,
                "visible_keypoint_count": visible_kp,
                "mean_keypoint_confidence": round(mean_kp_conf, 4),
            })

        # Build face summary
        faces = []
        matched_pairs = []
        unmatched_face_count = 0

        for obj in face_objects:
            bbox = _to_xyxy(getattr(obj, "bbox", None))
            conf = _safe_float(getattr(obj, "confidence", 0.0))

            # Landmarks
            lm_count = 0
            try:
                lm_attr = _read_attr(obj, "yolov8_face", "landmarks")
                if lm_attr is not None:
                    lm_list = list(lm_attr) if hasattr(lm_attr, "__iter__") else []
                    lm_count = len(lm_list) // 2 if len(lm_list) >= 2 else len(lm_list)
            except Exception:
                pass

            face_info: dict[str, Any] = {
                "bbox": bbox,
                "confidence": round(conf, 4),
                "landmarks_count": lm_count,
            }
            faces.append(face_info)

            # Association
            track_id = _read_attr(obj, "face_person_associator", "person_track_id")
            assoc_score = _read_attr(
                obj, "face_person_associator", "association_score"
            )
            assoc_method = _read_attr(
                obj, "face_person_associator", "association_method"
            )

            if track_id is not None and _safe_int(track_id) > 0:
                matched_pairs.append({
                    "track_id": str(track_id),
                    "association_method": str(assoc_method) if assoc_method else None,
                    "association_score": round(_safe_float(assoc_score), 4),
                })
            else:
                unmatched_face_count += 1

        # Determine reason if no match
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

        return {
            "source_id": anchor.get("source_id")
            or getattr(frame_meta, "source_id", None),
            "camera_id": getattr(frame_meta, "source_id", None),
            "frame_uuid": anchor.get("frame_uuid"),
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

    def _write_record(self, record: dict[str, Any]) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(
                json.dumps(record, sort_keys=True, separators=(",", ":"))
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
            print(
                f"[same_frame_debug] frame={self._frame_count} "
                f"frame_num={record.get('frame_num')} "
                f"persons={record['pose']['person_count']} "
                f"faces={record['face']['face_count']} "
                f"matched={len(record['association']['matched_face_person_pairs'])} "
                f"reason={record['association']['reason_if_no_match']}",
                flush=True,
            )
