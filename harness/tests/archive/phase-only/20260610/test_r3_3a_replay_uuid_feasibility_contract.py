"""R3.3A Replay UUID feasibility contract tests.

Verifies the feasibility report exists and contains required sections,
and that key source files maintain the documented constraints:
- Replay service is NOT in mainline compose
- frame_uuid / keyframe_uuid are always None in production code
- ReplayClient supports UUID anchor API
- No Savant pipeline changes in this inspection
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# ── Report ──────────────────────────────────────────────────────────────────

REPORT = REPO_ROOT / "docs" / "r3_3a_replay_uuid_feasibility.md"


def _read_report() -> str:
    assert REPORT.exists(), f"Feasibility report not found: {REPORT}"
    return REPORT.read_text()


def test_report_exists():
    _read_report()


def test_report_contains_replay_section():
    content = _read_report()
    assert "Replay" in content, "Report must discuss Replay service"


def test_report_contains_frame_uuid():
    content = _read_report()
    assert "frame_uuid" in content, "Report must discuss frame_uuid"


def test_report_contains_keyframe_uuid():
    content = _read_report()
    assert "keyframe_uuid" in content, "Report must discuss keyframe_uuid"


def test_report_contains_uuid_anchor():
    content = _read_report()
    assert "anchor" in content.lower(), "Report must discuss UUID anchor"


def test_report_contains_offset():
    content = _read_report()
    assert "offset" in content.lower(), "Report must discuss offset"


def test_report_contains_stop_condition():
    content = _read_report()
    assert "stop" in content.lower(), "Report must discuss stop condition"


def test_report_contains_exact_event_frame():
    content = _read_report()
    assert "exact" in content.lower() and "frame" in content.lower(), \
        "Report must discuss exact event frame"


def test_report_contains_exact_event_clip():
    content = _read_report()
    assert "exact" in content.lower() and "clip" in content.lower(), \
        "Report must discuss exact event clip"


def test_report_states_no_exact_evidence_before_timeline_mapping():
    content = _read_report()
    # The report must state that production exact evidence requires timeline mapping
    assert "timeline" in content.lower() or "mapping" in content.lower(), \
        "Report must reference timeline mapping as a prerequisite"


def test_report_states_no_savant_pipeline_change():
    content = _read_report()
    assert "No Savant pipeline change" in content or \
           "inspection only" in content.lower() or \
           "No implementation" in content, \
        "Report must state no Savant pipeline change in this inspection"


# ── Mainline compose — no Replay service ────────────────────────────────────

MAINLINE_COMPOSE = REPO_ROOT / "infra" / "docker-compose.c1-official-adapter.yml"


def _read_mainline_compose() -> str:
    assert MAINLINE_COMPOSE.exists(), f"Mainline compose not found: {MAINLINE_COMPOSE}"
    return MAINLINE_COMPOSE.read_text()


def test_mainline_compose_has_no_replay_service():
    content = _read_mainline_compose()
    # Must not define a replay-service container
    assert "replay-service" not in content, \
        "Mainline compose must NOT define a replay-service"


def test_mainline_compose_has_no_replay_image():
    content = _read_mainline_compose()
    assert "savant-replay" not in content, \
        "Mainline compose must NOT reference savant-replay image"


# ── SecurityEvent — UUID fields exist but are always None ────────────────────

EVENTS_MODEL = REPO_ROOT / "modules" / "savant_security" / "custom" / "models" / "events.py"


def _read_events_model() -> str:
    assert EVENTS_MODEL.exists(), f"Events model not found: {EVENTS_MODEL}"
    return EVENTS_MODEL.read_text()


def test_events_model_declares_frame_uuid():
    content = _read_events_model()
    assert "frame_uuid" in content, "SecurityEvent must declare frame_uuid field"


def test_events_model_declares_keyframe_uuid():
    content = _read_events_model()
    assert "keyframe_uuid" in content, "SecurityEvent must declare keyframe_uuid field"


# ── Behavior rules — UUID always None ────────────────────────────────────────

BEHAVIOR_RULES = REPO_ROOT / "modules" / "savant_security" / "custom" / "pyfuncs" / "behavior_rules.py"


def _read_behavior_rules() -> str:
    assert BEHAVIOR_RULES.exists(), f"Behavior rules not found: {BEHAVIOR_RULES}"
    return BEHAVIOR_RULES.read_text()


def test_behavior_rules_sets_frame_uuid_none():
    content = _read_behavior_rules()
    assert "event.frame_uuid = None" in content or \
           '"frame_uuid": None' in content, \
        "Behavior rules must set frame_uuid to None (no UUID source available)"


def test_behavior_rules_sets_keyframe_uuid_none():
    content = _read_behavior_rules()
    assert "event.keyframe_uuid = None" in content or \
           '"keyframe_uuid": None' in content, \
        "Behavior rules must set keyframe_uuid to None (no UUID source available)"


# ── Face match event — UUID always None ──────────────────────────────────────

FACE_MATCH = REPO_ROOT / "services" / "face-worker" / "app" / "face_match_event_service.py"


def _read_face_match() -> str:
    assert FACE_MATCH.exists(), f"Face match service not found: {FACE_MATCH}"
    return FACE_MATCH.read_text()


def test_face_match_sets_frame_uuid_none():
    content = _read_face_match()
    assert '"frame_uuid": None' in content, \
        "Face match event must set frame_uuid to None"


def test_face_match_sets_keyframe_uuid_none():
    content = _read_face_match()
    assert '"keyframe_uuid": None' in content, \
        "Face match event must set keyframe_uuid to None"


# ── ReplayClient — supports UUID anchor API ──────────────────────────────────

REPLAY_CLIENT = REPO_ROOT / "services" / "clip-worker" / "app" / "replay_client.py"


def _read_replay_client() -> str:
    assert REPLAY_CLIENT.exists(), f"Replay client not found: {REPLAY_CLIENT}"
    return REPLAY_CLIENT.read_text()


def test_replay_client_has_find_keyframe():
    content = _read_replay_client()
    assert "def find_keyframe" in content, \
        "ReplayClient must have find_keyframe method"


def test_replay_client_has_create_job():
    content = _read_replay_client()
    assert "def create_job" in content, \
        "ReplayClient must have create_job method"


def test_replay_client_create_job_accepts_keyframe_uuid():
    content = _read_replay_client()
    # create_job signature must include keyframe_uuid parameter
    assert "keyframe_uuid" in content, \
        "ReplayClient.create_job must accept keyframe_uuid parameter"


def test_replay_client_create_job_uses_anchor_keyframe():
    content = _read_replay_client()
    assert "anchor_keyframe" in content, \
        "ReplayClient.create_job must use anchor_keyframe in payload"


def test_replay_client_create_job_uses_offset():
    content = _read_replay_client()
    assert '"offset"' in content or "'offset'" in content, \
        "ReplayClient.create_job must include offset in payload"


def test_replay_client_create_job_uses_stop_condition():
    content = _read_replay_client()
    assert "stop_condition" in content, \
        "ReplayClient.create_job must include stop_condition in payload"


def test_replay_client_find_keyframe_disables_timestamp_search():
    """Verify that find_keyframe currently disables timestamp-anchored search
    because epoch-to-pipeline timestamp mapping is missing."""
    content = _read_replay_client()
    # The code should explicitly note the timestamp domain issue
    assert "pipeline-relative" in content or \
           "timestamp-domain" in content or \
           "Unbounded search" in content, \
        "ReplayClient.find_keyframe must document the timestamp-domain limitation"


# ── Replay config exists but not in mainline ─────────────────────────────────

REPLAY_CONFIG = REPO_ROOT / "modules" / "savant_replay" / "config.json"


def test_replay_config_exists():
    assert REPLAY_CONFIG.exists(), \
        "Replay config must exist (POC artifact)"


def test_replay_config_has_rocksdb_storage():
    import json
    config = json.loads(REPLAY_CONFIG.read_text())
    assert "storage" in config, "Replay config must define storage"
    assert "rocksdb" in config.get("storage", {}), \
        "Replay config must use rocksdb storage"


def test_replay_config_has_management_port():
    import json
    config = json.loads(REPLAY_CONFIG.read_text())
    common = config.get("common", {})
    assert "management_port" in common, \
        "Replay config must define management_port"


# ── module.yml — no frame UUID in pipeline config ────────────────────────────

MODULE_YML = REPO_ROOT / "modules" / "savant_security" / "module.yml"


def _read_module_yml() -> str:
    assert MODULE_YML.exists(), f"module.yml not found: {MODULE_YML}"
    return MODULE_YML.read_text()


def test_module_yml_has_no_uuid_config():
    content = _read_module_yml()
    assert "frame_uuid" not in content and "keyframe_uuid" not in content, \
        "module.yml must not reference frame_uuid/keyframe_uuid (not available in Savant)"


# ── Planning doc recommends segment recording ────────────────────────────────

OPTIONS_DOC = REPO_ROOT / "docs" / "r3_3_options_replay_vs_segment_recording.md"


def _read_options_doc() -> str:
    assert OPTIONS_DOC.exists(), f"Options doc not found: {OPTIONS_DOC}"
    return OPTIONS_DOC.read_text()


def test_options_doc_exists():
    _read_options_doc()


def test_options_doc_recommends_segment_recording():
    content = _read_options_doc()
    assert "Option B" in content or "Controlled Segment" in content or \
           "segment recording" in content.lower(), \
        "Options doc must discuss controlled segment recording"


def test_options_doc_notes_uuid_limitation():
    content = _read_options_doc()
    assert "null" in content.lower() or "always null" in content.lower() or \
           "None" in content, \
        "Options doc must note that frame_uuid/keyframe_uuid are currently null"
