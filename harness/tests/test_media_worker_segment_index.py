"""Incremental rolling segment index and read-pin contracts."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest


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
    _write_manifest(
        directory,
        epoch=epoch,
        source_id=source_id,
        name=name,
        pts_values=pts_values,
    )
    if mtime_s is not None:
        os.utime(video, (mtime_s, mtime_s))
        os.utime(metadata, (mtime_s, mtime_s))
        os.utime(directory / "segment_manifest.json", (mtime_s, mtime_s))
    return directory


def _write_manifest(
    directory: Path,
    *,
    epoch: str,
    source_id: str,
    name: str,
    pts_values: list[int],
) -> Path:
    metadata = directory / "metadata.json"
    video = directory / "video.mov"
    manifest = directory / "segment_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "rolling-segment-manifest-v1",
                "segment_id": name,
                "source_id": source_id,
                "runtime_epoch_id": epoch,
                "video_file": video.name,
                "metadata_file": metadata.name,
                "first_pts": min(pts_values),
                "last_pts": max(pts_values),
                "frame_count": len(pts_values),
                "source_first_pts": min(pts_values),
                "source_last_pts": max(pts_values),
                "video_size_bytes": video.stat().st_size,
                "metadata_size_bytes": metadata.stat().st_size,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _write_single_inode_segment(
    root: Path,
    *,
    epoch: str,
    source_id: str,
    name: str,
    pts_values: list[int],
    row_padding_bytes: int = 0,
) -> tuple[Path, list[dict]]:
    directory = root / "midterm" / "epochs" / epoch / source_id / "segments" / name
    directory.mkdir(parents=True, exist_ok=True)
    video = directory / "video.mov"
    metadata = directory / "metadata.json"
    manifest_path = directory / "segment_manifest.json"
    video.write_bytes((name.encode("utf-8") or b"x") * 512)
    rows = [
        {
            "type": "VideoFrame",
            "source_id": source_id,
            "pts": pts,
            "uuid": f"{name}-{index}",
            "padding": "x" * row_padding_bytes,
        }
        for index, pts in enumerate(pts_values)
    ]
    manifest = {
        "schema_version": "rolling-segment-manifest-v2",
        "segment_id": name,
        "source_id": source_id,
        "runtime_epoch_id": epoch,
        "video_file": video.name,
        "metadata_file": metadata.name,
        "first_pts": min(pts_values),
        "last_pts": max(pts_values),
        "frame_count": len(pts_values),
        "source_first_pts": min(pts_values),
        "source_last_pts": max(pts_values),
        "video_size_bytes": video.stat().st_size,
        "metadata_size_bytes": 0,
    }
    payload = b""
    for _attempt in range(10):
        payload = (
            json.dumps(manifest, separators=(",", ":")).encode("utf-8")
            + b"\n"
            + b"".join(
                json.dumps(row, separators=(",", ":")).encode("utf-8") + b"\n"
                for row in rows
            )
        )
        if manifest["metadata_size_bytes"] == len(payload):
            break
        manifest["metadata_size_bytes"] = len(payload)
    else:
        raise AssertionError("single-inode metadata size did not converge")
    metadata.write_bytes(payload)
    os.link(metadata, manifest_path)
    assert metadata.stat().st_ino == manifest_path.stat().st_ino
    return directory, rows


def _identity_payload(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _append_publication_record(directory: Path) -> Path:
    manifest_path = directory / "segment_manifest.json"
    metadata_path = directory / "metadata.json"
    video_path = directory / "video.mov"
    manifest = json.loads(
        manifest_path.read_text(encoding="utf-8").splitlines()[0]
    )
    record = {
        "schema_version": "rolling-segment-publication-v1",
        "source_id": manifest["source_id"],
        "runtime_epoch_id": manifest["runtime_epoch_id"],
        "segment_id": manifest["segment_id"],
        "manifest": manifest,
        "identities": {
            "manifest": _identity_payload(manifest_path),
            "metadata": _identity_payload(metadata_path),
            "video": _identity_payload(video_path),
        },
    }
    encoded = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    record["crc32"] = f"{zlib.crc32(encoded) & 0xFFFFFFFF:08x}"
    journal_path = directory.parent / ".segment-publications.jsonl"
    with journal_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
        handle.write("\n")
    return journal_path


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


def test_incremental_refresh_parses_only_new_or_changed_manifests(tmp_path: Path) -> None:
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
    initial_manifest_parses = int(index.snapshot()["manifest_parses"])
    assert int(index.snapshot()["full_row_parses"]) == 0

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
    assert int(index.snapshot()["manifest_parses"]) == initial_manifest_parses + 1
    assert int(index.snapshot()["full_row_parses"]) == 0

    index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    assert int(index.snapshot()["manifest_parses"]) == initial_manifest_parses + 1
    assert int(index.snapshot()["full_row_parses"]) == 0
    assert int(index.snapshot()["refreshes"]) >= 2


def test_publication_journal_discovers_new_segment_without_directory_enumeration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    first = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    _append_publication_record(first)
    index = _index(root)
    assert [
        segment.segment_id
        for segment in index.find_segments(
            source_id="camera-01",
            runtime_epoch_id="epoch-a",
            allow_fallback=False,
        )
    ] == ["0001"]
    manifest_parses_before = int(index.snapshot()["manifest_parses"])

    second = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0002",
        pts_values=[3, 4],
    )
    _append_publication_record(second)
    segments_dir = second.parent.resolve(strict=False)
    _activate()
    import app.segment_index as segment_index

    real_scandir = segment_index.os.scandir

    def reject_retention_directory_scan(path):
        if Path(path).resolve(strict=False) == segments_dir:
            raise AssertionError("steady refresh enumerated retained segment leaves")
        return real_scandir(path)

    monkeypatch.setattr(segment_index.os, "scandir", reject_retention_directory_scan)
    diagnostics = index.new_operation_diagnostics()
    segments = index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        allow_fallback=False,
        diagnostics=diagnostics,
    )

    assert [segment.segment_id for segment in segments] == ["0001", "0002"]
    assert int(index.snapshot()["manifest_parses"]) == manifest_parses_before
    assert diagnostics["segment_index_publication_records"] == 1


def test_single_inode_manifest_journal_is_bounded_and_frame_rows_stay_exact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    first = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    _append_publication_record(first)
    index = _index(root)
    assert [
        segment.segment_id
        for segment in index.find_segments(
            source_id="camera-01",
            runtime_epoch_id="epoch-a",
            allow_fallback=False,
        )
    ] == ["0001"]

    second, expected_rows = _write_single_inode_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0002",
        pts_values=list(range(3, 131)),
        row_padding_bytes=1024,
    )
    assert (second / "metadata.json").stat().st_size > 64 * 1024
    _append_publication_record(second)
    segments_dir = second.parent.resolve(strict=False)
    import app.segment_index as segment_index

    real_scandir = segment_index.os.scandir

    def reject_retention_directory_scan(path):
        if Path(path).resolve(strict=False) == segments_dir:
            raise AssertionError("steady refresh enumerated retained segment leaves")
        return real_scandir(path)

    monkeypatch.setattr(segment_index.os, "scandir", reject_retention_directory_scan)
    before = index.snapshot()
    segments = index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        allow_fallback=False,
    )
    after_discovery = index.snapshot()

    assert [segment.segment_id for segment in segments] == ["0001", "0002"]
    assert int(after_discovery["full_row_parses"]) == int(before["full_row_parses"])
    second_segment = next(segment for segment in segments if segment.segment_id == "0002")
    assert index.rows_for_segment(second_segment) == expected_rows
    assert all(
        row.get("schema_version") != "rolling-segment-manifest-v2"
        for row in index.rows_for_segment(second_segment)
    )
    after_rows = index.snapshot()
    assert int(after_rows["full_row_parses"]) == int(before["full_row_parses"]) + 1
    assert int(after_rows["publication_records"]) >= 1

    expected_identities = index._catalog_segment_identities(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        segments=[second_segment],
    )
    with (second / "metadata.json").open("ab") as handle:
        handle.write(b"{}\n")
    from app.segment_index import SegmentPinRetryableError

    with pytest.raises(SegmentPinRetryableError):
        with index.pin_segments(
            [second_segment],
            _expected_identities=expected_identities,
        ):
            pass


def test_single_inode_unjournaled_segment_is_recovered_by_manifest_reconcile(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    directory, expected_rows = _write_single_inode_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2, 3],
        row_padding_bytes=32 * 1024,
    )
    assert not (directory.parent / ".segment-publications.jsonl").exists()
    index = _index(root)

    segments = index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        allow_fallback=False,
    )

    assert [segment.segment_id for segment in segments] == ["0001"]
    assert index.rows_for_segment(segments[0]) == expected_rows
    snapshot = index.snapshot()
    assert int(snapshot["manifest_parses"]) == 1
    assert int(snapshot["full_row_parses"]) == 1
    assert int(snapshot["publication_records"]) == 0


def test_periodic_reconcile_recovers_unjournaled_publish_and_retention_delete(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    clock = [0.0]
    first = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    _append_publication_record(first)
    index = _index(
        root,
        refresh_interval_s=0.5,
        reconcile_interval_s=10.0,
        monotonic=lambda: clock[0],
    )
    assert [
        segment.segment_id
        for segment in index.find_segments(
            source_id="camera-01",
            runtime_epoch_id="epoch-a",
            allow_fallback=False,
        )
    ] == ["0001"]
    manifest_parses_before = int(index.snapshot()["manifest_parses"])

    # A periodic audit must consume the valid journal tail before enumerating
    # membership. That makes the journal-covered leaf known without parsing
    # its manifest during the audit. The unjournaled leaf simulates a sink
    # crash after atomic rename but before the best-effort journal append and
    # must still be recovered from the filesystem source of truth.
    journaled = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0002",
        pts_values=[3, 4],
    )
    _append_publication_record(journaled)
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0003",
        pts_values=[5, 6],
    )
    # Retention deletion must still prune an older journal/catalog entry in
    # the same membership audit.
    shutil.rmtree(first)
    (root / ".rolling-cache-generation").write_text("1\n", encoding="utf-8")
    clock[0] = 20.0

    segments = index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        allow_fallback=False,
    )

    assert [segment.segment_id for segment in segments] == ["0002", "0003"]
    snapshot = index.snapshot()
    assert int(snapshot["reconciliations"]) == 1
    assert int(snapshot["publication_records"]) == 1
    assert int(snapshot["publication_errors"]) == 0
    assert int(snapshot["manifest_parses"]) == manifest_parses_before + 1
    assert int(snapshot["full_row_parses"]) == 0


def test_corrupt_publication_record_falls_back_to_membership_reconcile(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    first = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    journal_path = _append_publication_record(first)
    index = _index(root)
    assert len(
        index.find_segments(
            source_id="camera-01",
            runtime_epoch_id="epoch-a",
            allow_fallback=False,
        )
    ) == 1

    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0002",
        pts_values=[3, 4],
    )
    with journal_path.open("ab") as handle:
        handle.write(b'{"schema_version":"truncated"}\n')
    diagnostics = index.new_operation_diagnostics()

    segments = index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        allow_fallback=False,
        diagnostics=diagnostics,
    )

    assert [segment.segment_id for segment in segments] == ["0001", "0002"]
    assert diagnostics["segment_index_publication_errors"] == 1
    assert diagnostics["segment_index_publication_reconciles"] == 1


def test_publication_journal_rotation_reconciles_without_losing_segment(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    first = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    journal_path = _append_publication_record(first)
    index = _index(root)
    assert len(
        index.find_segments(
            source_id="camera-01",
            runtime_epoch_id="epoch-a",
            allow_fallback=False,
        )
    ) == 1

    second = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0002",
        pts_values=[3, 4],
    )
    _append_publication_record(second)
    last_record = journal_path.read_bytes().splitlines(keepends=True)[-1]
    replacement = journal_path.with_suffix(".replacement")
    replacement.write_bytes(last_record)
    os.replace(replacement, journal_path)
    diagnostics = index.new_operation_diagnostics()

    segments = index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        allow_fallback=False,
        diagnostics=diagnostics,
    )

    assert [segment.segment_id for segment in segments] == ["0001", "0002"]
    assert diagnostics["segment_index_publication_records"] == 1
    assert diagnostics["segment_index_publication_errors"] == 1
    assert diagnostics["segment_index_publication_reconciles"] == 1


def test_changed_parent_reuses_known_immutable_leaf_membership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    directories = [
        _write_segment(
            root,
            epoch="epoch-a",
            source_id="camera-01",
            name=f"{sequence:04d}",
            pts_values=[sequence * 2 + 1, sequence * 2 + 2],
        )
        for sequence in range(32)
    ]
    index = _index(root)
    assert len(index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")) == 32

    segments_dir = directories[0].parent
    parent_stat = segments_dir.stat()
    new_directory = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0032",
        pts_values=[65, 66],
    )
    os.utime(
        segments_dir,
        ns=(parent_stat.st_atime_ns, parent_stat.st_mtime_ns + 1_000_000_000),
    )

    original_is_file = Path.is_file
    original_resolve = Path.resolve
    manifest_probes: list[Path] = []
    resolve_calls: list[Path] = []

    def counted_is_file(path: Path) -> bool:
        if path.name == "segment_manifest.json":
            manifest_probes.append(path)
        return original_is_file(path)

    def counted_resolve(path: Path, *args, **kwargs) -> Path:
        resolve_calls.append(path)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", counted_is_file)
    monkeypatch.setattr(Path, "resolve", counted_resolve)

    segments = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")

    assert [segment.segment_id for segment in segments] == [
        f"{sequence:04d}" for sequence in range(33)
    ]
    assert manifest_probes == [new_directory / "segment_manifest.json"]
    assert len(resolve_calls) <= 20


def test_selected_pin_identity_lookup_is_independent_of_retained_catalog_width(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    for sequence in range(128):
        _write_segment(
            root,
            epoch="epoch-a",
            source_id="camera-01",
            name=f"{sequence:04d}",
            pts_values=[sequence * 2 + 1, sequence * 2 + 2],
        )
    index = _index(root)
    segments = index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        allow_fallback=False,
    )
    selected = segments[60:66]
    catalog = index._catalogs[("camera-01", "epoch-a")]
    expected = {
        segment.directory.resolve(strict=False): (
            catalog.entries[
                segment.directory.resolve(strict=False)
                / "segment_manifest.json"
            ].metadata_identity,
            catalog.entries[
                segment.directory.resolve(strict=False)
                / "segment_manifest.json"
            ].video_identity,
        )
        for segment in selected
    }

    class CountingEntries(dict):
        value_visits = 0

        def values(self):
            for value in super().values():
                self.value_visits += 1
                yield value

    counting_entries = CountingEntries(catalog.entries)
    with catalog.lock:
        catalog.entries = counting_entries

    original_resolve = Path.resolve
    resolved_segment_directories: list[Path] = []

    def counted_resolve(path: Path, *args, **kwargs) -> Path:
        if path.parent.name == "segments":
            resolved_segment_directories.append(path)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counted_resolve)
    identities = index._catalog_segment_identities(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        segments=selected,
    )

    assert identities == expected
    assert counting_entries.value_visits <= len(selected)
    assert len(resolved_segment_directories) <= len(selected)


def test_periodic_reconcile_uses_incremental_membership_without_full_walk(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    clock = [0.0]
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    index = _index(
        root,
        refresh_interval_s=0.5,
        reconcile_interval_s=30.0,
        monotonic=lambda: clock[0],
    )
    walk_calls = 0
    original_walk = index._walk

    def counted_walk(path: Path):
        nonlocal walk_calls
        walk_calls += 1
        yield from original_walk(path)

    index._walk = counted_walk
    assert len(index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")) == 1
    assert walk_calls == 1

    clock[0] = 61.0
    assert len(index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")) == 1

    assert walk_calls == 1
    snapshot = index.snapshot()
    assert int(snapshot["initial_scans"]) == 1
    assert int(snapshot["reconciliations"]) == 1


def test_steady_lookup_does_not_restat_known_immutable_payloads(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    clock = [0.0]
    directories = [
        _write_segment(
            root,
            epoch="epoch-a",
            source_id="camera-01",
            name=f"{index_value:04d}",
            pts_values=[index_value * 2 + 1, index_value * 2 + 2],
        )
        for index_value in range(12)
    ]
    index = _index(
        root,
        refresh_interval_s=0.5,
        reconcile_interval_s=30.0,
        monotonic=lambda: clock[0],
    )
    assert len(index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")) == 12
    before = index.snapshot()
    catalog = index._catalogs[("camera-01", "epoch-a")]

    assert all(directory.resolve(strict=False) not in catalog.containers for directory in directories)

    clock[0] = 1.0
    assert len(index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")) == 12
    after = index.snapshot()

    # Root generation plus the source/segments discovery containers are the
    # only instrumented stats needed when immutable membership is unchanged.
    assert int(after["stat_calls"]) - int(before["stat_calls"]) <= 5
    assert int(after["manifest_parses"]) == int(before["manifest_parses"])
    assert int(after["full_row_parses"]) == 0


def test_discovery_uses_compact_manifests_and_parses_only_selected_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    for index_value in range(4):
        _write_segment(
            root,
            epoch="epoch-a",
            source_id="camera-01",
            name=f"000{index_value}",
            pts_values=[index_value * 2 + 1, index_value * 2 + 2],
        )
    index = _index(root)

    segments = index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
    )
    after_discovery = index.snapshot()

    assert len(segments) == 4
    assert int(after_discovery["manifest_parses"]) == 4
    assert int(after_discovery["full_row_parses"]) == 0
    assert int(after_discovery["row_cache_entries"]) == 0

    assert len(index.rows_for_segment(segments[2])) == 2
    after_selected = index.snapshot()
    assert int(after_selected["manifest_parses"]) == 4
    assert int(after_selected["full_row_parses"]) == 1
    assert int(after_selected["row_cache_entries"]) == 1


def test_segment_without_compact_manifest_does_not_trigger_full_row_fallback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    directory = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    (directory / "segment_manifest.json").unlink()
    index = _index(root)

    assert index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
    ) == []
    snapshot = index.snapshot()
    assert int(snapshot["manifest_parses"]) == 0
    assert int(snapshot["full_row_parses"]) == 0


def test_operation_diagnostics_cover_index_and_pin_timing(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    index = _index(root)
    diagnostics = index.new_operation_diagnostics()

    with index.pin_source_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        diagnostics=diagnostics,
    ) as segments:
        assert len(segments) == 1
        assert len(index.rows_for_segment(segments[0])) == 2

    expected_timings = {
        "segment_index_io_slot_wait_ms",
        "segment_index_lock_wait_ms",
        "segment_index_lock_hold_ms",
        "segment_index_refresh_ms",
        "segment_index_rebuild_ms",
        "segment_index_stat_ms",
        "segment_index_full_row_parse_ms",
        "segment_index_manifest_parse_ms",
        "segment_index_sort_ms",
        "segment_index_mutation_lock_wait_ms",
        "segment_index_pin_publish_ms",
        "segment_index_pin_release_ms",
    }
    assert expected_timings <= diagnostics.keys()
    assert all(float(diagnostics[name]) >= 0 for name in expected_timings)
    assert int(diagnostics["segment_index_full_row_parses"]) >= 1
    assert int(diagnostics["segment_index_manifest_parses"]) >= 1
    assert int(diagnostics["segment_index_stat_calls"]) >= 2
    assert int(diagnostics["segment_index_scanned_known"]) >= 1
    assert int(diagnostics["segment_index_new_or_changed"]) >= 1

    snapshot = index.snapshot()
    assert float(snapshot["lock_wait_ms_total"]) >= float(
        diagnostics["segment_index_lock_wait_ms"]
    )
    assert float(snapshot["full_row_parse_ms_total"]) >= float(
        diagnostics["segment_index_full_row_parse_ms"]
    )
    assert int(snapshot["full_row_parses"]) >= 1
    assert int(snapshot["manifest_parses"]) >= 1
    assert int(snapshot["stat_calls"]) >= 2


def test_source_window_pin_publishes_only_bounded_segment_subset(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    for sequence in range(8):
        _write_segment(
            root,
            epoch="epoch-a",
            source_id="camera-01",
            name=f"{sequence:04d}",
            pts_values=[sequence * 100, sequence * 100 + 99],
        )
    index = _index(root)
    diagnostics = index.new_operation_diagnostics()

    with index.pin_source_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        requested_source_start_pts=250,
        requested_source_end_pts=450,
        diagnostics=diagnostics,
    ) as segments:
        assert [segment.segment_id for segment in segments] == [
            "0001",
            "0002",
            "0003",
            "0004",
            "0005",
        ]
        marker_paths = list((root / ".read-pins").glob("*.json"))
        assert len(marker_paths) == 1
        marker = json.loads(marker_paths[0].read_text(encoding="utf-8"))
        assert [Path(path).name for path in marker["segments"]] == [
            "0001",
            "0002",
            "0003",
            "0004",
            "0005",
        ]
        assert int(diagnostics["segment_index_pinned_segments"]) == 5

    assert list((root / ".read-pins").glob("*.json")) == []


def test_source_window_pin_falls_back_to_full_catalog_for_legacy_bounds(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    for sequence in range(3):
        directory = _write_segment(
            root,
            epoch="epoch-a",
            source_id="camera-01",
            name=f"{sequence:04d}",
            pts_values=[sequence * 100, sequence * 100 + 99],
        )
        if sequence == 1:
            manifest_path = directory / "segment_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("source_first_pts")
            manifest.pop("source_last_pts")
            manifest_path.write_text(
                json.dumps(manifest, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
    index = _index(root)
    diagnostics = index.new_operation_diagnostics()

    with index.pin_source_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        requested_source_start_pts=100,
        requested_source_end_pts=199,
        diagnostics=diagnostics,
    ) as segments:
        assert [segment.segment_id for segment in segments] == [
            "0000",
            "0001",
            "0002",
        ]
        assert int(diagnostics["segment_index_pinned_segments"]) == 3


@pytest.mark.parametrize(
    ("requested_start_pts", "requested_end_pts"),
    [(250, 250), (450, 350), (1_000, 1_100)],
)
def test_source_window_pin_falls_back_for_invalid_or_unmatched_windows(
    tmp_path: Path,
    requested_start_pts: int,
    requested_end_pts: int,
) -> None:
    root = tmp_path / "cache"
    for sequence in range(3):
        _write_segment(
            root,
            epoch="epoch-a",
            source_id="camera-01",
            name=f"{sequence:04d}",
            pts_values=[sequence * 100, sequence * 100 + 99],
        )
    index = _index(root)

    with index.pin_source_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        requested_source_start_pts=requested_start_pts,
        requested_source_end_pts=requested_end_pts,
    ) as segments:
        assert [segment.segment_id for segment in segments] == [
            "0000",
            "0001",
            "0002",
        ]


def test_index_io_admission_bounds_concurrent_discovery_and_pin(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    source_ids = ["camera-a", "camera-b", "camera-c"]
    for sequence, source_id in enumerate(source_ids):
        _write_segment(
            root,
            epoch="epoch-a",
            source_id=source_id,
            name="0001",
            pts_values=[sequence * 2 + 1, sequence * 2 + 2],
        )
    index = _index(root, io_concurrency=2)
    original_rebuild = index._rebuild
    allow_rebuild = threading.Event()
    two_entered = threading.Event()
    state_lock = threading.Lock()
    active = 0
    peak = 0
    diagnostics = {
        source_id: index.new_operation_diagnostics() for source_id in source_ids
    }
    errors: list[BaseException] = []

    def slow_rebuild(*args, **kwargs):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_entered.set()
        try:
            assert allow_rebuild.wait(timeout=2.0)
            return original_rebuild(*args, **kwargs)
        finally:
            with state_lock:
                active -= 1

    index._rebuild = slow_rebuild

    def pin(source_id: str) -> None:
        try:
            with index.pin_source_segments(
                source_id=source_id,
                runtime_epoch_id="epoch-a",
                diagnostics=diagnostics[source_id],
            ):
                pass
        except BaseException as exc:  # retain thread failures for the test
            errors.append(exc)

    threads = [threading.Thread(target=pin, args=(source_id,)) for source_id in source_ids]
    for thread in threads:
        thread.start()
    assert two_entered.wait(timeout=1.0)
    time.sleep(0.03)
    with state_lock:
        assert active == 2
        assert peak == 2
    allow_rebuild.set()
    for thread in threads:
        thread.join(timeout=2.0)
        assert not thread.is_alive()

    assert errors == []
    assert peak == 2
    assert max(
        float(item["segment_index_io_slot_wait_ms"])
        for item in diagnostics.values()
    ) >= 20.0


def test_operation_diagnostics_measure_global_lock_contention(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    index = _index(root)
    diagnostics = index.new_operation_diagnostics()
    started = threading.Event()
    finished = threading.Event()

    def lookup() -> None:
        started.set()
        index.find_segments(
            source_id="camera-01",
            runtime_epoch_id="epoch-a",
            diagnostics=diagnostics,
        )
        finished.set()

    with index._lock:
        thread = threading.Thread(target=lookup)
        thread.start()
        assert started.wait(timeout=1.0)
        time.sleep(0.03)
        assert not finished.is_set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert float(diagnostics["segment_index_lock_wait_ms"]) >= 20.0


def test_slow_source_refresh_does_not_block_other_source_or_snapshot(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    source_a = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-a",
        name="0001",
        pts_values=[1, 2],
    )
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-b",
        name="0001",
        pts_values=[3, 4],
    )
    index = _index(root)
    assert len(index.find_segments(source_id="camera-a", runtime_epoch_id="epoch-a")) == 1
    assert len(index.find_segments(source_id="camera-b", runtime_epoch_id="epoch-a")) == 1

    new_source_a = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-a",
        name="0002",
        pts_values=[5, 6],
    )
    manifest_a = new_source_a / "segment_manifest.json"
    segments_dir = source_a.parent
    segments_stat = segments_dir.stat()
    os.utime(
        segments_dir,
        ns=(segments_stat.st_atime_ns, segments_stat.st_mtime_ns + 1_000_000_000),
    )
    refresh_entered = threading.Event()
    allow_refresh = threading.Event()
    source_b_finished = threading.Event()
    snapshot_finished = threading.Event()
    original_parse = index._parse_manifest

    def slow_parse(path: Path):
        if path == manifest_a.resolve(strict=False):
            refresh_entered.set()
            assert allow_refresh.wait(timeout=2.0)
        return original_parse(path)

    index._parse_manifest = slow_parse
    source_a_thread = threading.Thread(
        target=lambda: index.find_segments(
            source_id="camera-a",
            runtime_epoch_id="epoch-a",
        )
    )
    source_a_thread.start()
    assert refresh_entered.wait(timeout=1.0)
    catalog_a = index._catalogs[("camera-a", "epoch-a")]
    assert catalog_a.lock.acquire(timeout=0.1)
    catalog_a.lock.release()

    source_b_thread = threading.Thread(
        target=lambda: (
            index.find_segments(
                source_id="camera-b",
                runtime_epoch_id="epoch-a",
            ),
            source_b_finished.set(),
        )
    )
    snapshot_thread = threading.Thread(
        target=lambda: (index.snapshot(), snapshot_finished.set())
    )
    source_b_thread.start()
    snapshot_thread.start()
    try:
        assert source_b_finished.wait(timeout=0.25)
        assert snapshot_finished.wait(timeout=0.25)
    finally:
        allow_refresh.set()
    for thread in (source_a_thread, source_b_thread, snapshot_thread):
        thread.join(timeout=2.0)
        assert not thread.is_alive()


def test_catalog_refresh_publishes_copy_on_write_version(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    first = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    index = _index(root)
    index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    catalog = index._catalogs[("camera-01", "epoch-a")]
    entries_before = catalog.entries
    version_before = catalog.version

    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0002",
        pts_values=[3, 4],
    )
    parent = first.parent
    parent_stat = parent.stat()
    os.utime(
        parent,
        ns=(parent_stat.st_atime_ns, parent_stat.st_mtime_ns + 1_000_000_000),
    )
    segments = index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
    )

    assert [segment.segment_id for segment in segments] == ["0001", "0002"]
    assert catalog.entries is not entries_before
    assert catalog.version > version_before


def test_stale_catalog_publish_is_rejected_by_version_check(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    index = _index(root)
    index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    catalog = index._catalogs[("camera-01", "epoch-a")]
    first, first_version = index._copy_catalog(catalog)
    stale, stale_version = index._copy_catalog(catalog)

    assert first_version == stale_version
    assert index._publish_catalog(
        catalog,
        first,
        expected_version=first_version,
    )
    assert not index._publish_catalog(
        catalog,
        stale,
        expected_version=stale_version,
    )
    assert int(index.snapshot()["catalog_publish_conflicts"]) == 1


def test_concurrent_same_catalog_refresh_is_singleflight(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    first = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    clock = [0.0]
    index = _index(
        root,
        refresh_interval_s=0.5,
        monotonic=lambda: clock[0],
    )
    index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    new_directory = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0002",
        pts_values=[3, 4],
    )
    parent = first.parent
    parent_stat = parent.stat()
    os.utime(
        parent,
        ns=(parent_stat.st_atime_ns, parent_stat.st_mtime_ns + 1_000_000_000),
    )
    clock[0] = 1.0
    refreshes_before = int(index.snapshot()["refreshes"])
    parses_before = int(index.snapshot()["manifest_parses"])
    new_manifest = (new_directory / "segment_manifest.json").resolve(strict=False)
    parse_entered = threading.Event()
    duplicate_parse = threading.Event()
    allow_parse = threading.Event()
    parse_lock = threading.Lock()
    parse_calls = 0
    original_parse = index._parse_manifest

    def slow_parse(path: Path):
        nonlocal parse_calls
        if path == new_manifest:
            with parse_lock:
                parse_calls += 1
                if parse_calls > 1:
                    duplicate_parse.set()
            parse_entered.set()
            assert allow_parse.wait(timeout=2.0)
        return original_parse(path)

    index._parse_manifest = slow_parse
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def lookup() -> None:
        try:
            segments = index.find_segments(
                source_id="camera-01",
                runtime_epoch_id="epoch-a",
                allow_fallback=False,
            )
            results.append([segment.segment_id for segment in segments])
        except BaseException as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    first_thread = threading.Thread(target=lookup)
    second_thread = threading.Thread(target=lookup)
    first_thread.start()
    assert parse_entered.wait(timeout=1.0)
    second_thread.start()
    duplicate_observed = duplicate_parse.wait(timeout=0.2)
    clock[0] = 2.0
    allow_parse.set()
    threads = (first_thread, second_thread)
    for thread in threads:
        thread.join(timeout=2.0)
        assert not thread.is_alive()

    assert errors == []
    assert not duplicate_observed
    assert parse_calls == 1
    assert sorted(results) == [["0001", "0002"], ["0001", "0002"]]
    snapshot = index.snapshot()
    assert int(snapshot["refreshes"]) == refreshes_before + 1
    assert int(snapshot["manifest_parses"]) == parses_before + 1
    assert int(snapshot["catalog_publish_conflicts"]) == 0


def test_parsed_row_cache_is_bounded_by_entries_and_bytes(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    for index_value in range(4):
        _write_segment(
            root,
            epoch="epoch-a",
            source_id="camera-01",
            name=f"000{index_value}",
            pts_values=list(range(index_value * 50, index_value * 50 + 50)),
        )
    index = _index(
        root,
        row_cache_max_entries=10,
        row_cache_max_bytes=1_000,
    )

    segments = index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
    )
    for segment in segments:
        index.rows_for_segment(segment)
    snapshot = index.snapshot()

    assert int(snapshot["row_cache_entries"]) < 4
    assert int(snapshot["row_cache_bytes"]) <= 1_000
    assert int(snapshot["row_cache_byte_evictions"]) >= 1


def test_index_retains_compact_source_clock_bounds_after_row_cache_eviction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    for index_value in range(4):
        _write_segment(
            root,
            epoch="epoch-a",
            source_id="camera-01",
            name=f"000{index_value}",
            pts_values=[index_value * 10 + 1, index_value * 10 + 9],
        )
    index = _index(root, row_cache_max_entries=1)

    segments = index.find_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
    )

    assert [(segment.source_first_pts, segment.source_last_pts) for segment in segments] == [
        (1, 9),
        (11, 19),
        (21, 29),
        (31, 39),
    ]
    assert int(index.snapshot()["row_cache_entries"]) == 0


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


def test_unchanged_malformed_manifest_is_not_reparsed_each_task(tmp_path: Path) -> None:
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
    manifest_path = directory / "segment_manifest.json"
    manifest_path.write_text("{", encoding="utf-8")
    index = _index(root)

    assert index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a") == []
    parses = int(index.snapshot()["manifest_parses"])
    index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    assert int(index.snapshot()["manifest_parses"]) == parses
    assert int(index.snapshot()["full_row_parses"]) == 0

    _write_manifest(
        directory,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    segments = index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")
    assert [segment.segment_id for segment in segments] == ["0001"]
    assert int(index.snapshot()["manifest_parses"]) == parses + 1
    assert int(index.snapshot()["full_row_parses"]) == 0


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
    walk_calls = 0
    original_walk = index._walk

    def counted_walk(path: Path):
        nonlocal walk_calls
        walk_calls += 1
        yield from original_walk(path)

    index._walk = counted_walk
    assert len(index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a")) == 1
    assert walk_calls == 1

    shutil.rmtree(directory)
    (root / ".rolling-cache-generation").write_text("1\n", encoding="utf-8")
    assert index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a") == []
    stats = index.snapshot()
    assert int(stats["refreshes"]) == 1
    assert int(stats["reconciliations"]) == 0
    assert int(stats["stale_entries"]) >= 1
    assert walk_calls == 1

    # The refresh acknowledges the new root generation.  A second lookup must
    # not repeat either refresh or full reconciliation.
    assert index.find_segments(source_id="camera-01", runtime_epoch_id="epoch-a") == []
    repeated = index.snapshot()
    assert int(repeated["refreshes"]) == 1
    assert int(repeated["reconciliations"]) == 0
    assert walk_calls == 1


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
    assert int(index.snapshot()["row_cache_entries"]) == 0
    assert int(index.snapshot()["row_cache_evictions"]) == 0

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


def test_source_refresh_and_pin_publication_are_atomic_against_retention(
    tmp_path: Path,
) -> None:
    maintenance = _maintenance()
    root = tmp_path / "cache"
    directory = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
        mtime_s=10.0,
    )
    index = _index(root, read_pin_ttl_s=100.0)
    cleanup_started = threading.Event()
    cleanup_finished = threading.Event()
    cleanup_result: dict[str, object] = {}
    cleanup_thread: threading.Thread | None = None
    original_find = index.find_segments

    def cleanup() -> None:
        cleanup_started.set()
        cleanup_result.update(
            maintenance.cleanup_once(
                root,
                retention_s=5.0,
                max_bytes=0,
                read_pin_ttl_s=100.0,
                stability_age_s=0.0,
                now_s=20.0,
            )
        )
        cleanup_finished.set()

    def find_then_start_cleanup(**kwargs):
        nonlocal cleanup_thread
        segments = original_find(**kwargs)
        cleanup_thread = threading.Thread(target=cleanup)
        cleanup_thread.start()
        assert cleanup_started.wait(timeout=1.0)
        assert not cleanup_finished.wait(timeout=0.05)
        return segments

    index.find_segments = find_then_start_cleanup
    with index.pin_source_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
    ) as pinned_segments:
        assert [segment.directory for segment in pinned_segments] == [directory]
        assert cleanup_thread is not None
        cleanup_thread.join(timeout=2.0)
        assert not cleanup_thread.is_alive()
        assert cleanup_result["retention_deleted"] == 0
        assert cleanup_result["skipped_pinned"] == 1
        assert directory.exists()

    deleted = maintenance.cleanup_once(
        root,
        retention_s=5.0,
        max_bytes=0,
        read_pin_ttl_s=100.0,
        stability_age_s=0.0,
        now_s=20.0,
    )
    assert deleted["retention_deleted"] == 1
    assert not directory.exists()


def test_source_window_pin_protects_selected_leaves_during_retention(
    tmp_path: Path,
) -> None:
    maintenance = _maintenance()
    root = tmp_path / "cache"
    directories = []
    for sequence in range(6):
        directories.append(
            _write_segment(
                root,
                epoch="epoch-a",
                source_id="camera-01",
                name=f"{sequence:04d}",
                pts_values=[sequence * 100, sequence * 100 + 99],
                mtime_s=10.0,
            )
        )
    index = _index(root, read_pin_ttl_s=100.0)

    with index.pin_source_segments(
        source_id="camera-01",
        runtime_epoch_id="epoch-a",
        requested_source_start_pts=250,
        requested_source_end_pts=350,
    ) as pinned_segments:
        assert [segment.segment_id for segment in pinned_segments] == [
            "0001",
            "0002",
            "0003",
            "0004",
        ]
        cleanup = maintenance.cleanup_once(
            root,
            retention_s=5.0,
            max_bytes=0,
            read_pin_ttl_s=100.0,
            stability_age_s=0.0,
            now_s=20.0,
        )
        assert cleanup["retention_deleted"] == 2
        assert cleanup["skipped_pinned"] == 4
        assert not directories[0].exists()
        assert not directories[5].exists()
        assert all(directory.exists() for directory in directories[1:5])


def test_maintenance_tree_discovery_does_not_hold_mutation_lock(
    tmp_path: Path,
) -> None:
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
    discovery_entered = threading.Event()
    allow_discovery = threading.Event()
    cleanup_finished = threading.Event()
    original_discover = maintenance.discover_segments

    def slow_discover(*args, **kwargs):
        discovery_entered.set()
        assert allow_discovery.wait(timeout=2.0)
        return original_discover(*args, **kwargs)

    maintenance.discover_segments = slow_discover

    def cleanup() -> None:
        maintenance.cleanup_once(
            root,
            retention_s=5.0,
            max_bytes=0,
            read_pin_ttl_s=100.0,
            stability_age_s=0.0,
            now_s=20.0,
        )
        cleanup_finished.set()

    thread = threading.Thread(target=cleanup)
    thread.start()
    assert discovery_entered.wait(timeout=1.0)
    with index.pin_segments(segments):
        assert int(index.snapshot()["active_read_pins"]) == 1
        assert not cleanup_finished.is_set()
        allow_discovery.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert cleanup_finished.is_set()


def test_source_pin_reports_concurrent_writer_change_as_retryable(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    directory = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    index = _index(root)
    original_find = index.find_segments

    def find_then_grow_video(**kwargs):
        segments = original_find(**kwargs)
        with (directory / "video.mov").open("ab") as video:
            video.write(b"writer-grew-current-segment")
        return segments

    index.find_segments = find_then_grow_video
    from app.segment_index import SegmentPinRetryableError

    with pytest.raises(SegmentPinRetryableError, match="identity changed"):
        with index.pin_source_segments(
            source_id="camera-01",
            runtime_epoch_id="epoch-a",
        ):
            raise AssertionError("pin should not activate for a changed segment")


def test_source_pin_fences_same_size_atomic_payload_replacement(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    directory = _write_segment(
        root,
        epoch="epoch-a",
        source_id="camera-01",
        name="0001",
        pts_values=[1, 2],
    )
    index = _index(root)
    original_find = index.find_segments

    def find_then_replace_video(**kwargs):
        segments = original_find(**kwargs)
        video = directory / "video.mov"
        original = video.stat()
        replacement = directory / ".video.mov.replacement"
        replacement.write_bytes(b"z" * original.st_size)
        os.utime(
            replacement,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )
        os.replace(replacement, video)
        return segments

    index.find_segments = find_then_replace_video
    from app.segment_index import SegmentPinRetryableError

    with pytest.raises(SegmentPinRetryableError, match="identity changed"):
        with index.pin_source_segments(
            source_id="camera-01",
            runtime_epoch_id="epoch-a",
        ):
            raise AssertionError("pin should fence a same-size inode replacement")


def test_image_lane_defers_retryable_segment_pin_failures() -> None:
    source = (MEDIA_WORKER_ROOT / "app" / "worker.py").read_text(encoding="utf-8")
    legacy = source.split("def _process_rolling_cache_image_tasks", 1)[1]
    legacy = legacy.split("class _ImageFlightV2", 1)[0]
    v2 = source.split("def _run_rolling_image_job_v2", 1)[1]
    v2 = v2.split("def _run_snapshot_job_v2", 1)[0]

    for path in (legacy, v2):
        retryable = path.index("except SegmentPinRetryableError as exc:")
        extraction_retryable = path.index("except ImageExtractionRetryableError as exc:")
        coverage_retryable = path.index("except RollingCacheCoverageMiss as exc:")
        assert retryable < extraction_retryable < coverage_retryable
        assert "temporary_io_error:{exc}" in path[retryable:coverage_retryable]
        assert "_defer_rolling_cache_task" in path[retryable:coverage_retryable]
        coverage_body = path[coverage_retryable:]
        assert "_rolling_cache_coverage_retry_after_s(exc)" in coverage_body
        assert "_defer_rolling_cache_task" in coverage_body
        assert "_mark_image_evidence_failed" not in coverage_body.split(
            "except Exception as exc:", 1
        )[0]


def test_video_lane_defers_retryable_segment_pin_failures() -> None:
    source = (MEDIA_WORKER_ROOT / "app" / "worker.py").read_text(encoding="utf-8")
    video_lane = source.split("def _process_rolling_cache_tasks", 1)[1]
    video_lane = video_lane.split("def _process_rolling_cache_image_tasks", 1)[0]

    assert video_lane.count("except SegmentPinRetryableError as exc:") == 2
    assert video_lane.count('reason=f"temporary_io_error:{exc}"') == 2
    for section in video_lane.split("except SegmentPinRetryableError as exc:")[1:]:
        before_coverage = section.split("except RollingCacheCoverageMiss as exc:", 1)[0]
        assert "_defer_rolling_cache_task_safely" in before_coverage
        assert "retry_after_s=1.0" in before_coverage

    scheduler_v2 = source.split("def _drain_completed", 1)[1]
    scheduler_v2 = scheduler_v2.split("def shutdown", 1)[0]
    pin_retry = scheduler_v2.index("except SegmentPinRetryableError as exc:")
    coverage_retry = scheduler_v2.index("except RollingCacheCoverageMiss as exc:")
    assert pin_retry < coverage_retry
    assert "_defer_rolling_cache_task_safely" in scheduler_v2[
        pin_retry:coverage_retry
    ]
    assert 'reason=f"temporary_io_error:{exc}"' in scheduler_v2[
        pin_retry:coverage_retry
    ]


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
