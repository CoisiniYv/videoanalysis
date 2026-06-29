"""Post-Savant evidence video crop validation tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
MEDIA_WORKER_ROOT = str(REPO_ROOT / "services" / "media-worker")
for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        del sys.modules[name]
if MEDIA_WORKER_ROOT in sys.path:
    sys.path.remove(MEDIA_WORKER_ROOT)
sys.path.insert(0, MEDIA_WORKER_ROOT)

from app import post_savant_evidence_bundle as bundle  # noqa: E402


def test_time_domain_crop_rejects_zero_frame_mov_shell(monkeypatch, tmp_path: Path) -> None:
    source_video = tmp_path / "source.mov"
    source_video.write_bytes(b"source video")
    output_video = tmp_path / "raw_clip.mov"

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output_video.write_bytes(b"mov shell")
        return subprocess.CompletedProcess(
            args=["ffmpeg"],
            returncode=0,
            stdout="",
            stderr="Output file is empty, nothing was encoded",
        )

    monkeypatch.setattr(bundle, "_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(bundle.subprocess, "run", fake_run)
    monkeypatch.setattr(
        bundle,
        "read_decoded_video_frame_count",
        lambda _path: (_ for _ in ()).throw(
            RuntimeError("decoded_video_frame_count_unavailable")
        ),
    )

    try:
        bundle._copy_or_crop_video(
            source_video_path=source_video,
            output_video_path=output_video,
            source_frames=[{"pts": 1_000_000_000}],
            time_window={
                "requested_start_pts": 1_000_000_000,
                "requested_end_pts": 11_000_000_000,
                "time_domain_crop_applied": True,
            },
            copy_video=True,
            crop_video_to_time_window=True,
        )
    except RuntimeError as exc:
        assert str(exc) == (
            "video_time_domain_crop_failed:decoded_frame_count_unavailable"
        )
    else:
        raise AssertionError("zero-frame crop should fail closed")

    assert not output_video.exists()
    assert "nothing was encoded" in (tmp_path / "video_crop_ffmpeg.log").read_text(
        encoding="utf-8"
    )


def test_time_domain_crop_timeout_fails_closed(monkeypatch, tmp_path: Path) -> None:
    source_video = tmp_path / "source.mov"
    source_video.write_bytes(b"source video")
    output_video = tmp_path / "raw_clip.mov"
    output_video.write_bytes(b"partial")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 0.25
        raise subprocess.TimeoutExpired(command, timeout=0.25, stderr="timed out")

    monkeypatch.setattr(bundle, "_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(bundle.subprocess, "run", fake_run)

    try:
        bundle._copy_or_crop_video(
            source_video_path=source_video,
            output_video_path=output_video,
            source_frames=[{"pts": 1_000_000_000}],
            time_window={
                "requested_start_pts": 1_000_000_000,
                "requested_end_pts": 11_000_000_000,
                "time_domain_crop_applied": True,
            },
            copy_video=True,
            crop_video_to_time_window=True,
            materialization_timeout_s=0.25,
        )
    except RuntimeError as exc:
        assert str(exc) == "video_time_domain_crop_failed:timeout:0.25s"
    else:
        raise AssertionError("timeout should fail closed")

    assert not output_video.exists()
    assert "timed out" in (tmp_path / "video_crop_ffmpeg.log").read_text(
        encoding="utf-8"
    )


def test_time_domain_crop_passes_configured_ffmpeg_thread_limit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_video = tmp_path / "source.mov"
    source_video.write_bytes(b"source video")
    output_video = tmp_path / "raw_clip.mov"
    observed: dict[str, list[str]] = {}

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        output_video.write_bytes(b"valid clip")
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT", "4")
    monkeypatch.setattr(bundle, "_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(bundle.subprocess, "run", fake_run)
    monkeypatch.setattr(bundle, "read_decoded_video_frame_count", lambda _path: 12)

    bundle._copy_or_crop_video(
        source_video_path=source_video,
        output_video_path=output_video,
        source_frames=[{"pts": 1_000_000_000}],
        time_window={
            "requested_start_pts": 1_000_000_000,
            "requested_end_pts": 11_000_000_000,
            "time_domain_crop_applied": True,
        },
        copy_video=True,
        crop_video_to_time_window=True,
    )

    assert "-threads" in observed["command"]
    assert observed["command"][observed["command"].index("-threads") + 1] == "4"
    assert "-preset" in observed["command"]
    assert observed["command"][observed["command"].index("-preset") + 1] == "ultrafast"
    codec_index = observed["command"].index("-c:v")
    output_threads_index = observed["command"].index("-threads", codec_index)
    assert observed["command"][output_threads_index + 1] == "4"


def test_time_domain_selection_uses_latest_contiguous_pts_segment() -> None:
    frames = (
        _frames(0, [80, 81], "stale-head")
        + _frames(2, [1, 2, 3, 4, 5], "older-window")
        + _frames(7, [1, 2, 3, 4, 5], "current-window")
    )

    selected, time_window = bundle._select_time_domain_frames(
        frames,
        requested_start_pts=2_000_000_000,
        requested_end_pts=4_000_000_000,
        event_frame_pts=3_000_000_000,
        event_frame_uuid="original-event-frame",
        start_window_frame_uuid="original-start-frame",
        post_window_frame_uuid="original-post-frame",
        enabled=True,
    )

    assert [row["uuid"] for row in selected] == [
        "current-window-2",
        "current-window-3",
        "current-window-4",
    ]
    assert time_window["time_domain_selection_strategy"] == "latest_contiguous_pts_segment"
    assert time_window["frame_uuid_anchor_found"] is False
    assert time_window["crop_segment_start_index"] == 7
    assert time_window["crop_segment_first_pts"] == 1_000_000_000
    assert time_window["actual_start_index"] == 8
    assert time_window["candidate_contiguous_segments"] == 2
    assert time_window["source_metadata_pts_discontinuities"] == 2


def test_time_domain_selection_prefers_matching_sink_frame_uuid() -> None:
    frames = (
        _frames(0, [1, 2, 3], "older-window")
        + _frames(3, [1, 2, 3], "current-window")
    )

    selected, time_window = bundle._select_time_domain_frames(
        frames,
        requested_start_pts=1_000_000_000,
        requested_end_pts=3_000_000_000,
        event_frame_pts=2_000_000_000,
        event_frame_uuid="older-window-2",
        enabled=True,
    )

    assert [row["uuid"] for row in selected] == [
        "older-window-1",
        "older-window-2",
        "older-window-3",
    ]
    assert time_window["time_domain_selection_strategy"] == "frame_uuid_contiguous_segment"
    assert time_window["frame_uuid_anchor_found"] is True
    assert time_window["frame_uuid_anchor_used"] == "older-window-2"


def test_time_domain_crop_uses_segment_relative_filter_timeline(
    monkeypatch, tmp_path: Path
) -> None:
    source_video = tmp_path / "source.mov"
    source_video.write_bytes(b"source video")
    output_video = tmp_path / "raw_clip.mov"
    captured: dict[str, list[str]] = {}

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        output_video.write_bytes(b"cropped video")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(bundle, "_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(bundle.subprocess, "run", fake_run)
    monkeypatch.setattr(bundle, "read_decoded_video_frame_count", lambda _path: 240)

    result = bundle._copy_or_crop_video(
        source_video_path=source_video,
        output_video_path=output_video,
        source_frames=[{"pts": 80_000_000_000}],
        time_window={
            "requested_start_pts": 35_000_000_000,
            "requested_end_pts": 45_000_000_000,
            "actual_start_pts": 36_000_000_000,
            "crop_segment_first_pts": 1_000_000_000,
            "time_domain_crop_applied": True,
        },
        copy_video=True,
        crop_video_to_time_window=True,
    )

    assert "-ss" not in captured["command"]
    assert "-vf" in captured["command"]
    assert (
        "setpts=PTS-STARTPTS,trim=start=35.000000000:duration=10.000000000,setpts=PTS-STARTPTS"
        in captured["command"]
    )
    assert result["method"] == "ffmpeg_segment_normalized_transcode"
    assert result["start_seconds"] == 35.0
    assert result["crop_segment_first_pts"] == 1_000_000_000
    assert result["measurement_schema_version"] == "phase0-materialization-v1"
    assert result["materialization_mode"] == "baseline_crop"
    assert result["ffmpeg_returncode"] == 0
    assert result["ffmpeg_elapsed_ms"] >= 0
    assert result["ffmpeg_child_cpu_seconds"] is not None
    assert result["ffmpeg_stderr_bytes"] == 0
    assert result["decoded_frame_count"] == 240
    assert result["decoded_frame_count_probe_elapsed_ms"] >= 0
    assert result["input_bytes"] == len(b"source video")
    assert result["output_bytes"] == len(b"cropped video")
    assert result["materialization_elapsed_ms"] == result["ffmpeg_elapsed_ms"]
    assert result["source_metadata_frame_count"] == 1


def _frames(start_index: int, pts_seconds: list[int], prefix: str) -> list[dict[str, object]]:
    return [
        {
            "type": "VideoFrame",
            "pts": pts * 1_000_000_000,
            "uuid": f"{prefix}-{pts}",
            "frame_num": start_index + offset,
        }
        for offset, pts in enumerate(pts_seconds)
    ]
