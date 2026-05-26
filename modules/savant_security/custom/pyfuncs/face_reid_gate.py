"""FaceReidGatePyFunc — F2.2 quality gate + throttle before Redis.

Runs after AdaFace embedding. Evaluates face quality and per-track
throttle to decide if the embedding is eligible for downstream Redis
storage.

Does NOT:
- write to Redis security.face_observations
- implement face-worker / pgvector / watchlist
"""

from __future__ import annotations

import math
from typing import Any

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.services.face_reid_gate import (
    ReIDGateInput,
    ReIDThrottleMap,
    evaluate_reid_gate,
)


class FaceReidGatePyFunc(NvDsPyFuncPlugin):
    """Quality gate + per-track throttle for ReID eligibility."""

    def __init__(
        self,
        log_every_n_frames: int = 30,
        face_reid_min_confidence: float = 0.6,
        face_reid_min_face_size: float = 40.0,
        face_reid_min_interval_ms: int = 1000,
        face_reid_norm_tolerance: float = 0.10,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._log_interval = max(int(log_every_n_frames), 1)
        self._frame_count = 0
        self._config = {
            "face_reid_min_confidence": float(face_reid_min_confidence),
            "face_reid_min_face_size": float(face_reid_min_face_size),
            "face_reid_norm_tolerance": float(face_reid_norm_tolerance),
        }
        self._throttle = ReIDThrottleMap(
            min_interval_ms=int(face_reid_min_interval_ms),
        )

    def process_frame(self, buffer: Any, frame_meta: Any):
        self._frame_count += 1
        source_id = str(getattr(frame_meta, "source_id", "")) or "?"
        objects = list(frame_meta.objects)
        face_objects = [o for o in objects if getattr(o, "label", "") == "face"]

        allowed_count = 0
        skipped_count = 0

        for i, obj in enumerate(face_objects):
            inp = self._extract_input(obj, source_id, frame_meta)
            result = evaluate_reid_gate(inp, self._config)

            # Throttle check (only if gate passed)
            if result.allowed:
                ts = inp.timestamp_ms or self._frame_count
                if not self._throttle.is_allowed(result.throttle_key, ts):
                    result.allowed = False
                    result.skip_reason = "throttled"
                    result.next_allowed_at_ms = self._throttle.next_allowed_at(
                        result.throttle_key,
                    )
                else:
                    self._throttle.record(result.throttle_key, ts)

            # Attach metadata to face object
            self._attach_gate_meta(obj, result)

            if result.allowed:
                allowed_count += 1
            else:
                skipped_count += 1

        if self._frame_count % self._log_interval == 1:
            self._log_gate_results(
                face_objects, source_id, allowed_count, skipped_count,
            )

    def _extract_input(self, obj, source_id: str, frame_meta: Any) -> ReIDGateInput:
        """Extract ReIDGateInput from a Savant face object."""
        inp = ReIDGateInput()
        inp.camera_id = source_id
        inp.timestamp_ms = getattr(frame_meta, "pts", 0) or 0

        # Face bbox and confidence
        bbox = getattr(obj, "bbox", None)
        if bbox is not None:
            inp.face_width = float(getattr(bbox, "width", 0.0))
            inp.face_height = float(getattr(bbox, "height", 0.0))
        inp.face_confidence = float(getattr(obj, "confidence", 0.0))

        # Landmarks
        try:
            attr = obj.get_attr_meta("yolov8_face", "landmarks")
            if attr is not None:
                value = getattr(attr, "value", None)
                if value is not None:
                    inp.landmarks = list(value) if hasattr(value, "__iter__") else None
        except Exception:
            pass

        # person_track_id
        try:
            ptid_attr = obj.get_attr_meta(
                "face_person_associator", "person_track_id",
            )
            if ptid_attr is not None:
                ptid = getattr(ptid_attr, "value", None)
                if ptid is not None:
                    inp.person_track_id = int(ptid)
                    inp.has_track_id = inp.person_track_id > 0
            method_attr = obj.get_attr_meta(
                "face_person_associator", "association_method",
            )
            if method_attr is not None:
                inp.association_method = str(
                    getattr(method_attr, "value", ""),
                )
        except Exception:
            pass

        # AdaFace feature
        for ns in ("adaface", "reid"):
            try:
                attr = obj.get_attr_meta(ns, "feature")
                if attr is not None:
                    value = getattr(attr, "value", None)
                    if value is not None:
                        feat = list(value) if hasattr(value, "__iter__") else []
                        inp.feature = feat
                        inp.feature_dim = len(feat)
                        if feat:
                            inp.embedding_norm = math.sqrt(
                                sum(x * x for x in feat),
                            )
                    break
            except Exception:
                pass

        return inp

    def _attach_gate_meta(self, obj, result):
        """Attach gate result metadata to the face object."""
        try:
            obj.add_attr_meta(
                "face_reid_gate", "reid_allowed", result.allowed,
            )
            obj.add_attr_meta(
                "face_reid_gate", "reid_quality_score", result.quality_score,
            )
            obj.add_attr_meta(
                "face_reid_gate", "reid_skip_reason",
                result.skip_reason or "ok",
            )
            obj.add_attr_meta(
                "face_reid_gate", "reid_throttle_key", result.throttle_key,
            )
        except Exception:
            pass

    def _log_gate_results(
        self, face_objects, source_id, allowed_count, skipped_count,
    ):
        """Log gate results for smoke verification."""
        n = len(face_objects)
        print(
            f"[face_reid_gate] frame={self._frame_count} source={source_id} "
            f"faces={n} allowed={allowed_count} skipped={skipped_count}",
            flush=True,
        )

        for i, obj in enumerate(face_objects[:5]):
            parts = [f"  face[{i}]"]

            # person_track_id
            ptid = None
            try:
                attr = obj.get_attr_meta(
                    "face_person_associator", "person_track_id",
                )
                if attr is not None:
                    ptid = getattr(attr, "value", None)
                    parts.append(f"person_track_id={ptid}")
            except Exception:
                pass

            # gate result
            try:
                allowed_attr = obj.get_attr_meta(
                    "face_reid_gate", "reid_allowed",
                )
                allowed = getattr(allowed_attr, "value", None) if allowed_attr else None
                parts.append(f"allowed={allowed}")

                reason_attr = obj.get_attr_meta(
                    "face_reid_gate", "reid_skip_reason",
                )
                reason = (
                    getattr(reason_attr, "value", None) if reason_attr else None
                )
                if reason and reason != "ok":
                    parts.append(f"reason={reason}")

                score_attr = obj.get_attr_meta(
                    "face_reid_gate", "reid_quality_score",
                )
                score = (
                    getattr(score_attr, "value", 0.0) if score_attr else 0.0
                )
                parts.append(f"score={float(score):.2f}")
            except Exception:
                pass

            # feature info
            for ns in ("adaface", "reid"):
                try:
                    attr = obj.get_attr_meta(ns, "feature")
                    if attr is not None:
                        value = getattr(attr, "value", None)
                        if value is not None:
                            feat = list(value) if hasattr(value, "__iter__") else []
                            dim = len(feat)
                            norm = math.sqrt(sum(x * x for x in feat)) if feat else 0
                            parts.append(f"feature_dim={dim}")
                            parts.append(f"norm={norm:.3f}")
                        break
                except Exception:
                    pass

            # landmarks
            lm_count = 0
            try:
                attr = obj.get_attr_meta("yolov8_face", "landmarks")
                if attr is not None:
                    value = getattr(attr, "value", None)
                    if value is not None:
                        lm_count = (
                            len(list(value)) if hasattr(value, "__iter__") else 0
                        )
            except Exception:
                pass
            parts.append(f"landmarks={lm_count}")

            print(" ".join(parts), flush=True)
