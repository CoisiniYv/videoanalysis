"""Apply the configured FFmpeg-input initialization timeout.

Savant adapters-gstreamer 0.6.0 exposes ``FFMPEG_TIMEOUT_MS`` as the
``ffmpeg_src.timeout-ms`` property, but that property only controls waiting for
frames after ``FFMpegSource`` has initialized.  The bundled ffmpeg_input 0.2.0
constructor has a separate 10 second ``init_timeout_ms`` default which the
plugin does not expose.  Sixty simultaneous RTSP pulls can therefore enter a
restart storm before the configured frame timeout is ever used.

Pressure source containers bind-mount this file as ``/opt/savant/sitecustomize.py``.
The image already has ``/opt/savant`` on ``PYTHONPATH``, so Python imports this
module before the GStreamer Python plugin imports ``FFMpegSource``.  Normal
camera source containers do not mount it and retain upstream behaviour.
"""

from __future__ import annotations

import functools
import os

import ffmpeg_input


_original_ffmpeg_source = ffmpeg_input.FFMpegSource


if not getattr(_original_ffmpeg_source, "_va_init_timeout_wrapper", False):

    @functools.wraps(_original_ffmpeg_source)
    def _ffmpeg_source_with_configured_init_timeout(*args, **kwargs):
        if "init_timeout_ms" not in kwargs:
            raw_timeout = os.environ.get("FFMPEG_INIT_TIMEOUT_MS", "").strip()
            try:
                configured_timeout = int(raw_timeout)
            except ValueError:
                configured_timeout = 0
            if configured_timeout > 0:
                kwargs["init_timeout_ms"] = configured_timeout
        return _original_ffmpeg_source(*args, **kwargs)

    _ffmpeg_source_with_configured_init_timeout._va_init_timeout_wrapper = True
    ffmpeg_input.FFMpegSource = _ffmpeg_source_with_configured_init_timeout
