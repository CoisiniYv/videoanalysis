"""Fall / crowd_gathering / chasing produce video evidence like intrusion.

Every gate between a Savant behavior event and an 8090 evidence card must use
the same event-type set, otherwise a rule can emit events whose evidence task
starts as not_implemented or whose overlay sidecar is skipped.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
VIDEO_BEHAVIOR_TYPES = {"intrusion", "fall", "crowd_gathering", "chasing"}
NEW_VIDEO_BEHAVIOR_TYPES = ("fall", "crowd_gathering", "chasing")
EVENT_ONLY_BEHAVIOR_TYPES = ("loitering", "running")


def _activate(service: str, module_name: str):
    service_root = str(REPO_ROOT / "services" / service)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if service_root in sys.path:
        sys.path.remove(service_root)
    sys.path.insert(0, service_root)
    if str(REPO_ROOT) not in sys.path:
        sys.path.append(str(REPO_ROOT))
    return importlib.import_module(module_name)


def _csv(value: str) -> set[str]:
    return {item.strip() for item in str(value).split(",") if item.strip()}


def _compose_default(value: str) -> str:
    match = re.fullmatch(r"\$\{[A-Z0-9_]+:-(.*)\}", str(value).strip())
    return match.group(1) if match else str(value)


def _env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (REPO_ROOT / "infra" / "env" / "midterm.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _compose_env(service: str) -> dict[str, str]:
    compose = yaml.safe_load(
        (REPO_ROOT / "infra" / "docker-compose.midterm.yml").read_text(encoding="utf-8")
    )
    return compose["services"][service]["environment"]


def test_shared_contract_names_video_behavior_types() -> None:
    from libs.evidence_lifecycle import BEHAVIOR_VIDEO_EVIDENCE_EVENT_TYPES

    assert set(BEHAVIOR_VIDEO_EVIDENCE_EVENT_TYPES) == VIDEO_BEHAVIOR_TYPES


@pytest.mark.parametrize("event_type", sorted(VIDEO_BEHAVIOR_TYPES))
def test_event_worker_starts_video_behavior_tasks_as_materializable(event_type, monkeypatch) -> None:
    monkeypatch.delenv("EVIDENCE_MATERIALIZATION_DEFER_LOW_PRIORITY", raising=False)
    repository = _activate("event-worker", "app.repository")

    status, error = repository._evidence_task_initial_status(
        {"event_type": event_type, "source_event_id": f"{event_type}:1"}
    )

    assert (status, error) == ("materialization_pending", "")


@pytest.mark.parametrize("event_type", EVENT_ONLY_BEHAVIOR_TYPES)
def test_event_worker_keeps_event_only_behaviors_not_implemented(event_type) -> None:
    repository = _activate("event-worker", "app.repository")

    status, _ = repository._evidence_task_initial_status({"event_type": event_type})

    assert status == "not_implemented"


@pytest.mark.parametrize("event_type", NEW_VIDEO_BEHAVIOR_TYPES)
def test_event_worker_applies_default_policy_to_video_behaviors(event_type) -> None:
    worker = _activate("event-worker", "app.worker")
    event = {"event_type": event_type, "payload": {}}

    worker._apply_default_evidence_policy(event)

    assert event["snapshot_required"] is True
    assert event["clip_required"] is True
    assert event["payload"]["media"]["clip_required"] is True


@pytest.mark.parametrize("event_type", NEW_VIDEO_BEHAVIOR_TYPES)
def test_event_worker_respects_explicit_evidence_opt_out(event_type) -> None:
    worker = _activate("event-worker", "app.worker")
    event = {
        "event_type": event_type,
        "snapshot_required": False,
        "clip_required": False,
        "payload": {},
    }

    worker._apply_default_evidence_policy(event)

    assert event["snapshot_required"] is False
    assert event["clip_required"] is False


@pytest.mark.parametrize("event_type", NEW_VIDEO_BEHAVIOR_TYPES)
def test_media_worker_extracts_anchor_for_video_behaviors(event_type) -> None:
    window = _activate("media-worker", "app.frame_annotation_event_window")
    event = {
        "id": "11111111-1111-4111-8111-111111111111",
        "event_type": event_type,
        "source_id": "primary_rtsp",
        "camera_id": "cam_001",
        "track_id": 0 if event_type == "crowd_gathering" else 7,
        "payload": {
            "media": {
                "frame_uuid": "018f0000-0000-7000-8000-000000000001",
                "frame_pts": 123456,
            },
        },
    }

    anchor, summary = window.extract_evidence_event_anchor(event)

    assert summary["status"] == "ok"
    assert summary["missing_fields"] == []
    assert anchor["event_type"] == event_type
    assert anchor["frame_uuid"] == "018f0000-0000-7000-8000-000000000001"


def test_media_worker_sidecar_writes_overlays_for_video_behaviors() -> None:
    worker = _activate("media-worker", "app.worker")

    assert set(worker.FRAME_CACHE_SIDECAR_EVENT_TYPES) == VIDEO_BEHAVIOR_TYPES | {"watchlist_hit"}


@pytest.mark.parametrize("event_type", NEW_VIDEO_BEHAVIOR_TYPES)
def test_media_worker_styles_video_behaviors_as_behavior_events(event_type) -> None:
    style = _activate("media-worker", "app.annotation_style")

    assert style.build_style(event_type=event_type)["reason"] == "behavior_event"


def test_midterm_env_and_compose_record_video_behaviors() -> None:
    env = _env_file()
    event_worker = _compose_env("event-worker")
    media_worker = _compose_env("media-worker")

    assert VIDEO_BEHAVIOR_TYPES <= _csv(env["RECORDING_EVENT_TYPES"])
    assert VIDEO_BEHAVIOR_TYPES <= _csv(_compose_default(event_worker["RECORDING_EVENT_TYPES"]))
    assert VIDEO_BEHAVIOR_TYPES <= _csv(env["EVIDENCE_EVENT_COVERAGE_EVENT_TYPES"])
    assert VIDEO_BEHAVIOR_TYPES <= _csv(
        _compose_default(event_worker["EVIDENCE_EVENT_COVERAGE_EVENT_TYPES"])
    )
    assert VIDEO_BEHAVIOR_TYPES <= _csv(media_worker["FRAME_CACHE_SIDECAR_EVENT_TYPES"])


def test_chasing_is_a_behavior_category_everywhere() -> None:
    api_events = _activate("api", "app.repositories.events")
    assert "chasing" in api_events.EVENT_CATEGORY_TYPES["behavior"]
    assert "chasing" in api_events.EVENT_CATEGORY_TYPES["evidence"]

    maintenance = _activate("api", "app.services.storage_maintenance")
    assert "chasing" in maintenance.EVENT_CATEGORY_TYPES["behavior"]

    viewer_root = str(REPO_ROOT / "services" / "evidence-viewer")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    sys.path.insert(0, viewer_root)
    try:
        evidence_index = importlib.import_module("app.evidence_index")
    finally:
        sys.path.remove(viewer_root)
    assert "chasing" in evidence_index.EVENT_CATEGORY_TYPES["behavior"]


def test_8090_labels_chasing_evidence() -> None:
    js = (
        REPO_ROOT / "services" / "evidence-viewer" / "app" / "static" / "evidence.js"
    ).read_text(encoding="utf-8")

    assert 'chasing: "追逐告警"' in js
    behavior_line = next(
        line for line in js.splitlines() if 'return "behavior";' in line
    )
    assert '"chasing"' in behavior_line


def test_support_matrix_marks_video_behaviors_evidence_ready() -> None:
    _activate("api", "app.algorithm_registry")
    from app.algorithm_registry import get_algorithm_support, runtime_apply_state_for_algorithm
    from app.routers.algorithms import algorithms_support_matrix

    for algorithm_id in ("behavior.fall", "behavior.crowd_gathering", "behavior.chasing"):
        support = get_algorithm_support(algorithm_id)
        assert support.status == "evidence_ready"
        assert support.event_enabled is True
        assert support.evidence_enabled is True
        assert support.production_ready is False
        assert runtime_apply_state_for_algorithm(algorithm_id)["runtime_apply_state"] == "applied"

    assert "evidence_ready" in algorithms_support_matrix(request_id="r")["data"]["statuses"]

    operator_js = (
        REPO_ROOT / "services" / "evidence-viewer" / "app" / "static" / "operator.js"
    ).read_text(encoding="utf-8")
    assert 'evidence_ready: "证据可用"' in operator_js
