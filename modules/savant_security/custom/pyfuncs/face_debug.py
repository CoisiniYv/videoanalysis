"""FaceDebugPyFunc — minimal face detector visibility probe.

Logs face object count and first-few bbox/landmark values. Does NOT:
- write to Redis security.face_observations
- affect intrusion events
- implement face-person association
- invoke AdaFace
"""

from __future__ import annotations

from typing import Any, Dict

from savant.deepstream.pyfunc import NvDsPyFuncPlugin


class FaceDebugPyFunc(NvDsPyFuncPlugin):
    """Log face detections for smoke verification."""

    def __init__(self, log_every_n_frames: int = 30, **kwargs):
        super().__init__(**kwargs)
        self._log_interval = max(int(log_every_n_frames), 1)
        self._frame_count = 0

    def process_frame(self, buffer: Any, frame_meta: Any):
        self._frame_count += 1
        objects = list(frame_meta.objects)
        face_objects = [o for o in objects if getattr(o, "label", "") == "face"]

        if self._frame_count % self._log_interval == 1:
            self._log_face_info(face_objects, frame_meta)

    def _log_face_info(self, face_objects, frame_meta):
        n_faces = len(face_objects)
        source_id = getattr(frame_meta, "source_id", "") or "?"
        pts = getattr(frame_meta, "pts", 0) or 0

        if n_faces == 0:
            print(
                f"[face_debug] frame={self._frame_count} source={source_id} "
                f"pts={pts} faces=0",
                flush=True,
            )
            return

        lines = [
            f"[face_debug] frame={self._frame_count} source={source_id} "
            f"pts={pts} faces={n_faces}"
        ]
        for i, obj in enumerate(face_objects[:5]):
            bbox = getattr(obj, "bbox", None)
            bbox_str = ""
            if bbox is not None:
                try:
                    bbox_str = (
                        f"bbox=({bbox.x:.1f},{bbox.y:.1f},"
                        f"{bbox.width:.1f},{bbox.height:.1f})"
                    )
                except Exception:
                    bbox_str = f"bbox={bbox}"

            conf = getattr(obj, "confidence", 0.0)

            # --- landmarks: use Savant get_attr_meta API ---
            lm_str = ""
            try:
                attr = obj.get_attr_meta("yolov8_face", "landmarks")
                if attr is not None:
                    value = getattr(attr, "value", None)
                    if value is not None:
                        lm_list = list(value) if hasattr(value, "__iter__") else []
                        lm_str = f"landmarks={len(lm_list)}pts value={lm_list[:6]}..."
                    else:
                        lm_str = "landmarks=attr_present_value_None"
                else:
                    lm_str = "landmarks=attr_None"
            except Exception as e:
                lm_str = f"landmarks=error:{e}"

            # --- association metadata from FacePersonAssociatorPyFunc ---
            assoc_str = ""
            try:
                ptid_attr = obj.get_attr_meta("face_person_associator", "person_track_id")
                if ptid_attr is not None:
                    ptid = getattr(ptid_attr, "value", None)
                    score_attr = obj.get_attr_meta("face_person_associator", "association_score")
                    score = getattr(score_attr, "value", 0.0) if score_attr else 0.0
                    method_attr = obj.get_attr_meta("face_person_associator", "association_method")
                    method = getattr(method_attr, "value", "") if method_attr else ""
                    assoc_str = f" person_tid={ptid} assoc_score={score:.2f} method={method}"
            except Exception:
                pass

            lines.append(
                f"  face[{i}] conf={conf:.3f} {bbox_str} {lm_str}{assoc_str}"
            )

        print("\n".join(lines), flush=True)
