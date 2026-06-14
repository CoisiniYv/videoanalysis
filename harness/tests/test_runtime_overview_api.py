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
from app.services.runtime_overview import (
    RuntimeOverviewConfig,
    build_runtime_overview,
    parse_savant_metrics,
)


METRICS_TEXT = """
# HELP va_savant_frames_seen_total Frames seen.
# TYPE va_savant_frames_seen_total counter
va_savant_sources_active 2
va_savant_frames_seen_total{source_id="primary_rtsp"} 120
va_savant_frame_annotations_exported_total{source_id="primary_rtsp"} 118
va_savant_effective_fps{source_id="primary_rtsp"} 7.8
va_savant_last_frame_age_seconds{source_id="primary_rtsp"} 0.4
va_savant_pose_objects_total{source_id="primary_rtsp"} 44
va_savant_face_objects_total{source_id="primary_rtsp"} 18
va_savant_adaface_embeddings_total{source_id="primary_rtsp"} 9
va_savant_frames_seen_total{source_id="secondary_rtsp"} 80
va_savant_effective_fps{source_id="secondary_rtsp"} 5
va_savant_last_frame_age_seconds{source_id="secondary_rtsp"} 35
"""


class FakeDockerClient:
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
                "RestartCount": 3,
                "State": {
                    "Status": "running",
                    "Running": True,
                    "StartedAt": "2026-06-14T01:03:03Z",
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
    assert primary["last_frame_age_seconds"] == 0.4
    assert primary["frames_seen_total"] == 120
    assert primary["pose_objects_total"] == 44
    assert primary["face_objects_total"] == 18


def test_runtime_overview_aggregates_metrics_containers_and_supervisor() -> None:
    overview = build_runtime_overview(
        config=RuntimeOverviewConfig(metrics_url="http://savant-security:8080/metrics"),
        docker_client=FakeDockerClient(),
        metrics_text=METRICS_TEXT,
        supervisor_snapshot={
            "enabled": True,
            "savant_container_running": True,
            "annotation_age_s": 2,
        },
    )

    assert overview["metrics"]["sources"][1]["source_id"] == "secondary_rtsp"
    assert overview["containers"]["fixed"]["savant"]["restart_count"] == 1
    assert overview["containers"]["fixed"]["compose_source"]["restart_count"] == 3
    assert overview["containers"]["dynamic_sources"] == [
        {
            "name": "video-analytics-source-secondary_rtsp",
            "source_id": "secondary_rtsp",
            "state": "running",
            "status": "Up 2 minutes",
        }
    ]
    assert overview["health"]["ok"] is False
    assert "source_frame_age_high" in overview["health"]["issues"]


def test_runtime_overview_route_uses_api_envelope(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_router,
        "build_runtime_overview",
        lambda: {"metrics": {"available": True}, "health": {"ok": True}},
    )

    payload = runtime_router.runtime_overview(request_id="req-test")

    assert payload["request_id"] == "req-test"
    assert payload["error"] is None
    assert payload["data"]["metrics"]["available"] is True
    assert payload["data"]["health"]["ok"] is True
