"""Midterm Replay duration tuning diagnostics tests."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
CLIP_WORKER_ROOT = str(REPO_ROOT / "services" / "clip-worker")


def _activate_clip_worker():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if CLIP_WORKER_ROOT in sys.path:
        sys.path.remove(CLIP_WORKER_ROOT)
    sys.path.insert(0, CLIP_WORKER_ROOT)
    return importlib.import_module("app.worker")


def _proofs(worker):
    start = worker.FrameAnnotationAnchor(
        frame_uuid="start-window-frame",
        frame_pts=95_000_000_000,
        stream_id="95-0",
        source_id="primary_rtsp",
        camera_id="primary_rtsp",
        keyframe_uuid="start-window-keyframe",
        keyframe_pts=94_000_000_000,
    )
    post = worker.FrameAnnotationAnchor(
        frame_uuid="post-window-frame",
        frame_pts=105_000_000_000,
        stream_id="105-0",
        source_id="primary_rtsp",
        camera_id="primary_rtsp",
        keyframe_uuid="post-window-keyframe",
        keyframe_pts=104_000_000_000,
    )
    return worker.ReplayFrameDomainProofs(
        start_window_frame=start,
        post_window_frame=post,
    )


def _request() -> dict:
    return {
        "request_id": "request-1",
        "event_id": "event-1",
        "source_event_id": "intrusion:primary_rtsp:1",
        "frame_uuid": "event-frame",
        "frame_pts": 100_000_000_000,
        "requested_start_pts": 95_000_000_000,
        "requested_end_pts": 105_000_000_000,
        "runtime_epoch_id": "midterm-epoch-1",
    }


def test_duration_diagnostics_show_anchor_before_start_guard_not_used() -> None:
    worker = _activate_clip_worker()

    updated = worker._apply_replay_anchor_to_request(
        _request(),
        proofs=_proofs(worker),
        anchor_keyframe_uuid="post-window-keyframe",
        anchor_keyframe_pts=106_000_000_000,
        anchor_keyframe_source="frame_annotation",
        pre_seconds=5,
        post_seconds=5,
        replay_duration_extra_slack_s=5,
    )

    assert updated["replay_duration_base_seconds"] == 11.0
    assert updated["replay_duration_anchor_before_start_guard_used"] is False
    assert updated["replay_duration_anchor_before_start_guard_s"] == 0.0
    assert updated["replay_duration_before_slack_s"] == 11.0
    assert updated["replay_duration_extra_slack_s"] == 5.0
    assert updated["replay_duration_seconds"] == 16.0

    labels = worker._replay_job_labels(updated["event_id"], updated)
    assert labels["replay_duration_base_seconds"] == "11.0"
    assert labels["replay_duration_anchor_before_start_guard_used"] == "false"
    assert labels["replay_duration_anchor_before_start_guard_s"] == "0.0"
    assert labels["replay_duration_before_slack_s"] == "11.0"


def test_duration_diagnostics_show_anchor_before_start_guard_used() -> None:
    worker = _activate_clip_worker()

    updated = worker._apply_replay_anchor_to_request(
        _request(),
        proofs=_proofs(worker),
        anchor_keyframe_uuid="start-window-keyframe",
        anchor_keyframe_pts=95_000_000_000,
        anchor_keyframe_source="frame_annotation",
        pre_seconds=5,
        post_seconds=5,
        replay_duration_extra_slack_s=5,
    )

    assert updated["replay_duration_base_seconds"] == 11.0
    assert updated["replay_duration_anchor_before_start_guard_used"] is True
    assert updated["replay_duration_anchor_before_start_guard_s"] == 11.0
    assert updated["replay_duration_before_slack_s"] == 22.0
    assert updated["replay_duration_extra_slack_s"] == 5.0
    assert updated["replay_duration_seconds"] == 27.0

    labels = worker._replay_job_labels(updated["event_id"], updated)
    assert labels["replay_duration_base_seconds"] == "11.0"
    assert labels["replay_duration_anchor_before_start_guard_used"] == "true"
    assert labels["replay_duration_anchor_before_start_guard_s"] == "11.0"
    assert labels["replay_duration_before_slack_s"] == "22.0"
