"""Frame annotation exporter runtime throttling tests."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = ROOT / "modules" / "savant_security"
for name in list(sys.modules):
    if name == "custom" or name.startswith("custom."):
        del sys.modules[name]
module_path = str(MODULE_DIR)
if module_path in sys.path:
    sys.path.remove(module_path)
sys.path.insert(0, module_path)
root_path = str(ROOT)
if root_path not in sys.path:
    sys.path.insert(0, root_path)

from custom.services.frame_annotation_exporter import (  # noqa: E402
    FrameAnnotationExporterConfig,
    FrameAnnotationExportRuntime,
)
from custom.services.stream_session import StreamSessionTracker  # noqa: E402


class FakeExporter:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def export(self, message: dict[str, Any]) -> bool:
        self.messages.append(message)
        return True


class FakeVideoFrame:
    def __init__(
        self,
        *,
        uuid: str,
        pts: int,
        keyframe: bool = False,
        keyframe_uuid: str | None = None,
        previous_keyframe_uuid: str | None = None,
    ) -> None:
        self.uuid = uuid
        self.pts = pts
        self.keyframe = keyframe
        self.keyframe_uuid = keyframe_uuid
        self.previous_keyframe_uuid = previous_keyframe_uuid
        self.source_id = "source-a"
        self.time_base = "1/1000000000"


class FakeFrameMeta:
    def __init__(self, video_frame: FakeVideoFrame, *, frame_num: int) -> None:
        self.source_id = "source-a"
        self.video_frame = video_frame
        self.frame_num = frame_num
        self.objects: list[Any] = []


def _frame(
    *,
    uuid: str,
    pts: int,
    frame_num: int,
    keyframe: bool = False,
    keyframe_uuid: str | None = None,
    previous_keyframe_uuid: str | None = None,
) -> FakeFrameMeta:
    return FakeFrameMeta(
        FakeVideoFrame(
            uuid=uuid,
            pts=pts,
            keyframe=keyframe,
            keyframe_uuid=keyframe_uuid,
            previous_keyframe_uuid=previous_keyframe_uuid,
        ),
        frame_num=frame_num,
    )


def test_frame_annotation_min_interval_throttles_but_preserves_keyframes() -> None:
    exporter = FakeExporter()
    runtime = FrameAnnotationExportRuntime(
        config=FrameAnnotationExporterConfig(
            enabled=True,
            log_every_n=10_000,
            min_interval_ms=250,
        ),
        exporter=exporter,
        resolve_camera_id=lambda source_id: f"camera:{source_id}",
    )

    runtime.process_frame(_frame(uuid="kf-0", pts=0, frame_num=0, keyframe=True))
    runtime.process_frame(
        _frame(
            uuid="frame-100ms",
            pts=100_000_000,
            frame_num=1,
            previous_keyframe_uuid="kf-0",
        )
    )
    runtime.process_frame(
        _frame(uuid="kf-150ms", pts=150_000_000, frame_num=2, keyframe=True)
    )
    runtime.process_frame(
        _frame(
            uuid="frame-300ms",
            pts=300_000_000,
            frame_num=3,
            previous_keyframe_uuid="kf-150ms",
        )
    )
    runtime.process_frame(
        _frame(
            uuid="frame-400ms",
            pts=400_000_000,
            frame_num=4,
            previous_keyframe_uuid="kf-150ms",
        )
    )

    assert [message["frame_pts"] for message in exporter.messages] == [
        0,
        150_000_000,
        400_000_000,
    ]
    assert runtime.counters.frames_seen == 5
    assert runtime.counters.frames_exported == 3
    assert runtime.counters.frames_skipped_throttled == 2


def test_frame_annotation_min_interval_resets_on_small_pts_rollback_without_session_churn() -> None:
    exporter = FakeExporter()
    runtime = FrameAnnotationExportRuntime(
        config=FrameAnnotationExporterConfig(
            enabled=True,
            log_every_n=10_000,
            min_interval_ms=250,
        ),
        exporter=exporter,
        resolve_camera_id=lambda source_id: source_id,
    )

    runtime.process_frame(_frame(uuid="frame-1s", pts=1_000_000_000, frame_num=10))
    runtime.process_frame(_frame(uuid="frame-reset", pts=100_000_000, frame_num=0))

    assert [message["frame_pts"] for message in exporter.messages] == [
        1_000_000_000,
        100_000_000,
    ]
    assert runtime.counters.frames_skipped_throttled == 0
    assert exporter.messages[0]["stream_session_id"]
    assert (
        exporter.messages[1]["stream_session_id"]
        == exporter.messages[0]["stream_session_id"]
    )


def test_stream_session_tolerates_small_rollback_and_tracks_large_reset() -> None:
    tracker = StreamSessionTracker(
        session_prefix="test",
        pts_rollback_tolerance_ns=5_000_000_000,
    )

    first = tracker.session_id_for_frame("source-a", 10_000_000_000)
    small_rollback = tracker.session_id_for_frame("source-a", 9_500_000_000)
    cumulative_large_rollback = tracker.session_id_for_frame("source-a", 4_900_000_000)
    after_rollback = tracker.session_id_for_frame("source-a", 5_000_000_000)
    forward_jump = tracker.session_id_for_frame("source-a", 30_000_000_000)

    assert first
    assert small_rollback == first
    assert cumulative_large_rollback != first
    assert after_rollback == cumulative_large_rollback
    assert forward_jump == cumulative_large_rollback
