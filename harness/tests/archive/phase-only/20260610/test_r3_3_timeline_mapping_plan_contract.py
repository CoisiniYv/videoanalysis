"""R3.3 timeline mapping planning contract checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "r3_3_timeline_mapping_evidence_plan.md"
INVENTORY = ROOT / "docs" / "r3_3_code_inventory.md"
OPTIONS = ROOT / "docs" / "r3_3_options_replay_vs_segment_recording.md"
IMPLEMENTATION = ROOT / "docs" / "r3_3_implementation_plan.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_r3_3_docs_exist() -> None:
    assert PLAN.exists()
    assert INVENTORY.exists()
    assert OPTIONS.exists()
    assert IMPLEMENTATION.exists()


def test_plan_defines_mapping_fields_and_exactness() -> None:
    text = _text(PLAN)
    assert "event_ts_ms" in text
    assert "frame_uuid" in text
    assert "keyframe_uuid" in text
    assert "pipeline_ts_ns" in text
    assert "segment index" in text
    assert "exact_event_frame" in text
    assert "exact_event_clip" in text


def test_options_compare_required_strategies() -> None:
    text = _text(OPTIONS)
    assert "Replay / Keyframe" in text or "Replay/keyframe" in text
    assert "Controlled Segment Recording" in text
    assert "External NVR / MediaMTX" in text
    assert "Dual T4 / 60 streams" in text
    assert "Storage growth" in text


def test_implementation_plan_has_all_phases() -> None:
    text = _text(IMPLEMENTATION)
    for phase in ("R3.3A", "R3.3B", "R3.3C", "R3.3D", "R3.3E"):
        assert phase in text


def test_scope_guards() -> None:
    joined = "\n".join(_text(p) for p in (PLAN, OPTIONS, IMPLEMENTATION))
    assert "No production exact visual evidence before timeline mapping" in joined
    assert "No Savant pipeline change in planning" in joined
    assert "No performance test" in joined
