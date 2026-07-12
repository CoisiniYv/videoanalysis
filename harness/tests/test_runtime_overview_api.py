from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

import app.routers.runtime as runtime_router
import app.services.runtime_overview as runtime_overview_module
from app.services.runtime_overview import (
    RuntimeOverviewConfig,
    _reset_restart_rate_cache_for_tests,
    build_runtime_overview,
    parse_forwarder_metrics,
    parse_savant_metrics,
    summarize_runtime_health,
)


METRICS_TEXT = """
# HELP va_savant_frames_seen_total Frames seen.
# TYPE va_savant_frames_seen_total counter
va_savant_sources_active 2
va_savant_frames_seen_total{source_id="primary_rtsp"} 120
va_savant_frame_annotations_exported_total{source_id="primary_rtsp"} 118
va_savant_effective_fps{source_id="primary_rtsp"} 7.8
va_savant_pose_stage_fps{source_id="primary_rtsp",window="10s"} 7.8
va_savant_face_stage_fps{source_id="primary_rtsp",window="10s"} 1.0
va_savant_adaface_embedding_fps{source_id="primary_rtsp",window="10s"} 0.5
va_savant_last_frame_age_seconds{source_id="primary_rtsp"} 0.4
va_savant_pose_objects_total{source_id="primary_rtsp"} 44
va_savant_face_objects_total{source_id="primary_rtsp"} 18
va_savant_adaface_embeddings_total{source_id="primary_rtsp"} 9
va_savant_frames_seen_total{source_id="secondary_rtsp"} 80
va_savant_effective_fps{source_id="secondary_rtsp"} 5
va_savant_last_frame_age_seconds{source_id="secondary_rtsp"} 35
"""

FORWARDER_METRICS_TEXT = """
va_forwarder_queue_depth 0
va_forwarder_running 1
va_forwarder_frames_seen_total{source_id="primary_rtsp"} 240
va_forwarder_frames_forwarded_total{source_id="primary_rtsp"} 80
va_forwarder_frames_dropped_total{source_id="primary_rtsp"} 160
va_forwarder_savant_send_failures_total{source_id="primary_rtsp"} 0
"""

EVIDENCE_SUMMARY = {
    "available": True,
    "state_counts": [
        {"state": "waiting_proof", "count": 2},
        {"state": "ready", "count": 3},
    ],
    "recent": [
        {
            "event_id": "event-1",
            "source_id": "lab",
            "event_type": "intrusion",
            "evidence_state": "waiting_proof",
            "evidence_reason": "missing_post_savant_frame_pts_window",
            "task_status": "waiting_proof",
            "age_seconds": 4,
        }
    ],
    "recent_failures": [],
}


class FakeDockerClient:
    def __init__(
        self,
        *,
        compose_source_restart_count: int = 3,
        dynamic_restart_count: int = 12,
    ) -> None:
        self.compose_source_restart_count = compose_source_restart_count
        self.dynamic_restart_count = dynamic_restart_count

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        ok_statuses: set[int] | None = None,
    ) -> tuple[int, bytes]:
        if path == "/containers/json?all=true":
            payload = [
                {
                    "Names": ["/video-analytics-source-secondary_rtsp"],
                    "State": "running",
                    "Status": "Up 2 minutes",
                }
            ]
            return 200, json.dumps(payload).encode("utf-8")
        name = path.split("/containers/", 1)[1].split("/json", 1)[0]
        docs = {
            "video-analytics-midterm-savant": {
                "Id": "abcdef1234567890",
                "RestartCount": 1,
                "State": {
                    "Status": "running",
                    "Running": True,
                    "StartedAt": "2026-06-14T01:02:03Z",
                    "FinishedAt": "0001-01-01T00:00:00Z",
                    "Health": {"Status": "healthy"},
                },
            },
            "video-analytics-midterm-source-adapter": {
                "Id": "source1234567890",
                "RestartCount": self.compose_source_restart_count,
                "State": {
                    "Status": "running",
                    "Running": True,
                    "StartedAt": "2026-06-14T01:03:03Z",
                    "FinishedAt": "0001-01-01T00:00:00Z",
                },
            },
            "video-analytics-midterm-analysis-forwarder": {
                "Id": "forwarder1234567890",
                "RestartCount": 0,
                "State": {
                    "Status": "running",
                    "Running": True,
                    "StartedAt": "2026-06-14T01:02:30Z",
                    "FinishedAt": "0001-01-01T00:00:00Z",
                },
            },
            "video-analytics-source-secondary_rtsp": {
                "Id": "dynamic1234567890",
                "RestartCount": self.dynamic_restart_count,
                "State": {
                    "Status": "running",
                    "Running": True,
                    "StartedAt": "2026-06-14T01:04:03Z",
                    "FinishedAt": "0001-01-01T00:00:00Z",
                },
            },
        }
        return 200, json.dumps(docs.get(name, {})).encode("utf-8")


