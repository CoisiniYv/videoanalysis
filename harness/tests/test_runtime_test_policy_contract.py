"""Runtime test policy contract tests.

Verifies that P1 compose files, smoke scripts, and policy docs enforce:
- Fixed RTSP URI
- No local file / test video fallback
- Replay TTL configuration
- Replay RocksDB mount
- No source extraction fallback
- No second RTSP pull
- Restart-not-rebuild rule documented
- Bind mount verification rule documented
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

P1_RTSP_URI = "rtsp://10.37.57.112:8554/live/1080movie"

# ── Policy docs ─────────────────────────────────────────────────────────────

POLICY_DOC = REPO_ROOT / "docs" / "runtime_test_policy.md"


def _read_policy() -> str:
    assert POLICY_DOC.exists(), f"Policy doc not found: {POLICY_DOC}"
    return POLICY_DOC.read_text()


def test_policy_doc_exists():
    _read_policy()


def test_policy_doc_contains_restart_not_rebuild():
    content = _read_policy()
    assert "restart" in content.lower() and "rebuild" in content.lower(), \
        "Policy must document restart-not-rebuild rule"


def test_policy_doc_contains_bind_mount_verification():
    content = _read_policy()
    assert "bind mount" in content.lower() or "MOUNT_OK" in content, \
        "Policy must document bind mount verification"


def test_policy_doc_contains_fixed_rtsp_uri():
    content = _read_policy()
    assert P1_RTSP_URI in content, \
        f"Policy must contain fixed RTSP URI: {P1_RTSP_URI}"


def test_policy_doc_contains_replay_ttl():
    content = _read_policy()
    assert "TTL" in content or "ttl" in content or "expiration" in content.lower(), \
        "Policy must document Replay TTL"
    assert "storage.rocksdb.data_expiration_ttl" in content
    assert "replay_ttl_too_short" in content


def test_policy_doc_contains_poc_isolation():
    content = _read_policy()
    assert "isolation" in content.lower() or "compose project" in content.lower(), \
        "Policy must document POC container isolation"


def test_policy_doc_contains_smoke_output_requirements():
    content = _read_policy()
    assert "smoke" in content.lower() and "output" in content.lower(), \
        "Policy must document smoke output requirements"


# ── P1 RTSP compose files ───────────────────────────────────────────────────

P1_RTSP_COMPOSES = [
    REPO_ROOT / "infra" / "docker-compose.p1b-rtsp-replay-manual-sink.yml",
    REPO_ROOT / "infra" / "docker-compose.p1c-rtsp-replay-event-evidence.yml",
]


def _read_compose(path: Path) -> str:
    assert path.exists(), f"Compose not found: {path}"
    return path.read_text()


def test_p1b_rtsp_compose_has_correct_rtsp_uri():
    content = _read_compose(P1_RTSP_COMPOSES[0])
    assert P1_RTSP_URI in content, \
        f"P1b-RTSP compose must use {P1_RTSP_URI}"


def test_p1c_rtsp_compose_has_correct_rtsp_uri():
    content = _read_compose(P1_RTSP_COMPOSES[1])
    assert P1_RTSP_URI in content, \
        f"P1c-RTSP compose must use {P1_RTSP_URI}"


def test_p1b_rtsp_compose_has_no_file_source():
    content = _read_compose(P1_RTSP_COMPOSES[0]).lower()
    assert "file://" not in content, \
        "P1b-RTSP compose must not use file:// source"
    assert "testvideo" not in content and "test.mp4" not in content, \
        "P1b-RTSP compose must not use test video"


def test_p1c_rtsp_compose_has_no_file_source():
    content = _read_compose(P1_RTSP_COMPOSES[1]).lower()
    assert "file://" not in content, \
        "P1c-RTSP compose must not use file:// source"
    assert "testvideo" not in content and "test.mp4" not in content, \
        "P1c-RTSP compose must not use test video"


def test_p1b_rtsp_compose_has_replay_service():
    content = _read_compose(P1_RTSP_COMPOSES[0])
    assert "replay" in content.lower(), \
        "P1b-RTSP compose must define replay service"


def test_p1c_rtsp_compose_has_replay_service():
    content = _read_compose(P1_RTSP_COMPOSES[1])
    assert "replay" in content.lower(), \
        "P1c-RTSP compose must define replay service"


# ── Replay config ───────────────────────────────────────────────────────────

REPLAY_CONFIG = REPO_ROOT / "modules" / "savant_replay" / "config.json"


def test_replay_config_exists():
    assert REPLAY_CONFIG.exists(), "Replay config must exist"


def test_replay_config_has_ttl():
    config = json.loads(REPLAY_CONFIG.read_text())
    storage = config.get("storage", {})
    rocksdb = storage.get("rocksdb", {})
    ttl = rocksdb.get("data_expiration_ttl", {})
    assert ttl.get("secs", 0) > 0, \
        "Replay config must have data_expiration_ttl > 0"


def test_replay_config_has_rocksdb_path():
    config = json.loads(REPLAY_CONFIG.read_text())
    storage = config.get("storage", {})
    rocksdb = storage.get("rocksdb", {})
    path = rocksdb.get("path", "")
    assert path, "Replay config must have rocksdb path"


# ── P1 smoke scripts ────────────────────────────────────────────────────────

P1_SMOKE_SCRIPTS = [
    REPO_ROOT / "scripts" / "smoke" / "check_p1a_replay_inline_pass_through.sh",
    REPO_ROOT / "scripts" / "smoke" / "check_p1b_replay_manual_job_to_video_sink.sh",
    REPO_ROOT / "scripts" / "smoke" / "check_p1b_rtsp_replay_manual_job_to_video_sink.sh",
    REPO_ROOT / "scripts" / "smoke" / "check_p1c_rtsp_replay_event_evidence_bundle.sh",
]


def _read_smoke(path: Path) -> str:
    assert path.exists(), f"Smoke script not found: {path}"
    return path.read_text()


def test_p1b_rtsp_smoke_asserts_rtsp_input():
    content = _read_smoke(P1_SMOKE_SCRIPTS[2])
    assert "rtsp" in content.lower(), \
        "P1b-RTSP smoke must check for RTSP input"


def test_p1b_rtsp_smoke_no_source_extraction():
    content = _read_smoke(P1_SMOKE_SCRIPTS[2]).lower()
    assert "source_extraction" in content or "no source extraction" in content or \
           "source extraction" in content, \
        "P1b-RTSP smoke must check source extraction fallback is not used"


def test_p1c_smoke_no_source_extraction():
    content = _read_smoke(P1_SMOKE_SCRIPTS[3]).lower()
    assert "source_extraction" in content or "no source extraction" in content or \
           "source extraction" in content, \
        "P1c smoke must check source extraction fallback is not used"


def test_p1c_smoke_no_second_rtsp():
    content = _read_smoke(P1_SMOKE_SCRIPTS[3]).lower()
    assert "second_rtsp" in content or "second rtsp" in content or \
           "no second" in content, \
        "P1c smoke must check no second RTSP pull"


# ── Docker access detection in P1 smokes ────────────────────────────────────

def test_p1b_rtsp_smoke_has_docker_access_detection():
    content = _read_smoke(P1_SMOKE_SCRIPTS[2])
    assert "detect_docker" in content or "DOCKER_ACCESS" in content or \
           "SUDO_DOCKER" in content, \
        "P1b-RTSP smoke must detect Docker access"


def test_p1c_smoke_has_docker_access_detection():
    content = _read_smoke(P1_SMOKE_SCRIPTS[3])
    assert "detect_docker" in content or "DOCKER_ACCESS" in content or \
           "SUDO_DOCKER" in content, \
        "P1c smoke must detect Docker access"


def test_p1b_rtsp_smoke_uses_docker_variable():
    content = _read_smoke(P1_SMOKE_SCRIPTS[2])
    assert 'DOCKER=' in content or 'DOCKER="' in content or \
           'COMPOSE=' in content or 'COMPOSE="' in content, \
        "P1b-RTSP smoke must use DOCKER/COMPOSE variables"


def test_p1c_smoke_uses_docker_variable():
    content = _read_smoke(P1_SMOKE_SCRIPTS[3])
    assert 'DOCKER=' in content or 'DOCKER="' in content or \
           'COMPOSE=' in content or 'COMPOSE="' in content, \
        "P1c smoke must use DOCKER/COMPOSE variables"


def test_p1c_smoke_blocks_on_no_docker():
    content = _read_smoke(P1_SMOKE_SCRIPTS[3]).lower()
    assert "blocked" in content and "docker" in content, \
        "P1c smoke must BLOCKED when Docker unavailable"


# ── Policy doc Docker access section ────────────────────────────────────────

def test_policy_doc_contains_docker_access_section():
    content = _read_policy()
    assert "Docker Daemon Access" in content or "docker_daemon" in content or \
           "DOCKER_ACCESS" in content, \
        "Policy must contain Docker daemon access section"


def test_policy_doc_contains_sudo_policy():
    content = _read_policy()
    assert "sudo" in content.lower(), \
        "Policy must document sudo fallback"


def test_policy_doc_contains_detect_docker_pattern():
    content = _read_policy()
    assert "detect_docker" in content, \
        "Policy must include detect_docker() pattern"


def test_policy_doc_contains_no_build_gate_and_p1c_single_event_scope():
    content = _read_policy()
    assert "P1C_ALLOW_BUILD=1" in content
    assert "worker_image_missing_and_build_not_allowed" in content
    assert "single-event evidence POC" in content
    assert "uncontrolled_clip_generation" in content


def test_p1c_smoke_uses_docker_prefixes_after_detection():
    content = _read_smoke(P1_SMOKE_SCRIPTS[3])
    assert "DOCKER_ACCESS_OK" in content
    assert "SUDO_DOCKER_REQUIRED" in content
    assert "DOCKER_ACCESS_BLOCKED" in content
    after_detection = content.split("\ndetect_docker\n", 1)[1]
    for command in (
        "docker ps",
        "docker compose",
        "docker exec",
        "docker logs",
        "docker stop",
        "docker rm",
        "docker run",
    ):
        assert command not in after_detection
    assert "$DOCKER" in after_detection
    assert "$COMPOSE" in after_detection


def test_p1c_smoke_default_no_build_and_no_pull():
    content = _read_smoke(P1_SMOKE_SCRIPTS[3])
    assert "up -d --no-build --force-recreate" in content
    assert 'if [[ "$P1C_ALLOW_BUILD" == "1" ]]' in content
    assert "up -d --build --force-recreate" in content
    assert "docker pull" not in content


# ── P1 docs ─────────────────────────────────────────────────────────────────

P1_DOCS = [
    REPO_ROOT / "docs" / "p1_single_stream_replay_clip_output.md",
    REPO_ROOT / "docs" / "p1a_replay_inline_pass_through_topology.md",
    REPO_ROOT / "docs" / "p1b_replay_manual_job_to_video_sink.md",
    REPO_ROOT / "docs" / "p1b_rtsp_replay_manual_job_to_video_sink.md",
    REPO_ROOT / "docs" / "p1c_rtsp_replay_event_evidence_bundle.md",
]


def _read_doc(path: Path) -> str:
    assert path.exists(), f"Doc not found: {path}"
    return path.read_text()


def test_p1b_rtsp_doc_states_no_local_file():
    content = _read_doc(P1_DOCS[3]).lower()
    assert "no local file" in content or "local_file_used: no" in content, \
        "P1b-RTSP doc must state no local file source"


def test_p1b_rtsp_doc_states_rtsp_uri():
    content = _read_doc(P1_DOCS[3])
    assert P1_RTSP_URI in content, \
        f"P1b-RTSP doc must contain RTSP URI: {P1_RTSP_URI}"


def test_p1c_doc_states_no_source_extraction():
    content = _read_doc(P1_DOCS[4]).lower()
    assert "no source extraction" in content or "source_extraction_fallback: no" in content, \
        "P1c doc must state no source extraction fallback"


def test_p1c_doc_states_no_second_rtsp():
    content = _read_doc(P1_DOCS[4]).lower()
    assert "no second rtsp" in content or "second_rtsp_pull: no" in content, \
        "P1c doc must state no second RTSP pull"


def test_p1c_doc_states_rtsp_uri():
    content = _read_doc(P1_DOCS[4])
    assert P1_RTSP_URI in content, \
        f"P1c doc must contain RTSP URI: {P1_RTSP_URI}"


def test_p1_single_stream_doc_states_no_source_extraction():
    content = _read_doc(P1_DOCS[0]).lower()
    assert "no source extraction" in content, \
        "P1 single stream doc must state no source extraction fallback"


def test_p1_single_stream_doc_states_no_second_rtsp():
    content = _read_doc(P1_DOCS[0]).lower()
    assert "no second" in content and "rtsp" in content, \
        "P1 single stream doc must state no second RTSP pull"


# ── CLAUDE.md references policy ─────────────────────────────────────────────

CLAUDE_MD = REPO_ROOT / "CLAUDE.md"


def test_claude_md_references_runtime_policy():
    content = CLAUDE_MD.read_text()
    assert "runtime_test_policy" in content or "Runtime Test Policy" in content, \
        "CLAUDE.md must reference runtime_test_policy.md"
