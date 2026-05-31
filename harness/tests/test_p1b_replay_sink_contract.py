"""P1b Replay manual job to Video File Sink contract checks."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "p1b_replay_manual_job_to_video_sink.md"
P1A_COMPOSE = ROOT / "infra" / "docker-compose.p1a-replay-inline-poc.yml"
P1B_COMPOSE = ROOT / "infra" / "docker-compose.p1b-replay-manual-sink-poc.yml"
SMOKE = ROOT / "scripts" / "smoke" / "check_p1b_replay_manual_job_to_video_sink.sh"
PRODUCTION_COMPOSE = ROOT / "infra" / "docker-compose.c1-official-adapter.yml"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compose(path: Path) -> dict:
    return yaml.safe_load(_text(path))


def test_p1b_files_exist() -> None:
    assert DOC.exists()
    assert P1A_COMPOSE.exists()
    assert P1B_COMPOSE.exists()
    assert SMOKE.exists()


def test_p1b_compose_only_adds_manual_video_file_sink() -> None:
    compose = _compose(P1B_COMPOSE)
    assert set(compose["services"]) == {"video-file-sink"}

    service = compose["services"]["video-file-sink"]
    env = service["environment"]
    assert service["container_name"] == "p1b-replay-manual-video-file-sink"
    assert env["ZMQ_ENDPOINT"] == "sub+bind:tcp://0.0.0.0:6666"
    assert "p1b-replay-manual-sink" in env["DIR_LOCATION"]
    assert "%source_id%" in env["DIR_LOCATION"]
    assert "%src_filename%" in env["DIR_LOCATION"]

    network = compose["networks"]["p1a-replay-inline"]
    assert network["external"] is True
    assert network["name"] == "p1a-replay-inline_default"


def test_p1b_compose_has_no_forbidden_runtime_services_or_second_rtsp() -> None:
    services = set(_compose(P1B_COMPOSE)["services"])
    forbidden_services = {
        "clip-worker",
        "media-worker",
        "event-worker",
        "postgres",
        "rtsp-server",
        "ffmpeg-source",
        "source-adapter",
        "replay-service",
        "savant-security",
    }
    assert forbidden_services.isdisjoint(services)

    content = _text(P1B_COMPOSE)
    forbidden = (
        "source-adapter:",
        "replay-service:",
        "savant-security:",
        "raw_clip",
        "annotated_clip",
    )
    for snippet in forbidden:
        assert snippet not in content
    assert "rtsp://" not in content
    assert "rtsps://" not in content


def test_smoke_uses_manual_replay_job_with_keyframe_anchor() -> None:
    smoke = _text(SMOKE)
    assert "019e7c8d-b9e8-7ed1-92bf-a09384777df8" in smoke
    assert "/api/v1/job" in smoke
    assert '"anchor_keyframe"' in smoke
    assert '"offset"' in smoke
    assert '"seconds"' in smoke
    assert '"stop_condition"' in smoke
    assert '"frame_count"' in smoke
    assert "pub+connect:tcp://video-file-sink:6666" in smoke
    assert "sub+bind:tcp://0.0.0.0:6666" in smoke


def test_smoke_preserves_p1a_inline_path_and_boundaries() -> None:
    smoke = _text(SMOKE)
    assert "docker compose -f \"$P1A_COMPOSE\" up -d redis replay-service savant-security source-adapter" in smoke
    assert "docker compose -f \"$P1B_COMPOSE\" up -d --force-recreate video-file-sink" in smoke
    assert "dealer+connect:tcp://replay-service:5555" in smoke
    assert "dealer+connect:tcp://savant-security:5557" in smoke
    assert "router+bind:tcp://0.0.0.0:5557" in smoke
    assert "source_extraction_fallback_used=no" in smoke
    assert "second_rtsp_pull_used=no" in smoke
    assert "clip_worker_started=no" in smoke
    assert "media_worker_started=no" in smoke
    assert "db_write_used=no" in smoke


def test_smoke_does_not_use_source_extraction_or_workers() -> None:
    smoke = _text(SMOKE)
    forbidden = (
        "ffmpeg ",
        "generate_visual_result.py",
        "export_local_video_debug_evidence.py",
        "process_evidence_task.py",
        "psql ",
        "security.record_requests",
        "clip_path",
    )
    for snippet in forbidden:
        assert snippet not in smoke
    assert "ffprobe" in smoke
    assert "source_extraction_fallback_used=no" in smoke
    assert "annotated_clip_generated=no" in smoke


def test_production_compose_not_used_for_p1b() -> None:
    smoke = _text(SMOKE)
    assert str(PRODUCTION_COMPOSE) not in smoke
    assert "docker-compose.c1-official-adapter.yml" not in smoke


def test_doc_records_p1b_required_report_fields_and_boundaries() -> None:
    doc = _text(DOC)
    for expected in (
        "Replay API URL",
        "anchor keyframe_uuid",
        "Replay job request",
        "sink output directory",
        "metadata.json",
        "video file",
        "video duration",
        "source-adapter -> replay-service -> savant-security",
        "manual Replay job -> video-file-sink",
        "No clip-worker",
        "No media-worker",
        "No production compose",
        "No second RTSP",
        "No source extraction fallback",
    ):
        assert expected.lower() in doc.lower()