def test_parse_savant_metrics_returns_per_source_summary() -> None:
    parsed = parse_savant_metrics(METRICS_TEXT)

    assert parsed["available"] is True
    assert parsed["sources_active"] == 2
    assert [row["source_id"] for row in parsed["sources"]] == [
        "primary_rtsp",
        "secondary_rtsp",
    ]
    primary = parsed["sources"][0]
    assert primary["effective_fps"] == 7.8
    assert primary["pose_stage_fps"] == 7.8
    assert primary["face_stage_fps"] == 1.0
    assert primary["adaface_embedding_fps"] == 0.5
    assert primary["last_frame_age_seconds"] == 0.4
    assert primary["frames_seen_total"] == 120
    assert primary["pose_objects_total"] == 44
    assert primary["face_objects_total"] == 18


def test_parse_forwarder_metrics_returns_queue_and_per_source_summary() -> None:
    parsed = parse_forwarder_metrics(FORWARDER_METRICS_TEXT)

    assert parsed["available"] is True
    assert parsed["global"]["queue_depth"] == 0
    assert parsed["global"]["running"] == 1
    assert parsed["sources"] == [
        {
            "source_id": "primary_rtsp",
            "frames_seen_total": 240,
            "frames_forwarded_total": 80,
            "frames_dropped_total": 160,
            "savant_send_failures_total": 0,
        }
    ]


def test_runtime_overview_aggregates_metrics_containers_and_supervisor(monkeypatch) -> None:
    _reset_restart_rate_cache_for_tests()
    monkeypatch.setattr(
        runtime_overview_module,
        "summarize_runtime_epoch_barrier",
        lambda **_kwargs: {
            "available": True,
            "current_runtime_epoch_id": "midterm-current",
            "blocking_count": 1,
            "active_count": 1,
            "foreign_epoch_count": 1,
            "missing_epoch_count": 0,
            "unknown_current_epoch_count": 0,
            "source_breakdown": [],
            "epoch_breakdown": [],
            "tasks": [],
            "orphans_present": True,
        },
    )

    overview = build_runtime_overview(
        config=RuntimeOverviewConfig(metrics_url="http://savant-security:8080/metrics"),
        docker_client=FakeDockerClient(),
        metrics_text=METRICS_TEXT,
        forwarder_metrics_text=FORWARDER_METRICS_TEXT,
        evidence_summary=EVIDENCE_SUMMARY,
        supervisor_snapshot={
            "enabled": True,
            "savant_container_running": True,
            "annotation_age_s": 2,
        },
    )

    assert overview["metrics"]["sources"][1]["source_id"] == "secondary_rtsp"
    assert overview["evidence"]["state_counts"][0]["state"] == "waiting_proof"
    assert overview["evidence"]["recent"][0]["evidence_reason"] == (
        "missing_post_savant_frame_pts_window"
    )
    assert overview["forwarder"]["sources"][0]["frames_dropped_total"] == 160
    assert overview["containers"]["fixed"]["savant"]["restart_count"] == 1
    assert overview["containers"]["fixed"]["analysis_forwarder"]["restart_count"] == 0
    assert overview["containers"]["fixed"]["compose_source"]["restart_count"] == 3
    dynamic_source = overview["containers"]["dynamic_sources"][0]
    assert dynamic_source["name"] == "video-analytics-source-secondary_rtsp"
    assert dynamic_source["source_id"] == "secondary_rtsp"
    assert dynamic_source["state"] == "running"
    assert dynamic_source["status"] == "Up 2 minutes"
    assert dynamic_source["restart_count"] == 12
    assert dynamic_source["restart_count_warning"] is True
    assert dynamic_source["restart_rate_per_min"] is None
    assert overview["epoch_barrier"]["current_runtime_epoch_id"] == "midterm-current"
    assert overview["epoch_barrier"]["orphans_present"] is True
    assert overview["health"]["ok"] is False
    assert overview["health"]["source_count"] == 2
    assert "source_frame_age_high" in overview["health"]["issues"]
    assert "container_restart_count_high" in overview["health"]["issues"]


