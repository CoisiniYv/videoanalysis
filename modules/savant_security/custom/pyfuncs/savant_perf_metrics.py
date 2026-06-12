"""Stable project-level Savant performance metrics aliases."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

try:
    from savant.metrics import get_or_create_counter, get_or_create_gauge
except Exception:  # pragma: no cover - Savant is available only in runtime image.
    get_or_create_counter = None  # type: ignore[assignment]
    get_or_create_gauge = None  # type: ignore[assignment]


class SavantPerfMetricsPyFunc(NvDsPyFuncPlugin):
    """Emit stable ``va_savant_*`` metrics by source_id."""

    def __init__(self, fps_window_s: float = 10.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fps_window_s = max(float(fps_window_s), 1.0)
        self._frame_times: dict[str, deque[float]] = defaultdict(deque)
        self._last_seen: dict[str, float] = {}
        self._counters = {
            "frames_seen": _metric(
                "counter",
                "va_savant_frames_seen",
                "Frames observed by the project Savant metrics PyFunc.",
                ["source_id"],
            ),
            "frame_annotations_exported": _metric(
                "counter",
                "va_savant_frame_annotations_exported",
                "Frames reaching the post-inference frame annotation export point.",
                ["source_id"],
            ),
            "pose_stage_frames": _metric(
                "counter",
                "va_savant_pose_stage_frames",
                "Frames reaching the pose-stage metrics point.",
                ["source_id"],
            ),
            "pose_frames_with_person": _metric(
                "counter",
                "va_savant_pose_frames_with_person",
                "Frames with at least one person object.",
                ["source_id"],
            ),
            "pose_objects": _metric(
                "counter",
                "va_savant_pose_objects",
                "Person objects observed after pose/tracker stages.",
                ["source_id"],
            ),
            "face_stage_frames": _metric(
                "counter",
                "va_savant_face_stage_frames",
                "Frames reaching the face-stage metrics point.",
                ["source_id"],
            ),
            "face_frames_with_face": _metric(
                "counter",
                "va_savant_face_frames_with_face",
                "Frames with at least one face object.",
                ["source_id"],
            ),
            "face_objects": _metric(
                "counter",
                "va_savant_face_objects",
                "Face objects observed after face detector stages.",
                ["source_id"],
            ),
            "adaface_embeddings": _metric(
                "counter",
                "va_savant_adaface_embeddings",
                "AdaFace feature attributes observed on face objects.",
                ["source_id"],
            ),
            "person_observations_exported": _metric(
                "counter",
                "va_savant_person_observations_exported",
                "Person observation opportunities observed by the metrics PyFunc.",
                ["source_id"],
            ),
            "face_observations_exported": _metric(
                "counter",
                "va_savant_face_observations_exported",
                "Face observations allowed for export by the ReID gate.",
                ["source_id"],
            ),
        }
        self._gauges = {
            "effective_fps": _metric(
                "gauge",
                "va_savant_effective_fps",
                "Observed effective FPS over a short rolling window.",
                ["source_id", "window"],
            ),
            "last_frame_age": _metric(
                "gauge",
                "va_savant_last_frame_age_seconds",
                "Seconds since the last observed frame by source.",
                ["source_id"],
            ),
            "sources_active": _metric(
                "gauge",
                "va_savant_sources_active",
                "Number of sources observed within the rolling FPS window.",
                [],
            ),
        }

    def process_frame(self, buffer: Any, frame_meta: Any) -> None:
        del buffer
        now = time.time()
        source_id = str(getattr(frame_meta, "source_id", "") or "unknown")
        objects = list(getattr(frame_meta, "objects", []) or [])
        person_count = sum(1 for obj in objects if getattr(obj, "label", "") == "person")
        face_objects = [obj for obj in objects if getattr(obj, "label", "") == "face"]
        face_count = len(face_objects)
        embedding_count = sum(1 for obj in face_objects if _has_attr(obj, "adaface", "feature"))
        face_export_count = sum(
            1
            for obj in face_objects
            if _attr_value(obj, "face_reid_gate", "reid_allowed") is True
        )

        for key in self._counters:
            self._inc(key, source_id, 0)
        self._inc("frames_seen", source_id)
        self._inc("frame_annotations_exported", source_id)
        self._inc("pose_stage_frames", source_id)
        self._inc("face_stage_frames", source_id)
        if person_count:
            self._inc("pose_frames_with_person", source_id)
            self._inc("pose_objects", source_id, person_count)
            self._inc("person_observations_exported", source_id, person_count)
        if face_count:
            self._inc("face_frames_with_face", source_id)
            self._inc("face_objects", source_id, face_count)
        if embedding_count:
            self._inc("adaface_embeddings", source_id, embedding_count)
        if face_export_count:
            self._inc("face_observations_exported", source_id, face_export_count)

        self._observe_fps(source_id, now)

    def _inc(self, key: str, source_id: str, value: int = 1) -> None:
        _inc(self._counters[key], {"source_id": source_id}, value)

    def _observe_fps(self, source_id: str, now: float) -> None:
        window = self._frame_times[source_id]
        window.append(now)
        cutoff = now - self._fps_window_s
        while window and window[0] < cutoff:
            window.popleft()
        self._last_seen[source_id] = now
        fps = len(window) / self._fps_window_s
        _set(
            self._gauges["effective_fps"],
            {"source_id": source_id, "window": f"{int(self._fps_window_s)}s"},
            fps,
        )
        for sid, last_seen in list(self._last_seen.items()):
            if now - last_seen > self._fps_window_s:
                continue
            _set(self._gauges["last_frame_age"], {"source_id": sid}, now - last_seen)
        active = sum(1 for last_seen in self._last_seen.values() if now - last_seen <= self._fps_window_s)
        _set(self._gauges["sources_active"], {}, float(active))


def _metric(kind: str, name: str, description: str, labels: list[str]) -> Any:
    factory = get_or_create_counter if kind == "counter" else get_or_create_gauge
    if factory is None:
        return None
    for kwargs in ({"labels": labels}, {"labelnames": labels}, {"label_names": labels}, {}):
        try:
            return factory(name, description, **kwargs)
        except TypeError:
            continue
        except Exception:
            return None
    return None


def _inc(metric: Any, labels: dict[str, str], value: float) -> None:
    if metric is None:
        return
    label_values = list(labels.values())
    if label_values:
        try:
            metric.inc(value, label_values)
            return
        except TypeError:
            pass
        except Exception:
            return
    child = _child(metric, labels)
    for args in ((value,), ()):
        try:
            child.inc(*args)
            return
        except TypeError:
            continue
        except Exception:
            return


def _set(metric: Any, labels: dict[str, str], value: float) -> None:
    if metric is None:
        return
    label_values = list(labels.values())
    if label_values:
        try:
            metric.set(value, label_values)
            return
        except TypeError:
            pass
        except Exception:
            return
    child = _child(metric, labels)
    try:
        child.set(value)
    except Exception:
        return


def _child(metric: Any, labels: dict[str, str]) -> Any:
    if not labels:
        return metric
    try:
        return metric.labels(**labels)
    except Exception:
        return metric


def _has_attr(obj: Any, namespace: str, name: str) -> bool:
    return _attr_value(obj, namespace, name) is not None


def _attr_value(obj: Any, namespace: str, name: str) -> Any:
    try:
        attr = obj.get_attr_meta(namespace, name)
    except Exception:
        return None
    if attr is None:
        return None
    return getattr(attr, "value", None)
