"""Export cadence-eligible aligned 112x112 face crops to Redis.

The source frame is already decoded by the main Savant process. This plugin
uses the same GPU alignment implementation as inline AdaFace and downloads
only the resulting 112x112 crop; it never serializes a full frame or video.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Any

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.services.face_reid_gate import (
    ReIDGateInput,
    ReIDThrottleMap,
    evaluate_reid_candidate,
)
from custom.services.face_roi_stream import FaceRoiEnvelope, epoch_ms
from custom.services.frame_anchor_metadata import extract_frame_anchor_metadata
from custom.services.redis_stream_writer import AsyncRedisStreamWriter, env_int
from custom.services.stream_session import stream_session_id_for_frame
from custom.services.time_utils import normalize_pts_to_ms


_STOP = object()


class FaceRoiExporterPyFunc(NvDsPyFuncPlugin):
    """GPU-align eligible faces and enqueue bounded Redis ROI messages."""

    def __init__(
        self,
        enabled: bool = False,
        log_every_n_frames: int = 300,
        cameras_config_path: str = "",
        min_confidence: float = 0.45,
        min_face_size: float = 40.0,
        min_interval_ms: int = 1000,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._enabled = bool(enabled)
        self._log_every = max(int(log_every_n_frames), 1)
        self._frame_count = 0
        self._config = {
            "face_reid_min_confidence": float(min_confidence),
            "face_reid_min_face_size": float(min_face_size),
        }
        self._throttle = ReIDThrottleMap(min_interval_ms=int(min_interval_ms))
        self._camera_bundle = self._load_camera_bundle(cameras_config_path)
        self._writer = None
        self._aligner = None
        self._cuda_stream = None
        self._crop_queue = None
        self._crop_thread = None
        self._counters = {
            "candidates": 0,
            "eligible": 0,
            "throttled": 0,
            "gate_rejected": 0,
            "crop_errors": 0,
            "crop_batches": 0,
            "gpu_syncs": 0,
            "max_eligible_per_frame": 0,
            "crop_queue_depth": 0,
            "crop_queue_dropped": 0,
            "encode_errors": 0,
            "enqueued": 0,
            "queue_dropped": 0,
        }
        if self._enabled:
            self._init_runtime()

    @staticmethod
    def _load_camera_bundle(config_path: str):
        if not config_path:
            return None
        try:
            from custom.services.camera_config import load_camera_config

            return load_camera_config(config_path)
        except Exception:
            return None

    def _init_runtime(self) -> None:
        import cv2
        from savant.input_preproc.align_face import (
            AlignFacePreprocessingObjectImageGPU,
        )

        self._cuda_stream = cv2.cuda.Stream()
        self._aligner = AlignFacePreprocessingObjectImageGPU(
            attr_element_name="yolov8_face",
            attr_name="landmarks",
        )
        self._writer = AsyncRedisStreamWriter(
            redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            stream=os.getenv("FACE_ROI_STREAM", "security.face_rois"),
            maxlen=env_int("FACE_ROI_STREAM_MAXLEN", 20000),
            component="savant_security_face_roi_writer",
            queue_maxsize=env_int("FACE_ROI_QUEUE_MAXSIZE", 4096),
            socket_timeout_ms=env_int("FACE_ROI_REDIS_WRITE_TIMEOUT_MS", 500),
            connect_timeout_ms=env_int("FACE_ROI_REDIS_CONNECT_TIMEOUT_MS", 500),
        )
        self._crop_queue = queue.Queue(
            maxsize=max(env_int("FACE_ROI_CROP_QUEUE_MAXSIZE", 256), 1)
        )
        self._crop_thread = threading.Thread(
            target=self._crop_worker,
            name="face-roi-download-encoder",
            daemon=True,
        )
        self._crop_thread.start()

    def process_frame(self, buffer: Any, frame_meta: Any) -> None:
        self._frame_count += 1
        if not self._enabled:
            return

        source_id = str(getattr(frame_meta, "source_id", "") or "?")
        camera_id = source_id
        camera_entry = None
        if self._camera_bundle is not None:
            entry = self._camera_bundle.get_by_source_id(source_id)
            if entry is not None:
                camera_entry = entry
                camera_id = entry.camera_id

        anchor = extract_frame_anchor_metadata(frame_meta)
        frame_pts = anchor.get("frame_pts")
        timestamp_ms = normalize_pts_to_ms(frame_pts or 0) or self._frame_count
        stream_session_id = stream_session_id_for_frame(source_id, frame_pts)
        runtime_epoch_id = self._runtime_epoch_id(camera_entry)

        faces = [obj for obj in frame_meta.objects if getattr(obj, "label", "") == "face"]
        eligible: list[tuple[int, Any, ReIDGateInput, Any]] = []
        self._counters["candidates"] += len(faces)
        for face_index, obj in enumerate(faces):
            inp = self._candidate_input(obj, source_id, camera_id, timestamp_ms)
            verdict = evaluate_reid_candidate(inp, self._config)
            if not verdict.allowed:
                self._counters["gate_rejected"] += 1
                continue
            if not self._throttle.is_allowed(verdict.throttle_key, timestamp_ms):
                self._counters["throttled"] += 1
                continue
            eligible.append((face_index, obj, inp, verdict))

        if eligible:
            self._export_eligible(
                buffer,
                frame_meta,
                eligible,
                anchor=anchor,
                source_id=source_id,
                camera_id=camera_id,
                stream_session_id=stream_session_id,
                runtime_epoch_id=runtime_epoch_id,
            )

        if self._frame_count % self._log_every == 1:
            self._counters["crop_queue_depth"] = self._crop_queue.qsize()
            print(
                "stage=face_roi_exporter "
                + " ".join(f"{key}={value}" for key, value in self._counters.items()),
                flush=True,
            )

    def _export_eligible(
        self,
        buffer: Any,
        frame_meta: Any,
        eligible: list[tuple[int, Any, ReIDGateInput, Any]],
        **context: Any,
    ) -> None:
        from savant.deepstream.opencv_utils import nvds_to_gpu_mat
        from savant.utils.image import GPUImage

        ttl_ms = max(env_int("FACE_ROI_TTL_MS", 5000), 1)
        if self._crop_queue.full():
            self._drop_crop_batch(eligible)
            return
        with nvds_to_gpu_mat(buffer, frame_meta.frame_meta) as frame_mat:
            frame_image = GPUImage(frame_mat, cuda_stream=self._cuda_stream)
            aligned_faces = []
            for face_index, obj, inp, verdict in eligible:
                try:
                    aligned = self._aligner(obj, frame_image, self._cuda_stream)
                except Exception as exc:
                    self._counters["crop_errors"] += 1
                    self._log_error("crop", exc)
                    continue
                aligned_faces.append((face_index, obj, inp, verdict, aligned))

            if not aligned_faces:
                return
            try:
                # All warps use one CUDA stream. Synchronize once per frame,
                # rather than once per face, before downloading only the
                # aligned 112x112 results.
                self._cuda_stream.waitForCompletion()
            except Exception as exc:
                self._counters["crop_errors"] += len(aligned_faces)
                self._log_error("crop_sync", exc)
                return
            self._counters["crop_batches"] += 1
            self._counters["gpu_syncs"] += 1
            self._counters["max_eligible_per_frame"] = max(
                self._counters["max_eligible_per_frame"], len(aligned_faces)
            )

            created_at_ms = epoch_ms()
            transport_faces = []
            for face_index, obj, inp, verdict, aligned in aligned_faces:
                envelope = self._envelope(
                    face_index,
                    obj,
                    inp,
                    verdict,
                    created_at_ms=created_at_ms,
                    expires_at_ms=created_at_ms + ttl_ms,
                    **context,
                )
                transport_faces.append((aligned, envelope))

            try:
                self._crop_queue.put_nowait(transport_faces)
            except queue.Full:
                self._drop_crop_batch(eligible)
                return
            for _, _, inp, verdict, _ in aligned_faces:
                self._throttle.record(verdict.throttle_key, inp.timestamp_ms)
            self._counters["crop_queue_depth"] = self._crop_queue.qsize()

    def _crop_worker(self) -> None:
        import cv2

        quality = env_int("FACE_ROI_JPEG_QUALITY", 95)
        while True:
            job = self._crop_queue.get()
            try:
                if job is _STOP:
                    return
                for aligned, envelope in job:
                    try:
                        image = aligned.to_cpu().np_array
                    except Exception as exc:
                        self._counters["crop_errors"] += 1
                        self._log_error("crop_download", exc)
                        continue
                    try:
                        if image.ndim == 3 and image.shape[2] == 4:
                            image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
                        ok, encoded = cv2.imencode(
                            ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality]
                        )
                        if not ok:
                            raise RuntimeError("jpeg_encode_returned_false")
                    except Exception as exc:
                        self._counters["encode_errors"] += 1
                        self._log_error("encode", exc)
                        continue
                    if self._writer.enqueue(envelope.redis_fields(encoded.tobytes())):
                        self._counters["eligible"] += 1
                        self._counters["enqueued"] += 1
                    else:
                        self._counters["queue_dropped"] += 1
            except Exception as exc:
                self._counters["queue_dropped"] += len(job)
                self._log_error("crop_worker", exc)
            finally:
                self._crop_queue.task_done()
                self._counters["crop_queue_depth"] = self._crop_queue.qsize()

    def _drop_crop_batch(self, eligible) -> None:
        dropped = len(eligible)
        self._counters["queue_dropped"] += dropped
        self._counters["crop_queue_dropped"] += dropped
        for _, _, inp, verdict in eligible:
            self._throttle.record(verdict.throttle_key, inp.timestamp_ms)

    def on_stop(self) -> None:
        if self._crop_queue is not None and self._crop_thread is not None:
            self._crop_queue.join()
            self._crop_queue.put(_STOP)
            self._crop_thread.join(timeout=2.0)
        if self._writer is not None:
            self._writer.flush(timeout_s=5.0)
            self._writer.close(timeout_s=1.0)

    def _candidate_input(
        self, obj: Any, source_id: str, camera_id: str, timestamp_ms: int
    ) -> ReIDGateInput:
        inp = ReIDGateInput(
            source_id=source_id,
            camera_id=camera_id,
            timestamp_ms=timestamp_ms,
            face_confidence=float(getattr(obj, "confidence", 0.0)),
        )
        bbox = getattr(obj, "bbox", None)
        if bbox is not None:
            inp.face_width = float(getattr(bbox, "width", 0.0))
            inp.face_height = float(getattr(bbox, "height", 0.0))
        inp.landmarks = self._read_landmarks(obj)
        track_id = self._read_attr(obj, "face_person_associator", "person_track_id")
        try:
            inp.person_track_id = int(track_id or 0)
        except (TypeError, ValueError):
            inp.person_track_id = 0
        inp.has_track_id = inp.person_track_id > 0
        method = self._read_attr(obj, "face_person_associator", "association_method")
        inp.association_method = str(method or "")
        return inp

    def _envelope(
        self,
        face_index: int,
        obj: Any,
        inp: ReIDGateInput,
        verdict: Any,
        *,
        anchor: dict[str, Any],
        source_id: str,
        camera_id: str,
        stream_session_id: str,
        runtime_epoch_id: str | None,
        created_at_ms: int,
        expires_at_ms: int,
    ) -> FaceRoiEnvelope:
        bbox = getattr(obj, "bbox", None)
        face_bbox = {
            "format": "cxcywh",
            "values": [
                float(getattr(bbox, "xc", 0.0)),
                float(getattr(bbox, "yc", 0.0)),
                float(getattr(bbox, "width", 0.0)),
                float(getattr(bbox, "height", 0.0)),
            ],
            "coordinate_space": "pixel",
        }
        score = self._read_attr(obj, "face_person_associator", "association_score")
        return FaceRoiEnvelope(
            source_id=source_id,
            camera_id=camera_id,
            person_track_id=inp.person_track_id,
            face_index=face_index,
            timestamp_ms=inp.timestamp_ms,
            frame_num=anchor.get("frame_num"),
            frame_uuid=anchor.get("frame_uuid"),
            keyframe_uuid=anchor.get("keyframe_uuid"),
            previous_keyframe_uuid=anchor.get("previous_keyframe_uuid"),
            frame_pts=anchor.get("frame_pts"),
            frame_dts=anchor.get("frame_dts"),
            duration=anchor.get("duration"),
            time_base=anchor.get("time_base"),
            ntp_timestamp=anchor.get("ntp_timestamp"),
            runtime_epoch_id=runtime_epoch_id,
            stream_session_id=stream_session_id,
            face_bbox=face_bbox,
            landmarks=list(inp.landmarks or []),
            face_confidence=inp.face_confidence,
            quality=float(verdict.quality_score),
            association_score=float(score or 0.0),
            association_method=inp.association_method,
            throttle_key=verdict.throttle_key,
            created_at_ms=created_at_ms,
            expires_at_ms=expires_at_ms,
        )

    @staticmethod
    def _read_attr(obj: Any, namespace: str, name: str) -> Any:
        try:
            attr = obj.get_attr_meta(namespace, name)
            return getattr(attr, "value", None) if attr is not None else None
        except Exception:
            return None

    @classmethod
    def _read_landmarks(cls, obj: Any):
        value = cls._read_attr(obj, "yolov8_face", "landmarks")
        if value is None:
            return None
        try:
            import numpy as np

            return [float(item) for item in np.asarray(value).reshape(-1)]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _runtime_epoch_id(camera_entry: Any = None) -> str | None:
        value = os.getenv("RUNTIME_EPOCH_ID") or os.getenv(
            "VIDEO_ANALYTICS_RUNTIME_EPOCH_ID"
        )
        if not value and camera_entry is not None:
            value = getattr(camera_entry, "runtime_epoch_id", None)
        return str(value) if value else None

    def _log_error(self, stage: str, exc: Exception) -> None:
        if self._frame_count % self._log_every == 1:
            print(
                f"stage=face_roi_exporter_error operation={stage} "
                f"error={type(exc).__name__}:{exc}",
                flush=True,
            )
