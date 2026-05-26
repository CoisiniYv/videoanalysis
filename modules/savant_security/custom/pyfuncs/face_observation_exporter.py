"""FaceObservationExporterPyFunc — exports face observations to Redis Stream.

Runs after FaceReidGatePyFunc. Only exports faces with reid_allowed=true.
Writes to Redis Stream security.face_observations.

Does NOT:
- implement face-worker / pgvector / watchlist / live_search
- include image bytes
- write PostgreSQL
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, List, Optional

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.models.face_events import (
    FaceObservationEventDraft,
    build_face_source_observation_id,
)
from custom.services.face_observation_exporter import (
    FaceObservationExporter,
    create_face_observation_exporter,
)


class FaceObservationExporterPyFunc(NvDsPyFuncPlugin):
    """Export reid_allowed=true face observations to Redis Stream."""

    def __init__(
        self,
        log_every_n_frames: int = 30,
        producer: str = "savant-security",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._log_interval = max(int(log_every_n_frames), 1)
        self._frame_count = 0
        self._producer = producer
        self._exporter: FaceObservationExporter = create_face_observation_exporter()
        self._export_count = 0
        self._skip_count = 0

    def process_frame(self, buffer: Any, frame_meta: Any):
        self._frame_count += 1
        source_id = str(getattr(frame_meta, "source_id", "")) or "?"
        frame_num = getattr(frame_meta, "frame_num", None)
        pts = getattr(frame_meta, "pts", 0) or 0
        timestamp_ms = int(pts / 1_000_000) if pts else self._frame_count

        objects = list(frame_meta.objects)
        face_objects = [o for o in objects if getattr(o, "label", "") == "face"]

        exported = 0
        skipped = 0

        for i, obj in enumerate(face_objects):
            # Check reid_allowed gate
            reid_allowed = self._read_gate_bool(obj, "reid_allowed")
            if not reid_allowed:
                skipped += 1
                continue

            obs = self._build_observation(
                obj, source_id, frame_num, timestamp_ms, i,
            )
            if obs is None:
                skipped += 1
                continue

            self._exporter.export(obs)
            exported += 1

        self._export_count += exported
        self._skip_count += skipped

        if self._frame_count % self._log_interval == 1:
            stream_name = os.environ.get(
                "FACE_OBSERVATION_STREAM", "security.face_observations",
            )
            print(
                f"[face_obs_export] frame={self._frame_count} source={source_id} "
                f"exported={exported} skipped={skipped} "
                f"total_exported={self._export_count} "
                f"stream={stream_name}",
                flush=True,
            )

    def _read_gate_bool(self, obj, name: str) -> bool:
        try:
            attr = obj.get_attr_meta("face_reid_gate", name)
            if attr is not None:
                val = getattr(attr, "value", None)
                if val is not None:
                    return bool(val)
        except Exception:
            pass
        return False

    def _read_gate_float(self, obj, name: str, default: float = 0.0) -> float:
        try:
            attr = obj.get_attr_meta("face_reid_gate", name)
            if attr is not None:
                val = getattr(attr, "value", None)
                if val is not None:
                    return float(val)
        except Exception:
            pass
        return default

    def _read_gate_str(self, obj, name: str, default: str = "") -> str:
        try:
            attr = obj.get_attr_meta("face_reid_gate", name)
            if attr is not None:
                val = getattr(attr, "value", None)
                if val is not None:
                    return str(val)
        except Exception:
            pass
        return default

    def _read_feature(self, obj) -> Optional[List[float]]:
        for ns in ("adaface", "reid"):
            try:
                attr = obj.get_attr_meta(ns, "feature")
                if attr is not None:
                    value = getattr(attr, "value", None)
                    if value is not None:
                        return list(value) if hasattr(value, "__iter__") else None
            except Exception:
                pass
        return None

    def _read_landmarks(self, obj) -> Optional[List[float]]:
        try:
            attr = obj.get_attr_meta("yolov8_face", "landmarks")
            if attr is not None:
                value = getattr(attr, "value", None)
                if value is not None:
                    return list(value) if hasattr(value, "__iter__") else None
        except Exception:
            pass
        return None

    def _read_person_track_id(self, obj) -> int:
        try:
            attr = obj.get_attr_meta(
                "face_person_associator", "person_track_id",
            )
            if attr is not None:
                val = getattr(attr, "value", None)
                if val is not None:
                    return int(val)
        except Exception:
            pass
        return 0

    def _read_association_score(self, obj) -> float:
        try:
            attr = obj.get_attr_meta(
                "face_person_associator", "association_score",
            )
            if attr is not None:
                val = getattr(attr, "value", None)
                if val is not None:
                    return float(val)
        except Exception:
            pass
        return 0.0

    def _read_association_method(self, obj) -> str:
        try:
            attr = obj.get_attr_meta(
                "face_person_associator", "association_method",
            )
            if attr is not None:
                val = getattr(attr, "value", None)
                if val is not None:
                    return str(val)
        except Exception:
            pass
        return ""

    def _read_bbox_list(self, obj) -> Optional[List[float]]:
        bbox = getattr(obj, "bbox", None)
        if bbox is None:
            return None
        try:
            return [
                float(getattr(bbox, "xc", 0.0)),
                float(getattr(bbox, "yc", 0.0)),
                float(getattr(bbox, "width", 0.0)),
                float(getattr(bbox, "height", 0.0)),
            ]
        except Exception:
            return None

    def _build_observation(
        self,
        obj,
        source_id: str,
        frame_num: Optional[int],
        timestamp_ms: int,
        face_index: int,
    ) -> Optional[FaceObservationEventDraft]:
        """Build a FaceObservationEventDraft from a Savant face object."""
        track_id = self._read_person_track_id(obj)
        if track_id <= 0:
            return None

        feature = self._read_feature(obj)
        if not feature or len(feature) == 0:
            return None

        landmarks = self._read_landmarks(obj)
        face_bbox = self._read_bbox_list(obj)
        face_confidence = float(getattr(obj, "confidence", 0.0))
        quality_score = self._read_gate_float(obj, "reid_quality_score")
        throttle_key = self._read_gate_str(obj, "reid_throttle_key")

        # Idempotency key: deterministic per face per frame
        source_observation_id = build_face_source_observation_id(
            source_id, track_id, timestamp_ms,
        )
        # Add face_index to disambiguate multiple faces at same timestamp
        if face_index > 0:
            source_observation_id = f"{source_observation_id}:{face_index}"

        # Compute embedding norm
        emb_norm = math.sqrt(sum(x * x for x in feature)) if feature else 0.0

        # Person bbox from associated person (not available from face obj)
        # Leave None — face-worker can look up from person track if needed
        person_bbox = None

        obs = FaceObservationEventDraft(
            source_observation_id=source_observation_id,
            producer=self._producer,
            camera_id=source_id,
            source_id=source_id,
            track_id=track_id,
            timestamp_ms=timestamp_ms,
            frame_num=frame_num,
            person_bbox=person_bbox,
            face_bbox=face_bbox,
            landmarks=landmarks,
            face_confidence=face_confidence,
            quality=quality_score,
            detector_model="yolov8_face",
            embedding_model="adaface",
            embedding_dim=len(feature),
            embedding=feature,
            embedding_norm=emb_norm,
            reid_allowed=True,
            reid_throttle_key=throttle_key,
            association_score=self._read_association_score(obj),
            association_method=self._read_association_method(obj),
        )

        # Log concise summary for first few exports
        if self._export_count < 5:
            print(
                f"[face_obs_export] obs_id={source_observation_id} "
                f"track_id={track_id} embedding_dim={len(feature)} "
                f"quality={quality_score:.2f}",
                flush=True,
            )

        return obs
