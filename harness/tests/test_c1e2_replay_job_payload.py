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
    interval = {"secs": 0, "nanos": 33333333}
    assert payload["configuration"]["min_duration"] == interval
    assert payload["configuration"]["max_duration"] == interval
    assert payload["configuration"]["ts_discrepancy_fix_duration"] == interval


def test_replay_payload_uses_24fps_cadence_for_24fps_rtsp_source() -> None:
    _activate_clip_worker_path()
    from app.replay_client import build_job_payload

    payload = build_job_payload(
        source_id="c2_replay_first_rtsp",
        keyframe_uuid="kf-123",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels={"event_id": "ev-123"},
        stop_condition_mode="ts_delta_sec",
        fps=24,
    )

    interval = {"secs": 0, "nanos": 41666666}
    assert payload["configuration"]["min_duration"] == interval
    assert payload["configuration"]["max_duration"] == interval
    assert payload["configuration"]["ts_discrepancy_fix_duration"] == interval
    assert payload["stop_condition"] == {"ts_delta_sec": {"max_delta_sec": 10.0}}


def test_event_start_anchor_strategy_uses_event_anchor_and_replay_offset() -> None:
    _activate_clip_worker_path()
    from app.worker import (
        REPLAY_ANCHOR_STRATEGY_EVENT_START,
        _replay_anchor_lookup_ts_ms,
        _replay_offset_seconds,
    )

    req = {"event_ts_ms": 1_780_906_981_235}

    assert _replay_anchor_lookup_ts_ms(
        req,
        pre_seconds=5,
        post_seconds=5,
        anchor_strategy=REPLAY_ANCHOR_STRATEGY_EVENT_START,
    ) == 1_780_906_986_235
    assert _replay_offset_seconds(
        replay_stop_strategy="anchor_start_offset_zero",
        anchor_strategy=REPLAY_ANCHOR_STRATEGY_EVENT_START,
        pre_seconds=5,
        post_seconds=5,
    ) is None


def test_replay_anchor_lookup_requires_keyframe_covering_post_window() -> None:
    _activate_clip_worker_path()
    from app.replay_client import _select_keyframe_uuid
    from app.worker import REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME, _replay_anchor_selection

    assert _replay_anchor_selection(REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME) == (
        "strict_at_or_after"
    )
    assert _select_keyframe_uuid(
        [
            "019ea72a-7587-7403-ae7f-fb03a3c3c3c4",
            "019ea72a-9e42-73f0-9d14-202735611b69",
        ],
        ts_ms=1_780_921_059_069,
        selection="strict_at_or_after",
    ) is None


def test_event_start_anchor_strategy_prefers_frame_uuid_time_domain() -> None:
    _activate_clip_worker_path()
    from app.worker import (
        REPLAY_ANCHOR_STRATEGY_EVENT_START,
        _replay_anchor_lookup_ts_ms,
    )

    req = {
        "event_ts_ms": 1_780_906_981_235,
        "frame_uuid": "019ea673-362c-7142-a716-a50edb062ebe",
    }

    assert _replay_anchor_lookup_ts_ms(
        req,
        pre_seconds=5,
        post_seconds=5,
        anchor_strategy=REPLAY_ANCHOR_STRATEGY_EVENT_START,
    ) == 1_780_909_033_908


def test_event_keyframe_strategy_covers_offset_plus_post_window() -> None:
    _activate_clip_worker_path()
    from app.worker import (
        REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME,
        _replay_duration_seconds,
        _replay_offset_seconds,
    )

    offset = _replay_offset_seconds(
        replay_stop_strategy="event_anchor_pre_seconds_rewind",
        anchor_strategy=REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME,
        pre_seconds=5,
        post_seconds=5,
        event_frame_uuid="019ea722-e76e-74a3-b448-be6876fa4ee7",
        keyframe_uuid="019ea722-e9b6-79f1-b21c-f910aede49ad",
    )

    assert offset == 5.584
    assert _replay_duration_seconds(
        pre_seconds=5,
        post_seconds=5,
        offset_seconds_override=offset,
    ) == 10.584


def test_event_start_anchor_strategy_forces_keyframe_lookup() -> None:
    _activate_clip_worker_path()
    from app.worker import (
        REPLAY_ANCHOR_STRATEGY_EVENT_START,
        _should_lookup_replay_anchor,
    )

    assert _should_lookup_replay_anchor(REPLAY_ANCHOR_STRATEGY_EVENT_START) is True
    assert _should_lookup_replay_anchor("request_keyframe") is False


def test_clip_worker_default_config_uses_reliable_sink_and_replay_fps(monkeypatch) -> None:
    _activate_clip_worker_path()
    from app.config import load_config

    monkeypatch.delenv("REPLAY_JOB_SINK_URL", raising=False)
    monkeypatch.delenv("REPLAY_FPS", raising=False)
    monkeypatch.delenv("REPLAY_DURATION_EXTRA_SLACK_S", raising=False)
    cfg = load_config()

    assert cfg.replay_job_sink_url == "dealer+connect:tcp://video-file-sink:6666"
    assert cfg.replay_fps == 30
    assert cfg.replay_duration_extra_slack_s == 0.0


def test_clip_worker_config_reads_replay_duration_extra_slack(monkeypatch) -> None:
    _activate_clip_worker_path()
    from app.config import load_config

    monkeypatch.setenv("REPLAY_DURATION_EXTRA_SLACK_S", "15")

    assert load_config().replay_duration_extra_slack_s == 15.0


def test_replay_config_ttl_and_default_sink_options_match_c1e2() -> None:
    replay_config = ROOT / "modules" / "savant_replay" / "config.c2_replay_first_dev.json"
    data = json.loads(replay_config.read_text(encoding="utf-8"))

    assert data["storage"]["rocksdb"]["data_expiration_ttl"] == {
        "secs": 30,
        "nanos": 0,
    }
    assert data["storage"]["rocksdb"]["compaction_period"] == {
        "secs": 30,
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
