"""R3.2B raw clip evidence contract checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "r3_2b_raw_clip_evidence_mvp.md"
MEDIA_SERVICE = ROOT / "services" / "clip-worker" / "app" / "evidence_media_service.py"
RAW_CLIP_WRITER = ROOT / "services" / "clip-worker" / "app" / "evidence_raw_clip_writer.py"
METADATA_WRITER = ROOT / "services" / "clip-worker" / "app" / "evidence_metadata_writer.py"
API_SCHEMA = ROOT / "services" / "api" / "app" / "schemas" / "events.py"
PROCESS_CLI = ROOT / "services" / "clip-worker" / "process_evidence_task.py"
SMOKE = ROOT / "scripts" / "smoke" / "check_r3_2b_raw_clip_evidence.sh"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_r3_2b_files_exist() -> None:
    assert DOC.exists()
    assert MEDIA_SERVICE.exists()
    assert RAW_CLIP_WRITER.exists()
    assert METADATA_WRITER.exists()
    assert PROCESS_CLI.exists()
    assert SMOKE.exists()


def test_docs_define_raw_clip_policy() -> None:
    doc = _text(DOC)
    assert "raw_clip.mp4" in doc
    assert "canonical production evidence video" in doc
    assert "annotated_clip.mp4" in doc
    assert "optional/on-demand" in doc
    assert "must not create default dual video output" in doc


def test_docs_define_clip_source_strategy() -> None:
    doc = _text(DOC)
    assert "ReplayClient" in doc
    assert "replay source" in doc
    assert "raw_mp4_fallback" in doc
    assert "does not use live RTSP current clip recording as a production default" in doc
    assert "clip_status=not_implemented" in doc
    assert "media_status=partial" in doc
    assert "remux/copy" in doc
    assert "should not reencode by default" in doc


def test_api_schema_exposes_clip_fields() -> None:
    schema = _text(API_SCHEMA)
    assert "raw_clip_path" in schema
    assert "annotated_clip_path" in schema
    assert "clip_status" in schema
    assert "clip_error_message" in schema
    assert "raw_clip_url" in schema
    assert "annotated_clip_url" in schema


def test_code_supports_raw_clip_without_annotated_default() -> None:
    writer = _text(RAW_CLIP_WRITER)
    service = _text(MEDIA_SERVICE)
    metadata = _text(METADATA_WRITER)
    assert '"raw_clip.mp4"' in writer
    assert "raw_mp4_fallback" in writer
    assert "not_implemented" in writer
    assert "live RTSP current recording" in writer
    assert "reencoded" in writer
    assert "process_raw_clip" in service
    assert '"raw_clip_path": raw_clip_path' in metadata
    assert '"annotated_clip_path": None' in metadata
    assert "annotated_clip.mp4" not in writer


def test_process_cli_requires_explicit_raw_mp4_fallback() -> None:
    cli = _text(PROCESS_CLI)
    assert "--raw-mp4" in cli
    assert "R3_2B_RAW_MP4" in cli
    assert "not production default" in cli


def test_scope_guards() -> None:
    doc = _text(DOC)
    assert "does not change the Savant pipeline" in doc
    assert "does not run performance tests" in doc
    smoke = _text(SMOKE)
    assert "annotated_clip.mp4" in smoke
    assert "raw_clip.mp4" in smoke
