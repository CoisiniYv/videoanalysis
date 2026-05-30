"""R3.2A snapshot capture backend policy contract checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "r3_2a_snapshot_capture_backend_policy.md"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_r3_2a_snapshot_backend_policy_doc_exists() -> None:
    assert DOC.exists()


def test_ffprobe_is_smoke_preflight_only() -> None:
    text = _text()
    assert "ffprobe" in text
    assert "smoke preflight" in text
    assert "not a production snapshot capture backend" in text


def test_default_backend_is_opencv_or_gstreamer_compatible() -> None:
    text = _text()
    assert "opencv" in text
    assert "gstreamer-compatible capture" in text
    assert "production default snapshot capture backend" in text
    assert "SNAPSHOT_CAPTURE_BACKEND=opencv" in text


def test_ffmpeg_is_fallback_debug_only() -> None:
    text = _text()
    assert "ffmpeg" in text
    assert "explicit fallback/debug only" in text
    assert "capture_backend\": \"ffmpeg_fallback" in text


def test_metadata_records_capture_backend_and_best_effort_mode() -> None:
    text = _text()
    assert "capture_backend" in text
    assert "snapshot_capture_mode" in text
    assert "rtsp_current" in text
    assert "exact_event_frame" in text
    assert "exact_event_frame=false" in text


def test_metadata_records_fallback_fields_when_used() -> None:
    text = _text()
    assert "fallback_used" in text
    assert "fallback_reason" in text
    assert "fallback_used=true" in text


def test_scope_guards_for_media_policy() -> None:
    text = _text()
    assert "must not modify the Savant pipeline" in text
    assert "must not generate `raw_clip.mp4`" in text
    assert "must not generate `annotated_clip.mp4`" in text
    assert "must not create default dual video output" in text
    assert "must not run performance tests" in text
