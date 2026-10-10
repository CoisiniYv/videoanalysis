"""Create throttled face candidates before AdaFace inference.

The original YOLO face objects stay untouched for annotation and trajectory
display. Only lightweight clones that pass embedding-independent quality rules
and the per-track identity refresh policy (first usable face immediately, then
a clear face every identity_refresh_ms) are presented to AdaFace.
"""

from __future__ import annotations

from typing import Any

from savant.deepstream.pyfunc import NvDsPyFuncPlugin
from savant.meta.object import ObjectMeta

from custom.services.face_identity_refresh import (
    IdentityRefreshConfig,
    IdentityRefreshPolicy,
)
from custom.services.face_reid_gate import (
    ReIDGateInput,
    evaluate_reid_candidate,
)
from custom.services.time_utils import normalize_pts_to_ms


class FaceReidCandidateGatePyFunc(NvDsPyFuncPlugin):
    """Clone only cadence-eligible YOLO face objects for AdaFace."""

    def __init__(
        self,
        source_element_name: str = "yolov8_face",
        candidate_element_name: str = "face_reid_candidate",
        face_label: str = "face",
        face_reid_min_confidence: float = 0.45,
        face_reid_min_face_size: float = 40.0,
        face_reid_min_interval_ms: int = 1000,
        cameras_config_path: str = "",
        log_every_n_frames: int = 30,
        identity_refresh_ms: int | None = None,
        clear_min_face_size: float | None = None,
        clear_min_confidence: float | None = None,
        clear_max_yaw_ratio: float | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._source_element_name = str(source_element_name)
        self._candidate_element_name = str(candidate_element_name)
        self._face_label = str(face_label)
        self._config = {
            "face_reid_min_confidence": float(face_reid_min_confidence),
            "face_reid_min_face_size": float(face_reid_min_face_size),
        }
        self._identity = IdentityRefreshPolicy(
            IdentityRefreshConfig.from_env(
                min_interval_ms=face_reid_min_interval_ms,
                refresh_ms=identity_refresh_ms,
                clear_min_face_size=clear_min_face_size,
                clear_min_confidence=clear_min_confidence,
                clear_max_yaw_ratio=clear_max_yaw_ratio,
            )
        )
        self._camera_bundle = self._load_camera_bundle(cameras_config_path)
        self._log_interval = max(int(log_every_n_frames), 1)
        self._frame_count = 0
        self._source_faces = 0
        self._candidates = 0
        self._throttled = 0

    @staticmethod
    def _load_camera_bundle(config_path: str):
        if not config_path:
            return None
        from custom.services.camera_config import load_camera_config

        try:
            return load_camera_config(config_path)
        except Exception:
            return None

    def _camera_id(self, source_id: str) -> str:
        if self._camera_bundle is not None:
            entry = self._camera_bundle.get_by_source_id(source_id)
            if entry is not None:
                return str(entry.camera_id)
        return source_id

    def process_frame(self, buffer: Any, frame_meta: Any) -> None:
        self._frame_count += 1
        source_id = str(getattr(frame_meta, "source_id", "")) or "?"
        camera_id = self._camera_id(source_id)
        pts = getattr(frame_meta, "pts", 0) or 0
        timestamp_ms = normalize_pts_to_ms(pts) if pts else self._frame_count
        objects = list(frame_meta.objects)
        faces = [
            obj
            for obj in objects
            if getattr(obj, "element_name", "") == self._source_element_name
            and getattr(obj, "label", "") == self._face_label
        ]

        created = 0
        throttled = 0
        for obj in faces:
            gate_input = self._gate_input(obj, camera_id, source_id, timestamp_ms)
            result = evaluate_reid_candidate(gate_input, self._config)
            if not result.allowed:
                continue
            clarity = self._identity.assess(
                face_width=gate_input.face_width,
                face_height=gate_input.face_height,
                face_confidence=gate_input.face_confidence,
                landmarks=gate_input.landmarks,
            )
            decision = self._identity.decide(
                result.throttle_key, timestamp_ms, clarity.clear
            )
            if not decision.allowed:
                throttled += 1
                continue
            result.clear = clarity.clear
            result.clarity_score = clarity.score
            result.identity_reason = decision.reason

            candidate = self._clone_candidate(obj, result)
            frame_meta.add_obj_meta(candidate)
            self._identity.record(result.throttle_key, timestamp_ms, clarity.clear)
            created += 1

        self._source_faces += len(faces)
        self._candidates += created
        self._throttled += throttled
        if self._frame_count % self._log_interval == 1:
            print(
                "[face_reid_candidate_gate] "
                f"frame={self._frame_count} source={source_id} faces={len(faces)} "
                f"candidates={created} throttled={throttled} "
                f"total_faces={self._source_faces} "
                f"total_candidates={self._candidates} "
                f"total_throttled={self._throttled} "
                f"identity_tracks={self._identity.tracked_count}",
                flush=True,
            )

    def _gate_input(
        self,
        obj: Any,
        camera_id: str,
        source_id: str,
        timestamp_ms: int,
    ) -> ReIDGateInput:
        bbox = getattr(obj, "bbox", None)
        person_track_id = int(
            self._attr_value(obj, "face_person_associator", "person_track_id", 0)
            or 0
        )
        landmarks = self._attr_value(obj, "yolov8_face", "landmarks", None)
        return ReIDGateInput(
            face_confidence=float(getattr(obj, "confidence", 0.0) or 0.0),
            face_width=float(getattr(bbox, "width", 0.0) or 0.0),
            face_height=float(getattr(bbox, "height", 0.0) or 0.0),
            landmarks=(
                list(landmarks) if landmarks is not None and hasattr(landmarks, "__iter__") else None
            ),
            person_track_id=person_track_id,
            has_track_id=person_track_id > 0,
            camera_id=camera_id,
            source_id=source_id,
            timestamp_ms=timestamp_ms,
            association_method=str(
                self._attr_value(
                    obj, "face_person_associator", "association_method", ""
                )
                or ""
            ),
        )

    def _clone_candidate(self, obj: Any, result: Any) -> ObjectMeta:
        candidate = ObjectMeta(
            element_name=self._candidate_element_name,
            label=self._face_label,
            bbox=obj.bbox,
            confidence=getattr(obj, "confidence", 0.0),
            track_id=getattr(obj, "track_id", 0),
            draw_label=self._face_label,
        )
        self._copy_attr(obj, candidate, "yolov8_face", "landmarks")
        for name in ("person_track_id", "association_score", "association_method"):
            self._copy_attr(obj, candidate, "face_person_associator", name)
        candidate.add_attr_meta(
            self._candidate_element_name,
            "source_object_uid",
            int(getattr(obj, "uid", 0) or 0),
        )
        candidate.add_attr_meta(
            self._candidate_element_name,
            "pre_gate_quality_score",
            float(result.quality_score),
        )
        candidate.add_attr_meta(
            self._candidate_element_name,
            "pre_gate_throttle_key",
            str(result.throttle_key),
        )
        candidate.add_attr_meta(
            self._candidate_element_name,
            "identity_refresh_reason",
            str(result.identity_reason or ""),
        )
        candidate.add_attr_meta(
            self._candidate_element_name,
            "face_clear",
            bool(result.clear),
        )
        return candidate

    @staticmethod
    def _attr_value(obj: Any, element_name: str, name: str, default: Any) -> Any:
        try:
            attr = obj.get_attr_meta(element_name, name)
            value = getattr(attr, "value", None) if attr is not None else None
            return default if value is None else value
        except Exception:
            return default

    @staticmethod
    def _copy_attr(
        source: Any,
        target: ObjectMeta,
        element_name: str,
        name: str,
    ) -> None:
        try:
            attr = source.get_attr_meta(element_name, name)
            if attr is None:
                return
            target.add_attr_meta(
                element_name,
                name,
                getattr(attr, "value", None),
                confidence=float(getattr(attr, "confidence", 1.0) or 1.0),
            )
        except Exception:
            return
