"""R3.2E local-video debug evidence contract checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "r3_2e_local_video_debug_evidence.md"
SERVICE = ROOT / "services" / "clip-worker" / "app" / "local_video_debug_evidence.py"
CLI = ROOT / "services" / "clip-worker" / "export_local_video_debug_evidence.py"
SMOKE = ROOT / "scripts" / "smoke" / "check_r3_2e_local_video_debug_evidence.sh"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_r3_2e_files_exist() -> None:
    assert SERVICE.exists()
    assert CLI.exists()
    assert DOC.exists()
    assert SMOKE.exists()


def test_doc_defines_local_video_debug_scope() -> None:
    doc = _text(DOC)
    assert "local_video_debug" in doc
    assert "not production RTSP evidence" in doc
    assert "source_mp4_path" in doc
    assert "timestamp_ms is local video offset" in doc
    assert "CAP_PROP_POS_MSEC" in doc
    assert "[xc, yc, w, h]" in doc


def test_doc_defines_outputs_and_boundaries() -> None:
    doc = _text(DOC)
    assert "debug_overlay_snapshot.jpg" in doc
    assert "debug_annotated_clip.mp4" in doc
    assert "debug_visual_metadata.json" in doc
    assert "no Savant pipeline change" in doc
    assert "no performance test" in doc
    assert "R3.3 timeline mapping still required for production RTSP" in doc


def test_service_uses_local_mp4_timeline_and_bbox_format() -> None:
    service = _text(SERVICE)
    assert "cv2.CAP_PROP_POS_MSEC" in service
    assert "timestamp_ms is local video offset in milliseconds" in service
    assert "evidence_mode" in service
    assert "local_video_debug" in service
    assert "not_production_rtsp_evidence" in service
    assert "_bbox_to_rect" in service
    assert "debug_overlay_snapshot.jpg" in service
    assert "debug_annotated_clip.mp4" in service


def test_cli_requires_source_mp4_and_does_not_touch_production_evidence() -> None:
    cli = _text(CLI)
    service = _text(SERVICE)
    assert "--source-mp4" in cli
    assert "--source-id" in cli
    assert "export_local_video_debug_evidence" in cli
    assert "update_evidence_media_result" not in service
    assert "events.clip_path" not in service
    assert "raw_clip_path" not in service


def test_smoke_uses_local_video_adapter_not_rtsp() -> None:
    smoke = _text(SMOKE)
    assert "file://" in smoke
    assert "R3_2E_SOURCE_MP4" in smoke
    assert "export_local_video_debug_evidence.py" in smoke
    assert "not_production_rtsp_evidence" in smoke
    assert "RTSP" in smoke
