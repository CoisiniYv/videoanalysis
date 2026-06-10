from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "r3_3a2c_replay_uuid_domain_report.md"
SMOKE = ROOT / "scripts" / "smoke" / "check_r3_3a2c_replay_uuid_domain.sh"
MAINLINE_COMPOSE = ROOT / "infra" / "docker-compose.c1-official-adapter.yml"


def _report_text() -> str:
    return REPORT.read_text(encoding="utf-8")


def test_report_exists_and_has_allowed_conclusion():
    text = _report_text()
    assert "R3.3A2c Replay/Cache UUID Domain Report" in text
    match = re.search(r"Conclusion:\s*`([^`]+)`", text)
    assert match, "report must include a machine-readable Conclusion field"
    assert match.group(1) in {"same_domain", "different_domain", "blocked"}


def test_report_records_topology_uuid_generation_and_correlation_method():
    text = _report_text()
    assert "source adapter -> Replay/cache -> Savant module" in text
    assert "UUID Generation Point" in text
    assert "VideoFrame.uuid" in text
    assert "Correlation Key Method" in text
    assert "UUID-independent" in text
    assert "PTS/timestamp" in text


def test_report_does_not_mark_blocked_as_pass():
    text = _report_text()
    lowered = text.lower()
    assert "conclusion = blocked" in lowered
    assert "pass was not claimed" in lowered
    assert "same_domain = unknown" in lowered
    assert "replay_cache_metadata_compared = false" in lowered


def test_report_records_no_clip_no_visual_alignment_no_production_boundary():
    text = _report_text()
    assert "clip_generated = false" in text
    assert "visual_alignment_performed = false" in text
    assert "No Replay clip" in text
    assert "No visual alignment" in text
    assert "No production clip-worker" in text
    assert "No production media-worker" in text
    assert "No production Video File Sink deployment" in text
    assert "No DB migration" in text
    assert "No performance test" in text
    assert "No mainline compose change" in text


def test_report_lists_gap_candidates_without_claiming_replay_unusable():
    text = _report_text()
    assert "GAP Candidates" in text
    assert "out_stream: null" in text
    assert "Legacy phase3a/phase3b POCs are sidecar" in text
    assert "same-stream bridge or Replay pass-through" in text
    assert "Replay scheme is not viable" not in text
    assert "Replay alignment failed" not in text


def test_smoke_is_report_only_and_does_not_call_replay_or_generate_clip():
    text = SMOKE.read_text(encoding="utf-8")
    assert "r3_3a2c_replay_uuid_domain_report.md" in text
    assert "allowed = {'same_domain', 'different_domain', 'blocked'}" in text
    assert "clip_generated" in text
    assert "visual_alignment_performed" in text
    assert "urllib.request" not in text
    assert "/api/v1/job" not in text
    assert "/api/v1/keyframes/find" not in text


def test_mainline_compose_does_not_add_replay_service():
    text = MAINLINE_COMPOSE.read_text(encoding="utf-8")
    assert "replay-service:" not in text
    assert "savant-replay" not in text
