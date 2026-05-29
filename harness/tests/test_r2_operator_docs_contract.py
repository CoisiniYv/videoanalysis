"""Static contract for R2 operator documentation and cleanup policy."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

DOCS = [
    ROOT / "docs" / "r2_file_inventory_and_cleanup_plan.md",
    ROOT / "docs" / "media_output_directory_policy.md",
    ROOT / "docs" / "current_mainline_status.md",
    ROOT / "docs" / "operator_add_camera.md",
    ROOT / "docs" / "operator_register_face.md",
    ROOT / "docs" / "operator_run_local_video_face_test.md",
    ROOT / "docs" / "operator_add_algorithm.md",
    ROOT / "docs" / "operator_configure_roi_and_rules.md",
    ROOT / "docs" / "performance_test_preflight.md",
    ROOT / "docs" / "legacy_and_prototype_inventory.md",
    ROOT / "docs" / "r2_pipeline_runtime_verification.md",
]


def _text(path: Path) -> str:
    assert path.exists(), f"missing R2 doc: {path}"
    return path.read_text(encoding="utf-8")


def _all_docs_text() -> str:
    return "\n".join(_text(path) for path in DOCS)


def test_required_r2_docs_exist() -> None:
    for path in DOCS:
        assert path.exists(), f"missing R2 doc: {path}"


def test_media_root_and_git_exclusions_are_documented() -> None:
    text = _all_docs_text()
    assert "/data/video-analytics/media" in text
    assert "face/" in text
    assert "testVideo/" in text
    assert "manual-inspection/" in text
    assert "do not commit" in text.lower() or "never commit" in text.lower()


def test_operator_commands_are_documented() -> None:
    text = _all_docs_text()
    assert "register_face_image.py" in text
    assert "camera_source_controller.py" in text
    assert "c1-official-adapter" in text


def test_main_pipeline_stack_is_documented() -> None:
    text = _all_docs_text()
    assert "YOLO26-pose" in text
    assert "YOLOv8-Face" in text
    assert "AdaFace" in text
    assert "nvtracker" in text
    assert "face_observation_exporter" in text


def test_f4_3b_is_not_documented_as_production_evidence() -> None:
    text = _all_docs_text()
    assert "F4.3 debug end-to-end recognition smoke: PASS" in text
    assert "F4.3 production evidence pipeline: NOT DONE" in text
    assert "debug evidence" in text
    assert "not production" in text.lower() or "not a production" in text.lower()
    assert "watchlist_hit" in text
    assert "live_search" in text


def test_performance_work_is_after_r2() -> None:
    text = _text(ROOT / "docs" / "performance_test_preflight.md")
    assert "after R2" in text
    assert "1 stream" in text
    assert "16 streams" in text
    assert "GPU utilization" in text
    assert "pgvector search latency" in text


def test_common_troubleshooting_is_present() -> None:
    text = _all_docs_text()
    assert "Common Troubleshooting" in text
    assert "No events" in text or "No face observations" in text
    assert "Redis" in text
    assert "PostgreSQL" in text


def test_pipeline_runtime_static_verification_doc_points_to_main_module() -> None:
    text = _text(ROOT / "docs" / "r2_pipeline_runtime_verification.md")
    assert "static verified" in text
    assert "modules/savant_security" in text
    assert "infra/docker-compose.c1-official-adapter.yml" in text
    assert "not configured as a face-only module" in text

