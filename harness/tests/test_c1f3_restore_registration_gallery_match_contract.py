"""C1F.3 restore registration + gallery match smoke contract tests."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = (
    REPO_ROOT
    / "scripts"
    / "smoke"
    / "check_c1f3_restore_registration_and_gallery_match.sh"
)
DOC = REPO_ROOT / "docs" / "c1f3_restore_registration_and_gallery_match.md"

FINCH_PATH = (
    "/data/video-analytics/archive/r2_3_20260529_052937/"
    "repo_uncommitted/face/finch.jpg"
)
REESE_PATH = (
    "/data/video-analytics/archive/r2_3_20260529_052937/"
    "repo_uncommitted/face/reese.jpg"
)


def _script() -> str:
    return SMOKE_SCRIPT.read_text(encoding="utf-8")


def _script_lower() -> str:
    return _script().lower()


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def test_smoke_script_exists() -> None:
    assert SMOKE_SCRIPT.exists()


def test_smoke_script_references_archived_images() -> None:
    content = _script()
    assert FINCH_PATH in content
    assert REESE_PATH in content
    assert "finch.jpg" in content
    assert "reese.jpg" in content


def test_smoke_uses_deterministic_external_person_ids() -> None:
    content = _script()
    assert 'FINCH_EXTERNAL_ID="test:archive:finch"' in content
    assert 'REESE_EXTERNAL_ID="test:archive:reese"' in content


def test_smoke_checks_persons_table() -> None:
    content = _script()
    assert "persons" in content
    assert "persons_total" in content


def test_smoke_checks_gallery_embeddings_table() -> None:
    content = _script()
    assert "person_gallery_embeddings" in content
    assert "active_gallery_embeddings_total" in content


def test_smoke_checks_embedding_dim_512() -> None:
    content = _script()
    assert "embedding_dim" in content
    assert "512" in content


def test_smoke_checks_embedding_norm() -> None:
    content = _script()
    assert "embedding_norm" in content
    assert "0.90" in content
    assert "1.10" in content


def test_smoke_checks_face_observations() -> None:
    content = _script()
    assert "face_observations" in content
    assert "source_observation_id_unique" in content


def test_smoke_executes_gallery_match() -> None:
    content = _script()
    assert "FaceVectorStore" in content
    assert "search_gallery" in content
    assert "MatchResultRepository" in content


def test_smoke_distinguishes_match_and_no_match_pass() -> None:
    content = _script()
    assert "PASS_MATCH" in content
    assert "PASS_NO_MATCH_PIPELINE_OK" in content


def test_smoke_does_not_implement_watchlist_or_live_search() -> None:
    content = _script()
    assert "watchlist_hit implemented: NO" in content
    assert "live_search_hit implemented: NO" in content
    assert "watchlist_rules" not in content
    assert "emit_face_match_events" not in content
    assert "search_mode\": \"live_search" not in content


def test_smoke_does_not_call_api_or_frontend() -> None:
    content = _script_lower()
    forbidden = [
        "curl ",
        "services/api",
        "npm ",
        "yarn ",
        "pnpm ",
        "services/frontend",
        "http://localhost",
    ]
    for token in forbidden:
        assert token not in content
    assert "api/frontend implemented: no" in content


def test_smoke_does_not_use_second_rtsp() -> None:
    content = _script_lower()
    assert "second rtsp: no" in content
    assert "second_rtsp=true" not in content
    assert "rtsp://10.37.57.112" not in content


def test_smoke_does_not_use_source_extraction_or_ffmpeg_clipping() -> None:
    content = _script_lower()
    assert "source extraction: no" in content
    assert "ffmpeg" not in content
    assert "crop clip" not in content


def test_smoke_does_not_delete_archive_images() -> None:
    content = _script()
    assert "rm -f \"$FINCH_IMAGE\"" not in content
    assert "rm -f \"$REESE_IMAGE\"" not in content
    assert "DELETE FROM face_observations" not in content


def test_smoke_does_not_use_docker_compose_down_v() -> None:
    assert "down -v" not in _script_lower()


def test_smoke_does_not_write_image_bytes_or_base64() -> None:
    content = _script_lower()
    assert "xadd" not in content
    assert "base64.b64encode" not in content
    assert "copyfile" not in content


def test_doc_clearly_excludes_watchlist_and_live_search() -> None:
    content = _doc()
    assert "watchlist_hit" in content
    assert "live_search_hit" in content
    assert "API/frontend" in content
    assert "production evidence" in content
    assert "not" in content.lower()


def test_doc_exists_and_mentions_c1f3() -> None:
    assert DOC.exists()
    assert "C1F.3" in _doc()
