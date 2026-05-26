"""FacePersonAssociatorPyFunc — links face detections to person tracks.

Reads person objects (from YOLO26-pose + nvtracker) and face objects
(from YOLOv8-Face), runs spatial association, and attaches
person_track_id / association metadata to face objects.

Does NOT:
- write to Redis security.face_observations
- invoke AdaFace
- implement face-worker / pgvector / watchlist
"""

from __future__ import annotations

from typing import Any, List, Optional

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.services.face_person_association import (
    AssociationConfig,
    BBox,
    FaceInput,
    PersonInput,
    associate_faces_to_persons,
)


class FacePersonAssociatorPyFunc(NvDsPyFuncPlugin):
    """Associate YOLOv8-Face detections with YOLO26-pose person tracks."""

    def __init__(self, log_every_n_frames: int = 30, **kwargs):
        super().__init__(**kwargs)
        self._log_interval = max(int(log_every_n_frames), 1)
        self._frame_count = 0
        self._config = AssociationConfig()

    def process_frame(self, buffer: Any, frame_meta: Any):
        self._frame_count += 1

        persons = self._extract_persons(frame_meta)
        faces = self._extract_faces(frame_meta)

        associations = associate_faces_to_persons(faces, persons, self._config)

        # Attach association metadata to face objects
        face_objs = [
            o for o in frame_meta.objects
            if getattr(o, "label", "") == "face"
        ]
        for assoc in associations:
            if assoc.face_index < len(face_objs):
                self._attach_association(face_objs[assoc.face_index], assoc)

        if self._frame_count % self._log_interval == 1:
            self._log_association(frame_meta, persons, faces, associations)

    def _extract_persons(self, frame_meta) -> List[PersonInput]:
        persons: List[PersonInput] = []
        for i, obj in enumerate(frame_meta.objects):
            label = str(getattr(obj, "label", ""))
            el_name = str(getattr(obj, "element_name", ""))
            if label != "person" and el_name != "yolo26_pose":
                continue

            bbox = self._read_bbox(obj)
            if bbox is None:
                continue

            track_id = getattr(obj, "track_id", None)
            if track_id is None:
                track_id = getattr(obj, "object_id", None)
            has_tid = track_id is not None and int(track_id) > 0
            tid = int(track_id) if has_tid else 0

            confidence = float(getattr(obj, "confidence", 0.0))
            persons.append(
                PersonInput(
                    bbox=bbox,
                    track_id=tid,
                    has_track_id=has_tid,
                    confidence=confidence,
                    index=i,
                )
            )
        return persons

    def _extract_faces(self, frame_meta) -> List[FaceInput]:
        faces: List[FaceInput] = []
        face_idx = 0
        for i, obj in enumerate(frame_meta.objects):
            label = str(getattr(obj, "label", ""))
            if label != "face":
                continue

            bbox = self._read_bbox(obj)
            if bbox is None:
                continue

            confidence = float(getattr(obj, "confidence", 0.0))
            faces.append(
                FaceInput(
                    bbox=bbox,
                    confidence=confidence,
                    index=face_idx,
                )
            )
            face_idx += 1
        return faces

    def _read_bbox(self, obj) -> Optional[BBox]:
        if hasattr(obj, "bbox"):
            b = obj.bbox
            return BBox(
                xc=float(getattr(b, "xc", 0.0)),
                yc=float(getattr(b, "yc", 0.0)),
                width=float(getattr(b, "width", 0.0)),
                height=float(getattr(b, "height", 0.0)),
            )
        return None

    def _attach_association(self, face_obj, assoc):
        """Attach association metadata to face object via add_attr_meta."""
        try:
            face_obj.add_attr_meta(
                "face_person_associator", "person_track_id", assoc.person_track_id
            )
            face_obj.add_attr_meta(
                "face_person_associator", "association_score", assoc.score
            )
            face_obj.add_attr_meta(
                "face_person_associator", "association_method", assoc.method
            )
        except Exception:
            # If attribute write fails, association is still logged
            pass

    def _log_association(self, frame_meta, persons, faces, associations):
        source_id = str(getattr(frame_meta, "source_id", "")) or "?"
        n_persons = len(persons)
        n_faces = len(faces)
        n_assoc = len(associations)

        lines = [
            f"[face_assoc] frame={self._frame_count} source={source_id} "
            f"persons={n_persons} faces={n_faces} associated={n_assoc}"
        ]

        face_objs = [
            o for o in frame_meta.objects
            if getattr(o, "label", "") == "face"
        ]

        for assoc in associations[:5]:
            face_bbox_str = ""
            if assoc.face_index < len(face_objs):
                fb = self._read_bbox(face_objs[assoc.face_index])
                if fb:
                    face_bbox_str = (
                        f"({fb.xc:.0f},{fb.yc:.0f},{fb.width:.0f},{fb.height:.0f})"
                    )

            pb = assoc.person_bbox
            person_bbox_str = (
                f"({pb.xc:.0f},{pb.yc:.0f},{pb.width:.0f},{pb.height:.0f})"
            )

            # Check landmarks
            lm_str = ""
            if assoc.face_index < len(face_objs):
                try:
                    attr = face_objs[assoc.face_index].get_attr_meta(
                        "yolov8_face", "landmarks"
                    )
                    if attr is not None:
                        value = getattr(attr, "value", None)
                        if value is not None:
                            lm_str = f" landmarks={len(list(value))}pts"
                except Exception:
                    pass

            lines.append(
                f"  face[{assoc.face_index}] person_track_id={assoc.person_track_id} "
                f"score={assoc.score:.2f} face_bbox={face_bbox_str} "
                f"person_bbox={person_bbox_str}{lm_str} method={assoc.method}"
            )

        print("\n".join(lines), flush=True)
