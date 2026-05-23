"""PoseMetadataProbe — stdout-only PyFunc for Phase 1D.

Reads YOLO26-pose detections from complex-model metadata and prints
bbox + keypoints.  Writes nothing to Redis, PostgreSQL, or any external
service.

Every ``log_every_n_frames`` frames a heartbeat line is printed even
when no objects are detected.  All output uses ``flush=True``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

import numpy as np

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.yolo26_pose_decode import DECODER_LAYOUT_POST_NMS_57

logger = logging.getLogger(__name__)

COCO_KEYPOINT_NAMES = [
    "nose",
    "left_eye",
    "right_eye",
    "left_ear",
    "right_ear",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
]

# Limit frame_meta introspection to first N frames to avoid spam
_MAX_INTROSPECT_FRAMES = 3


class PoseMetadataProbe(NvDsPyFuncPlugin):
    """Logs YOLO26-pose detections to stdout every ``log_every_n_frames``.

    Output format (key=value on a single line)::

        stage=phase1d_pose_probe
        decoder_layout=post_nms_57
        frame=<global frame count>
        objects=<count>
        obj0_bbox=(left,top,width,height)
        obj0_confidence=0.92
        obj0_keypoints_count=17
        obj0_valid_keypoints=15
        obj0_keypoint_nose=(x,y,conf)
        ...
    """

    def __init__(
        self,
        log_every_n_frames: int = 15,
        decoder_layout: str = DECODER_LAYOUT_POST_NMS_57,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.log_every_n_frames = int(log_every_n_frames)
        self.decoder_layout = decoder_layout
        self.frame_count = 0
        self._introspect_count = 0

    def on_start(self) -> bool:
        self._emit(
            "stage=phase1d_pose_probe heartbeat "
            f"decoder_layout={self.decoder_layout} "
            "frame_id=0 objects=0 keypoints_status=starting"
        )
        return super().on_start()

    def process_frame(self, buffer, frame_meta):
        self.frame_count += 1

        if self.frame_count % self.log_every_n_frames != 0:
            return

        # Read frame ID — best-effort, don't crash on failure
        frame_id = "unknown"
        try:
            if hasattr(frame_meta, "frame_num"):
                frame_id = frame_meta.frame_num
            elif hasattr(frame_meta, "batch_id"):
                frame_id = frame_meta.batch_id
        except Exception:
            pass

        # ==============================================================
        # Source A: Savant-level objects API
        # ==============================================================
        savant_objects: List[Dict[str, Any]] = []
        savant_objects_available = "unavailable"
        try:
            savant_objects = self._collect_savant_objects(frame_meta)
            savant_objects_available = str(len(savant_objects))
        except Exception as exc:
            print(
                f"stage=phase1d_pose_probe_read_error "
                f"source=savant_objects error={exc}",
                flush=True,
            )

        # ==============================================================
        # Source B: DeepStream raw obj_meta_list
        # ==============================================================
        nvds_objects: List[Dict[str, Any]] = []
        nvds_num_obj_meta = "unavailable"
        try:
            nvds_objects = self._collect_nvds_objects(frame_meta)
            nvds_num_obj_meta = str(len(nvds_objects))
        except Exception as exc:
            print(
                f"stage=phase1d_pose_probe_read_error "
                f"source=nvds_obj_meta error={exc}",
                flush=True,
            )

        # ==============================================================
        # Source C: frame_meta introspection (first 3 frames only)
        # ==============================================================
        if self._introspect_count < _MAX_INTROSPECT_FRAMES:
            self._introspect_count += 1
            self._introspect_frame_meta(frame_meta, frame_id)

        # ==============================================================
        # Summary: meta_sources
        # ==============================================================
        print(
            f"stage=phase1d_pose_probe_meta_sources "
            f"frame_id={frame_id} "
            f"savant_objects_count={savant_objects_available} "
            f"nvds_num_obj_meta={nvds_num_obj_meta}",
            flush=True,
        )

        # ==============================================================
        # Dump NVDS objects if any
        # ==============================================================
        for obj in nvds_objects:
            print(
                f"stage=phase1d_pose_probe_nvds_object "
                f"class_id={obj.get('class_id', '?')} "
                f"confidence={obj.get('confidence', '?'):.4f} "
                f"bbox=({obj.get('left', '?'):.2f},{obj.get('top', '?'):.2f},"
                f"{obj.get('width', '?'):.2f},{obj.get('height', '?'):.2f}) "
                f"component_id={obj.get('component_id', '?')}",
                flush=True,
            )

        # ==============================================================
        # Dump Savant objects if any
        # ==============================================================
        for obj in savant_objects:
            print(
                f"stage=phase1d_pose_probe_savant_object "
                f"label={obj.get('label', '?')} "
                f"confidence={obj.get('confidence', 0):.4f} "
                f"bbox=({obj.get('bbox_left', 0):.2f},{obj.get('bbox_top', 0):.2f},"
                f"{obj.get('bbox_width', 0):.2f},{obj.get('bbox_height', 0):.2f}) "
                f"keypoints_status={'present' if obj.get('keypoints') is not None else 'absent'}",
                flush=True,
            )

        # ==============================================================
        # Build heartbeat / per-object details (from Savant objects)
        # ==============================================================
        # Use whichever source had objects; prefer Savant objects
        objects_meta = savant_objects or nvds_objects
        keypoints_status = "ok"

        parts = [
            f"stage=phase1d_pose_probe",
            f"decoder_layout={self.decoder_layout}",
            f"frame_id={frame_id}",
            f"objects={len(objects_meta)}",
        ]

        if not objects_meta:
            parts.append("keypoints_status=no_objects")
            self._emit(" ".join(parts))
            return

        for idx, obj in enumerate(objects_meta):
            bbox = obj.get("bbox", (0, 0, 0, 0))
            parts.append(
                f"obj{idx}_bbox=({bbox[0]:.1f},{bbox[1]:.1f},{bbox[2]:.1f},{bbox[3]:.1f})"
            )
            parts.append(f"obj{idx}_confidence={obj.get('confidence', 0):.4f}")
            parts.append(f"obj{idx}_keypoints_count={obj.get('keypoints_count', 0)}")
            parts.append(f"obj{idx}_valid_keypoints={obj.get('valid_keypoints', 0)}")

            kpts = obj.get("keypoints")
            if kpts is not None:
                kpts_17x3 = self._normalize_keypoints(kpts)
                if kpts_17x3 is not None:
                    for ki in range(17):
                        parts.append(
                            f"obj{idx}_keypoint_{COCO_KEYPOINT_NAMES[ki]}="
                            f"({kpts_17x3[ki,0]:.3f},{kpts_17x3[ki,1]:.3f},{kpts_17x3[ki,2]:.3f})"
                        )

        self._emit(" ".join(parts))

    # ------------------------------------------------------------------
    # Output helper
    # ------------------------------------------------------------------

    @staticmethod
    def _emit(msg: str):
        """Print to stdout with immediate flush."""
        print(msg, flush=True)

    # ------------------------------------------------------------------
    # Source A: Savant-level objects
    # ------------------------------------------------------------------

    def _collect_savant_objects(self, frame_meta) -> List[Dict[str, Any]]:
        """Iterate using Savant-level frame_meta API."""
        objects: List[Dict[str, Any]] = []

        # Try various Savant object iteration patterns
        obj_iter = None

        # Pattern 1: frame_meta.objects()  (callable)
        if hasattr(frame_meta, "objects") and callable(getattr(frame_meta, "objects", None)):
            try:
                obj_iter = frame_meta.objects()
            except Exception:
                pass

        # Pattern 2: frame_meta.get_objects()  (alternative API)
        if obj_iter is None:
            if hasattr(frame_meta, "get_objects") and callable(getattr(frame_meta, "get_objects", None)):
                try:
                    obj_iter = frame_meta.get_objects()
                except Exception:
                    pass

        # Pattern 3: frame_meta.object_meta (iterable property)
        if obj_iter is None:
            if hasattr(frame_meta, "object_meta"):
                try:
                    obj_iter = frame_meta.object_meta
                except Exception:
                    pass

        if obj_iter is None:
            return objects

        for obj in obj_iter:
            obj_dict = self._extract_savant_object(obj)
            if obj_dict:
                objects.append(obj_dict)

        return objects

    def _extract_savant_object(self, obj) -> Optional[Dict[str, Any]]:
        """Extract from a Savant-level Object wrapper (complex model)."""
        try:
            result: Dict[str, Any] = {}

            # --- bbox ---
            if hasattr(obj, "bbox"):
                b = obj.bbox
                result["bbox"] = (float(b.x), float(b.y), float(b.width), float(b.height))
                result["bbox_left"] = float(b.x)
                result["bbox_top"] = float(b.y)
                result["bbox_width"] = float(b.width)
                result["bbox_height"] = float(b.height)
            else:
                result["bbox"] = (0, 0, 0, 0)

            result["confidence"] = float(getattr(obj, "confidence", 0))
            result["label"] = str(getattr(obj, "label", ""))
            result["class_id"] = int(getattr(obj, "class_id", getattr(obj, "label_id", -1)))

            # --- keypoints from attributes ---
            kpts_raw = self._read_keypoints_from_attributes(obj)
            kpts_norm = self._normalize_keypoints(kpts_raw)
            if kpts_norm is not None:
                result["keypoints"] = kpts_norm
                result["keypoints_count"] = 17
                result["valid_keypoints"] = int(np.sum(kpts_norm[:, 2] >= 0.5))
            else:
                result["keypoints_count"] = 0
                result["valid_keypoints"] = 0

            return result
        except Exception as exc:
            print(
                f"stage=phase1d_pose_probe_read_error "
                f"source=extract_savant_object error={exc}",
                flush=True,
            )
            return None

    # ------------------------------------------------------------------
    # Source B: DeepStream raw obj_meta_list
    # ------------------------------------------------------------------

    def _collect_nvds_objects(self, frame_meta) -> List[Dict[str, Any]]:
        """Walk DeepStream ``obj_meta_list`` directly."""
        objects: List[Dict[str, Any]] = []

        # Check num_obj_meta if available
        try:
            if hasattr(frame_meta, "num_obj_meta"):
                _ = frame_meta.num_obj_meta  # just probe
        except Exception:
            pass

        # Walk obj_meta_list
        try:
            obj_meta_ptr = getattr(frame_meta, "obj_meta_list", None)
            while obj_meta_ptr is not None:
                try:
                    obj_meta = obj_meta_ptr.data
                except Exception:
                    break
                obj_dict = self._extract_nvds_object(obj_meta)
                if obj_dict:
                    objects.append(obj_dict)
                try:
                    obj_meta_ptr = obj_meta_ptr.next
                except Exception:
                    break
        except Exception as exc:
            print(
                f"stage=phase1d_pose_probe_read_error "
                f"source=obj_meta_list_walk error={exc}",
                flush=True,
            )

        return objects

    def _extract_nvds_object(self, obj_meta) -> Optional[Dict[str, Any]]:
        """Extract from NvDsObjectMeta (ctypes)."""
        try:
            result: Dict[str, Any] = {}
            result["class_id"] = int(getattr(obj_meta, "class_id", -1))
            result["confidence"] = float(getattr(obj_meta, "confidence", 0))
            result["component_id"] = int(getattr(obj_meta, "unique_component_id", -1))

            rect = getattr(obj_meta, "rect_params", None)
            if rect is not None:
                result["left"] = float(getattr(rect, "left", 0))
                result["top"] = float(getattr(rect, "top", 0))
                result["width"] = float(getattr(rect, "width", 0))
                result["height"] = float(getattr(rect, "height", 0))
                result["bbox"] = (
                    result["left"],
                    result["top"],
                    result["width"],
                    result["height"],
                )
            else:
                result["bbox"] = (0, 0, 0, 0)

            # Read keypoints from user_meta
            kpts_raw = self._read_keypoints_from_user_meta(obj_meta)
            kpts_norm = self._normalize_keypoints(kpts_raw)
            if kpts_norm is not None:
                result["keypoints"] = kpts_norm
                result["keypoints_count"] = 17
                result["valid_keypoints"] = int(np.sum(kpts_norm[:, 2] >= 0.5))

            return result
        except Exception as exc:
            print(
                f"stage=phase1d_pose_probe_read_error "
                f"source=extract_nvds_object error={exc}",
                flush=True,
            )
            return None

    # ------------------------------------------------------------------
    # Source C: frame_meta introspection (first 3 frames)
    # ------------------------------------------------------------------

    def _introspect_frame_meta(self, frame_meta, frame_id):
        """Print type and relevant attribute names of frame_meta."""
        try:
            meta_type = type(frame_meta).__name__
            meta_module = getattr(type(frame_meta), "__module__", "?")
            print(
                f"stage=phase1d_pose_probe_introspect "
                f"frame_id={frame_id} "
                f"meta_type={meta_type} "
                f"meta_module={meta_module}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"stage=phase1d_pose_probe_read_error "
                f"source=introspect_type error={exc}",
                flush=True,
            )

        # Print attributes matching keywords
        try:
            keywords = ["object", "obj", "meta", "frame", "batch"]
            matching = []
            for attr_name in dir(frame_meta):
                if any(kw in attr_name.lower() for kw in keywords):
                    matching.append(attr_name)
            print(
                f"stage=phase1d_pose_probe_introspect_attrs "
                f"frame_id={frame_id} "
                f"attrs={matching}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"stage=phase1d_pose_probe_read_error "
                f"source=introspect_attrs error={exc}",
                flush=True,
            )

        # Print batch_size / num_obj_meta / objects count if available
        for num_attr in ["num_obj_meta", "num_objects", "batch_size"]:
            try:
                val = getattr(frame_meta, num_attr, None)
                if val is not None:
                    print(
                        f"stage=phase1d_pose_probe_introspect_attr "
                        f"frame_id={frame_id} "
                        f"attr={num_attr} value={val}",
                        flush=True,
                    )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Keypoint attribute readers
    # ------------------------------------------------------------------

    ATTRIBUTE_NAME_KEYPOINTS = "keypoints"

    def _read_keypoints_from_attributes(self, obj):
        """Read keypoints from a Savant Object's attributes dict.

        Complex model attributes are stored on the object by name.
        Tries several attribute access patterns.  Never raises.
        """
        try:
            # Pattern 1: obj.attributes dict
            if hasattr(obj, "attributes") and isinstance(obj.attributes, dict):
                kpts = obj.attributes.get(self.ATTRIBUTE_NAME_KEYPOINTS)
                if kpts is not None:
                    return kpts

            # Pattern 2: obj.get_attribute(name) method
            if hasattr(obj, "get_attribute") and callable(obj.get_attribute):
                try:
                    kpts = obj.get_attribute(self.ATTRIBUTE_NAME_KEYPOINTS)
                    if kpts is not None:
                        return kpts
                except Exception:
                    pass

            # Pattern 3: direct obj.keypoints attribute
            if hasattr(obj, self.ATTRIBUTE_NAME_KEYPOINTS):
                return getattr(obj, self.ATTRIBUTE_NAME_KEYPOINTS)

            # Pattern 4: obj.metadata dict
            if hasattr(obj, "metadata") and isinstance(obj.metadata, dict):
                kpts = obj.metadata.get(self.ATTRIBUTE_NAME_KEYPOINTS)
                if kpts is not None:
                    return kpts
        except Exception:
            pass

        return None

    @staticmethod
    def _read_keypoints_from_user_meta(obj_meta):
        """Walk ``obj_user_meta_list`` looking for keypoint data."""
        try:
            user_meta_ptr = obj_meta.obj_user_meta_list
            while user_meta_ptr is not None:
                user_meta = user_meta_ptr.data
                if hasattr(user_meta, "user_meta_data"):
                    data = user_meta.user_meta_data
                    if isinstance(data, np.ndarray) and data.shape == (17, 3):
                        return data
                    if hasattr(data, "contents"):
                        try:
                            return np.ctypeslib.as_array(
                                data.contents, shape=(17, 3)
                            )
                        except Exception:
                            pass
                user_meta_ptr = user_meta_ptr.next
        except Exception:
            pass
        return None

    @staticmethod
    def _normalize_keypoints(kpts):
        """Normalize keypoints to a ``(17, 3)`` numpy array regardless of input format.

        Handles:
        - Flat Python list of 51 floats -> reshape to (17, 3)
        - List of 17 lists/tuples of 3 -> array
        - ``(17, 3)`` ndarray -> as-is
        """
        if kpts is None:
            return None
        arr = np.asarray(kpts, dtype=float)
        if arr.ndim == 1 and arr.shape[0] == 51:
            arr = arr.reshape(17, 3)
        if arr.shape == (17, 3):
            return arr
        if arr.size == 51:
            return arr.reshape(17, 3)
        return None
