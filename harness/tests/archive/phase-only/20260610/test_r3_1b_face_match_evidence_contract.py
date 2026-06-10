"""R3.1B face match evidence MVP contract checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "services" / "face-worker" / "app" / "face_match_event_service.py"
CLI = ROOT / "services" / "face-worker" / "emit_face_match_events.py"
DOC = ROOT / "docs" / "r3_1b_face_match_evidence_mvp.md"
SMOKE = ROOT / "scripts" / "smoke" / "check_r3_1b_face_match_evidence.sh"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_r3_1b_files_exist() -> None:
    assert SERVICE.exists()
    assert CLI.exists()
    assert DOC.exists()
    assert SMOKE.exists()


def test_docs_define_watchlist_and_live_search_boundary() -> None:
    doc = _text(DOC)
    assert "watchlist_hit" in doc
    assert "live_search_hit" in doc
    assert "contract-only/deferred" in doc
    assert "face_intelligence" in doc


def test_docs_define_evidence_media_policy() -> None:
    doc = _text(DOC)
    assert "raw_clip.mp4" in doc
    assert "canonical production evidence video" in doc
    assert "annotated_clip.mp4" in doc
    assert "optional/on-demand" in doc
    assert "must not generate two video files per event" in doc
    assert "render dynamic overlays from `metadata.json`" in doc
    assert "Annotated video generation is deferred" in doc


def test_service_builds_face_match_payload_contract() -> None:
    service = _text(SERVICE)
    assert "matched_person" in service
    assert "similarity" in service
    assert "source_observation_id" in service
    assert "overlay" in service
    assert "face_bbox" in service
    assert "raw_clip_path" in service
    assert "annotated_clip_path" in service
    assert "metadata_path" in service


def test_service_emits_unified_security_event_fields() -> None:
    service = _text(SERVICE)
    assert '"event_type": WATCHLIST_HIT_EVENT_TYPE' in service
    assert 'WATCHLIST_HIT_EVENT_TYPE = "watchlist_hit"' in service
    assert '"algorithm_type": FACE_INTELLIGENCE_ALGORITHM_TYPE' in service
    assert 'FACE_INTELLIGENCE_ALGORITHM_TYPE = "face_intelligence"' in service
    assert '"snapshot_required": True' in service
    assert '"clip_required": True' in service
    assert "DEFAULT_EVIDENCE_POLICY" in service
    assert "not_implemented" in service


def test_service_uses_idempotent_source_event_id() -> None:
    service = _text(SERVICE)
    assert "watchlist_hit:{source_observation_id}:{person_id}" in service
    assert "build_source_event_id" in service


def test_service_publishes_to_security_events_not_direct_events_table() -> None:
    service = _text(SERVICE)
    assert "security.events" in service
    assert "xadd" in service
    assert "INSERT INTO events" not in service
    assert "INSERT INTO evidence_tasks" not in service


def test_event_worker_has_face_match_not_implemented_reason() -> None:
    repo = _text(ROOT / "services" / "event-worker" / "app" / "repository.py")
    assert "_not_implemented_reason" in repo
    assert "R3.1B face match evidence MVP" in repo
    assert "face_intelligence" in repo
    assert "watchlist_hit" in repo
    assert "snapshot/raw_clip/metadata" in repo


def test_cli_supports_threshold_and_scope_options() -> None:
    cli = _text(CLI)
    assert "FACE_MATCH_THRESHOLD" in cli
    assert "--threshold" in cli
    assert "--external-person-id" in cli
    assert "--all-active-gallery" in cli
    assert "--dry-run" in cli
    assert "--output-json" in cli


def test_smoke_declares_no_fabricated_hits_and_no_media_generation() -> None:
    smoke = _text(SMOKE)
    assert "NO_HIT_ABOVE_THRESHOLD" in smoke
    assert "annotated_clip" in smoke
    assert "raw_clip" in smoke
    assert "not generate" in smoke
    assert "check_r2_5_single_rtsp_camera_inference.sh" in smoke


def test_docs_state_scope_limits() -> None:
    joined = "\n".join(_text(path) for path in (DOC, SERVICE, SMOKE))
    assert "does not change the Savant pipeline" in joined
    assert "does not run performance tests" in joined
    assert "does not generate" in joined
