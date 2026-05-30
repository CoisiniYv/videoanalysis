"""R3.1C media retention and debug sink policy contract checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

POLICY = ROOT / "docs" / "r3_1c_media_retention_and_debug_sink_policy.md"
MEDIA_POLICY = ROOT / "docs" / "media_output_directory_policy.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_r3_1c_policy_doc_exists() -> None:
    assert POLICY.exists()


def test_policy_names_debug_sinks_and_boundary() -> None:
    text = _text(POLICY)
    assert "video-file-sink" in text
    assert "metadata-sink" in text
    assert "debug/development sink" in text
    assert "not be treated as production evidence" in text


def test_policy_defines_retention_limits() -> None:
    text = _text(POLICY)
    assert "retention_days" in text
    assert "max_total_gb" in text
    assert "max_camera_gb" in text
    assert "min_free_disk_percent" in text
    assert "delete_oldest_first" in text


def test_policy_preserves_business_rows_and_marks_media_state() -> None:
    text = _text(POLICY)
    assert "never delete DB rows" in text
    assert "media_expired" in text or "media_deleted" in text
    assert "face_observations" in text
    assert "match_results" in text


def test_policy_restates_production_media_shape() -> None:
    text = _text(POLICY)
    assert "raw_clip.mp4" in text
    assert "canonical production evidence video" in text
    assert "annotated_clip.mp4" in text
    assert "optional/on-demand" in text
    assert "no default dual video output" in text
    assert "dynamic overlays" in text


def test_policy_defines_degradation_and_capacity_terms() -> None:
    text = _text(POLICY)
    assert "snapshot-first / clip-later" in text
    assert "storage_limited" in text
    assert "backpressure" in text.lower()
    assert "dual T4 / 60 streams" in text


def test_policy_states_scope_guards() -> None:
    text = _text(POLICY)
    assert "does not implement cleanup worker" in text
    assert "does not implement snapshot generation" in text
    assert "does not implement clip generation" in text
    assert "Savant pipeline change" in text
    assert "performance test" in text


def test_media_output_policy_contains_retention_summary() -> None:
    text = _text(MEDIA_POLICY)
    assert "retention_days" in text
    assert "max_total_gb" in text
    assert "max_camera_gb" in text
    assert "min_free_disk_percent" in text
    assert "delete_oldest_first" in text
    assert "video-file-sink" in text
    assert "metadata-sink" in text
    assert "media_expired" in text or "media_deleted" in text
