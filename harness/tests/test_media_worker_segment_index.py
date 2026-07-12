"""Incremental rolling segment index and read-pin contracts."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"
MAINTENANCE_PATH = ROOT / "scripts" / "runtime" / "rolling_cache_maintenance.py"


def _activate() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    path = str(MEDIA_WORKER_ROOT)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)


def _maintenance():
    spec = importlib.util.spec_from_file_location(
        "rolling_cache_maintenance_test",
        MAINTENANCE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_segment(
    root: Path,
    *,
    epoch: str,
    source_id: str,
    name: str,
    pts_values: list[int],
    video_bytes: int = 2048,
    mtime_s: float | None = None,
) -> Path:
    directory = root / "midterm" / "epochs" / epoch / source_id / "segments" / name
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / "video.mov"
    metadata = directory / "metadata.json"
    video.write_bytes((name.encode("utf-8") or b"x") * max(1, video_bytes // len(name)))
    metadata.write_text(
        json.dumps(
            {
                "frames": [
                    {
                        "type": "VideoFrame",
                        "source_id": source_id,
                        "pts": pts,
                        "uuid": f"{name}-{index}",
                    }
                    for index, pts in enumerate(pts_values)
                ]
            }
        ),
        encoding="utf-8",
    )
    if mtime_s is not None:
        os.utime(video, (mtime_s, mtime_s))
        os.utime(metadata, (mtime_s, mtime_s))
    return directory


def _index(root: Path, **overrides):
    _activate()
    from app.segment_index import RollingSegmentIndex

    values = {
        "refresh_interval_s": 0.0,
        "reconcile_interval_s": 10_000.0,
        "stability_age_s": 0.0,
        "row_cache_max_entries": 8,
        "min_video_bytes": 1,
    }
    values.update(overrides)
    return RollingSegmentIndex(root, **values)


def test_initial_scan_is_epoch_fenced_and_steady_lookup_avoids_full_walk(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    _write_segment(
        root,
        epoch="epoch-b",
        source_id="camera-01",
        name="0002",
        pts_values=[3, 4],
    )
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-010",
        name="prefix-neighbor",
        pts_values=[7, 8],
    )
    _write_segment(
        root / "legacy",
        epoch="epoch-a",
        source_id="camera-01",
        name="legacy",
        pts_values=[5, 6],
    )
    index = _index(root, refresh_interval_s=60.0)
    walk_calls = 0
    original_walk = index._walk

    def counted_walk(path: Path):
        nonlocal walk_calls
        walk_calls += 1
        yield from original_walk(path)

    index._walk = counted_walk
    first = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    second = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")

    assert [segment.segment_id for segment in first] == ["0001"]
    assert second == first
    assert walk_calls == 1
    stats = index.snapshot()
    assert stats["misses"] == 1
    assert stats["hits"] == 1
    assert stats["initial_scans"] == 1
    assert stats["fallback_scans"] == 0


def test_incremental_refresh_parses_only_new_or_changed_segments(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    first_dir = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    index = _index(root)
    assert len(index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")) == 1
    initial_parses = int(index.snapshot()["metadata_parses"])

    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0002",
        pts_values=[3, 4],
    )
    segments_dir = first_dir.parent
    directory_stat = segments_dir.stat()
    os.utime(
        segments_dir,
        ns=(
            directory_stat.st_atime_ns,
            directory_stat.st_mtime_ns + 1_000_000_000,
        ),
    )
    segments = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    assert [segment.segment_id for segment in segments] == ["0001", "0002"]
    assert int(index.snapshot()["metadata_parses"]) == initial_parses + 1

    index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    assert int(index.snapshot()["metadata_parses"]) == initial_parses + 1
    assert int(index.snapshot()["refreshes"]) >= 2


def test_half_written_segment_waits_for_stable_metadata_and_video(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    clock = [100.0]
    directory = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
        mtime_s=100.0,
    )
    index = _index(
        root,
        stability_age_s=5.0,
        wall_clock=lambda: clock[0],
    )

    assert index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a") == []
    assert int(index.snapshot()["pending"]) == 1
    clock[0] = 106.0
    segments = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    assert [segment.directory for segment in segments] == [directory]


def test_unchanged_malformed_metadata_is_not_reparsed_each_task(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    directory = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-010",
        name="prefix-neighbor",
        pts_values=[3, 4],
    )
    metadata_path = directory / "metadata.json"
    metadata_path.write_text("{", encoding="utf-8")
    index = _index(root)

    assert index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a") == []
    parses = int(index.snapshot()["metadata_parses"])
    index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    assert int(index.snapshot()["metadata_parses"]) == parses

    metadata_path.write_text(
        json.dumps({"frames": [{"pts": 1}, {"pts": 2}]}),
        encoding="utf-8",
    )
    segments = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    assert [segment.segment_id for segment in segments] == ["0001"]
    assert int(index.snapshot()["metadata_parses"]) == parses + 1


def test_generation_change_invalidates_retention_deleted_entry(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    directory = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    index = _index(root, refresh_interval_s=60.0)
    assert len(index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")) == 1

    shutil.rmtree(directory)
    (root / ".rolling-cache-generation").write_text("1\n", encoding="utf-8")
    assert index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a") == []
    stats = index.snapshot()
    assert int(stats["reconciliations"]) == 1
    assert int(stats["stale_entries"]) >= 1


def test_parsed_row_cache_is_bounded_lru(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    for index_value in range(3):
        _write_segment(
            root,
            epoch="epoch-a",
            source_id="camera-01",
            name=f"000{index_value}",
            pts_values=[index_value * 2 + 1, index_value * 2 + 2],
        )
    index = _index(root, row_cache_max_entries=2)
    segments = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    assert len(segments) == 3
    assert int(index.snapshot()["row_cache_entries"]) == 2
    assert int(index.snapshot()["row_cache_evictions"]) >= 1

    evictions_before_reads = int(index.snapshot()["row_cache_evictions"])
    for segment in segments:
        assert len(index.rows_for_segment(segment)) == 2
    assert int(index.snapshot()["row_cache_entries"]) == 2
    assert int(index.snapshot()["row_cache_evictions"]) >= evictions_before_reads + 1


def test_read_pin_marker_is_bounded_and_removed_on_exit(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    index = _index(root, read_pin_ttl_s=30.0)
    segments = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")

    with index.pin_segments(segments) as pin:
        payload = json.loads(pin.marker_path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == "rolling-segment-read-pin-v1"
        assert payload["segments"] == [
            "midterm/epochs/epoch-a/camera-01/segments/0001"
        ]
        assert int(index.snapshot()["active_read_pins"]) == 1
    assert not pin.marker_path.exists()
    assert int(index.snapshot()["active_read_pins"]) == 0


def test_maintenance_honors_active_pin_then_advances_generation(tmp_path: Path) -> None:
    maintenance = _maintenance()
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
        mtime_s=10.0,
    )
    index = _index(root, read_pin_ttl_s=100.0)
    segments = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    assert len(segments) == 1

    with index.pin_segments(segments):
        pinned = maintenance.cleanup_once(
            root,
            retention_s=5.0,
            max_bytes=0,
            read_pin_ttl_s=100.0,
            stability_age_s=0.0,
            now_s=20.0,
        )
        assert pinned["retention_deleted"] == 0
        assert pinned["skipped_pinned"] == 1
        assert segments[0].directory.exists()

    deleted = maintenance.cleanup_once(
        root,
        retention_s=5.0,
        max_bytes=0,
        read_pin_ttl_s=100.0,
        stability_age_s=0.0,
        now_s=20.0,
    )
    assert deleted["retention_deleted"] == 1
    assert deleted["generation"] == 1
    assert not segments[0].directory.exists()


def test_expired_sigkill_pin_does_not_block_retention(tmp_path: Path) -> None:
    maintenance = _maintenance()
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
        mtime_s=10.0,
    )
    index = _index(root, read_pin_ttl_s=1.0)
    segments = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    pin = index.pin_segments(segments, ttl_s=1.0)
    pin.__enter__()
    payload = json.loads(pin.marker_path.read_text(encoding="utf-8"))

    result = maintenance.cleanup_once(
        root,
        retention_s=1.0,
        max_bytes=0,
        read_pin_ttl_s=1.0,
        stability_age_s=0.0,
        now_s=float(payload["expires_at_epoch_s"]) + 1.0,
    )
    assert result["expired_pins_removed"] == 1
    assert result["retention_deleted"] == 1
    assert not pin.marker_path.exists()
    pin.__exit__(None, None, None)


def test_byte_quota_deletes_oldest_unpinned_segments(tmp_path: Path) -> None:
    maintenance = _maintenance()
    root = tmp_path / "cache"
    oldest = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
        video_bytes=4096,
        mtime_s=10.0,
    )
    newest = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0002",
        pts_values=[3, 4],
        video_bytes=4096,
        mtime_s=11.0,
    )
    candidates = maintenance.discover_segments(
        root,
        now_s=20.0,
        stability_age_s=0.0,
    )
    newest_size = next(
        candidate.size_bytes
        for candidate in candidates
        if candidate.directory == newest.resolve(strict=False)
    )

    result = maintenance.cleanup_once(
        root,
        retention_s=0.0,
        max_bytes=newest_size,
        read_pin_ttl_s=100.0,
        stability_age_s=0.0,
        now_s=20.0,
    )
    assert result["quota_deleted"] == 1
    assert not oldest.exists()
    assert newest.exists()
    assert int(result["remaining_bytes"]) <= newest_size


def test_maintenance_owner_lock_allows_only_one_process_owner(tmp_path: Path) -> None:
    maintenance = _maintenance()
    root = tmp_path / "cache"
    first = maintenance._acquire_owner_lock(root)
    assert first is not None
    try:
        assert maintenance._acquire_owner_lock(root) is None
    finally:
        import fcntl

        fcntl.flock(first.fileno(), fcntl.LOCK_UN)
        first.close()
    second = maintenance._acquire_owner_lock(root)
    assert second is not None
    import fcntl

    fcntl.flock(second.fileno(), fcntl.LOCK_UN)
    second.close()


def test_catalog_failure_uses_observable_legacy_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-010",
        name="prefix-neighbor",
        pts_values=[3, 4],
    )
    index = _index(root)
    monkeypatch.setattr(
        index,
        "_rebuild",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("overflow")),
    )

    segments = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    assert [segment.segment_id for segment in segments] == ["0001"]
    assert int(index.snapshot()["fallback_scans"]) == 1
