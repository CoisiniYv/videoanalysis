"""FrameAnnotationExporterPyFunc — lightweight producer.

Exports lightweight frame-indexed person/face metadata to
``security.frame_annotations`` when ``FRAME_ANNOTATION_EXPORT_ENABLED=true``.
It does not alter evidence generation, identity matching, SQL fallback, or
media-worker behavior.
"""

from __future__ import annotations

from typing import Any

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.services.frame_annotation_exporter import (
    FrameAnnotationExporterConfig,
    FrameAnnotationExportRuntime,
    create_frame_annotation_exporter,
)


class FrameAnnotationExporterPyFunc(NvDsPyFuncPlugin):
    """Gated Savant adapter for frame annotation stream export."""

    def __init__(
        self,
        enabled: bool = False,
        producer: str = "savant-security",
        stream: str = "security.frame_annotations",
        ttl_seconds: int = 120,
        max_objects_per_frame: int = 100,
        include_keypoints: str = "compact",
        include_landmarks: str = "compact",
        include_embedding: bool = False,
        redis_maxlen: int = 10000,
        source_stream_enabled: bool = False,
        source_stream_pattern: str = "security.frame_annotations.{source_id}",
        stream_mode: str = "global",
        source_redis_maxlen: int = 5000,
        write_timeout_ms: int = 50,
        log_every_n_frames: int = 300,
        min_interval_ms: int | None = None,
        cameras_config_path: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._camera_bundle = self._load_camera_bundle(cameras_config_path)
        if min_interval_ms is None:
            min_interval_ms = _env_int(
                "FRAME_ANNOTATION_EXPORT_MIN_INTERVAL_MS",
                _env_int("FRAME_ANNOTATION_MIN_INTERVAL_MS", 0),
            )
        config = FrameAnnotationExporterConfig(
            enabled=_as_bool(enabled),
            producer=str(producer),
            stream=str(stream or "security.frame_annotations"),
            ttl_seconds=int(ttl_seconds),
            max_objects_per_frame=int(max_objects_per_frame),
            include_keypoints=str(include_keypoints or "compact"),
            include_landmarks=str(include_landmarks or "compact"),
            include_embedding=False,
            redis_maxlen=int(redis_maxlen),
            source_stream_enabled=_as_bool(source_stream_enabled),
            source_stream_pattern=str(
                source_stream_pattern or "security.frame_annotations.{source_id}"
            ),
            stream_mode=str(stream_mode or "global"),
            source_redis_maxlen=int(source_redis_maxlen),
            write_timeout_ms=int(write_timeout_ms),
            log_every_n=int(log_every_n_frames),
            min_interval_ms=int(min_interval_ms),
        )
        exporter = create_frame_annotation_exporter(config)
        self._runtime = FrameAnnotationExportRuntime(
            config=config,
            exporter=exporter,
            resolve_camera_id=self._resolve_camera_id,
        )
        print(
            "component=savant_security_frame_annotation_pyfunc_init "
            f"enabled={config.enabled} "
            f"stream={config.stream} "
            f"ttl_seconds={config.ttl_seconds} "
            f"max_objects_per_frame={config.max_objects_per_frame} "
            f"redis_maxlen={config.redis_maxlen} "
            f"source_stream_enabled={config.source_stream_enabled} "
            f"stream_mode={config.stream_mode} "
            f"source_redis_maxlen={config.source_redis_maxlen} "
            f"min_interval_ms={config.min_interval_ms} "
            f"include_keypoints={config.include_keypoints} "
            f"include_landmarks={config.include_landmarks} "
            "include_embedding_vector=false",
            flush=True,
        )

    @staticmethod
    def _load_camera_bundle(config_path: str):
        if not config_path:
            return None
        from custom.services.camera_config import load_camera_config

        try:
            return load_camera_config(config_path)
        except Exception as exc:
            print(
                "component=savant_security_frame_annotation_camera_config_warning "
                f"config_path={config_path} "
                f"error={type(exc).__name__}:{str(exc).replace(chr(10), ' | ')}",
                flush=True,
            )
            return None

    def _resolve_camera_id(self, source_id: str) -> str:
        if self._camera_bundle is not None:
            entry = self._camera_bundle.get_by_source_id(source_id)
            if entry is not None:
                return entry.camera_id
        return source_id

    def process_frame(self, buffer: Any, frame_meta: Any) -> None:
        try:
            self._runtime.process_frame(frame_meta)
        except Exception as exc:
            print(
                "component=savant_security_frame_annotation_unexpected_warning "
                f"error={type(exc).__name__}:{str(exc).replace(chr(10), ' | ')}",
                flush=True,
            )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    import os

    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default
