"""Frame annotation event-window selection tests."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = str(ROOT / "services" / "media-worker")
for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        del sys.modules[name]
if MEDIA_WORKER_ROOT in sys.path:
    sys.path.remove(MEDIA_WORKER_ROOT)
sys.path.insert(0, MEDIA_WORKER_ROOT)

from app.frame_annotation_event_window import select_frame_annotation_event_window  # noqa: E402


def test_event_window_filters_old_loop_frames_by_wall_clock() -> None:
    event_ts_ms = 1_780_918_000_000
    messages = [
        _message(
            frame_uuid="old-loop",
            frame_pts=10_000_000_000,
            created_at="2026-06-08T11:00:00Z",
        ),
        _message(
            frame_uuid="event-frame",
            frame_pts=10_000_000_000,
            created_at=event_ts_ms,
        ),
        _message(
            frame_uuid="after-event",
            frame_pts=10_041_666_666,
            created_at=event_ts_ms + 1_000,
        ),
    ]

    window, summary = select_frame_annotation_event_window(
        messages,
        source_id="c2_replay_first_rtsp",
        camera_id="c2_replay_first_rtsp",
        anchor_frame_pts=10_000_000_000,
        anchor_frame_uuid="event-frame",
        anchor_source_observation_id=None,
        anchor_event_ts_ms=event_ts_ms,
        pre_seconds=5,
        post_seconds=5,
        max_frames=30,
    )

    assert [row["frame_uuid"] for row in window] == ["event-frame", "after-event"]
    assert summary["wall_clock_filter_enabled"] is True
    assert summary["wall_clock_filter_rejected_messages"] == 1
    assert summary["source_camera_candidate_messages"] == 3
    assert summary["candidate_messages_after_source_camera_filter"] == 2


def test_event_window_preserves_existing_behavior_without_event_wall_clock() -> None:
    messages = [
        _message(frame_uuid="frame-a", frame_pts=10_000_000_000, created_at="2026-06-08T11:00:00Z"),
        _message(frame_uuid="frame-b", frame_pts=10_041_666_666, created_at="2026-06-08T11:00:01Z"),
    ]

    window, summary = select_frame_annotation_event_window(
        messages,
        source_id="c2_replay_first_rtsp",
        camera_id="c2_replay_first_rtsp",
        anchor_frame_pts=10_000_000_000,
        anchor_frame_uuid="frame-a",
        anchor_source_observation_id=None,
        pre_seconds=5,
        post_seconds=5,
        max_frames=30,
    )

    assert [row["frame_uuid"] for row in window] == ["frame-a", "frame-b"]
    assert summary["wall_clock_filter_enabled"] is False


def _message(*, frame_uuid: str, frame_pts: int, created_at: str | int) -> dict:
    return {
        "source_id": "c2_replay_first_rtsp",
        "camera_id": "c2_replay_first_rtsp",
        "frame_uuid": frame_uuid,
        "frame_pts": frame_pts,
        "timestamp_ms": frame_pts // 1_000_000,
        "created_at": created_at,
        "objects": [],
    }
