"""Rolling-cache segment lookup and materialization contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
MEDIA_WORKER_ROOT = str(REPO_ROOT / "services" / "media-worker")
for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        del sys.modules[name]
if MEDIA_WORKER_ROOT in sys.path:
    sys.path.remove(MEDIA_WORKER_ROOT)
sys.path.insert(0, MEDIA_WORKER_ROOT)

from app import rolling_cache  # noqa: E402


def test_wall_clock_event_window_maps_to_stable_mux_clock() -> None:
    rows = [
        {
            "pts": 10_000_000_000,
            "rolling_cache_mux_pts": 20_000_000_000,
            "uuid": "before",
        },
        {
            "pts": 11_700_000_000,
            "rolling_cache_mux_pts": 20_041_708_333,
            "uuid": "event-frame",
        },
        {
            "pts": 11_700_001_000,
            "rolling_cache_mux_pts": 20_083_416_666,
            "uuid": "after",
        },
    ]

    mapped = rolling_cache._map_source_window_to_mux(
        [SimpleNamespace(metadata_path=Path("unused"))],
        requested_start_pts=6_700_000_000,
        requested_end_pts=16_700_000_000,
        labels={
            "event_frame_pts": 11_700_000_000,
            "event_frame_uuid": "event-frame",
        },
        row_loader=lambda _segment: rows,
    )

    assert mapped == (15_041_708_333, 25_041_708_333, 20_041_708_333)


def test_source_clock_mapping_reads_only_nearest_segment_ranges() -> None:
    segments = []
    rows_by_name = {}
    for index in range(225):
        source_start = index * 4_000_000_000
        source_end = source_start + 3_999_999_999
        name = f"segment-{index:03d}"
        segments.append(
            SimpleNamespace(
                segment_id=name,
                metadata_path=Path(name),
                source_first_pts=source_start,
                source_last_pts=source_end,
            )
        )
        rows_by_name[name] = [
            {
                "pts": source_start,
                "rolling_cache_mux_pts": source_start + 50_000_000_000,
                "uuid": f"frame-{index}-first",
            },
            {
                "pts": source_end,
                "rolling_cache_mux_pts": source_end + 50_000_000_000,
                "uuid": f"frame-{index}-last",
            },
        ]

    loaded = []

    def load_rows(segment):
        loaded.append(segment.segment_id)
        return rows_by_name[segment.segment_id]

    event_index = 170
    event_source_pts = event_index * 4_000_000_000 + 3_999_999_999
    mapped = rolling_cache._map_source_window_to_mux(
        segments,
        requested_start_pts=event_source_pts - 5_000_000_000,
        requested_end_pts=event_source_pts + 5_000_000_000,
        labels={
            "event_frame_pts": event_source_pts,
            "event_frame_uuid": f"frame-{event_index}-last",
        },
        row_loader=load_rows,
    )

    assert mapped == (
        event_source_pts + 45_000_000_000,
        event_source_pts + 55_000_000_000,
        event_source_pts + 50_000_000_000,
    )
    assert loaded == [f"segment-{event_index:03d}"]


def test_source_clock_mapping_keeps_legacy_full_scan_without_bounds() -> None:
    segments = [
        SimpleNamespace(segment_id="first", metadata_path=Path("first")),
        SimpleNamespace(segment_id="second", metadata_path=Path("second")),
    ]
    loaded = []

    def load_rows(segment):
        loaded.append(segment.segment_id)
        source_pts = 100 if segment.segment_id == "first" else 200
        return [{"pts": source_pts, "rolling_cache_mux_pts": source_pts + 1_000}]

    assert rolling_cache._map_source_window_to_mux(
        segments,
        requested_start_pts=150,
        requested_end_pts=250,
        labels={"event_frame_pts": 200},
        row_loader=load_rows,
    ) == (1_150, 1_250, 1_200)
    assert loaded == ["first", "second"]


def _write_segment(
    root: Path,
    *,
    epoch: str,
    source_id: str,
    name: str,
    pts_values: list[int],
) -> Path:
    segment_dir = root / "midterm" / "epochs" / epoch / source_id / "segments" / name
    segment_dir.mkdir(parents=True)
    (segment_dir / "video.mov").write_bytes(b"video-" + name.encode("ascii"))
    rows = [
        {
            "type": "VideoFrame",
            "source_id": source_id,
            "pts": pts,
            "uuid": f"frame-{name}-{index}",
            "objects": [],
        }
        for index, pts in enumerate(pts_values)
    ]
    (segment_dir / "metadata.json").write_text(
        json.dumps({"frames": rows}),
        encoding="utf-8",
    )
    return segment_dir


def test_find_segments_derives_pts_boundaries_from_native_metadata(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1_000_000_000, 2_000_000_000],
    )

    segments = rolling_cache.find_segments(
        root,
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
    )

    assert len(segments) == 1
    assert segments[0].first_pts == 1_000_000_000
    assert segments[0].last_pts == 2_000_000_000
    assert segments[0].video_path.name == "video.mov"


def test_find_segments_accepts_video_files_sink_percent_source_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01%",
        name="0001",
        pts_values=[1_000_000_000, 2_000_000_000],
    )

    segments = rolling_cache.find_segments(
        root,
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
    )

    assert len(segments) == 1
    assert segments[0].directory.parts[-3:] == ("camera-01%", "segments", "0001")


def test_find_segments_stays_within_requested_runtime_epoch(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1_000_000_000, 2_000_000_000],
    )
    _write_segment(
        root,
        epoch="epoch-b",
        source_id="camera-01",
        name="0002",
        pts_values=[3_000_000_000, 4_000_000_000],
    )

    segments = rolling_cache.find_segments(
        root,
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
    )

    assert len(segments) == 1
    assert segments[0].runtime_epoch_id == "epoch-a"
    assert segments[0].segment_id == "0001"


def test_materialize_window_writes_sink_like_metadata_and_concat_command(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    output_root = tmp_path / "materialized"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1_000_000_000, 2_000_000_000],
    )
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0002",
        pts_values=[3_000_000_000, 4_000_000_000],
    )
    commands: list[list[str]] = []

    def fake_runner(command: list[str], _log_path: Path) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"joined-video")

    result = rolling_cache.materialize_window(
        root=root,
        output_root=output_root,
        event_id="11111111-1111-4111-8111-111111111111",
        source_id="camera-01",
        requested_start_pts=1_500_000_000,
        requested_end_pts=3_500_000_000,
        runtime_epoch_id="epoch-a",
        labels={"event_type": "intrusion"},
        ffmpeg="/usr/bin/ffmpeg",
        command_runner=fake_runner,
        coverage_slack_ns=1_000_000_000,
    )

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert result.video_path.read_bytes() == b"joined-video"
    assert result.segment_ids == ("0001", "0002")
    assert metadata["labels"]["materialization_mode"] == "rolling_cache_copy"
    assert metadata["labels"]["canonical_clip"] == "true"
    assert metadata["labels"]["time_domain_crop_applied"] == "true"
    assert metadata["labels"]["requested_duration_s"] == "2"
    assert metadata["rolling_cache"]["requested_start_pts"] == 1_500_000_000
    assert metadata["rolling_cache"]["actual_start_pts"] == 1_500_000_000
    assert metadata["rolling_cache"]["actual_end_pts"] == 3_500_000_000
    assert metadata["rolling_cache"]["requested_duration_s"] == 2.0
    assert metadata["rolling_cache"]["canonical_clip"] is True
    assert metadata["rolling_cache"]["segment_start_pts"] == 1_000_000_000
    assert metadata["rolling_cache"]["segment_end_pts"] == 4_000_000_000
    assert metadata["rolling_cache"]["trim_start_s"] == 0.5
    assert metadata["rolling_cache"]["output_duration_s"] == 2.0
    assert metadata["frames"][0]["pts"] == 2_000_000_000
    assert commands[0][:4] == ["/usr/bin/ffmpeg", "-hide_banner", "-y", "-f"]
    assert "-ss" in commands[0]
    assert "-t" in commands[0]


def test_materialize_window_retries_copy_remux_with_input_seek(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    output_root = tmp_path / "materialized"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1_000_000_000, 2_000_000_000],
    )
    commands: list[list[str]] = []

    def flaky_runner(command: list[str], _log_path: Path) -> None:
        commands.append(command)
        if len(commands) == 1:
            raise rolling_cache.RollingCacheError("ffmpeg_failed:1")
        Path(command[-1]).write_bytes(b"retry-video")

    result = rolling_cache.materialize_window(
        root=root,
        output_root=output_root,
        event_id="11111111-1111-4111-8111-111111111111",
        source_id="camera-01",
        requested_start_pts=1_000_000_000,
        requested_end_pts=2_000_000_000,
        runtime_epoch_id="epoch-a",
        ffmpeg="/usr/bin/ffmpeg",
        command_runner=flaky_runner,
    )

    assert result.video_path.read_bytes() == b"retry-video"
    assert len(commands) == 2
    assert commands[1][:5] == ["/usr/bin/ffmpeg", "-hide_banner", "-y", "-ss", "0"]
    assert "-avoid_negative_ts" in commands[1]


def test_materialize_window_transcodes_when_copy_duration_is_short(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    output_root = tmp_path / "materialized"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1_000_000_000, 2_000_000_000, 3_000_000_000],
    )
    commands: list[list[str]] = []
    durations = [0.5, 2.0]

    def duration_probe(_path: Path) -> float | None:
        return durations.pop(0)

    def fake_runner(command: list[str], _log_path: Path) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"video")

    result = rolling_cache.materialize_window(
        root=root,
        output_root=output_root,
        event_id="11111111-1111-4111-8111-111111111111",
        source_id="camera-01",
        requested_start_pts=1_000_000_000,
        requested_end_pts=3_000_000_000,
        runtime_epoch_id="epoch-a",
        ffmpeg="/usr/bin/ffmpeg",
        command_runner=fake_runner,
        duration_probe=duration_probe,
    )

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert len(commands) == 2
    assert commands[1][0:2] == ["/usr/bin/ffmpeg", "-hide_banner"]
    assert "-c:v" in commands[1]
    assert "libx264" in commands[1]
    assert metadata["rolling_cache"]["duration_repair_attempted"] is True
    assert metadata["rolling_cache"]["duration_repair_status"] == "transcode_retry_duration_ok"
    assert metadata["rolling_cache"]["probed_output_duration_s"] == 2.0
    immutable_probe = metadata["rolling_cache"]["immutable_probe"]
    assert immutable_probe["status"] == "ready"
    assert immutable_probe["duration_s"] == 2.0
    assert immutable_probe["identity"]["size"] == result.video_path.stat().st_size
    assert result.immutable_probe == immutable_probe


def test_materialize_window_transcodes_when_copy_frame_rate_is_low(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    output_root = tmp_path / "materialized"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1_000_000_000, 2_000_000_000, 3_000_000_000],
    )
    commands: list[list[str]] = []
    durations = [2.0, 2.0]
    frame_rates = [23.0, 23.976]

    def fake_runner(command: list[str], _log_path: Path) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"video")

    result = rolling_cache.materialize_window(
        root=root,
        output_root=output_root,
        event_id="11111111-1111-4111-8111-111111111111",
        source_id="camera-01",
        requested_start_pts=1_000_000_000,
        requested_end_pts=3_000_000_000,
        runtime_epoch_id="epoch-a",
        command_runner=fake_runner,
        duration_probe=lambda _path: durations.pop(0),
        frame_rate_probe=lambda _path: frame_rates.pop(0),
    )

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert len(commands) == 2
    assert "libx264" in commands[1]
    assert metadata["rolling_cache"]["duration_repair_attempted"] is True
    assert metadata["rolling_cache"]["probed_output_frame_rate_fps"] == 23.976


def test_select_rows_sorts_and_deduplicates_pts(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    first = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[3, 1, 2],
    )
    second = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0002",
        pts_values=[2, 4],
    )
    segments = rolling_cache.find_segments(
        root,
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
    )

    rows = rolling_cache._select_rows(segments, 1, 4)

    assert [row["pts"] for row in rows if row.get("pts") is not None] == [1, 2, 3, 4]
    assert first.is_dir() and second.is_dir()


def test_materialize_window_reports_initial_and_retry_ffmpeg_failures(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    output_root = tmp_path / "materialized"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1_000_000_000, 2_000_000_000],
    )
    attempts: list[str] = []

    def failing_runner(command: list[str], _log_path: Path) -> None:
        attempts.append(Path(command[-1]).name)
        if len(attempts) == 1:
            raise rolling_cache.RollingCacheError("ffmpeg_failed:1 tail=first")
        raise rolling_cache.RollingCacheError("ffmpeg_failed:2 tail=retry")

    try:
        rolling_cache.materialize_window(
            root=root,
            output_root=output_root,
            event_id="11111111-1111-4111-8111-111111111111",
            source_id="camera-01",
            requested_start_pts=1_000_000_000,
            requested_end_pts=2_000_000_000,
            runtime_epoch_id="epoch-a",
            ffmpeg="/usr/bin/ffmpeg",
            command_runner=failing_runner,
        )
    except rolling_cache.RollingCacheError as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive assertion path
        raise AssertionError("expected rolling-cache materialization failure")

    assert attempts == ["video.mov", "video.mov"]
    assert "ffmpeg_failed:2 tail=retry" in message
    assert "initial_attempt=ffmpeg_failed:1 tail=first" in message


def test_default_command_runner_includes_ffmpeg_log_tail(tmp_path: Path) -> None:
    log_path = tmp_path / "ffmpeg.log"
    command = [
        sys.executable,
        "-c",
        "import sys; print('bad packet at segment boundary', file=sys.stderr); sys.exit(7)",
    ]

    try:
        rolling_cache._default_command_runner(command, log_path)
    except rolling_cache.RollingCacheError as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive assertion path
        raise AssertionError("expected command runner failure")

    assert "ffmpeg_failed:7" in message
    assert "bad packet at segment boundary" in message


def test_materialize_window_allows_small_segment_edge_gap(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    output_root = tmp_path / "materialized"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1_050_000_000, 4_950_000_000],
    )

    def fake_runner(command: list[str], _log_path: Path) -> None:
        Path(command[-1]).write_bytes(b"joined-video")

    result = rolling_cache.materialize_window(
        root=root,
        output_root=output_root,
        event_id="11111111-1111-4111-8111-111111111111",
        source_id="camera-01",
        requested_start_pts=1_000_000_000,
        requested_end_pts=5_000_000_000,
        runtime_epoch_id="epoch-a",
        command_runner=fake_runner,
    )

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["rolling_cache"]["start_gap_ns"] == 50_000_000
    assert metadata["rolling_cache"]["end_gap_ns"] == 50_000_000
    assert metadata["rolling_cache"]["coverage_slack_ns"] == 750_000_000


def test_materialize_window_rejects_partial_window_by_default(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    output_root = tmp_path / "materialized"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[8_000_000_000, 9_000_000_000, 10_000_000_000],
    )

    def fake_runner(command: list[str], _log_path: Path) -> None:
        Path(command[-1]).write_bytes(b"joined-video")

    try:
        rolling_cache.materialize_window(
            root=root,
            output_root=output_root,
            event_id="11111111-1111-4111-8111-111111111111",
            source_id="camera-01",
            requested_start_pts=1_000_000_000,
            requested_end_pts=10_000_000_000,
            runtime_epoch_id="epoch-a",
            labels={"event_frame_pts": "9_000_000_000".replace("_", "")},
            command_runner=fake_runner,
        )
    except rolling_cache.RollingCacheCoverageMiss as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive assertion path
        raise AssertionError("expected partial rolling cache window to be rejected")

    assert "rolling_cache_requested_window_not_fully_covered" in message
    assert "pre_gap_ns=7000000000" in message


def test_materialize_window_retries_when_middle_fragment_is_not_visible(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    output_root = tmp_path / "materialized"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1_000_000_000, 2_000_000_000],
    )
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0003",
        pts_values=[6_000_000_000, 7_000_000_000],
    )
    commands: list[list[str]] = []

    def fake_runner(command: list[str], _log_path: Path) -> None:
        commands.append(command)

    try:
        rolling_cache.materialize_window(
            root=root,
            output_root=output_root,
            event_id="11111111-1111-4111-8111-111111111111",
            source_id="camera-01",
            requested_start_pts=1_000_000_000,
            requested_end_pts=7_000_000_000,
            runtime_epoch_id="epoch-a",
            command_runner=fake_runner,
        )
    except rolling_cache.RollingCacheCoverageMiss as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive assertion path
        raise AssertionError("expected internal rolling-cache gap to be retried")

    assert "rolling_cache_requested_window_internal_gap" in message
    assert "internal_gap_ns=4000000000" in message
    assert commands == []


def test_materialize_window_retries_when_segment_endpoints_hide_row_gap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    output_root = tmp_path / "materialized"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[
            1_000_000_000,
            1_100_000_000,
            1_200_000_000,
            3_000_000_000,
        ],
    )
    commands: list[list[str]] = []

    try:
        rolling_cache.materialize_window(
            root=root,
            output_root=output_root,
            event_id="11111111-1111-4111-8111-111111111111",
            source_id="camera-01",
            requested_start_pts=1_000_000_000,
            requested_end_pts=3_000_000_000,
            runtime_epoch_id="epoch-a",
            command_runner=lambda command, _log: commands.append(command),
        )
    except rolling_cache.RollingCacheCoverageMiss as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive assertion path
        raise AssertionError("expected metadata row gap to be retried")

    assert "rolling_cache_selected_rows_internal_gap" in message
    assert "internal_gap_ns=1800000000" in message
    assert commands == []


def test_materialize_window_still_short_after_transcode_is_retryable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    output_root = tmp_path / "materialized"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1_000_000_000, 2_000_000_000, 3_000_000_000],
    )
    durations = [0.5, 0.75]

    def duration_probe(_path: Path) -> float | None:
        return durations.pop(0)

    def fake_runner(command: list[str], _log_path: Path) -> None:
        Path(command[-1]).write_bytes(b"video")

    try:
        rolling_cache.materialize_window(
            root=root,
            output_root=output_root,
            event_id="11111111-1111-4111-8111-111111111111",
            source_id="camera-01",
            requested_start_pts=1_000_000_000,
            requested_end_pts=3_000_000_000,
            runtime_epoch_id="epoch-a",
            command_runner=fake_runner,
            duration_probe=duration_probe,
        )
    except rolling_cache.RollingCacheCoverageMiss as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive assertion path
        raise AssertionError("expected short decoded output to be retried")

    assert "rolling_cache_output_duration_short" in message


def test_materialize_window_can_opt_into_partial_window_for_diagnostics(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    output_root = tmp_path / "materialized"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[8_000_000_000, 9_000_000_000, 10_000_000_000],
    )

    def fake_runner(command: list[str], _log_path: Path) -> None:
        Path(command[-1]).write_bytes(b"joined-video")

    result = rolling_cache.materialize_window(
        root=root,
        output_root=output_root,
        event_id="11111111-1111-4111-8111-111111111111",
        source_id="camera-01",
        requested_start_pts=1_000_000_000,
        requested_end_pts=10_000_000_000,
        runtime_epoch_id="epoch-a",
        labels={"event_frame_pts": "9_000_000_000".replace("_", "")},
        command_runner=fake_runner,
        allow_partial=True,
    )

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["rolling_cache"]["coverage_status"] == "partial"
    assert metadata["rolling_cache"]["canonical_clip"] is False
    assert metadata["labels"]["canonical_clip"] == "false"
    assert metadata["rolling_cache"]["pre_window_truncated"] is True
    assert metadata["rolling_cache"]["post_window_truncated"] is False
    assert metadata["labels"]["pre_window_truncated"] == "true"


def test_materialize_window_reports_coverage_miss(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[10_000_000_000, 11_000_000_000],
    )

    try:
        rolling_cache.materialize_window(
            root=root,
            output_root=tmp_path / "out",
            event_id="11111111-1111-4111-8111-111111111111",
            source_id="camera-01",
            requested_start_pts=1_000_000_000,
            requested_end_pts=2_000_000_000,
            runtime_epoch_id="epoch-a",
            command_runner=lambda _cmd, _log: None,
        )
    except rolling_cache.RollingCacheCoverageMiss as exc:
        assert "no_overlapping_segments" in str(exc)
    else:
        raise AssertionError("expected rolling cache coverage miss")
