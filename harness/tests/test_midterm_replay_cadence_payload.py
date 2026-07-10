"""Midterm Replay cadence payload behavior tests."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
CLIP_WORKER_ROOT = str(REPO_ROOT / "services" / "clip-worker")


def _activate_replay_client():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if CLIP_WORKER_ROOT in sys.path:
        sys.path.remove(CLIP_WORKER_ROOT)
    sys.path.insert(0, CLIP_WORKER_ROOT)
    return importlib.import_module("app.replay_client")


def _payload(replay_client, **kwargs: Any) -> dict[str, Any]:
    values = {
        "source_id": "primary_rtsp",
        "keyframe_uuid": "keyframe-1",
        "pre_seconds": 5,
        "post_seconds": 5,
        "sink_endpoint": "dealer+connect:tcp://video-file-sink:6666",
        "stop_condition_mode": "ts_delta_sec",
        "fps": 24,
    }
    values.update(kwargs)
    return replay_client.build_job_payload(**values)


def test_non_cadence_payload_omits_forced_frame_duration(monkeypatch) -> None:
    replay_client = _activate_replay_client()
    monkeypatch.setenv("REPLAY_FORCE_CONSTANT_CADENCE", "false")

    payload = _payload(replay_client)
    config = payload["configuration"]

    assert "min_duration" not in config
    assert "max_duration" not in config
    assert "ts_discrepancy_fix_duration" not in config
    assert payload["stop_condition"] == {"ts_delta_sec": {"max_delta_sec": 10.0}}


def test_replay_evidence_payload_defaults_to_fast_export(monkeypatch) -> None:
    replay_client = _activate_replay_client()
    monkeypatch.delenv("REPLAY_TS_SYNC", raising=False)

    payload = _payload(replay_client)

    assert payload["configuration"]["ts_sync"] is False


def test_replay_evidence_payload_can_opt_into_realtime_ts_sync(monkeypatch) -> None:
    replay_client = _activate_replay_client()
    monkeypatch.setenv("REPLAY_TS_SYNC", "true")

    env_payload = _payload(replay_client)
    override_payload = _payload(replay_client, ts_sync=False)

    assert env_payload["configuration"]["ts_sync"] is True
    assert override_payload["configuration"]["ts_sync"] is False


def test_constant_cadence_payload_is_available_for_fallback(monkeypatch) -> None:
    replay_client = _activate_replay_client()
    monkeypatch.setenv("REPLAY_FORCE_CONSTANT_CADENCE", "false")

    payload = _payload(replay_client, force_constant_cadence=True)
    config = payload["configuration"]
    interval = {"secs": 0, "nanos": 41_666_666}

    assert config["min_duration"] == interval
    assert config["max_duration"] == interval
    assert config["ts_discrepancy_fix_duration"] == interval


def test_replay_retries_constant_cadence_before_frame_count(monkeypatch) -> None:
    replay_client = _activate_replay_client()
    monkeypatch.setenv("REPLAY_FORCE_CONSTANT_CADENCE", "false")
    monkeypatch.delenv("REPLAY_TS_SYNC", raising=False)
    client = replay_client.ReplayClient("http://replay-service:8080")
    submitted: list[dict[str, Any]] = []

    def fake_submit(payload: dict[str, Any]) -> str:
        submitted.append(payload)
        if len(submitted) < 3:
            request = replay_client.httpx.Request(
                "PUT",
                "http://replay-service:8080/api/v1/job",
            )
            response = replay_client.httpx.Response(
                400,
                request=request,
                text="bad payload",
            )
            raise replay_client.httpx.HTTPStatusError(
                "bad payload",
                request=request,
                response=response,
            )
        return "job-3"

    monkeypatch.setattr(client, "_submit_job_payload", fake_submit)

    job_id = client.create_job(
        source_id="primary_rtsp",
        keyframe_uuid="keyframe-1",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        stop_condition_mode="ts_delta_sec",
        fps=24,
    )

    assert job_id == "job-3"
    assert len(submitted) == 3
    assert "min_duration" not in submitted[0]["configuration"]
    assert submitted[0]["configuration"]["ts_sync"] is False
    assert submitted[1]["fallback_reason"] == "replay_api_rejected_without_constant_cadence"
    assert "min_duration" in submitted[1]["configuration"]
    assert submitted[1]["configuration"]["ts_sync"] is False
    assert submitted[1]["stop_condition"] == {"ts_delta_sec": {"max_delta_sec": 10.0}}
    assert submitted[2]["fallback_reason"] == "replay_api_rejected_ts_delta_sec_constant_cadence"
    assert submitted[2]["stop_condition"] == {"frame_count": 240}
    assert submitted[2]["configuration"]["ts_sync"] is False
