"""R3.2D rejection report contract checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "r3_2d_debug_visual_rejection_report.md"


def test_rejection_report_exists() -> None:
    assert REPORT.exists()


def test_rejection_report_states_required_failure_semantics() -> None:
    text = REPORT.read_text(encoding="utf-8")
    assert "debug visual not accepted" in text
    assert "snapshot is not exact event frame" in text
    assert "raw clip has no verified timeline mapping" in text
    assert "bbox and frame are not same source" in text
    assert "R3.3 timeline mapping required" in text
    assert "do not commit R3.2D debug visual implementation" in text
