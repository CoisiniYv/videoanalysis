"""C2.8 API evidence-detail recovery contract tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "services" / "api"
BUILDER_PATH = ROOT / "scripts" / "tools" / "build_c2_8_api_evidence_detail.py"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from app.main import app  # noqa: E402
from app.routers.events import _repo as events_repo_dep  # noqa: E402
from app.schemas.events import EventResponse  # noqa: E402
from app.services.evidence_detail_resolver import resolve_event_evidence_detail  # noqa: E402


def test_evidence_detail_response_requires_source_observation_id() -> None:
    detail = resolve_event_evidence_detail(_event_response(source_observation_id=""))
    builder = _load_builder()

    try:
        builder.validate_summary(_summary(detail), detail, _unsafe_ok())
    except RuntimeError as exc:
        assert "detail_missing_source_observation_id" in str(exc)
    else:
        raise AssertionError("missing source_observation_id should fail C2.8")


def test_response_includes_watchlist_person_and_match_fields() -> None:
    detail = resolve_event_evidence_detail(_event_response())

    assert detail["event_type"] == "watchlist_hit"
    assert detail["person"]["person_id"] == 4
    assert detail["person"]["external_person_id"] == "test:c2_4:person"
    assert detail["watchlist"]["watchlist_rule_id"] == "c2_6r_test_watchlist_rule"
    assert detail["watchlist"]["similarity"] == 1.0
    assert detail["watchlist"]["threshold"] == 0.99
    assert detail["watchlist"]["match_result_id"] == 6
    assert detail["watchlist"]["gallery_embedding_id"] == 3


def test_response_includes_evidence_paths_video_integrity_and_workaround_flags() -> None:
    detail = resolve_event_evidence_detail(_event_response())
    evidence = detail["evidence"]

    assert evidence["bundle_path"] == str(_bundle_dir())
    assert evidence["c2_7_output_dir"] == "/data/video-analytics/media/evidence/c2_7_event_worker_persistence_20260607T223424"
    assert evidence["raw_clip_path"].endswith("raw_clip.mov")
    assert evidence["summary_path"].endswith("summary.json")
    assert evidence["sidecar_path"].endswith("annotations.frame_cache.identity.jsonl")
    assert evidence["watchlist_event_path"].endswith("redis_watchlist_event.json")
    assert evidence["video_integrity_status"] == "pass"
    assert evidence["production_ready"] is True
    assert evidence["known_face_count"] == 1
    assert evidence["watchlist_hit_count"] == 1
    assert evidence["capture_mode"] == "stable_post_savant_sink_time_crop"
    assert evidence["workaround_used"] is True
    assert evidence["event_style_replay_job_passed"] is False


def test_response_does_not_include_embedding_image_base64_or_crop_bytes() -> None:
    detail = resolve_event_evidence_detail(_event_response())
    scan = detail["unsafe_payload_scan"]

    assert scan["payload_has_embedding"] is False
    assert scan["payload_has_image_bytes"] is False
    assert scan["forbidden_key_paths"] == []


def test_missing_evidence_bundle_returns_safe_degraded_response_not_crash() -> None:
    detail = resolve_event_evidence_detail(
        _event_response(bundle_path="/tmp/c2_8_missing_bundle")
    )

    assert detail["evidence"]["bundle_exists"] is False
    assert detail["evidence"]["raw_clip_path"] == ""
    assert detail["unsafe_payload_scan"]["forbidden_key_paths"] == []


def test_testclient_evidence_endpoint_by_source_event_id_includes_detail() -> None:
    repo = _FakeEventRepository([_db_row()])
    app.dependency_overrides.clear()

    def fake_repo():
        yield repo

    app.dependency_overrides[events_repo_dep] = fake_repo
    try:
        with TestClient(app) as client:
            resp = client.get(f"/api/v1/events/{_source_event_id()}/evidence")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    detail = body["data"]["evidence_detail"]
    assert detail["source_event_id"] == _source_event_id()
    assert detail["event_type"] == "watchlist_hit"
    assert detail["source_observation_id"] == "face:c2_post_savant_fps_probe:4:17854:1"
    assert detail["evidence"]["capture_mode"] == "stable_post_savant_sink_time_crop"


def test_builder_validates_full_response_contract() -> None:
    builder = _load_builder()
    detail = resolve_event_evidence_detail(_event_response())

    builder.validate_summary(_summary(detail), detail, _unsafe_ok())


def test_builder_rejects_unsafe_response_payload() -> None:
    builder = _load_builder()
    detail = resolve_event_evidence_detail(_event_response())
    detail["evidence"]["image_bytes"] = "abc"
    scan = builder.scan_for_unsafe_payload(detail)

    try:
        builder.validate_summary(_summary(detail), detail, scan)
    except RuntimeError as exc:
        assert "unsafe_image_bytes_present" in str(exc) or "unsafe_forbidden_key_paths_present" in str(exc)
    else:
        raise AssertionError("unsafe image bytes should fail C2.8")


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location("build_c2_8_api_evidence_detail", BUILDER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _event_response(
    *,
    source_observation_id: str = "face:c2_post_savant_fps_probe:4:17854:1",
    bundle_path: str | None = None,
) -> EventResponse:
    return EventResponse.from_db_row(_db_row(source_observation_id=source_observation_id, bundle_path=bundle_path))


def _db_row(
    *,
    source_observation_id: str = "face:c2_post_savant_fps_probe:4:17854:1",
    bundle_path: str | None = None,
) -> dict[str, Any]:
    evidence = {
        "bundle_path": bundle_path or str(_bundle_dir()),
        "audit_path": "/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035",
        "capture_mode": "stable_post_savant_sink_time_crop",
        "workaround_used": True,
        "event_style_replay_job_passed": False,
    }
    payload = {
        "source_observation_id": source_observation_id,
        "person_id": 4,
        "external_person_id": "test:c2_4:person",
        "matched_person": {
            "person_id": 4,
            "external_person_id": "test:c2_4:person",
        },
        "watchlist_rule_id": "c2_6r_test_watchlist_rule",
        "similarity": 1.0,
        "threshold": 0.99,
        "match_result_id": 6,
        "gallery_embedding_id": 3,
        "evidence": evidence,
        "evidence_capture_mode": "stable_post_savant_sink_time_crop",
        "workaround_used": True,
        "event_style_replay_job_passed": False,
        "media": {
            "evidence_bundle_path": evidence["bundle_path"],
            "evidence_audit_path": evidence["audit_path"],
            "evidence_capture_mode": "stable_post_savant_sink_time_crop",
            "workaround_used": True,
            "event_style_replay_job_passed": False,
        },
    }
    return {
        "id": "b4cf4b6b-9d90-4282-9b7f-5e0c56e81a32",
        "source_event_id": _source_event_id(),
        "event_type": "watchlist_hit",
        "camera_id": "c2_post_savant_fps_probe",
        "source_id": "c2_post_savant_fps_probe",
        "track_id": "4",
        "person_id": 4,
        "algorithm_type": "face_intelligence",
        "algorithm_version": "r3.1b-mvp",
        "severity": "high",
        "confidence": 1.0,
        "start_ts_ms": 17854,
        "end_ts_ms": 17854,
        "start_ts": datetime(2026, 6, 7, tzinfo=timezone.utc),
        "end_ts": datetime(2026, 6, 7, tzinfo=timezone.utc),
        "event_ts_ms": 17854,
        "status": "new",
        "snapshot_path": None,
        "clip_path": None,
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": {},
        "media_status": "not_implemented",
        "payload": payload,
        "created_at": datetime(2026, 6, 7, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 7, tzinfo=timezone.utc),
    }


def _summary(detail: dict[str, Any]) -> dict[str, Any]:
    evidence = detail["evidence"]
    return {
        "api_route_verified": True,
        "repository_verified": True,
        "event_type": detail.get("event_type"),
        "source_observation_id": detail.get("source_observation_id"),
        "payload_has_embedding": False,
        "payload_has_image_bytes": False,
        "evidence_capture_mode": evidence.get("capture_mode"),
        "workaround_used": evidence.get("workaround_used"),
        "event_style_replay_job_passed": evidence.get("event_style_replay_job_passed"),
    }


def _unsafe_ok() -> dict[str, Any]:
    return {
        "payload_has_embedding": False,
        "payload_has_image_bytes": False,
        "forbidden_key_paths": [],
    }


def _bundle_dir() -> Path:
    return Path("/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035")


def _source_event_id() -> str:
    return (
        "c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:"
        "c2_7_event_worker_persistence_20260607T223424"
    )


class _FakeEventRepository:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def get_by_id(self, event_id: str) -> dict[str, Any] | None:
        for row in self._rows:
            if row["id"] == event_id:
                return row
        return None

    def get_by_source_event_id(self, source_event_id: str) -> dict[str, Any] | None:
        for row in self._rows:
            if row["source_event_id"] == source_event_id:
                return row
        return None

    def get_by_id_or_sid(self, event_id: str) -> dict[str, Any] | None:
        return self.get_by_id(event_id) or self.get_by_source_event_id(event_id)

    def list_evidence_tasks(self, event_id: str) -> list[dict[str, Any]]:
        return []