def test_runtime_health_source_count_prefers_active_source_gauge() -> None:
    metrics_text = """
va_savant_sources_active 1
va_savant_frames_seen_total{source_id="old_pressure_01"} 100
va_savant_last_frame_age_seconds{source_id="old_pressure_01"} 9
va_savant_frames_seen_total{source_id="old_pressure_02"} 120
va_savant_last_frame_age_seconds{source_id="old_pressure_02"} 9
va_savant_frames_seen_total{source_id="lab"} 20
va_savant_last_frame_age_seconds{source_id="lab"} 0
"""

    overview = build_runtime_overview(
        config=RuntimeOverviewConfig(metrics_url="http://savant-security:8080/metrics"),
        docker_client=FakeDockerClient(compose_source_restart_count=0, dynamic_restart_count=0),
        metrics_text=metrics_text,
        forwarder_metrics_text=FORWARDER_METRICS_TEXT,
        evidence_summary=EVIDENCE_SUMMARY,
        supervisor_snapshot={
            "enabled": True,
            "savant_container_running": True,
            "annotation_age_s": 2,
        },
    )

    assert len(overview["metrics"]["sources"]) == 3
    assert overview["metrics"]["sources_active"] == 1
    assert overview["health"]["source_count"] == 1
    assert "source_frame_age_high" not in overview["health"]["issues"]


def test_runtime_health_uses_dynamic_source_convergence_as_the_primary_source_state() -> None:
    health = summarize_runtime_health(
        metrics={"available": False, "sources": [], "sources_active": None},
        containers={
            "fixed": {
                "compose_source": {"present": True, "state": "exited"},
                "savant": {"present": True, "state": "running"},
            },
            "dynamic_sources": [],
        },
        supervisor={
            "enabled": True,
            "savant_container_running": True,
            "source_convergence": {
                "healthy": False,
                "source_states": [
                    {
                        "camera_id": "camera-lab",
                        "source_id": "source_lab",
                        "enabled": True,
                        "compose_source": False,
                        "dynamic_source": True,
                        "actual_state": "exited",
                        "running": False,
                    }
                ],
            },
        },
    )

    assert "compose_source_not_running" not in health["issues"]
    assert "source_convergence_unhealthy" in health["issues"]
    assert "no_active_sources" in health["issues"]
    assert health["source_convergence"] == {
        "known": True,
        "healthy": False,
        "enabled_count": 1,
        "running_count": 0,
        "stopped_source_ids": ["source_lab"],
    }


def test_runtime_overview_computes_short_window_restart_rate() -> None:
    _reset_restart_rate_cache_for_tests()

    config = RuntimeOverviewConfig(
        metrics_url="http://savant-security:8080/metrics",
        restart_count_warn_threshold=0,
        restart_rate_warn_per_min=1.0,
    )
    first = build_runtime_overview(
        config=config,
        docker_client=FakeDockerClient(dynamic_restart_count=12),
        metrics_text=METRICS_TEXT.replace("35", "0.5"),
        forwarder_metrics_text=FORWARDER_METRICS_TEXT,
        evidence_summary=EVIDENCE_SUMMARY,
        supervisor_snapshot={"enabled": True, "savant_container_running": True},
        now_epoch_s=1000.0,
    )
    second = build_runtime_overview(
        config=config,
        docker_client=FakeDockerClient(dynamic_restart_count=14),
        metrics_text=METRICS_TEXT.replace("35", "0.5"),
        forwarder_metrics_text=FORWARDER_METRICS_TEXT,
        evidence_summary=EVIDENCE_SUMMARY,
        supervisor_snapshot={"enabled": True, "savant_container_running": True},
        now_epoch_s=1060.0,
    )

    assert first["containers"]["dynamic_sources"][0]["restart_rate_per_min"] is None
    dynamic_source = second["containers"]["dynamic_sources"][0]
    assert dynamic_source["restart_count_delta"] == 2
    assert dynamic_source["restart_rate_window_seconds"] == 60
    assert dynamic_source["restart_rate_per_min"] == 2
    assert dynamic_source["restart_rate_warning"] is True
    assert "container_restart_rate_high" in second["health"]["issues"]
    assert second["health"]["restart_rate_high_containers"] == [
        "video-analytics-source-secondary_rtsp"
    ]


def test_runtime_overview_route_uses_api_envelope(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_router,
        "build_runtime_overview",
        lambda: {
            "metrics": {"available": True},
            "evidence": {"available": True, "state_counts": []},
            "health": {"ok": True},
        },
    )

    payload = runtime_router.runtime_overview(request_id="req-test")

    assert payload["request_id"] == "req-test"
    assert payload["error"] is None
    assert payload["data"]["metrics"]["available"] is True
    assert payload["data"]["evidence"]["available"] is True
    assert payload["data"]["health"]["ok"] is True
