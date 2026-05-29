"""R3.1 evidence output planning contract checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

ARCH = ROOT / "docs" / "r3_1_evidence_output_architecture_plan.md"
INVENTORY = ROOT / "docs" / "r3_1_code_inventory.md"
PERF = ROOT / "docs" / "r3_1_performance_design.md"
PLAN = ROOT / "docs" / "r3_1_implementation_plan.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_r3_1_documents_exist() -> None:
    assert ARCH.exists()
    assert INVENTORY.exists()
    assert PERF.exists()
    assert PLAN.exists()


def test_architecture_doc_covers_output_classes_and_flow() -> None:
    text = _text(ARCH)
    assert "behavior event evidence" in text
    assert "face match evidence" in text
    assert "trajectory / appearances" in text
    assert "SecurityEvent -> EvidenceTask" in text
    assert "media_status State Machine" in text


def test_performance_doc_covers_60_stream_design() -> None:
    text = _text(PERF)
    assert "dual T4 / 60 streams" in text
    assert "async evidence generation" in text
    assert "cooldown / rate limit" in text
    assert "backpressure" in text.lower()


def test_implementation_plan_is_split_into_subphases() -> None:
    text = _text(PLAN)
    assert "R3.1A - Behavior Event Evidence MVP" in text
    assert "R3.1B - Face Match Evidence MVP" in text
    assert "R3.1C - Trajectory / Appearance Output MVP" in text


def test_docs_state_scope_limits() -> None:
    joined = "\n".join(_text(path) for path in (ARCH, INVENTORY, PERF, PLAN))
    assert "does not implement concrete algorithm logic" in joined
    assert "does not run performance tests" in joined
    assert "does not change the Savant pipeline" in joined
