"""D1 RTSP 15-minute detection report contract checks."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "d1_rtsp_15min_detection_report.md"
COMPOSE = ROOT / "infra" / "docker-compose.d1-rtsp-15min-detection.yml"
CAMERA_CONFIG = ROOT / "modules" / "savant_security" / "config" / "cameras.d1_rtsp_15min.yml"
SMOKE = ROOT / "scripts" / "smoke" / "check_d1_rtsp_15min_detection_report.sh"
EXPORTER = ROOT / "scripts" / "debug" / "export_d1_detection_report.py"
FIXED_RTSP = "rtsp://10.37.57.112:8554/live/1080movie"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(_text(COMPOSE))


def _load_exporter():
    spec = importlib.util.spec_from_file_location("export_d1_detection_report", EXPORTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _entry(payload: dict) -> tuple[str, dict[str, str]]:
    return ("1-0", {"data": json.dumps(payload)})


def test_d1_files_exist() -> None:
    assert DOC.exists()
    assert COMPOSE.exists()
    assert CAMERA_CONFIG.exists()
    assert SMOKE.exists()
    assert EXPORTER.exists()


def test_d1_compose_is_detection_only_rtsp_path() -> None:
    compose = _compose()
    services = set(compose["services"])
    assert {"redis", "replay-service", "savant-security", "source-adapter"}.issubset(services)
    forbidden = {
        "postgres",
        "event-worker",
        "face-worker",
        "clip-worker",
        "media-worker",
        "video-file-sink",
        "metadata-sink",
        "evidence-worker",
        "api",
        "rtsp-server",
        "ffmpeg-source",
    }
    assert forbidden.isdisjoint(services)

    source_env = compose["services"]["source-adapter"]["environment"]
    savant_env = compose["services"]["savant-security"]["environment"]
    assert source_env["SOURCE_ID"] == "d1_rtsp_15min"
    assert source_env["RTSP_URI"] == FIXED_RTSP
    assert source_env["LOCATION"] == FIXED_RTSP
    assert source_env["ZMQ_ENDPOINT"] == "dealer+connect:tcp://replay-service:5555"
    assert savant_env["SOURCE_ID"] == "d1_rtsp_15min"
    assert savant_env["ZMQ_SRC_ENDPOINT"] == "router+bind:tcp://0.0.0.0:5557"
    assert savant_env["FACE_OBSERVATION_EXPORT_ENABLED"] == "true"
    assert savant_env["EVENT_EXPORTER"] == "redis"
    assert savant_env["RECORDING_ENABLED"] == "false"
    assert savant_env["CLIP_WORKER_DISABLED"] == "true"
    assert savant_env["MEDIA_WORKER_DISABLED"] == "true"


def test_d1_compose_and_camera_have_no_file_fallback_or_recording_sink() -> None:
    content = (_text(COMPOSE) + "\n" + _text(CAMERA_CONFIG)).lower()
    assert FIXED_RTSP in _text(COMPOSE)
    assert FIXED_RTSP in _text(CAMERA_CONFIG)
    for forbidden in (
        ".mp4",
        "file://",
        "testvideo",
        "test source",
        "looping file source",
        "video_path",
        "video_loop.sh",
        "ffmpeg-source",
        "rtsp-server",
    ):
        assert forbidden not in content

    camera = yaml.safe_load(_text(CAMERA_CONFIG))["cameras"]["cam_d1_rtsp_15min"]
    assert camera["source_id"] == "d1_rtsp_15min"
    assert camera["rtsp_url"] == FIXED_RTSP
    assert camera["rules"]["intrusion"]["clip_required"] is False
    assert camera["rules"]["intrusion"]["snapshot_required"] is False


def test_d1_smoke_docker_detection_and_no_bare_runtime_after_detection() -> None:
    smoke = _text(SMOKE)
    assert "detect_docker()" in smoke
    assert "DOCKER_ACCESS_OK" in smoke
    assert "SUDO_DOCKER_REQUIRED" in smoke
    assert "DOCKER_ACCESS_BLOCKED" in smoke
    assert 'DOCKER="sudo docker"' in smoke
    assert 'COMPOSE="sudo docker compose"' in smoke

    after_detection = smoke.split("\ndetect_docker\n", 1)[1]
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
    assert "$DOCKER exec" in after_detection
    assert "$COMPOSE -f" in after_detection


def test_d1_smoke_no_build_no_pull_and_duration_override() -> None:
    smoke = _text(SMOKE)
    assert 'DURATION_SECONDS="${D1_DURATION_SECONDS:-900}"' in smoke
    assert "D1_DURATION_SECONDS=60" not in smoke
    assert "up -d --no-build --force-recreate --pull never" in smoke
    assert "up -d --build" not in smoke
    assert "docker pull" not in smoke
    assert "PULL_USED=\"no\"" in smoke
    assert "BUILD_USED=\"no\"" in smoke


def test_d1_smoke_validates_outputs_and_boundaries() -> None:
    smoke = _text(SMOKE)
    for expected in (
        "summary.json exists",
        "people_tracks.json exists",
        "face_observations.json exists",
        "report.md exists",
        "clip_generated=false",
        "recording_enabled=false",
        "source_extraction_fallback=no",
        "second_rtsp_pull=no",
        "clip_worker_started=no",
        "media_worker_started=no",
        "video_file_sink_started=no",
        "source_adapter_stopped_after_run=",
    ):
        assert expected in smoke


def test_exporter_converts_person_bbox_xywh_to_xyxy() -> None:
    exporter = _load_exporter()
    conv = exporter.bbox_to_xyxy({"x": 10, "y": 20, "width": 30, "height": 40})
    assert conv.xyxy == [10.0, 20.0, 40.0, 60.0]
    assert conv.source_format == "xywh"


def test_exporter_people_track_schema_from_behavior_event() -> None:
    exporter = _load_exporter()
    event = {
        "source_event_id": "evt-1",
        "event_type": "intrusion",
        "camera_id": "cam_d1_rtsp_15min",
        "source_id": "d1_rtsp_15min",
        "track_id": "42",
        "event_ts_ms": 1000,
        "frame_uuid": "frame-1",
        "keyframe_uuid": "kf-1",
        "confidence": 0.91,
        "payload": {
            "zone_id": "full",
            "rule": "intrusion",
            "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
            "bbox_source": "savant_detection",
        },
    }
    events = exporter.collect_behavior_events([_entry(event)], source_id="d1_rtsp_15min")
    tracks = exporter.aggregate_people_tracks(events)
    assert events[0]["bbox_xyxy"] == [10.0, 20.0, 40.0, 60.0]
    assert tracks[0]["track_id"] == "42"
    assert tracks[0]["event_count"] == 1
    assert tracks[0]["last_bbox"] == [10.0, 20.0, 40.0, 60.0]


def test_exporter_face_observation_schema_and_no_gallery_equivalence() -> None:
    exporter = _load_exporter()
    obs = {
        "source_observation_id": "face:d1:42:1000",
        "camera_id": "cam_d1_rtsp_15min",
        "source_id": "d1_rtsp_15min",
        "track_id": "42",
        "timestamp_ms": 1000,
        "face_bbox": [100.0, 120.0, 40.0, 50.0],
        "landmarks": [1, 2, 3, 4, 5],
        "face_confidence": 0.88,
        "quality": 0.77,
        "embedding": [0.01] * 512,
        "embedding_norm": 1.0,
        "payload": {"media": {"frame_uuid": "frame-face"}},
    }
    observations = exporter.collect_face_observations([_entry(obs)], source_id="d1_rtsp_15min")
    face_tracks = exporter.aggregate_face_tracks(observations)
    summary = exporter.build_summary(
        source_id="d1_rtsp_15min",
        camera_id="cam_d1_rtsp_15min",
        input_uri=FIXED_RTSP,
        duration_seconds=900,
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:15:00Z",
        behavior_events=[],
        people_tracks=[],
        face_observations=observations,
        face_tracks=face_tracks,
        gallery_hits=[],
        quality_fields=exporter.quality_field_status([], observations, []),
    )
    assert observations[0]["face_bbox_xyxy"] == [80.0, 95.0, 120.0, 145.0]
    assert observations[0]["embedding_status"] == "present"
    assert face_tracks[0]["face_observation_count"] == 1
    assert summary["gallery_recognition_available"] is False
    assert summary["gallery_recognition_reason"] == "no_watchlist_hit_or_live_search_hit_in_run"
    assert summary["face_observation_count"] == 1
    assert summary["gallery_hit_count"] == 0


def test_exporter_gallery_hit_schema_is_separate() -> None:
    exporter = _load_exporter()
    hit = {
        "source_event_id": "hit-1",
        "event_type": "watchlist_hit",
        "camera_id": "cam_d1_rtsp_15min",
        "source_id": "d1_rtsp_15min",
        "track_id": "42",
        "event_ts_ms": 2000,
        "person_id": "p1",
        "payload": {
            "source_observation_id": "face:d1:42:2000",
            "person": {"external_person_id": "ext-1", "name": "Alice"},
            "match": {"similarity": 0.92, "threshold": 0.8},
            "media": {"frame_uuid": "frame-hit"},
        },
    }
    hits = exporter.collect_gallery_hits([_entry(hit)], source_id="d1_rtsp_15min")
    assert len(hits) == 1
    assert hits[0]["event_type"] == "watchlist_hit"
    assert hits[0]["person_id"] == "p1"
    assert hits[0]["external_person_id"] == "ext-1"
    assert hits[0]["similarity"] == 0.92
    assert hits[0]["source_observation_id"] == "face:d1:42:2000"


def test_exporter_summary_schema_records_no_recording_or_artifacts() -> None:
    exporter = _load_exporter()
    summary = exporter.build_summary(
        source_id="d1_rtsp_15min",
        camera_id="cam_d1_rtsp_15min",
        input_uri=FIXED_RTSP,
        duration_seconds=900,
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:15:00Z",
        behavior_events=[],
        people_tracks=[],
        face_observations=[],
        face_tracks=[],
        gallery_hits=[],
        quality_fields=exporter.quality_field_status([], [], []),
    )
    assert summary["phase"] == "D1"
    assert summary["input_type"] == "rtsp"
    assert summary["input_uri"] == FIXED_RTSP
    assert summary["duration_seconds"] == 900
    assert summary["recording_enabled"] is False
    assert summary["clip_generated"] is False
    assert summary["annotated_clip_generated"] is False
    assert summary["evidence_bundle_generated"] is False
    assert summary["local_file_used"] is False
    assert summary["test_video_used"] is False
    assert summary["source_extraction_fallback"] is False
    assert summary["second_rtsp_pull"] is False


def test_doc_records_d1_scope_and_outputs() -> None:
    doc = _text(DOC)
    assert FIXED_RTSP in doc
    assert "detection-only" in doc
    assert "clip_generated = false" in doc
    assert "face_observation" in doc
    assert "not a gallery recognition result" in doc
    assert "manual-inspection/d1_15min_detection_latest/" in doc
