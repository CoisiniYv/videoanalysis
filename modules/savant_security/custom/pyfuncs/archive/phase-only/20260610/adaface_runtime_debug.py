"""AdaFaceRuntimeDebugPyFunc — C1F.2c AdaFace runtime validation probe.

Observes AdaFace embedding metadata on face objects. Writes summary to
/data/video-analytics/artifacts/c1f2c/adaface_runtime_summary.jsonl.

HARDENED: No file I/O in on_start(). All operations wrapped in try/except.
Does NOT write Redis, PostgreSQL, images, crops, or base64.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

DEFAULT_OUTPUT_DIR = "/data/video-analytics/artifacts/c1f2c"
DEFAULT_OUTPUT_FILE = "adaface_runtime_summary.jsonl"


class AdaFaceRuntimeDebugPyFunc(NvDsPyFuncPlugin):
    """Observe AdaFace embedding metadata. Never raise."""

    def __init__(self, log_every_n_frames: int = 30, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._log_interval = max(int(log_every_n_frames), 1)
        self._output_dir = Path(
            os.getenv("C1F2C_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)
        )
        self._output_file = os.getenv(
            "C1F2C_OUTPUT_FILE", DEFAULT_OUTPUT_FILE
        )
        self._frame_count = 0
        self._frames_with_face = 0
        self._faces_seen = 0
        self._faces_with_landmarks = 0
        self._faces_with_embedding = 0
        self._faces_with_dim_512 = 0
        self._faces_with_valid_norm = 0
        self._fh: Any = None
        self._file_initialized = False

    def _ensure_file(self) -> None:
        """Lazy file init — called from process_frame, NOT on_start."""
        if self._file_initialized:
            return
        self._file_initialized = True
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            output_path = self._output_dir / self._output_file
            self._fh = output_path.open("a", encoding="utf-8")
            print(
                f"[adaface_debug] enabled output={output_path}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[adaface_debug] file_init_error={type(exc).__name__}: {exc}",
                flush=True,
            )

    def on_stop(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def process_frame(self, buffer: Any, frame_meta: Any) -> None:
        """Top-level wrapper — never raise."""
        try:
            self._ensure_file()
            self._process_frame_inner(buffer, frame_meta)
        except Exception as exc:
            print(
                f"[adaface_debug] process_error={type(exc).__name__}: {exc}",
                flush=True,
            )

    def _process_frame_inner(self, buffer: Any, frame_meta: Any) -> None:
        self._frame_count += 1
        source_id = getattr(frame_meta, "source_id", "?")

        face_objects = []
        try:
            for o in frame_meta.objects:
                try:
                    if getattr(o, "label", "") == "face":
                        face_objects.append(o)
                except Exception:
                    pass
        except Exception:
            pass

        if not face_objects:
            if self._frame_count % self._log_interval == 1:
                print(
                    f"[adaface_debug] frame={self._frame_count} source={source_id} faces=0",
                    flush=True,
                )
            return

        self._frames_with_face += 1

        for obj in face_objects:
            self._faces_seen += 1
            self._process_face(obj, source_id)

    def _process_face(self, obj: Any, source_id: str) -> None:
        """Process a single face object — each field guarded."""
        has_landmarks = False
        has_embedding = False
        embedding_dim = 0
        embedding_norm = 0.0
        valid_norm = False

        # Landmarks
        try:
            lm_attr = obj.get_attr_meta("yolov8_face", "landmarks")
            if lm_attr is not None:
                value = getattr(lm_attr, "value", None)
                if value is not None:
                    lm_list = list(value) if hasattr(value, "__iter__") else []
                    if len(lm_list) >= 5:
                        has_landmarks = True
                        self._faces_with_landmarks += 1
        except Exception:
            pass

        # AdaFace embedding
        for ns in ("adaface", "reid"):
            try:
                attr = obj.get_attr_meta(ns, "feature")
                if attr is not None:
                    value = getattr(attr, "value", None)
                    if value is not None:
                        feat = list(value) if hasattr(value, "__iter__") else []
                        if feat:
                            has_embedding = True
                            embedding_dim = len(feat)
                            embedding_norm = math.sqrt(
                                sum(x * x for x in feat)
                            )
                            self._faces_with_embedding += 1
                            if embedding_dim == 512:
                                self._faces_with_dim_512 += 1
                            if 0.8 <= embedding_norm <= 1.2:
                                valid_norm = True
                                self._faces_with_valid_norm += 1
                    break
            except Exception:
                pass

        # Write per-face record
        self._write_record({
            "frame_num": self._frame_count,
            "source_id": source_id,
            "has_landmarks": has_landmarks,
            "has_embedding": has_embedding,
            "embedding_dim": embedding_dim,
            "embedding_norm": round(embedding_norm, 6),
            "valid_norm": valid_norm,
        })

        # Log periodically
        if self._faces_seen % (self._log_interval * 2) <= 1:
            print(
                f"[adaface_debug] face#{self._faces_seen} "
                f"dim={embedding_dim} norm={embedding_norm:.4f} "
                f"valid={valid_norm} lm={has_landmarks}",
                flush=True,
            )

    def _write_record(self, record: dict[str, Any]) -> None:
        """Write JSONL record — never raise."""
        if self._fh is None:
            return
        try:
            self._fh.write(
                json.dumps(record, default=str, sort_keys=True, separators=(",", ":"))
            )
            self._fh.write("\n")
            self._fh.flush()
        except Exception as exc:
            print(
                f"[adaface_debug] write_error={type(exc).__name__}: {exc}",
                flush=True,
            )

    def get_summary(self) -> dict[str, Any]:
        """Return current summary stats (for smoke parsing)."""
        return {
            "frames_inspected": self._frame_count,
            "frames_with_face": self._frames_with_face,
            "faces_seen": self._faces_seen,
            "faces_with_landmarks": self._faces_with_landmarks,
            "faces_with_embedding": self._faces_with_embedding,
            "faces_with_embedding_dim_512": self._faces_with_dim_512,
            "faces_with_valid_norm": self._faces_with_valid_norm,
        }
