"""FaceEmbeddingDebugPyFunc — F2.1 AdaFace embedding smoke visibility probe.

Logs AdaFace feature dim, raw L2 norm, person_track_id, landmarks,
and F2.2 reid gate metadata (reid_allowed, skip_reason, quality_score).
Does NOT:
- write to Redis security.face_observations
- normalize features
- implement watchlist / live_search
"""

from __future__ import annotations

import math
from typing import Any

from savant.deepstream.pyfunc import NvDsPyFuncPlugin


class FaceEmbeddingDebugPyFunc(NvDsPyFuncPlugin):
    """Log AdaFace embedding metadata for F2.1 smoke verification."""

    def __init__(self, log_every_n_frames: int = 30, **kwargs):
        super().__init__(**kwargs)
        self._log_interval = max(int(log_every_n_frames), 1)
        self._frame_count = 0

    def process_frame(self, buffer: Any, frame_meta: Any):
        self._frame_count += 1
        objects = list(frame_meta.objects)
        face_objects = [o for o in objects if getattr(o, "label", "") == "face"]

        if self._frame_count % self._log_interval == 1:
            self._log_embedding_info(face_objects, frame_meta)

    def _log_embedding_info(self, face_objects, frame_meta):
        n_faces = len(face_objects)
        source_id = getattr(frame_meta, "source_id", "") or "?"

        if n_faces == 0:
            print(
                f"[face_embedding] frame={self._frame_count} source={source_id} "
                f"faces=0",
                flush=True,
            )
            return

        # Count associated and embedded faces
        associated = 0
        embedded = 0
        for obj in face_objects:
            ptid_attr = obj.get_attr_meta("face_person_associator", "person_track_id")
            if ptid_attr is not None:
                ptid = getattr(ptid_attr, "value", None)
                if ptid is not None and int(ptid) > 0:
                    associated += 1
            feat_attr = self._read_feature(obj)
            if feat_attr is not None:
                embedded += 1

        print(
            f"[face_embedding] frame={self._frame_count} source={source_id} "
            f"faces={n_faces} associated={associated} embedded={embedded}",
            flush=True,
        )

        for i, obj in enumerate(face_objects[:5]):
            self._log_single_face(obj, i)

    def _read_feature(self, obj):
        """Read AdaFace feature attribute from the face object."""
        # Try official sample namespace first
        for namespace in ("adaface", "reid"):
            try:
                attr = obj.get_attr_meta(namespace, "feature")
                if attr is not None:
                    return attr
            except Exception:
                pass
        return None

    def _log_single_face(self, obj, index):
        parts = [f"  face[{index}]"]

        # person_track_id
        ptid = None
        try:
            ptid_attr = obj.get_attr_meta("face_person_associator", "person_track_id")
            if ptid_attr is not None:
                ptid = getattr(ptid_attr, "value", None)
                parts.append(f"person_track_id={ptid}")
        except Exception:
            pass

        # AdaFace feature
        feat_attr = self._read_feature(obj)
        feature_dim = 0
        raw_l2 = 0.0
        first3 = []
        if feat_attr is not None:
            try:
                value = getattr(feat_attr, "value", None)
                if value is not None:
                    feat_list = list(value) if hasattr(value, "__iter__") else []
                    feature_dim = len(feat_list)
                    if feature_dim > 0:
                        raw_l2 = math.sqrt(sum(x * x for x in feat_list))
                        first3 = [f"{x:.4f}" for x in feat_list[:3]]
                parts.append(f"feature_dim={feature_dim}")
                parts.append(f"raw_l2_norm={raw_l2:.1f}")
                if first3:
                    parts.append(f"first3=[{','.join(first3)}]")
            except Exception as e:
                parts.append(f"feature_error={e}")
        else:
            # Inspect available attr namespaces for debugging
            ns_list = []
            try:
                for attr in getattr(obj, "attr_meta", []) or []:
                    ns = getattr(attr, "namespace", "?")
                    nm = getattr(attr, "name", "?")
                    ns_list.append(f"{ns}.{nm}")
            except Exception:
                pass
            if ns_list:
                parts.append(f"feature=None attrs=[{','.join(ns_list[:8])}]")
            else:
                parts.append("feature=None")

        # landmarks
        lm_count = 0
        try:
            attr = obj.get_attr_meta("yolov8_face", "landmarks")
            if attr is not None:
                value = getattr(attr, "value", None)
                if value is not None:
                    lm_count = len(list(value)) if hasattr(value, "__iter__") else 0
        except Exception:
            pass
        parts.append(f"landmarks={lm_count}")

        # F2.2 reid gate metadata
        try:
            allowed_attr = obj.get_attr_meta("face_reid_gate", "reid_allowed")
            if allowed_attr is not None:
                allowed = getattr(allowed_attr, "value", None)
                parts.append(f"reid_allowed={allowed}")
                reason_attr = obj.get_attr_meta(
                    "face_reid_gate", "reid_skip_reason",
                )
                reason = (
                    getattr(reason_attr, "value", "")
                    if reason_attr
                    else ""
                )
                if reason and reason != "ok":
                    parts.append(f"skip={reason}")
                score_attr = obj.get_attr_meta(
                    "face_reid_gate", "reid_quality_score",
                )
                if score_attr is not None:
                    score = getattr(score_attr, "value", 0.0)
                    parts.append(f"quality={float(score):.2f}")
        except Exception:
            pass

        print(" ".join(parts), flush=True)
