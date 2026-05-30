"""R3.2A metadata + snapshot evidence contract checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "r3_2a_metadata_snapshot_evidence_mvp.md"
MEDIA_SERVICE = ROOT / "services" / "clip-worker" / "app" / "evidence_media_service.py"
METADATA_WRITER = ROOT / "services" / "clip-worker" / "app" / "evidence_metadata_writer.py"
SNAPSHOT_WRITER = ROOT / "services" / "clip-worker" / "app" / "evidence_snapshot_writer.py"
PROCESS_CLI = ROOT / "services" / "clip-worker" / "process_evidence_task.py"
SMOKE = ROOT / "scripts" / "smoke" / "check_r3_2a_metadata_snapshot_evidence.sh"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_r3_2a_files_exist() -> None:
    assert MEDIA_SERVICE.exists()
    assert METADATA_WRITER.exists()
    assert SNAPSHOT_WRITER.exists()
    assert PROCESS_CLI.exists()
    assert DOC.exists()
    assert SMOKE.exists()


def test_docs_define_metadata_and_snapshot_only_scope() -> None:
    doc = _text(DOC)
    assert "metadata.json" in doc
    assert "snapshot.jpg" in doc
    assert "raw_clip.mp4" in doc
    assert "deferred" in doc
    assert "annotated_clip.mp4" in doc
    assert "optional/on-demand" in doc
    assert "must not create default dual video output" in doc


def test_docs_define_correct_snapshot_backend_policy() -> None:
    doc = _text(DOC)
    assert "rtsp_current" in doc
    assert "best-effort current frame" in doc
    assert "OpenCV or GStreamer-compatible" in doc
    assert "ffprobe is smoke preflight only" in doc
    assert "ffmpeg" in doc
    assert "fallback/debug only" in doc


def test_docs_define_overlay_and_failure_behavior() -> None:
    doc = _text(DOC)
    assert "plain current frame" in doc
    assert "overlay_missing_reason" in doc
    assert "metadata is still generated" in doc
    assert "does not change the Savant pipeline" in doc
    assert "does not run performance tests" in doc


def test_api_evidence_schema_exposes_status_fields() -> None:
    schema = _text(ROOT / "services" / "api" / "app" / "schemas" / "events.py")
    assert "metadata_path" in schema
    assert "metadata_url" in schema
    assert "snapshot_status" in schema
    assert "metadata_status" in schema
    assert "clip_status" in schema


def test_worker_service_supports_partial_media_status() -> None:
    service = _text(MEDIA_SERVICE)
    repo = _text(ROOT / "services" / "event-worker" / "app" / "repository.py")
    assert '"partial"' in repo
    assert 'media_status = "ready" if snapshot_status == "ready" else "partial"' in service
    assert "metadata_status = \"ready\"" in service


def test_code_does_not_generate_annotated_clip_by_default() -> None:
    joined = "\n".join(
        _text(path) for path in (MEDIA_SERVICE, METADATA_WRITER, SNAPSHOT_WRITER, PROCESS_CLI)
    )
    assert "annotated_clip.mp4" not in joined
    assert "\"annotated_clip_path\": None" in _text(METADATA_WRITER)
    assert "raw_clip.mp4" not in joined


def test_output_path_uses_data_media_events() -> None:
    service = _text(MEDIA_SERVICE)
    assert 'DEFAULT_MEDIA_ROOT = "/data/video-analytics/media"' in service
    assert '"events"' in service
    assert '"metadata.json"' in service
    assert '"snapshot.jpg"' in service


def test_snapshot_writer_uses_opencv_default_not_ffmpeg_default() -> None:
    writer = _text(SNAPSHOT_WRITER)
    assert "capture_backend: str = \"opencv\"" in writer
    assert "cv2.VideoCapture" in writer
    assert "ffmpeg command-line capture is" in writer
    assert "intentionally not used here" in writer


def test_smoke_uses_ffprobe_preflight_only_and_checks_no_video() -> None:
    smoke = _text(SMOKE)
    assert "ffprobe" in smoke
    assert "preflight only" in smoke
    assert "raw_clip.mp4" in smoke
    assert "annotated_clip.mp4" in smoke
    assert "process_evidence_task.py" in smoke
