"""C1E.2 Replay job payload contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CW_DIR = str(ROOT / "services" / "clip-worker")


def _activate_clip_worker_path() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if CW_DIR in sys.path:
        sys.path.remove(CW_DIR)
    sys.path.insert(0, CW_DIR)


def test_replay_payload_uses_reliable_sink_options_and_30fps_pacing() -> None:
    _activate_clip_worker_path()
    from app.replay_client import build_job_payload

    payload = build_job_payload(
        source_id="c1e_rtsp_replay",
        keyframe_uuid="kf-123",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels={"event_id": "ev-123"},
        fps=30,
    )

    assert payload["sink"]["url"] == "dealer+connect:tcp://video-file-sink:6666"
    assert payload["sink"]["options"] == {
        "send_timeout": {"secs": 5, "nanos": 0},
        "send_retries": 5,
        "receive_timeout": {"secs": 5, "nanos": 0},
        "receive_retries": 5,
        "send_hwm": 10000,
        "receive_hwm": 10000,
        "inflight_ops": 100,
    }
    cfg = payload["configuration"]
    interval = {"secs": 0, "nanos": 33333333}
    assert cfg["min_duration"] == interval
    assert cfg["max_duration"] == interval
    assert cfg["ts_discrepancy_fix_duration"] == interval
    assert payload["stop_condition"]["frame_count"] == 300
    assert payload["offset"]["seconds"] == 5.0


def test_replay_payload_frame_count_and_interval_follow_fps() -> None:
    _activate_clip_worker_path()
    from app.replay_client import build_job_payload

    payload = build_job_payload(
        source_id="c1e_rtsp_replay",
        keyframe_uuid="kf-123",
        pre_seconds=2.5,
        post_seconds=1.5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels={"event_id": "ev-123"},
        fps=25,
    )

    interval = {"secs": 0, "nanos": 40000000}
    assert payload["configuration"]["min_duration"] == interval
    assert payload["configuration"]["max_duration"] == interval
    assert payload["configuration"]["ts_discrepancy_fix_duration"] == interval
    assert payload["stop_condition"]["frame_count"] == 100
    assert payload["offset"]["seconds"] == 2.5


def test_replay_payload_ts_delta_keeps_reliable_sink_and_offset() -> None:
    _activate_clip_worker_path()
    from app.replay_client import build_job_payload

    payload = build_job_payload(
        source_id="c1e_rtsp_replay",
        keyframe_uuid="kf-123",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels={"event_id": "ev-123"},
        stop_condition_mode="ts_delta_sec",
        fps=30,
    )

    assert payload["stop_condition"]["ts_delta_sec"]["max_delta_sec"] == 10.0
    assert payload["offset"]["seconds"] == 5.0
    assert payload["sink"]["options"]["send_retries"] == 5


def test_clip_worker_default_config_uses_reliable_sink_and_replay_fps(monkeypatch) -> None:
    _activate_clip_worker_path()
    from app.config import load_config

    monkeypatch.delenv("REPLAY_JOB_SINK_URL", raising=False)
    monkeypatch.delenv("REPLAY_FPS", raising=False)
    cfg = load_config()

    assert cfg.replay_job_sink_url == "dealer+connect:tcp://video-file-sink:6666"
    assert cfg.replay_fps == 30


def test_replay_config_ttl_and_default_sink_options_match_c1e2() -> None:
    replay_config = ROOT / "modules" / "savant_replay" / "config.p1c_rtsp_inline.json"
    data = json.loads(replay_config.read_text(encoding="utf-8"))

    assert data["storage"]["rocksdb"]["data_expiration_ttl"] == {
        "secs": 300,
        "nanos": 0,
    }
    assert data["storage"]["rocksdb"]["compaction_period"] == {
        "secs": 120,
        "nanos": 0,
    }
    assert data["common"]["default_job_sink_options"] == {
        "send_timeout": {"secs": 5, "nanos": 0},
        "send_retries": 5,
        "receive_timeout": {"secs": 5, "nanos": 0},
        "receive_retries": 5,
        "send_hwm": 10000,
        "receive_hwm": 10000,
        "inflight_ops": 100,
    }
