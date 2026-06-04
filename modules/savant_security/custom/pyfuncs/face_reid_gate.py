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
import os
from typing import Any, Optional

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.services.face_reid_gate import (
    ReIDGateInput,
    ReIDThrottleMap,
    evaluate_reid_gate,
)
from custom.services.time_utils import normalize_pts_to_ms


class FaceReidGatePyFunc(NvDsPyFuncPlugin):
    """Quality gate + per-track throttle for ReID eligibility."""

    def __init__(
        self,
        log_every_n_frames: int = 30,
        face_detector_confidence_threshold: float = 0.25,
        face_reid_min_confidence: float = 0.45,
        face_reid_min_face_size: float = 40.0,
        face_reid_min_interval_ms: int = 1000,
        face_reid_norm_tolerance: float = 0.10,
        cameras_config_path: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._log_interval = max(int(log_every_n_frames), 1)
        self._frame_count = 0
        self._face_detector_confidence_threshold = float(
            face_detector_confidence_threshold,
        )
        self._config = {
            "face_reid_min_confidence": float(face_reid_min_confidence),
            "face_reid_min_face_size": float(face_reid_min_face_size),
            "face_reid_norm_tolerance": float(face_reid_norm_tolerance),
        }
        self._throttle = ReIDThrottleMap(
            min_interval_ms=int(face_reid_min_interval_ms),
        )
        self._camera_bundle = self._load_camera_bundle(cameras_config_path)
        self._counters = {
            "raw_face_detections": 0,
            "detector_confidence_passed": 0,
            "detector_confidence_rejected": 0,
            "reid_allowed": 0,
            "reid_skipped": 0,
            "reid_rejected_by_confidence": 0,
            "reid_rejected_by_size": 0,
            "reid_rejected_by_landmarks": 0,
            "reid_rejected_by_embedding_norm": 0,
            "reid_rejected_by_other": 0,
        }

    @staticmethod
    def _load_camera_bundle(config_path: str):
        if not config_path:
            return None
        from custom.services.camera_config import load_camera_config

        try:
            return load_camera_config(config_path)
        except Exception:
            return None

    def _resolve_camera_id(self, source_id: str) -> tuple:
        """Resolve business camera_id from source_id.

        Returns (camera_id, resolved: bool).
        """
        if self._camera_bundle is not None:
            entry = self._camera_bundle.get_by_source_id(source_id)
            if entry is not None:
                return entry.camera_id, True
        return source_id, False

    def process_frame(self, buffer: Any, frame_meta: Any):
        self._frame_count += 1
        source_id = str(getattr(frame_meta, "source_id", "")) or "?"
        camera_id, _camera_resolved = self._resolve_camera_id(source_id)
        objects = list(frame_meta.objects)
        face_objects = [o for o in objects if getattr(o, "label", "") == "face"]

        allowed_count = 0
        skipped_count = 0
        self._counters["raw_face_detections"] += len(face_objects)

        for i, obj in enumerate(face_objects):
            inp = self._extract_input(obj, source_id, camera_id, frame_meta)
            if inp.face_confidence >= self._face_detector_confidence_threshold:
                self._counters["detector_confidence_passed"] += 1
            else:
                self._counters["detector_confidence_rejected"] += 1
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
                self._counters["reid_allowed"] += 1
            else:
                skipped_count += 1
                self._counters["reid_skipped"] += 1
                self._count_rejection_reason(result.skip_reason)

        if self._frame_count % self._log_interval == 1:
            self._log_gate_results(
                face_objects, source_id, camera_id, allowed_count, skipped_count,
            )

    def _count_rejection_reason(self, reason: Optional[str]) -> None:
        reason = reason or "unknown"
        if reason == "low_confidence":
            self._counters["reid_rejected_by_confidence"] += 1
        elif reason == "face_too_small":
            self._counters["reid_rejected_by_size"] += 1
        elif reason in ("no_landmarks", "bad_landmarks"):
            self._counters["reid_rejected_by_landmarks"] += 1
        elif reason.startswith("bad_norm"):
            self._counters["reid_rejected_by_embedding_norm"] += 1
        elif reason != "throttled":
            self._counters["reid_rejected_by_other"] += 1

    def _extract_input(
        self, obj, source_id: str, camera_id: str, frame_meta: Any,
    ) -> ReIDGateInput:
        """Extract ReIDGateInput from a Savant face object."""
        inp = ReIDGateInput()
        inp.camera_id = camera_id
        inp.source_id = source_id
        inp.timestamp_ms = normalize_pts_to_ms(getattr(frame_meta, "pts", 0))

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
        self, face_objects, source_id, camera_id, allowed_count, skipped_count,
    ):
        """Log gate results for smoke verification."""
        n = len(face_objects)
        print(
            f"[face_reid_gate] frame={self._frame_count} source={source_id} "
            f"camera={camera_id} "
            f"faces={n} allowed={allowed_count} skipped={skipped_count}",
            flush=True,
        )
        print(
            "[face_reid_gate_summary] "
            f"raw_face_detections={self._counters['raw_face_detections']} "
            f"detector_confidence_passed={self._counters['detector_confidence_passed']} "
            f"detector_confidence_rejected={self._counters['detector_confidence_rejected']} "
            f"reid_allowed={self._counters['reid_allowed']} "
            f"reid_skipped={self._counters['reid_skipped']} "
            f"reid_rejected_by_confidence={self._counters['reid_rejected_by_confidence']} "
            f"reid_rejected_by_size={self._counters['reid_rejected_by_size']} "
            f"reid_rejected_by_landmarks={self._counters['reid_rejected_by_landmarks']} "
            f"reid_rejected_by_embedding_norm={self._counters['reid_rejected_by_embedding_norm']} "
            f"reid_rejected_by_other={self._counters['reid_rejected_by_other']} "
            f"detector_threshold={self._face_detector_confidence_threshold:.3f} "
            f"reid_min_confidence={self._config['face_reid_min_confidence']:.3f} "
            f"reid_min_face_size={self._config['face_reid_min_face_size']:.1f} "
            f"reid_norm_tolerance={self._config['face_reid_norm_tolerance']:.3f}",
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
