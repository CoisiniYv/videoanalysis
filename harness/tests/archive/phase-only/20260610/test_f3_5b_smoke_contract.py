"""Static contract checks for F3.5b E2E smoke script.

Verifies the smoke script exists and contains required safety guards,
deterministic IDs, equality assertions, similarity threshold, and cleanup.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke" / "check_f3_5b_one_face_recognition_e2e.sh"

FIXED_UUID = "f35b0000-0000-4000-8000-000000000001"


def _read() -> str:
    return SMOKE_SCRIPT.read_text()


def test_smoke_script_exists():
    assert SMOKE_SCRIPT.exists(), f"Smoke script not found: {SMOKE_SCRIPT}"


def test_uses_trap_cleanup():
    assert "trap cleanup EXIT" in _read(), "Smoke script must use trap cleanup EXIT"


def test_calls_enroll_gallery():
    assert "enroll_gallery.py" in _read(), "Smoke script must call enroll_gallery.py"


def test_calls_match_gallery():
    assert "match_gallery.py" in _read(), "Smoke script must call match_gallery.py"


def test_does_not_delete_face_observations():
    content = _read()
    assert "DELETE FROM face_observations" not in content, \
        "Smoke script must NOT delete from face_observations"


def test_deterministic_search_request_id():
    content = _read()
    assert FIXED_UUID in content, \
        f"Smoke script must use deterministic search_request_id {FIXED_UUID}"
    assert "F3_5B_SEARCH_REQUEST_ID" in content, \
        "Smoke script must define F3_5B_SEARCH_REQUEST_ID variable"


def test_no_random_uuid_generation():
    content = _read()
    assert "uuid.uuid4()" not in content, \
        "Smoke script must NOT generate random UUIDs for search_request_id"
    assert "import uuid" not in content, \
        "Smoke script must NOT import uuid (uses fixed UUID constant)"


def test_verifies_query_observation_id_equals_selected():
    content = _read()
    assert "query_observation_id mismatch" in content, \
        "Smoke must assert query_observation_id equals selected observation UUID"
    assert "SELECTED_OBS_UUID" in content, \
        "Smoke must save and compare selected observation UUID"


def test_verifies_query_source_observation_id_equals_selected():
    content = _read()
    assert "query_source_observation_id mismatch" in content, \
        "Smoke must assert query_source_observation_id equals selected source_observation_id"
    assert "SELECTED_SOURCE_OBS_ID" in content, \
        "Smoke must save and compare selected source_observation_id"


def test_verifies_matched_observation_id_is_null():
    content = _read()
    assert "matched_observation_id should be NULL" in content, \
        "Smoke must verify matched_observation_id IS NULL for gallery_match"


def test_enforces_similarity_threshold():
    content = _read()
    assert "0.99" in content, "Smoke must reference 0.99 similarity threshold"
    assert "similarity" in content.lower(), "Smoke must check similarity"
    assert "sim < 0.99" in content or "similarity.*<.*0.99" in content, \
        "Smoke must fail when similarity < 0.99"


def test_similarity_pass_message():
    content = _read()
    assert "similarity >= 0.99" in content, \
        "Smoke must print PASS message when similarity >= 0.99"


def test_cleanup_uses_deterministic_search_request_id():
    content = _read()
    # Cleanup must use the fixed UUID, not a variable read from temp file
    assert "F3_5B_SEARCH_REQUEST_ID" in content, \
        "Cleanup must use F3_5B_SEARCH_REQUEST_ID constant"
    assert "search_request_id = %s" in content or "search_request_id::text = %s" in content, \
        "Cleanup query must parameterize search_request_id"


def test_cleanup_verifies_zero_leftovers():
    assert "Cleanup verified" in _read() or "all test:f3_5b data removed" in _read(), \
        "Smoke script must verify cleanup"


def test_checks_external_person_id():
    assert "test:f3_5b:person" in _read(), "Smoke script must check external_person_id"


def test_prints_camera_context():
    content = _read()
    for field in ("camera_id", "source_id", "track_id", "timestamp_ms"):
        assert field in content, f"Smoke script must print camera context field: {field}"


def test_final_summary_includes_all_fields():
    content = _read()
    # The final recognition summary must include these fields
    required = [
        "search_request_id",
        "query_observation_id",
        "query_source_observation_id",
        "person_id",
        "gallery_embedding_id",
        "rank",
        "similarity",
        "camera_id",
        "source_id",
        "track_id",
        "timestamp_ms",
    ]
    for field in required:
        assert field in content, f"Final summary must include field: {field}"


def test_preserves_original_observation():
    content = _read()
    assert "Original observation preserved" in content or \
           "original face_observation" in content.lower(), \
        "Smoke must verify original face_observation is preserved"


def test_exports_database_url():
    content = _read()
    assert 'export DATABASE_URL="$DB_URL"' in content, \
        "Smoke must export DATABASE_URL so child CLIs inherit the connection string"
