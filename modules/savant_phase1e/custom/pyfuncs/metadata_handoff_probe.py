"""MetadataHandoffProbe — Phase 1E Step 2B: metadata handoff diagnostic.

Reads YOLO26-pose complex_model output from frame metadata using
multiple read paths to determine whether the Savant metadata handoff
is working correctly.

Phase 1E Step 2B adds a strict official-API read path following Savant's
"Working With Metadata" documentation:
  - ``frame_meta.objects`` (property, not method)
  - ``frame_meta.objects_number``
  - ``obj_meta.get_attr_meta(element_name, attr_name)``

All output uses ``flush=True``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

logger = logging.getLogger(__name__)

# How often (in frames) to emit the main diagnostic line
_DEFAULT_LOG_INTERVAL = 15

# Introspect frame_meta type/attrs on first N frames only
_MAX_INTROSPECT_FRAMES = 3

# How many probe calls to output per-object detail (after that, summary only)
_DETAIL_PROBE_LIMIT = 3


class MetadataHandoffProbe(NvDsPyFuncPlugin):
    """Logs metadata handoff diagnostics to stdout.

    Tries multiple read paths for the same metadata so that we can
    differentiate between:
    - Savant wrapper API not exposing objects
    - DeepStream obj_meta_list being empty
    - Objects not yet written when pyfunc runs

    Phase 1E Step 2B adds a strict official-API read path
    (``_official_api_probe``) that uses only documented Savant APIs.
    """

    def __init__(
        self,
        log_every_n_frames: int = _DEFAULT_LOG_INTERVAL,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.log_every_n_frames = int(log_every_n_frames)
        self.frame_count = 0
        self._introspect_count = 0
        self._probe_detail_count = _DETAIL_PROBE_LIMIT

    def on_start(self) -> bool:
        self._emit(
            "phase=phase1e_legacy_metadata_handoff "
            "frame_num=0 "
            "status=starting"
        )
        return super().on_start()

    def process_frame(self, buffer, frame_meta):
        self.frame_count += 1

        if self.frame_count % self.log_every_n_frames != 0:
            return

        # ---- frame ID ----
        frame_num = "unknown"
        try:
            if hasattr(frame_meta, "frame_num"):
                frame_num = frame_meta.frame_num
            elif hasattr(frame_meta, "batch_id"):
                frame_num = frame_meta.batch_id
        except Exception:
            pass

        # ---- Official Savant API probe (Phase 1E Step 2B) ----
        self._official_api_probe(frame_meta)

        # ---- Introspect frame_meta (first few frames) ----
        if self._introspect_count < _MAX_INTROSPECT_FRAMES:
            self._introspect_count += 1
            self._introspect_frame_meta(frame_meta, frame_num)

        # ---- Direct DeepStream metadata peek -------------------------------
        ds_num_obj_meta = "unavailable"
        try:
            ds_val = getattr(frame_meta, "num_obj_meta", None)
            if ds_val is not None:
                ds_num_obj_meta = str(ds_val)
        except Exception:
            pass

        try:
            ds_batch_meta = getattr(frame_meta, "batch_meta", None)
            if ds_batch_meta is not None:
                ds_num_frame_meta = getattr(ds_batch_meta, "num_frame_meta", None)
            else:
                ds_num_frame_meta = None
        except Exception:
            ds_num_frame_meta = None

        print(
            f"phase=phase1e_probe_metadata_check "
            f"frame_num={frame_num} "
            f"frame_meta_type={type(frame_meta).__name__} "
            f"ds_num_obj_meta={ds_num_obj_meta} "
            f"has_batch_meta={'yes' if ds_batch_meta is not None else 'no'} "
            f"batch_num_frame_meta={ds_num_frame_meta}",
            flush=True,
        )

        # ---- Legacy read paths (kept for comparison, not authoritative) ----
        savant_objects: List[Dict[str, Any]] = []
        nvds_objects: List[Dict[str, Any]] = []
        warnings: List[str] = []

        # Path A: Savant API (legacy)
        try:
            savant_objects = self._collect_via_savant_api(frame_meta)
        except Exception as exc:
            warnings.append(f"savant_api_error:{exc}")

        # Path B: DeepStream raw obj_meta_list (legacy)
        try:
            nvds_objects = self._collect_via_nvds(frame_meta)
        except Exception as exc:
            warnings.append(f"nvds_error:{exc}")

        # ---- Determine which path returned data ----
        read_path = "none"
        if savant_objects:
            read_path = "savant_api"
        elif nvds_objects:
            read_path = "nvds_obj_meta_list"

        # Merge for person counting (prefer Savant for keypoints)
        all_objects = savant_objects if savant_objects else nvds_objects
        persons = [o for o in all_objects if o.get("class_id") == 0 or o.get("label") == "person"]

        savant_count = str(len(savant_objects)) if savant_objects is not None else "unavailable"
        nvds_count = str(len(nvds_objects)) if nvds_objects is not None else "unavailable"

        # ---- Build diagnostic line ----
        parts = [
            f"phase=phase1e_legacy_metadata_handoff",
            f"frame_num={frame_num}",
            f"savant_objects_count={savant_count}",
            f"nvds_num_obj_meta={nvds_count}",
            f"person_count={len(persons)}",
            f"read_path={read_path}",
        ]

        if persons:
            p = persons[0]
            bbox = p.get("bbox", (0, 0, 0, 0))
            parts.append(f"first_bbox=({bbox[0]:.2f},{bbox[1]:.2f},{bbox[2]:.2f},{bbox[3]:.2f})")
            parts.append(f"first_confidence={p.get('confidence', 0):.4f}")
            kpts = p.get("keypoints")
            if kpts is not None:
                kpts_len = len(kpts) if isinstance(kpts, (list, tuple)) else (
                    kpts.shape[0] * kpts.shape[1] if hasattr(kpts, "shape") and len(kpts.shape) == 2 else 0
                )
                parts.append(f"first_keypoints_len={kpts_len}")
            else:
                parts.append("first_keypoints_len=0")
            parts.append(f"first_track_id={p.get('track_id', 'none')}")
        else:
            parts.append("first_bbox=(0,0,0,0)")
            parts.append("first_confidence=0.0")
            parts.append("first_keypoints_len=0")
            parts.append("first_track_id=none")

        if warnings:
            parts.append(f"warning_or_error=\"{'; '.join(warnings)}\"")
        else:
            parts.append("warning_or_error=none")

        self._emit(" ".join(parts))

        # ---- Per-object detail (first 5 persons) ----
        for idx, obj in enumerate(persons[:5]):
            bbox = obj.get("bbox", (0, 0, 0, 0))
            kpts = obj.get("keypoints")
            kpts_len = 0
            if kpts is not None:
                if isinstance(kpts, (list, tuple)):
                    kpts_len = len(kpts)
                elif hasattr(kpts, "shape"):
                    kpts_len = kpts.shape[0] * kpts.shape[1] if len(kpts.shape) == 2 else kpts.size

            self._emit(
                f"phase=phase1e_legacy_metadata_handoff_obj "
                f"frame_num={frame_num} "
                f"idx={idx} "
                f"bbox=({bbox[0]:.2f},{bbox[1]:.2f},{bbox[2]:.2f},{bbox[3]:.2f}) "
                f"confidence={obj.get('confidence', 0):.4f} "
                f"class_id={obj.get('class_id', '?')} "
                f"keypoints_len={kpts_len} "
                f"track_id={obj.get('track_id', 'none')} "
                f"source={obj.get('_source', 'unknown')}"
            )

    # ------------------------------------------------------------------
    # Output helper
    # ------------------------------------------------------------------

    @staticmethod
    def _emit(msg: str):
        print(msg, flush=True)

    # ------------------------------------------------------------------
    # Official Savant API probe  (Phase 1E Step 2B)
    # ------------------------------------------------------------------

    def _official_api_probe(self, frame_meta):
        """Strictly follow Savant "Working With Metadata" official API.

        Read path documented at:
          - frame_meta.objects  (property, not callable)
          - frame_meta.objects_number
          - obj_meta.get_attr_meta(element_name, attr_name)
        """
        frame_num = getattr(frame_meta, "frame_num", "unknown")
        meta_type = type(frame_meta).__name__

        # ---- A. Frame-level metadata ----
        source_id = "unknown"
        try:
            source_id = str(getattr(frame_meta, "source_id", "unknown"))
        except Exception:
            pass

        objects_number = "unavailable"
        try:
            val = getattr(frame_meta, "objects_number", None)
            if val is not None:
                objects_number = str(val)
        except Exception as e:
            objects_number = f"error:{e}"

        has_objects_prop = str(hasattr(frame_meta, "objects"))
        objects_prop_type = "unavailable"
        try:
            objs = getattr(frame_meta, "objects", None)
            objects_prop_type = type(objs).__name__ if objs is not None else "None"
        except Exception as e:
            objects_prop_type = f"error:{e}"

        self._emit(
            "phase=phase1e_official_metadata_api "
            f"frame_num={frame_num} "
            f"frame_meta_type={meta_type} "
            f"source_id={source_id} "
            f"objects_number={objects_number} "
            f"has_objects_property={has_objects_prop} "
            f"objects_property_type={objects_prop_type}"
        )

        # ---- B. Object iteration via frame_meta.objects ----
        obj_iter = None
        try:
            obj_iter = frame_meta.objects
        except Exception as e:
            self._emit(
                "phase=phase1e_official_metadata_error "
                f"frame_num={frame_num} "
                f"reason=objects_property_access_error:{e}"
            )
            return

        if obj_iter is None:
            self._emit(
                "phase=phase1e_official_metadata_error "
                f"frame_num={frame_num} "
                f"reason=objects_property_is_none"
            )
            return

        count = 0
        has_primary_frame = False
        has_person = False
        person_count = 0
        tracked_person_count = 0
        untracked_person_count = 0
        first_valid_track_id = "none"
        keypoints_objects = 0
        keypoints_attr_list_len = 0
        keypoints_value_len = 0
        element_names_seen = set()

        try:
            for obj_meta in obj_iter:
                label = str(getattr(obj_meta, "label", ""))
                el_name = str(getattr(obj_meta, "element_name", ""))
                element_names_seen.add(el_name)
                is_primary = bool(getattr(obj_meta, "is_primary", False))

                if is_primary or label == "frame":
                    has_primary_frame = True

                if label == "person" or el_name == "yolo26_pose":
                    has_person = True
                    person_count += 1

                    # Track id for tracker readiness assessment
                    tid = getattr(obj_meta, "track_id", None)
                    if tid is None:
                        tid = getattr(obj_meta, "object_id", None)
                    try:
                        tid_int = int(tid) if tid is not None else -1
                    except (ValueError, TypeError):
                        tid_int = -1
                    is_valid = tid_int > 0 and tid_int != 18446744073709551615
                    if is_valid:
                        tracked_person_count += 1
                        if first_valid_track_id == "none":
                            first_valid_track_id = str(tid_int)
                    else:
                        untracked_person_count += 1

                # Full detail for first 10 objects (first N probe calls only)
                if count < 10 and self._probe_detail_count > 0:
                    confidence = float(getattr(obj_meta, "confidence", 0))
                    track_id = str(getattr(obj_meta, "track_id", ""))
                    obj_class_id = str(getattr(obj_meta, "class_id", -1))

                    # bbox — try Savant wrapper bbox then DS rect_params
                    bbox_xc = bbox_yc = bbox_w = bbox_h = 0.0
                    try:
                        if hasattr(obj_meta, "bbox"):
                            b = obj_meta.bbox
                            bbox_xc = float(getattr(b, "xc", getattr(b, "x", 0)))
                            bbox_yc = float(getattr(b, "yc", getattr(b, "y", 0)))
                            bbox_w = float(getattr(b, "width", 0))
                            bbox_h = float(getattr(b, "height", 0))
                        elif hasattr(obj_meta, "rect_params"):
                            r = obj_meta.rect_params
                            left = float(getattr(r, "left", 0))
                            top = float(getattr(r, "top", 0))
                            w = float(getattr(r, "width", 0))
                            h = float(getattr(r, "height", 0))
                            bbox_xc = left + w / 2
                            bbox_yc = top + h / 2
                            bbox_w = w
                            bbox_h = h
                    except Exception:
                        pass

                    # parent info
                    parent_label = ""
                    parent_el = ""
                    try:
                        parent = getattr(obj_meta, "parent", None)
                        if parent is not None:
                            parent_label = str(getattr(parent, "label", ""))
                            parent_el = str(getattr(parent, "element_name", ""))
                    except Exception:
                        pass

                    self._emit(
                        "phase=phase1e_official_obj "
                        f"frame_num={frame_num} "
                        f"index={count} "
                        f"element_name={el_name} "
                        f"label={label} "
                        f"confidence={confidence:.4f} "
                        f"track_id={track_id} "
                        f"is_primary={is_primary} "
                        f"class_id={obj_class_id} "
                        f"bbox_xc={bbox_xc:.2f} "
                        f"bbox_yc={bbox_yc:.2f} "
                        f"bbox_width={bbox_w:.2f} "
                        f"bbox_height={bbox_h:.2f} "
                        f"parent_label={parent_label} "
                        f"parent_element_name={parent_el}"
                    )

                    # ---- C. Keypoints attribute on person objects ----
                    if label == "person" or el_name == "yolo26_pose":
                        kpts_vlen, kpts_alen = self._check_official_attrs(obj_meta, frame_num, count, element_names_seen)
                        if kpts_vlen > 0:
                            keypoints_objects += 1
                            keypoints_value_len = kpts_vlen
                            keypoints_attr_list_len = kpts_alen

                        # ---- D. Attr introspection (first 3 person objects) ----
                        if count < 3:
                            self._attr_introspect_obj(obj_meta, frame_num, count)
                            self._brute_force_attr_lookup(obj_meta, frame_num, count)

                count += 1
        except Exception as e:
            self._emit(
                "phase=phase1e_official_metadata_error "
                f"frame_num={frame_num} "
                f"reason=iteration_error:{e}"
            )
            return

        # Decrement detail counter — after _DETAIL_PROBE_LIMIT calls,
        # per-object detail output stops (summary continues every N frames)
        if self._probe_detail_count > 0:
            self._probe_detail_count -= 1

        # ---- D. Primary frame object check ----
        if not has_primary_frame and count > 0:
            self._emit(
                "phase=phase1e_official_metadata_error "
                f"frame_num={frame_num} "
                f"reason=no_primary_frame_object_seen "
                f"total_objects={count} "
                f"element_names_seen={sorted(element_names_seen)}"
            )
        elif count == 0:
            self._emit(
                "phase=phase1e_official_metadata_error "
                f"frame_num={frame_num} "
                f"reason=iterable_is_empty "
                f"objects_number={objects_number}"
            )

        # ---- Summary ----
        metadata_handoff_ok = (
            person_count > 0
            and keypoints_objects > 0
            and keypoints_value_len == 51
        )
        track_id_ready = tracked_person_count > 0
        self._emit(
            "phase=phase1e_official_probe_summary "
            f"frame_num={frame_num} "
            f"official_objects_number={count} "
            f"official_person_count={person_count} "
            f"official_keypoints_objects={keypoints_objects} "
            f"official_keypoints_attr_list_len={keypoints_attr_list_len} "
            f"official_keypoints_value_len={keypoints_value_len} "
            f"metadata_handoff_ok={'true' if metadata_handoff_ok else 'false'} "
            f"official_tracked_person_count={tracked_person_count} "
            f"official_untracked_person_count={untracked_person_count} "
            f"first_valid_track_id={first_valid_track_id} "
            f"track_id_ready={'true' if track_id_ready else 'false'} "
            f"element_names_seen={sorted(element_names_seen)}"
        )

    def _check_official_attrs(self, obj_meta, frame_num, obj_index, known_element_names):
        """Try official attribute API: get_attr_meta and get_attr_meta_list.

        Tries each known element_name plus common patterns:
          - yolo26_pose  (element name from module.yml)
          - ''           (empty — some Savant versions accept this)

        Properly unwraps ``AttributeMeta.value`` — the actual keypoints data,
        NOT the AttributeMeta list length.

        Returns ``(value_len, attr_list_len)`` — both 0 if not found.
        """
        candidates = list(dict.fromkeys(
            list(known_element_names) + ["yolo26_pose", ""]
        ))

        for el_name in candidates:
            # Path 1: get_attr_meta (single AttributeMeta)
            result = self._try_get_attr_meta(obj_meta, el_name, frame_num, obj_index)
            if result is not None:
                return result

            # Path 2: get_attr_meta_list (List[AttributeMeta])
            result = self._try_get_attr_meta_list(obj_meta, el_name, frame_num, obj_index)
            if result is not None:
                return result

        # None of the paths succeeded
        self._emit(
            "phase=phase1e_official_attr "
            f"frame_num={frame_num} "
            f"object_index={obj_index} "
            f"attr_read_path=all "
            f"attr_found=false "
            f"attr_name=keypoints "
            f"keypoints_value_len=0 "
            f"keypoints_value_type=unknown "
            f"attr_confidence=0.0 "
            f"element_names_tried={candidates}"
        )
        return (0, 0)

    def _try_get_attr_meta(self, obj_meta, el_name, frame_num, obj_index):
        """Try ``get_attr_meta(element_name, attr_name)`` → single AttributeMeta.

        Returns ``(value_len, attr_list_len)`` or None if attribute not found.
        ``attr_list_len`` is always 1 for the single-path (a single AttributeMeta).
        """
        if not hasattr(obj_meta, "get_attr_meta"):
            return None
        try:
            attr = obj_meta.get_attr_meta(el_name, "keypoints")
            if attr is None:
                return None

            # Extract AttributeMeta fields
            attr_name = str(getattr(attr, "name", "keypoints"))
            attr_el = str(getattr(attr, "element_name", str(el_name)))
            attr_conf = float(getattr(attr, "confidence", 0))

            value = getattr(attr, "value", None)
            vtype, vlen, triplet, repr_sample = self._extract_value_info(value)

            self._emit(
                "phase=phase1e_official_attr "
                f"frame_num={frame_num} "
                f"object_index={obj_index} "
                f"attr_read_path=get_attr_meta "
                f"element_name={el_name} "
                f"attr_found=true "
                f"attr_list_len=1 "
                f"attr_meta_type={type(attr).__name__} "
                f"attr_name={attr_name} "
                f"attr_element_name={attr_el} "
                f"attr_confidence={attr_conf:.4f} "
                f"keypoints_value_type={vtype} "
                f"keypoints_value_len={vlen} "
                f"keypoints_first_triplet={triplet} "
                f"value_repr_sample={repr_sample}"
            )
            return (vlen, 1)
        except Exception:
            return None

    def _try_get_attr_meta_list(self, obj_meta, el_name, frame_num, obj_index):
        """Try ``get_attr_meta_list(element_name, attr_name)`` → List[AttributeMeta].

        Iterates each AttributeMeta in the list and reads ``.value``.
        Returns ``(value_len, attr_list_len)`` or None if not found.
        """
        if not hasattr(obj_meta, "get_attr_meta_list"):
            return None
        try:
            attrs = obj_meta.get_attr_meta_list(el_name, "keypoints")
            if not attrs:
                return None

            attr_list_len = len(attrs)
            effective_len = 0
            first_vtype = "unknown"
            first_triplet = ""
            first_repr = ""
            last_attr_name = "keypoints"
            last_attr_el = str(el_name)
            last_attr_conf = 0.0

            for i, attr in enumerate(attrs):
                aname = str(getattr(attr, "name", "keypoints"))
                ael = str(getattr(attr, "element_name", str(el_name)))
                aconf = float(getattr(attr, "confidence", 0))
                value = getattr(attr, "value", None)
                vtype, vlen, triplet, repr_sample = self._extract_value_info(value)

                if i == 0:
                    first_vtype = vtype
                    first_triplet = triplet
                    first_repr = repr_sample
                    last_attr_name = aname
                    last_attr_el = ael
                    last_attr_conf = aconf

                # Use the longest value found (the real keypoints data)
                if vlen > effective_len:
                    effective_len = vlen

            self._emit(
                "phase=phase1e_official_attr "
                f"frame_num={frame_num} "
                f"object_index={obj_index} "
                f"attr_read_path=get_attr_meta_list "
                f"element_name={el_name} "
                f"attr_found=true "
                f"attr_list_len={attr_list_len} "
                f"attr_meta_type={type(attrs[0]).__name__} "
                f"attr_name={last_attr_name} "
                f"attr_element_name={last_attr_el} "
                f"attr_confidence={last_attr_conf:.4f} "
                f"keypoints_value_type={first_vtype} "
                f"keypoints_value_len={effective_len} "
                f"keypoints_first_triplet={first_triplet} "
                f"value_repr_sample={first_repr}"
            )
            return (effective_len, attr_list_len)
        except Exception:
            return None

    @staticmethod
    def _extract_value_info(value):
        """Extract (type_name, length, first_triplet, repr_sample) from a value.

        Handles plain lists, nested lists, numpy arrays, dict wraps,
        and other iterables.  Designed to correctly identify the actual
        data length for keypoints (expected 51).
        """
        if value is None:
            return "None", 0, "", ""

        base_type = type(value).__name__

        # --- numpy array ---
        if hasattr(value, "shape"):
            shape = list(value.shape)
            flat = value.reshape(-1) if len(shape) > 1 else value
            flat_len = flat.shape[0]
            trip = ""
            if flat_len >= 3:
                trip = f"({float(flat[0]):.4f},{float(flat[1]):.4f},{float(flat[2]):.4f})"
            return f"ndarray{shape}", flat_len, trip, ""

        # --- list / tuple ---
        if isinstance(value, (list, tuple)):
            vlen = len(value)
            # Nested: [[...51 floats...]] → unwrap
            if vlen == 1 and isinstance(value[0], (list, tuple)):
                inner = value[0]
                ilen = len(inner)
                itrip = ""
                if ilen >= 3:
                    itrip = f"({inner[0]:.4f},{inner[1]:.4f},{inner[2]:.4f})"
                return (
                    f"nested_list",
                    ilen,
                    itrip,
                    f"outer_len={vlen},inner_type={type(inner).__name__},inner_len={ilen}",
                )
            # Nested: [[...]] where each inner is also a list
            if vlen > 0 and isinstance(value[0], (list, tuple)):
                return (
                    f"nested_list",
                    vlen,
                    "",
                    f"first_element_is_list,len={len(value[0])}",
                )
            # Flat list
            trip = ""
            if vlen >= 3:
                trip = f"({value[0]:.4f},{value[1]:.4f},{value[2]:.4f})"
            return base_type, vlen, trip, ""

        # --- dict ---
        if isinstance(value, dict):
            keys = list(value.keys())
            if "value" in keys:
                return (
                    f"dict_wrap",
                    0,
                    "",
                    f"has_key=value,value_type={type(value['value']).__name__}",
                )
            return base_type, 0, "", f"keys={keys[:3]}"

        # --- string (try JSON) ---
        if isinstance(value, str):
            try:
                import json
                parsed = json.loads(value)
                ptype, plen, ptrip, prep = MetadataHandoffProbe._extract_value_info(parsed)
                return f"json({ptype})", plen, ptrip, "parsed_from_json"
            except Exception:
                pass
            return base_type, len(value), value[:60], ""

        # --- object with tolist() ---
        if hasattr(value, "tolist") and callable(value.tolist):
            try:
                lst = value.tolist()
                ltype, llen, ltrip, lrepr = MetadataHandoffProbe._extract_value_info(lst)
                return f"{base_type}.tolist()->{ltype}", llen, ltrip, lrepr
            except Exception:
                pass

        # --- fallback ---
        try:
            vlen = len(value)
            return base_type, vlen, "", f"fallback_len={vlen}"
        except Exception:
            return base_type, 0, "", "no_len_available"

    # ------------------------------------------------------------------
    # Task D: Attr introspection on person objects
    # ------------------------------------------------------------------

    def _attr_introspect_obj(self, obj_meta, frame_num, obj_index):
        """Enumerate attribute-related attributes and containers on obj_meta.

        Tries:
          - obj_meta.attributes
          - obj_meta.attrs
          - obj_meta.attr_meta_list
          - obj_meta._attributes
          - dir() filter for attr/attribute
        """
        label = str(getattr(obj_meta, "label", ""))
        el_name = str(getattr(obj_meta, "element_name", ""))

        # Collect attr-related attribute names from dir()
        attr_related = []
        try:
            for aname in dir(obj_meta):
                low = aname.lower()
                if any(kw in low for kw in ["attr", "attribute"]):
                    attr_related.append(aname)
        except Exception:
            attr_related = ["(dir_error)"]

        self._emit(
            "phase=phase1e_attr_introspect_obj "
            f"frame_num={frame_num} "
            f"object_index={obj_index} "
            f"object_element_name={el_name} "
            f"object_label={label} "
            f"dir_attr_related={attr_related}"
        )

        # Try known attribute containers
        containers = ["attributes", "attrs", "attr_meta_list", "_attributes"]
        for cname in containers:
            try:
                val = getattr(obj_meta, cname, None)
                if val is not None:
                    vtype = type(val).__name__
                    vlen = -1
                    try:
                        vlen = len(val)
                    except Exception:
                        pass
                    self._emit(
                        "phase=phase1e_attr_seen "
                        f"frame_num={frame_num} "
                        f"object_index={obj_index} "
                        f"attr_container={cname} "
                        f"attr_container_type={vtype} "
                        f"attr_container_len={vlen}"
                    )
                    # If iterable, enumerate contents
                    if isinstance(val, (list, tuple, dict)):
                        self._introspect_attr_container(val, cname, frame_num, obj_index)
            except Exception as exc:
                self._emit(
                    "phase=phase1e_attr_seen "
                    f"frame_num={frame_num} "
                    f"object_index={obj_index} "
                    f"attr_container={cname} "
                    f"attr_container_error={exc}"
                )

    def _introspect_attr_container(self, container, container_name, frame_num, obj_index):
        """Enumerate individual items within an attribute container."""
        if isinstance(container, dict):
            for key, val in container.items():
                vtype = type(val).__name__
                vlen = -1
                try:
                    vlen = len(val)
                except Exception:
                    pass
                sample = ""
                if isinstance(val, (list, tuple)) and len(val) > 0:
                    sample = str(val[:3])[:60]
                elif isinstance(val, str):
                    sample = val[:60]
                self._emit(
                    "phase=phase1e_attr_seen_item "
                    f"frame_num={frame_num} "
                    f"object_index={obj_index} "
                    f"container={container_name} "
                    f"key={key} "
                    f"value_type={vtype} "
                    f"value_len={vlen} "
                    f"value_sample={sample}"
                )
        elif isinstance(container, (list, tuple)):
            for i, item in enumerate(container):
                itype = type(item).__name__
                ilen = -1
                try:
                    ilen = len(item)
                except Exception:
                    pass
                # Try to extract name and value if it's an AttributeMeta-like object
                aname = getattr(item, "name", getattr(item, "__name__", ""))
                avalue = getattr(item, "value", item)
                avtype = type(avalue).__name__
                avlen = -1
                try:
                    avlen = len(avalue)
                except Exception:
                    pass
                aconf = getattr(item, "confidence", "?")
                self._emit(
                    "phase=phase1e_attr_seen_item "
                    f"frame_num={frame_num} "
                    f"object_index={obj_index} "
                    f"container={container_name} "
                    f"index={i} "
                    f"item_type={itype} "
                    f"item_len={ilen} "
                    f"attr_name={aname} "
                    f"attr_value_type={avtype} "
                    f"attr_value_len={avlen} "
                    f"attr_confidence={aconf}"
                )

    # ------------------------------------------------------------------
    # Task E: Brute-force attr lookup
    # ------------------------------------------------------------------

    def _brute_force_attr_lookup(self, obj_meta, frame_num, obj_index):
        """Try common element_name/attr_name combinations for keypoints.

        element_names tried:
          - yolo26_pose  (from module.yml element name)
          - auto         (seen element name in metadata)
          - ""           (empty string)
          - obj_meta.element_name
          - parent.element_name

        attr_names tried:
          - keypoints
          - yolo26_pose.keypoints
          - output0.keypoints
          - pose
          - points
        """
        # Collect element name candidates
        parent_el = ""
        try:
            parent = getattr(obj_meta, "parent", None)
            if parent is not None:
                parent_el = str(getattr(parent, "element_name", ""))
        except Exception:
            pass

        own_el = str(getattr(obj_meta, "element_name", ""))

        element_candidates = list(dict.fromkeys([
            "yolo26_pose",
            "auto",
            "",
            own_el,
            parent_el,
        ]))

        attr_candidates = [
            "keypoints",
            "yolo26_pose.keypoints",
            "output0.keypoints",
            "pose",
            "points",
        ]

        for el_name in element_candidates:
            for attr_name in attr_candidates:
                # Try get_attr_meta
                try:
                    if hasattr(obj_meta, "get_attr_meta"):
                        attr = obj_meta.get_attr_meta(el_name, attr_name)
                        if attr is not None:
                            value = getattr(attr, "value", None)
                            vlen = len(value) if value is not None else 0
                            self._emit(
                                "phase=phase1e_attr_lookup_attempt "
                                f"frame_num={frame_num} "
                                f"object_index={obj_index} "
                                f"element_name={el_name} "
                                f"attr_name={attr_name} "
                                f"method=get_attr_meta "
                                f"found=true "
                                f"value_len={vlen} "
                                f"attr_meta_type={type(attr).__name__}"
                            )
                            return  # found it
                except Exception:
                    pass

                # Try get_attr_meta_list
                try:
                    if hasattr(obj_meta, "get_attr_meta_list"):
                        attrs = obj_meta.get_attr_meta_list(el_name, attr_name)
                        if attrs:
                            # Check if any attr has a real value
                            found_with_value = False
                            for attr in attrs:
                                value = getattr(attr, "value", None)
                                if value is not None:
                                    vlen = len(value) if hasattr(value, "__len__") else 0
                                    if vlen > 0:
                                        found_with_value = True
                                        break
                            if found_with_value:
                                self._emit(
                                    "phase=phase1e_attr_lookup_attempt "
                                    f"frame_num={frame_num} "
                                    f"object_index={obj_index} "
                                    f"element_name={el_name} "
                                    f"attr_name={attr_name} "
                                    f"method=get_attr_meta_list "
                                    f"found=true "
                                    f"value_len={vlen} "
                                    f"attr_meta_type={type(attr).__name__}"
                                )
                                return
                except Exception:
                    pass

        # None of the combinations succeeded
        self._emit(
            "phase=phase1e_attr_lookup_attempt "
            f"frame_num={frame_num} "
            f"object_index={obj_index} "
            f"element_name=all "
            f"attr_name=all "
            f"method=all "
            f"found=false "
            f"element_candidates={element_candidates} "
            f"attr_candidates={attr_candidates}"
        )

    def _collect_via_savant_api(self, frame_meta) -> List[Dict[str, Any]]:
        """Iterate objects using Savant-level frame_meta API.

        NOTE: This method tries frame_meta.objects(), get_objects(),
        and object_meta — but NOT frame_meta.objects (the official
        property without parentheses).  The official property is
        handled by _official_api_probe() above.
        """
        objects: List[Dict[str, Any]] = []

        obj_iter = None

        # Pattern 1: frame_meta.objects() (callable)
        if hasattr(frame_meta, "objects") and callable(getattr(frame_meta, "objects", None)):
            try:
                obj_iter = frame_meta.objects()
            except Exception:
                pass

        # Pattern 2: frame_meta.get_objects() (alternative API)
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
                obj_dict["_source"] = "savant_api"
                objects.append(obj_dict)

        return objects

    def _extract_savant_object(self, obj) -> Optional[Dict[str, Any]]:
        """Extract bbox / confidence / keypoints / track_id from a Savant Object."""
        try:
            result: Dict[str, Any] = {}

            # bbox
            if hasattr(obj, "bbox"):
                b = obj.bbox
                result["bbox"] = (float(b.x), float(b.y), float(b.width), float(b.height))
            else:
                result["bbox"] = (0, 0, 0, 0)

            result["confidence"] = float(getattr(obj, "confidence", 0))
            result["label"] = str(getattr(obj, "label", ""))
            result["class_id"] = int(getattr(obj, "class_id", getattr(obj, "label_id", -1)))

            # track_id — if nvtracker is active
            result["track_id"] = getattr(obj, "track_id", None)
            if result["track_id"] is None and hasattr(obj, "object_id"):
                result["track_id"] = obj.object_id

            # keypoints from attributes
            kpts_raw = self._read_keypoints_from_attributes(obj)
            kpts_norm = self._normalize_keypoints(kpts_raw)
            if kpts_norm is not None:
                result["keypoints"] = kpts_norm.tolist()  # plain Python list

            return result
        except Exception as exc:
            logger.debug("extract_savant_object error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Path B: DeepStream NvDsFrameMeta obj_meta_list
    # ------------------------------------------------------------------

    def _collect_via_nvds(self, frame_meta) -> List[Dict[str, Any]]:
        """Walk DeepStream ``obj_meta_list`` pointer chain directly.

        Tries both raw pointer access and pyds.NvDsObjectMeta.cast().
        """
        objects: List[Dict[str, Any]] = []

        obj_meta_ptr = getattr(frame_meta, "obj_meta_list", None)
        if obj_meta_ptr is None:
            return objects

        # Check num_obj_meta if this is a real NvDsFrameMeta
        try:
            num = getattr(frame_meta, "num_obj_meta", None)
            if num is not None:
                print(
                    f"phase=phase1e_probe_nvds_meta "
                    f"frame_num={getattr(frame_meta, 'frame_num', '?')} "
                    f"num_obj_meta={num}",
                    flush=True,
                )
        except Exception:
            pass

        while obj_meta_ptr is not None:
            try:
                obj_meta = obj_meta_ptr.data
            except Exception:
                break

            obj_dict = None
            # Try direct extraction first
            try:
                obj_dict = self._extract_nvds_object(obj_meta)
            except Exception:
                pass

            # If direct extraction yielded empty/invalid, try casting
            if obj_dict is None or not obj_dict.get("bbox"):
                try:
                    import pyds
                    cast_meta = pyds.NvDsObjectMeta.cast(obj_meta_ptr.data)
                    obj_dict = self._extract_nvds_object(cast_meta)
                except Exception:
                    pass

            if obj_dict:
                obj_dict["_source"] = "nvds_obj_meta"
                objects.append(obj_dict)
            else:
                # Print diagnostic about this unreadable object
                try:
                    print(
                        f"phase=phase1e_probe_unreadable_obj "
                        f"ptr_type={type(obj_meta).__name__} "
                        f"ptr_str={str(obj_meta)[:100]}",
                        flush=True,
                    )
                except Exception:
                    pass

            try:
                obj_meta_ptr = obj_meta_ptr.next
            except Exception:
                break

        return objects

    def _extract_nvds_object(self, obj_meta) -> Optional[Dict[str, Any]]:
        """Extract from NvDsObjectMeta (ctypes)."""
        try:
            result: Dict[str, Any] = {}
            result["class_id"] = int(getattr(obj_meta, "class_id", -1))
            result["confidence"] = float(getattr(obj_meta, "confidence", 0))
            result["label"] = str(getattr(obj_meta, "obj_label", ""))
            result["track_id"] = getattr(obj_meta, "object_id", None)

            rect = getattr(obj_meta, "rect_params", None)
            if rect is not None:
                result["bbox"] = (
                    float(getattr(rect, "left", 0)),
                    float(getattr(rect, "top", 0)),
                    float(getattr(rect, "width", 0)),
                    float(getattr(rect, "height", 0)),
                )
            else:
                result["bbox"] = (0, 0, 0, 0)

            # keypoints from user_meta
            kpts_raw = self._read_keypoints_from_user_meta(obj_meta)
            kpts_norm = self._normalize_keypoints(kpts_raw)
            if kpts_norm is not None:
                result["keypoints"] = kpts_norm.tolist()

            return result
        except Exception as exc:
            logger.debug("extract_nvds_object error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # frame_meta introspection (first few frames)
    # ------------------------------------------------------------------

    def _introspect_frame_meta(self, frame_meta, frame_num):
        """Print type and relevant attribute names for debugging."""
        try:
            meta_type = type(frame_meta).__name__
            meta_module = getattr(type(frame_meta), "__module__", "?")
            self._emit(
                f"phase=phase1e_metadata_handoff_introspect "
                f"frame_num={frame_num} "
                f"meta_type={meta_type} "
                f"meta_module={meta_module}"
            )
        except Exception as exc:
            self._emit(
                f"phase=phase1e_metadata_handoff_introspect "
                f"frame_num={frame_num} "
                f"warning_or_error=introspect_type_error:{exc}"
            )

        # Print matching attribute names
        try:
            keywords = ["object", "obj", "meta", "frame", "batch", "num_"]
            matching = []
            for attr_name in dir(frame_meta):
                if any(kw in attr_name.lower() for kw in keywords):
                    matching.append(attr_name)
            self._emit(
                f"phase=phase1e_metadata_handoff_introspect_attrs "
                f"frame_num={frame_num} "
                f"attrs={matching}"
            )
        except Exception as exc:
            self._emit(
                f"phase=phase1e_metadata_handoff_introspect "
                f"frame_num={frame_num} "
                f"warning_or_error=introspect_attrs_error:{exc}"
            )

        # Print numeric attributes
        for num_attr in ["num_obj_meta", "num_objects", "batch_size"]:
            try:
                val = getattr(frame_meta, num_attr, None)
                if val is not None:
                    self._emit(
                        f"phase=phase1e_metadata_handoff_introspect_attr "
                        f"frame_num={frame_num} "
                        f"attr={num_attr} value={val}"
                    )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Keypoint attribute readers
    # ------------------------------------------------------------------

    ATTRIBUTE_NAME_KEYPOINTS = "keypoints"

    def _read_keypoints_from_attributes(self, obj):
        """Read keypoints from a Savant Object's attributes."""
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
                            return np.ctypeslib.as_array(data.contents, shape=(17, 3))
                        except Exception:
                            pass
                user_meta_ptr = user_meta_ptr.next
        except Exception:
            pass
        return None

    @staticmethod
    def _normalize_keypoints(kpts):
        """Normalize keypoints to (17, 3) array.  Returns None on failure."""
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
