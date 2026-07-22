from __future__ import annotations

import json
import multiprocessing
import sys
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = SERVICE_ROOT / "app"
REPO_ROOT = SERVICE_ROOT.parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from config import EpochResolver, SinkConfig, safe_component  # noqa: E402
from gst_sink import SourcePipeline  # noqa: E402
from observability import HealthState, SinkMetrics  # noqa: E402
import publishing  # noqa: E402
from publishing import AtomicSegmentPublisher, FragmentLedger  # noqa: E402


def _publisher(tmp_path: Path) -> AtomicSegmentPublisher:
    return AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s0123456789abcdef",
    )


def _hold_exclusive_flock(
    lock_path: str,
    acquired: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    import fcntl

    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        acquired.set()
        try:
            if not release.wait(timeout=5):
                raise TimeoutError("test_flock_release_timeout")
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def test_atomic_publication_matches_media_worker_layout_and_native_jsonl(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    fragment = publisher.prepare(7)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.extend(
        [
            {
                "type": "VideoFrame",
                "source_id": "camera-01",
                "pts": 1_000_000_000,
                "uuid": "frame-1",
                "objects": [],
            },
            {"source_id": "camera-01", "schema": "EndOfStream"},
        ]
    )

    final_dir = publisher.publish(fragment)

    assert final_dir.relative_to(tmp_path / "cache").parts == (
        "midterm",
        "epochs",
        "epoch-a",
        "camera-01",
        "segments",
        "s0123456789abcdef-00000007",
    )
    assert (final_dir / "video.mov").read_bytes() == b"encoded-h264-in-mov" * 128
    rows = [
        json.loads(line)
        for line in (final_dir / "metadata.json")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows == fragment.rows
    manifest = json.loads(
        (final_dir / "segment_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest == {
        "schema_version": "rolling-segment-manifest-v1",
        "segment_id": "s0123456789abcdef-00000007",
        "source_id": "camera-01",
        "runtime_epoch_id": "epoch-a",
        "video_file": "video.mov",
        "metadata_file": "metadata.json",
        "first_pts": 1_000_000_000,
        "last_pts": 1_000_000_000,
        "frame_count": 1,
        "source_first_pts": 1_000_000_000,
        "source_last_pts": 1_000_000_000,
        "video_size_bytes": (final_dir / "video.mov").stat().st_size,
        "metadata_size_bytes": (final_dir / "metadata.json").stat().st_size,
    }
    assert not fragment.staging_dir.exists()
    assert list((tmp_path / "cache").rglob("*.partial")) == []


def test_publication_phase_timings_account_for_measured_and_hidden_time() -> None:
    import publishing

    clock_values = iter(
        (
            0,
            1_000_000,
            4_000_000,
            5_000_000,
            10_000_000,
            12_000_000,
        )
    )
    timings = publishing._PublicationPhaseTimings(
        clock_ns=lambda: next(clock_values)
    )

    with timings.measure("metadata_write_ms"):
        pass
    with timings.measure("metadata_fsync_ms"):
        pass

    assert timings.finish() == {
        "metadata_write_ms": 3.0,
        "metadata_fsync_ms": 5.0,
        "publish_total_ms": 12.0,
        "publish_accounted_ms": 8.0,
        "publish_unattributed_ms": 4.0,
    }


def test_atomic_publication_exposes_complete_phase_diagnostics(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    fragment = publisher.prepare(8)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.extend(
        [
            {"source_id": "camera-01", "pts": 10},
            {"source_id": "camera-01", "pts": 20},
        ]
    )

    publisher.publish(fragment)

    diagnostics = fragment.publication_diagnostics
    assert diagnostics["schema_version"] == "rolling-segment-publication-timing-v1"
    assert diagnostics["first_pts"] == 10
    assert diagnostics["last_pts"] == 20
    expected_phases = {
        "publish_validate_ms",
        "publish_metadata_write_ms",
        "publish_metadata_fsync_ms",
        "publish_metadata_stat_ms",
        "publish_manifest_write_ms",
        "publish_manifest_fsync_ms",
        "publish_manifest_stat_ms",
        "publish_commit_lock_wait_ms",
        "publish_commit_lock_hold_ms",
        "publish_commit_wall_ms",
        "publish_staging_dir_fsync_ms",
        "publish_parent_prepare_ms",
        "publish_rename_ms",
        "publish_parent_dir_fsync_ms",
        "publish_journal_append_ms",
        "publish_total_ms",
        "publish_accounted_ms",
        "publish_unattributed_ms",
    }
    assert expected_phases <= diagnostics.keys()
    assert all(float(diagnostics[name]) >= 0 for name in expected_phases)
    assert diagnostics["publish_total_ms"] == pytest.approx(
        diagnostics["publish_accounted_ms"]
        + diagnostics["publish_unattributed_ms"],
        abs=0.001,
    )


def test_atomic_publication_stages_before_the_unchanged_durable_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _publisher(tmp_path)
    fragment = publisher.prepare(9)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.extend(
        [
            {"source_id": "camera-01", "pts": 10},
            {"source_id": "camera-01", "pts": 20},
        ]
    )
    events: list[tuple[str, str]] = []
    real_fsync = publishing.os.fsync
    real_replace = publishing.os.replace

    def record_fsync(fd: int) -> None:
        events.append(("fsync", str(Path(f"/proc/self/fd/{fd}").resolve())))
        real_fsync(fd)

    def record_replace(source, destination) -> None:
        events.append(("replace", f"{source}->{destination}"))
        real_replace(source, destination)

    monkeypatch.setattr(publishing.os, "fsync", record_fsync)
    monkeypatch.setattr(publishing.os, "replace", record_replace)

    staged = publisher.stage_publication(fragment)

    assert events == []
    assert fragment.staging_dir.is_dir()
    assert (fragment.staging_dir / "metadata.json").is_file()
    assert (fragment.staging_dir / "segment_manifest.json").is_file()
    assert not fragment.final_dir.exists()

    final_dir = publisher.commit_publication(staged)

    assert final_dir == fragment.final_dir
    assert final_dir.is_dir()
    assert [kind for kind, _value in events] == [
        "fsync",
        "fsync",
        "fsync",
        "replace",
        "fsync",
    ]
    assert Path(events[0][1]).name == "metadata.json"
    assert Path(events[1][1]).name == "segment_manifest.json"
    assert Path(events[2][1]).name.endswith(".partial")
    assert Path(events[4][1]).name == "segments"
    assert (final_dir / "metadata.json").stat().st_ino != (
        final_dir / "segment_manifest.json"
    ).stat().st_ino
    assert fragment.publication_diagnostics["publish_stage_ms"] >= 0
    assert fragment.publication_diagnostics["publish_commit_ms"] >= 0
    assert fragment.publication_diagnostics["publish_file_sync_mode"] == "fsync"
    assert fragment.publication_diagnostics[
        "publish_file_fdatasync_enabled"
    ] == 0
    assert fragment.publication_diagnostics["publish_metadata_layout"] == "split"
    assert fragment.publication_diagnostics[
        "publish_single_inode_enabled"
    ] == 0
    assert fragment.publication_diagnostics[
        "publish_regular_file_sync_count"
    ] == 2


def test_single_inode_metadata_layout_uses_one_file_fsync_and_keeps_dir_fences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s0123456789abcdef",
        metadata_layout="single_inode",
    )
    fragment = publisher.prepare(90)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.extend(
        [
            {"source_id": "camera-01", "pts": 10, "uuid": "frame-10"},
            {"source_id": "camera-01", "pts": 20, "uuid": "frame-20"},
        ]
    )
    events: list[tuple[str, str]] = []
    real_fsync = publishing.os.fsync
    real_replace = publishing.os.replace

    def record_fsync(fd: int) -> None:
        events.append(("fsync", str(Path(f"/proc/self/fd/{fd}").resolve())))
        real_fsync(fd)

    def record_replace(source, destination) -> None:
        events.append(("replace", f"{source}->{destination}"))
        real_replace(source, destination)

    monkeypatch.setattr(publishing.os, "fsync", record_fsync)
    monkeypatch.setattr(publishing.os, "replace", record_replace)

    staged = publisher.stage_publication(fragment)
    metadata_path = fragment.staging_dir / "metadata.json"
    manifest_path = fragment.staging_dir / "segment_manifest.json"
    metadata_stat = metadata_path.stat()
    manifest_stat = manifest_path.stat()
    lines = metadata_path.read_text(encoding="utf-8").splitlines()
    manifest = json.loads(lines[0])

    assert events == []
    assert (metadata_stat.st_dev, metadata_stat.st_ino) == (
        manifest_stat.st_dev,
        manifest_stat.st_ino,
    )
    assert metadata_stat.st_nlink == 2
    assert manifest["schema_version"] == "rolling-segment-manifest-v2"
    assert manifest["metadata_size_bytes"] == metadata_stat.st_size
    assert [json.loads(line) for line in lines[1:]] == fragment.rows

    final_dir = publisher.commit_publication(staged)

    assert final_dir == fragment.final_dir
    assert [kind for kind, _value in events] == [
        "fsync",
        "fsync",
        "replace",
        "fsync",
    ]
    assert Path(events[0][1]).name == "metadata.json"
    assert Path(events[1][1]).name.endswith(".partial")
    assert Path(events[3][1]).name == "segments"
    assert fragment.publication_diagnostics["publish_metadata_layout"] == (
        "single_inode"
    )
    assert fragment.publication_diagnostics[
        "publish_single_inode_enabled"
    ] == 1
    assert fragment.publication_diagnostics[
        "publish_regular_file_sync_count"
    ] == 1
    assert fragment.publication_diagnostics["publish_manifest_fsync_ms"] == 0


def test_single_inode_file_sync_failure_preserves_staging_and_never_renames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s0123456789abcdef",
        metadata_layout="single_inode",
    )
    fragment = publisher.prepare(93)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.append({"source_id": "camera-01", "pts": 10})
    staged = publisher.stage_publication(fragment)
    replace_called = False

    def fail_fsync(_fd: int) -> None:
        raise OSError("injected single-inode fsync failure")

    def record_replace(_source, _destination) -> None:
        nonlocal replace_called
        replace_called = True

    monkeypatch.setattr(publishing.os, "fsync", fail_fsync)
    monkeypatch.setattr(publishing.os, "replace", record_replace)

    with pytest.raises(OSError, match="injected single-inode fsync failure"):
        publisher.commit_publication(staged)

    metadata_stat = (fragment.staging_dir / "metadata.json").stat()
    manifest_stat = (fragment.staging_dir / "segment_manifest.json").stat()
    assert replace_called is False
    assert fragment.staging_dir.is_dir()
    assert fragment.final_dir.exists() is False
    assert metadata_stat.st_ino == manifest_stat.st_ino
    assert fragment.publication_diagnostics["publish_metadata_layout"] == (
        "single_inode"
    )
    assert fragment.publication_diagnostics[
        "publish_regular_file_sync_count"
    ] == 1


def test_single_inode_publication_journal_records_one_aliased_identity(
    tmp_path: Path,
) -> None:
    publisher = AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s0123456789abcdef",
        metadata_layout="single_inode",
    )
    fragment = publisher.prepare(94)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.extend(
        [
            {"source_id": "camera-01", "pts": 10},
            {"source_id": "camera-01", "pts": 20},
        ]
    )

    final_dir = publisher.publish(fragment)

    record = json.loads(
        (final_dir.parent / ".segment-publications.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert record["manifest"]["schema_version"] == "rolling-segment-manifest-v2"
    assert record["identities"]["manifest"] == record["identities"]["metadata"]
    assert (final_dir / "metadata.json").stat().st_ino == (
        final_dir / "segment_manifest.json"
    ).stat().st_ino


def test_metadata_only_layout_uses_one_file_fsync_without_manifest_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s0123456789abcdef",
        metadata_layout="metadata_only",
    )
    fragment = publisher.prepare(95)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.extend(
        [
            {"source_id": "camera-01", "pts": 10, "uuid": "frame-10"},
            {"source_id": "camera-01", "pts": 20, "uuid": "frame-20"},
        ]
    )
    events: list[tuple[str, str]] = []
    real_fsync = publishing.os.fsync
    real_replace = publishing.os.replace

    def record_fsync(fd: int) -> None:
        events.append(("fsync", str(Path(f"/proc/self/fd/{fd}").resolve())))
        real_fsync(fd)

    def record_replace(source, destination) -> None:
        events.append(("replace", f"{source}->{destination}"))
        real_replace(source, destination)

    monkeypatch.setattr(publishing.os, "fsync", record_fsync)
    monkeypatch.setattr(publishing.os, "replace", record_replace)

    staged = publisher.stage_publication(fragment)
    metadata_path = fragment.staging_dir / "metadata.json"
    manifest_path = fragment.staging_dir / "segment_manifest.json"
    lines = metadata_path.read_text(encoding="utf-8").splitlines()
    manifest = json.loads(lines[0])

    assert events == []
    assert metadata_path.is_file()
    assert not manifest_path.exists()
    assert manifest["schema_version"] == "rolling-segment-manifest-v3"
    assert manifest["metadata_size_bytes"] == metadata_path.stat().st_size
    assert len(lines[0].encode("utf-8")) < 64 * 1024
    assert [json.loads(line) for line in lines[1:]] == fragment.rows

    final_dir = publisher.commit_publication(staged)

    assert final_dir == fragment.final_dir
    assert [kind for kind, _value in events] == [
        "fsync",
        "fsync",
        "replace",
        "fsync",
    ]
    assert Path(events[0][1]).name == "metadata.json"
    assert Path(events[1][1]).name.endswith(".partial")
    assert Path(events[3][1]).name == "segments"
    assert not (final_dir / "segment_manifest.json").exists()
    assert fragment.publication_diagnostics["publish_metadata_layout"] == (
        "metadata_only"
    )
    assert fragment.publication_diagnostics[
        "publish_single_inode_enabled"
    ] == 0
    assert fragment.publication_diagnostics[
        "publish_metadata_only_enabled"
    ] == 1
    assert fragment.publication_diagnostics[
        "publish_regular_file_sync_count"
    ] == 1
    assert fragment.publication_diagnostics["publish_manifest_write_ms"] == 0
    assert fragment.publication_diagnostics["publish_manifest_fsync_ms"] == 0
    assert fragment.publication_diagnostics["publish_manifest_stat_ms"] == 0


def test_metadata_only_file_sync_failure_preserves_single_staging_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s0123456789abcdef",
        metadata_layout="metadata_only",
    )
    fragment = publisher.prepare(96)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.append({"source_id": "camera-01", "pts": 10})
    staged = publisher.stage_publication(fragment)
    replace_called = False

    def fail_fsync(_fd: int) -> None:
        raise OSError("injected metadata-only fsync failure")

    def record_replace(_source, _destination) -> None:
        nonlocal replace_called
        replace_called = True

    monkeypatch.setattr(publishing.os, "fsync", fail_fsync)
    monkeypatch.setattr(publishing.os, "replace", record_replace)

    with pytest.raises(OSError, match="injected metadata-only fsync failure"):
        publisher.commit_publication(staged)

    assert replace_called is False
    assert fragment.staging_dir.is_dir()
    assert (fragment.staging_dir / "metadata.json").is_file()
    assert not (fragment.staging_dir / "segment_manifest.json").exists()
    assert fragment.final_dir.exists() is False
    assert fragment.publication_diagnostics["publish_metadata_layout"] == (
        "metadata_only"
    )
    assert fragment.publication_diagnostics[
        "publish_regular_file_sync_count"
    ] == 1


def test_metadata_only_publication_journal_uses_metadata_identity_without_alias(
    tmp_path: Path,
) -> None:
    publisher = AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s0123456789abcdef",
        metadata_layout="metadata_only",
    )
    fragment = publisher.prepare(97)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.extend(
        [
            {"source_id": "camera-01", "pts": 10},
            {"source_id": "camera-01", "pts": 20},
        ]
    )

    final_dir = publisher.publish(fragment)

    record = json.loads(
        (final_dir.parent / ".segment-publications.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert record["manifest"]["schema_version"] == (
        "rolling-segment-manifest-v3"
    )
    assert record["identities"]["manifest"] == record["identities"]["metadata"]
    assert record["identities"]["metadata"] == publishing._identity_payload(
        (final_dir / "metadata.json").stat()
    )
    assert not (final_dir / "segment_manifest.json").exists()


def test_atomic_publication_fdatasync_mode_keeps_directory_fsync_and_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s0123456789abcdef",
        file_sync_mode="fdatasync",
    )
    fragment = publisher.prepare(91)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.extend(
        [
            {"source_id": "camera-01", "pts": 10},
            {"source_id": "camera-01", "pts": 20},
        ]
    )
    events: list[tuple[str, str]] = []
    real_fsync = publishing.os.fsync
    real_fdatasync = publishing.os.fdatasync
    real_replace = publishing.os.replace

    def record_fsync(fd: int) -> None:
        events.append(("fsync", str(Path(f"/proc/self/fd/{fd}").resolve())))
        real_fsync(fd)

    def record_fdatasync(fd: int) -> None:
        events.append(("fdatasync", str(Path(f"/proc/self/fd/{fd}").resolve())))
        real_fdatasync(fd)

    def record_replace(source, destination) -> None:
        events.append(("replace", f"{source}->{destination}"))
        real_replace(source, destination)

    monkeypatch.setattr(publishing.os, "fsync", record_fsync)
    monkeypatch.setattr(publishing.os, "fdatasync", record_fdatasync)
    monkeypatch.setattr(publishing.os, "replace", record_replace)

    staged = publisher.stage_publication(fragment)

    assert events == []
    final_dir = publisher.commit_publication(staged)

    assert final_dir == fragment.final_dir
    assert [kind for kind, _value in events] == [
        "fdatasync",
        "fdatasync",
        "fsync",
        "replace",
        "fsync",
    ]
    assert Path(events[0][1]).name == "metadata.json"
    assert Path(events[1][1]).name == "segment_manifest.json"
    assert Path(events[2][1]).name.endswith(".partial")
    assert Path(events[4][1]).name == "segments"
    assert fragment.publication_diagnostics["publish_file_sync_mode"] == (
        "fdatasync"
    )
    assert fragment.publication_diagnostics[
        "publish_file_fdatasync_enabled"
    ] == 1


def test_fdatasync_failure_preserves_staging_and_never_renames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s0123456789abcdef",
        file_sync_mode="fdatasync",
    )
    fragment = publisher.prepare(92)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.append({"source_id": "camera-01", "pts": 10})
    staged = publisher.stage_publication(fragment)
    replace_called = False

    def fail_fdatasync(_fd: int) -> None:
        raise OSError("injected fdatasync failure")

    def record_replace(_source, _destination) -> None:
        nonlocal replace_called
        replace_called = True

    monkeypatch.setattr(publishing.os, "fdatasync", fail_fdatasync)
    monkeypatch.setattr(publishing.os, "replace", record_replace)

    with pytest.raises(OSError, match="injected fdatasync failure"):
        publisher.commit_publication(staged)

    assert replace_called is False
    assert fragment.staging_dir.is_dir()
    assert fragment.final_dir.exists() is False
    assert fragment.publication_diagnostics["publish_file_sync_mode"] == (
        "fdatasync"
    )
    assert fragment.publication_diagnostics[
        "publish_file_fdatasync_enabled"
    ] == 1


def test_epoch_commit_arbitration_is_disabled_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert publishing.SEGMENT_PUBLICATION_COMMIT_ARBITRATION_ENABLED is False
    publisher = _publisher(tmp_path)
    fragment = publisher.prepare(10)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.append({"source_id": "camera-01", "pts": 10})
    commit_lock_operations: list[int] = []
    real_flock = publishing.fcntl.flock

    def record_flock(fd: int, operation: int) -> None:
        target = Path(f"/proc/self/fd/{fd}").resolve()
        if target.name == publishing.SEGMENT_PUBLICATION_COMMIT_LOCK_FILE:
            commit_lock_operations.append(operation)
        real_flock(fd, operation)

    monkeypatch.setattr(publishing.fcntl, "flock", record_flock)

    final_dir = publisher.publish(fragment)

    commit_lock_path = (
        tmp_path
        / "cache"
        / "midterm"
        / "epochs"
        / "epoch-a"
        / publishing.SEGMENT_PUBLICATION_COMMIT_LOCK_FILE
    )
    assert final_dir.is_dir()
    assert commit_lock_operations == []
    assert commit_lock_path.exists() is False
    assert fragment.publication_diagnostics[
        "publish_commit_lock_wait_ms"
    ] == 0
    assert fragment.publication_diagnostics[
        "publish_commit_lock_hold_ms"
    ] == 0
    assert fragment.publication_diagnostics["publish_commit_slot_count"] == 0
    assert fragment.publication_diagnostics["publish_commit_slot_index"] == -1


def test_epoch_commit_arbiter_serializes_concurrent_source_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publishers = {
        source_id: AtomicSegmentPublisher(
            cache_root=tmp_path / "cache",
            namespace="midterm",
            runtime_epoch_id="epoch-a",
            source_id=source_id,
            session_id=f"s{source_id[-1] * 16}",
            commit_arbitration_enabled=True,
        )
        for source_id in ("camera-a", "camera-b")
    }
    staged = {}
    for index, (source_id, publisher) in enumerate(publishers.items(), start=1):
        fragment = publisher.prepare(index)
        fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
        fragment.rows.append({"source_id": source_id, "pts": index})
        staged[source_id] = publisher.stage_publication(fragment)

    first_replace_entered = threading.Event()
    second_replace_entered = threading.Event()
    release_first_replace = threading.Event()
    real_replace = publishing.os.replace
    errors: list[BaseException] = []

    def controlled_replace(source: Path, destination: Path) -> None:
        destination = Path(destination)
        if "camera-a" in destination.parts:
            first_replace_entered.set()
            assert release_first_replace.wait(timeout=2)
        elif "camera-b" in destination.parts:
            second_replace_entered.set()
        real_replace(source, destination)

    def commit(source_id: str) -> None:
        try:
            publishers[source_id].commit_publication(staged[source_id])
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(publishing.os, "replace", controlled_replace)
    first_thread = threading.Thread(target=commit, args=("camera-a",), daemon=True)
    second_thread = threading.Thread(target=commit, args=("camera-b",), daemon=True)
    first_thread.start()
    assert first_replace_entered.wait(timeout=1)
    second_thread.start()
    second_entered_while_first_held = second_replace_entered.wait(timeout=0.1)
    release_first_replace.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert second_entered_while_first_held is False
    assert first_thread.is_alive() is False
    assert second_thread.is_alive() is False
    assert errors == []
    for item in staged.values():
        diagnostics = item.fragment.publication_diagnostics
        assert diagnostics["publish_commit_lock_wait_ms"] >= 0
        assert diagnostics["publish_commit_lock_hold_ms"] >= 0
    assert (
        staged["camera-b"].fragment.publication_diagnostics[
            "publish_commit_lock_wait_ms"
        ]
        >= 90
    )


def test_two_commit_slots_serialize_same_lane_while_peer_lane_progresses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert zlib.crc32(b"camera-04") % 2 == 0
    assert zlib.crc32(b"camera-05") % 2 == 0
    assert zlib.crc32(b"camera-00") % 2 == 1
    publishers = {
        source_id: AtomicSegmentPublisher(
            cache_root=tmp_path / "cache",
            namespace="midterm",
            runtime_epoch_id="epoch-a",
            source_id=source_id,
            session_id=f"s{source_id[-1] * 16}",
            commit_slot_count=2,
        )
        for source_id in ("camera-04", "camera-05", "camera-00")
    }
    staged = {}
    for index, (source_id, publisher) in enumerate(publishers.items(), start=31):
        fragment = publisher.prepare(index)
        fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
        fragment.rows.append({"source_id": source_id, "pts": index})
        staged[source_id] = publisher.stage_publication(fragment)

    first_entered = threading.Event()
    same_lane_entered = threading.Event()
    peer_lane_entered = threading.Event()
    release_first = threading.Event()
    real_replace = publishing.os.replace
    errors: list[BaseException] = []

    def controlled_replace(source: Path, destination: Path) -> None:
        destination = Path(destination)
        if "camera-04" in destination.parts:
            first_entered.set()
            assert release_first.wait(timeout=2)
        elif "camera-05" in destination.parts:
            same_lane_entered.set()
        elif "camera-00" in destination.parts:
            peer_lane_entered.set()
        real_replace(source, destination)

    def commit(source_id: str) -> None:
        try:
            publishers[source_id].commit_publication(staged[source_id])
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(publishing.os, "replace", controlled_replace)
    first_thread = threading.Thread(target=commit, args=("camera-04",), daemon=True)
    same_thread = threading.Thread(target=commit, args=("camera-05",), daemon=True)
    peer_thread = threading.Thread(target=commit, args=("camera-00",), daemon=True)
    first_thread.start()
    assert first_entered.wait(timeout=1)
    same_thread.start()
    peer_thread.start()

    assert peer_lane_entered.wait(timeout=1)
    assert same_lane_entered.wait(timeout=0.1) is False
    release_first.set()
    for thread in (first_thread, same_thread, peer_thread):
        thread.join(timeout=2)

    assert errors == []
    assert all(not thread.is_alive() for thread in (first_thread, same_thread, peer_thread))
    assert same_lane_entered.is_set()
    for source_id, item in staged.items():
        diagnostics = item.fragment.publication_diagnostics
        assert diagnostics["publish_commit_slot_count"] == 2
        assert diagnostics["publish_commit_slot_index"] == (
            zlib.crc32(source_id.encode("utf-8")) % 2
        )
    assert staged["camera-05"].fragment.publication_diagnostics[
        "publish_commit_lock_wait_ms"
    ] >= 90


def test_epoch_commit_arbiter_blocks_cross_process_and_shutdown_is_bounded(
    tmp_path: Path,
) -> None:
    publisher = AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s0123456789abcdef",
        commit_arbitration_enabled=True,
    )
    fragment = publisher.prepare(21)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.append({"source_id": "camera-01", "pts": 21})
    lock_path = (
        tmp_path
        / "cache"
        / "midterm"
        / "epochs"
        / "epoch-a"
        / ".segment-publication-commit.lock"
    )
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_exclusive_flock,
        args=(str(lock_path), acquired, release),
    )
    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=2,
        thread_name="test-cross-process-commit-arbiter",
    )
    published: list[Path] = []
    holder.start()
    try:
        assert acquired.wait(timeout=2)
        dispatcher.submit(
            source_id="camera-01",
            publisher=publisher,
            fragment=fragment,
            on_published=lambda _fragment, path: published.append(path),
            on_publish_error=lambda _fragment, error: pytest.fail(str(error)),
        )
        deadline = time.monotonic() + 1
        while not (fragment.staging_dir / "metadata.json").exists():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        first_close = dispatcher.close(timeout_s=0.05)
    finally:
        release.set()
        holder.join(timeout=2)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2)

    assert holder.exitcode == 0
    assert first_close is False
    assert dispatcher.close(timeout_s=2) is True
    assert published == [fragment.final_dir]
    assert fragment.publication_diagnostics["publish_commit_lock_wait_ms"] >= 40
    assert fragment.publication_diagnostics["publish_commit_lock_hold_ms"] >= 0
    assert dispatcher.snapshot()["shutdown_timeout_total"] == 1


def test_two_commit_slots_exclude_cross_process_per_lane_and_drain_peer(
    tmp_path: Path,
) -> None:
    publishers = {
        source_id: AtomicSegmentPublisher(
            cache_root=tmp_path / "cache",
            namespace="midterm",
            runtime_epoch_id="epoch-a",
            source_id=source_id,
            session_id=f"s{source_id[-1] * 16}",
            commit_slot_count=2,
        )
        for source_id in ("camera-04", "camera-00")
    }
    fragments = {}
    for index, (source_id, publisher) in enumerate(publishers.items(), start=41):
        fragment = publisher.prepare(index)
        fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
        fragment.rows.append({"source_id": source_id, "pts": index})
        fragments[source_id] = fragment

    slot_zero_path = (
        tmp_path
        / "cache"
        / "midterm"
        / "epochs"
        / "epoch-a"
        / ".segment-publication-commit-slot-0.lock"
    )
    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_exclusive_flock,
        args=(str(slot_zero_path), acquired, release),
    )
    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=4,
        worker_count=2,
        thread_name="test-cross-process-commit-slots",
    )
    published: list[str] = []
    errors: list[tuple[str, str]] = []
    holder.start()
    try:
        assert acquired.wait(timeout=2)
        for source_id in ("camera-04", "camera-00"):
            dispatcher.submit(
                source_id=source_id,
                publisher=publishers[source_id],
                fragment=fragments[source_id],
                on_published=lambda fragment, _path: published.append(
                    fragment.segment_id
                ),
                on_publish_error=lambda fragment, error: errors.append(
                    (fragment.segment_id, str(error))
                ),
            )
        deadline = time.monotonic() + 1
        while not fragments["camera-00"].final_dir.is_dir():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert fragments["camera-00"].final_dir.is_dir()
        assert fragments["camera-04"].final_dir.exists() is False
        first_close = dispatcher.close(timeout_s=0.05)
    finally:
        release.set()
        holder.join(timeout=2)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2)

    assert holder.exitcode == 0
    assert first_close is False
    assert dispatcher.close(timeout_s=2) is True
    assert errors == []
    assert sorted(published) == sorted(
        fragment.segment_id for fragment in fragments.values()
    )
    assert fragments["camera-04"].publication_diagnostics[
        "publish_commit_lock_wait_ms"
    ] >= 40
    assert fragments["camera-00"].publication_diagnostics[
        "publish_commit_slot_index"
    ] == 1
    assert dispatcher.snapshot()["shutdown_timeout_total"] == 1


def test_epoch_commit_arbiter_acquire_failure_preserves_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s0123456789abcdef",
        commit_arbitration_enabled=True,
    )
    fragment = publisher.prepare(22)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.append({"source_id": "camera-01", "pts": 22})
    real_flock = publishing.fcntl.flock

    def fail_commit_lock(fd: int, operation: int) -> None:
        target = Path(f"/proc/self/fd/{fd}").resolve()
        if (
            target.name == ".segment-publication-commit.lock"
            and operation & publishing.fcntl.LOCK_EX
        ):
            raise OSError("injected-commit-lock-failure")
        real_flock(fd, operation)

    monkeypatch.setattr(publishing.fcntl, "flock", fail_commit_lock)

    with pytest.raises(OSError, match="injected-commit-lock-failure"):
        publisher.publish(fragment)

    assert fragment.staging_dir.is_dir()
    assert fragment.final_dir.exists() is False
    assert fragment.publication_diagnostics["publish_commit_lock_wait_ms"] >= 0
    assert fragment.publication_diagnostics["publish_commit_lock_hold_ms"] == 0


def test_epoch_commit_arbiter_releases_after_commit_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publishers = {
        source_id: AtomicSegmentPublisher(
            cache_root=tmp_path / "cache",
            namespace="midterm",
            runtime_epoch_id="epoch-a",
            source_id=source_id,
            session_id=f"s{source_id[-1] * 16}",
            commit_arbitration_enabled=True,
        )
        for source_id in ("camera-a", "camera-b")
    }
    fragments = {}
    for index, (source_id, publisher) in enumerate(publishers.items(), start=23):
        fragment = publisher.prepare(index)
        fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
        fragment.rows.append({"source_id": source_id, "pts": index})
        fragments[source_id] = fragment

    real_replace = publishing.os.replace

    def fail_first_replace(source: Path, destination: Path) -> None:
        if "camera-a" in Path(destination).parts:
            raise OSError("injected-commit-failure")
        real_replace(source, destination)

    monkeypatch.setattr(publishing.os, "replace", fail_first_replace)

    with pytest.raises(OSError, match="injected-commit-failure"):
        publishers["camera-a"].publish(fragments["camera-a"])
    second_final = publishers["camera-b"].publish(fragments["camera-b"])

    assert fragments["camera-a"].staging_dir.is_dir()
    assert fragments["camera-a"].final_dir.exists() is False
    assert second_final.is_dir()
    assert fragments["camera-a"].publication_diagnostics[
        "publish_commit_lock_hold_ms"
    ] >= 0
    assert fragments["camera-b"].publication_diagnostics[
        "publish_commit_lock_hold_ms"
    ] >= 0


def test_commit_slot_releases_after_commit_error_for_same_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publishers = {
        source_id: AtomicSegmentPublisher(
            cache_root=tmp_path / "cache",
            namespace="midterm",
            runtime_epoch_id="epoch-a",
            source_id=source_id,
            session_id=f"s{source_id[-1] * 16}",
            commit_slot_count=2,
        )
        for source_id in ("camera-04", "camera-05")
    }
    fragments = {}
    for index, (source_id, publisher) in enumerate(publishers.items(), start=51):
        fragment = publisher.prepare(index)
        fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
        fragment.rows.append({"source_id": source_id, "pts": index})
        fragments[source_id] = fragment

    real_replace = publishing.os.replace

    def fail_first_replace(source: Path, destination: Path) -> None:
        if "camera-04" in Path(destination).parts:
            raise OSError("injected-slot-commit-failure")
        real_replace(source, destination)

    monkeypatch.setattr(publishing.os, "replace", fail_first_replace)

    with pytest.raises(OSError, match="injected-slot-commit-failure"):
        publishers["camera-04"].publish(fragments["camera-04"])
    second_final = publishers["camera-05"].publish(fragments["camera-05"])

    assert fragments["camera-04"].staging_dir.is_dir()
    assert second_final.is_dir()
    assert {
        fragment.publication_diagnostics["publish_commit_slot_index"]
        for fragment in fragments.values()
    } == {0}


def test_missing_video_is_not_published_and_staging_is_preserved(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    fragment = publisher.prepare(0)
    fragment.rows.append({"source_id": "camera-01", "pts": 1})

    with pytest.raises(RuntimeError, match="rolling_segment_video_missing"):
        publisher.publish(fragment)

    assert fragment.staging_dir.is_dir()
    assert not fragment.final_dir.exists()
    assert not (fragment.staging_dir / "metadata.json").exists()
    assert not (fragment.staging_dir / "segment_manifest.json").exists()


def test_compact_manifest_is_published_with_the_atomic_segment_rename(
    monkeypatch,
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    fragment = publisher.prepare(9)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.extend(
        [
            {
                "source_id": "camera-01",
                "pts": 10,
                "rolling_cache_mux_pts": 100,
            },
            {
                "source_id": "camera-01",
                "pts": 20,
                "rolling_cache_mux_pts": 200,
            },
        ]
    )
    import publishing

    real_replace = publishing.os.replace
    observed = {}

    def inspect_then_replace(source: Path, destination: Path) -> None:
        source = Path(source)
        destination = Path(destination)
        assert source == fragment.staging_dir
        assert destination == fragment.final_dir
        assert not destination.exists()
        observed.update(
            json.loads((source / "segment_manifest.json").read_text(encoding="utf-8"))
        )
        real_replace(source, destination)

    monkeypatch.setattr(publishing.os, "replace", inspect_then_replace)

    final_dir = publisher.publish(fragment)

    assert final_dir == fragment.final_dir
    assert observed["first_pts"] == 100
    assert observed["last_pts"] == 200
    assert observed["source_first_pts"] == 10
    assert observed["source_last_pts"] == 20
    assert observed["frame_count"] == 2


def test_atomic_publication_appends_identity_fenced_discovery_record(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    fragment = publisher.prepare(10)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.extend(
        [
            {
                "source_id": "camera-01",
                "pts": 10,
                "rolling_cache_mux_pts": 100,
            },
            {
                "source_id": "camera-01",
                "pts": 20,
                "rolling_cache_mux_pts": 200,
            },
        ]
    )

    final_dir = publisher.publish(fragment)

    journal_path = final_dir.parent / ".segment-publications.jsonl"
    records = journal_path.read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    record = json.loads(records[0])
    checksum = record.pop("crc32")
    encoded = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert checksum == f"{zlib.crc32(encoded) & 0xFFFFFFFF:08x}"
    assert record["schema_version"] == "rolling-segment-publication-v1"
    assert record["source_id"] == "camera-01"
    assert record["runtime_epoch_id"] == "epoch-a"
    assert record["segment_id"] == fragment.segment_id
    assert record["manifest"] == json.loads(
        (final_dir / "segment_manifest.json").read_text(encoding="utf-8")
    )
    assert set(record["identities"]) == {"manifest", "metadata", "video"}
    for name, filename in (
        ("manifest", "segment_manifest.json"),
        ("metadata", "metadata.json"),
        ("video", "video.mov"),
    ):
        stat = (final_dir / filename).stat()
        assert record["identities"][name] == {
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }


def test_committed_segment_survives_publication_journal_append_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    fragment = publisher.prepare(11)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.append({"source_id": "camera-01", "pts": 10})
    import publishing

    def fail_append(**_kwargs) -> None:
        raise OSError("injected-journal-failure")

    monkeypatch.setattr(publishing, "_append_publication_record", fail_append)

    final_dir = publisher.publish(fragment)

    assert final_dir.is_dir()
    assert (final_dir / "segment_manifest.json").is_file()
    assert not (final_dir.parent / ".segment-publications.jsonl").exists()


def test_publication_journal_rotation_is_bounded_and_keeps_current_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import publishing

    monkeypatch.setattr(publishing, "SEGMENT_PUBLICATION_JOURNAL_MAX_BYTES", 1)
    publisher = _publisher(tmp_path)
    final_dirs = []
    for fragment_id in (12, 13):
        fragment = publisher.prepare(fragment_id)
        fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
        fragment.rows.append(
            {"source_id": "camera-01", "pts": fragment_id}
        )
        final_dirs.append(publisher.publish(fragment))

    journal_path = final_dirs[-1].parent / ".segment-publications.jsonl"
    records = [json.loads(line) for line in journal_path.read_text().splitlines()]
    assert [record["segment_id"] for record in records] == [
        "s0123456789abcdef-00000013"
    ]


def test_fragment_ledger_dispatches_durable_publication_off_callback_thread(
    tmp_path: Path,
) -> None:
    real_publisher = _publisher(tmp_path)
    publication_started = threading.Event()
    release_publication = threading.Event()
    published: list[Path] = []

    class SlowPublisher:
        prepare = real_publisher.prepare

        @staticmethod
        def publish(fragment):
            publication_started.set()
            assert release_publication.wait(timeout=2)
            return real_publisher.publish(fragment)

    metrics = SinkMetrics()
    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=2,
        metrics=metrics,
        thread_name="test-publication",
    )
    ledger = FragmentLedger(
        SlowPublisher(),
        publication_dispatcher=dispatcher,
        on_published=lambda _fragment, path: published.append(path),
    )
    ledger.queue_frame(1, {"source_id": "camera-01", "pts": 1})
    location = ledger.open_fragment(0, 1)
    Path(location).write_bytes(b"video")
    ledger.record_eos({"source_id": "camera-01", "schema": "EndOfStream"})

    started_at = time.monotonic()
    final_dir = ledger.close_fragment(location)

    assert time.monotonic() - started_at < 0.2
    assert publication_started.wait(timeout=1)
    assert final_dir is not None
    assert not final_dir.exists()
    assert ledger.pending_count() == 1
    assert metrics.snapshot()["publication_outstanding"] == 1

    release_publication.set()
    assert dispatcher.close(timeout_s=2) is True
    assert final_dir.is_dir()
    assert published == [final_dir]
    assert ledger.pending_count() == 0
    assert metrics.snapshot()["publication_outstanding"] == 0


def test_bounded_publication_dispatcher_backpressures_without_reordering_or_drop(
    tmp_path: Path,
) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    third_submitted = threading.Event()
    order: list[str] = []
    callbacks: list[str] = []
    metrics = SinkMetrics()

    class OrderedPublisher:
        @staticmethod
        def publish(fragment):
            order.append(fragment.segment_id)
            if fragment.segment_id == "segment-1":
                first_started.set()
                assert release_first.wait(timeout=2)
            fragment.final_dir.mkdir(parents=True)
            return fragment.final_dir

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=2,
        metrics=metrics,
        thread_name="test-publication-order",
    )
    fragments = [
        SimpleNamespace(
            segment_id=f"segment-{index}",
            final_dir=tmp_path / f"segment-{index}",
        )
        for index in (1, 2, 3)
    ]

    def submit(fragment) -> None:
        dispatcher.submit(
            source_id="camera-01",
            publisher=OrderedPublisher(),
            fragment=fragment,
            on_published=lambda item, _path: callbacks.append(item.segment_id),
            on_publish_error=lambda _item, error: pytest.fail(str(error)),
        )

    submit(fragments[0])
    assert first_started.wait(timeout=1)
    submit(fragments[1])
    third_thread = threading.Thread(
        target=lambda: (submit(fragments[2]), third_submitted.set()),
        daemon=True,
    )
    third_thread.start()

    assert not third_submitted.wait(timeout=0.1)
    snapshot = dispatcher.snapshot()
    assert snapshot["outstanding"] == 2
    assert snapshot["outstanding_peak"] == 2

    release_first.set()
    assert third_submitted.wait(timeout=1)
    third_thread.join(timeout=1)
    assert dispatcher.close(timeout_s=2) is True
    assert order == ["segment-1", "segment-2", "segment-3"]
    assert callbacks == order
    snapshot = dispatcher.snapshot()
    assert snapshot["outstanding"] == 0
    assert metrics.snapshot()["publication_queue_wait_events_total"] >= 1
    assert metrics.snapshot()["publication_outstanding_peak"] == 2
    peak = dispatcher.peak_snapshot()
    assert peak["outstanding"] == 2
    assert peak["queue_depth"] == 1
    assert peak["source_id"] == "camera-01"
    assert peak["segment_id"] == "segment-2"
    assert int(peak["at_epoch_ms"]) > 0
    first_diagnostics = fragments[0].publication_diagnostics
    second_diagnostics = fragments[1].publication_diagnostics
    third_diagnostics = fragments[2].publication_diagnostics
    assert first_diagnostics["publication_worker_service_ms"] >= 100
    assert second_diagnostics["publication_queue_residence_ms"] >= 100
    assert third_diagnostics["publication_capacity_wait_ms"] >= 100
    for diagnostics in (
        first_diagnostics,
        second_diagnostics,
        third_diagnostics,
    ):
        assert diagnostics["publication_dispatch_total_ms"] >= (
            diagnostics["publication_capacity_wait_ms"]
            + diagnostics["publication_queue_residence_ms"]
            + diagnostics["publication_worker_service_ms"]
            - 1.0
        )
        assert diagnostics["publication_outstanding_at_submit"] in {1, 2}
        assert diagnostics["publication_queue_depth_at_submit"] in {0, 1}
    assert snapshot["queue_residence_ms_max"] >= 100
    assert snapshot["worker_service_ms_max"] >= 100
    assert snapshot["dispatch_total_ms_max"] >= 100


def test_publication_dispatcher_disables_group_preparation_by_default() -> None:
    assert publishing.SEGMENT_PUBLICATION_PREPARE_GROUP_LIMIT == 1

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=8,
        thread_name="test-publication-group-default",
    )
    try:
        assert dispatcher.prepare_group_limit == 1
        assert dispatcher.snapshot()["prepare_group_limit"] == 1
    finally:
        assert dispatcher.close(timeout_s=1) is True


def test_publication_dispatcher_runs_different_source_shards_concurrently(
    tmp_path: Path,
) -> None:
    assert zlib.crc32(b"camera-00") % 2 == 1
    assert zlib.crc32(b"camera-04") % 2 == 0
    rendezvous = threading.Barrier(2)
    callbacks: list[str] = []
    errors: list[tuple[str, str]] = []
    metrics = SinkMetrics()

    class ConcurrentPublisher:
        @staticmethod
        def publish(fragment):
            rendezvous.wait(timeout=2)
            fragment.final_dir.mkdir(parents=True)
            return fragment.final_dir

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=4,
        worker_count=2,
        metrics=metrics,
        thread_name="test-publication-shards",
    )
    fragments = {
        source_id: SimpleNamespace(
            segment_id=f"segment-{source_id}",
            final_dir=tmp_path / source_id,
        )
        for source_id in ("camera-00", "camera-04")
    }
    for source_id, fragment in fragments.items():
        dispatcher.submit(
            source_id=source_id,
            publisher=ConcurrentPublisher(),
            fragment=fragment,
            on_published=lambda item, _path: callbacks.append(item.segment_id),
            on_publish_error=lambda item, error: errors.append(
                (item.segment_id, str(error))
            ),
        )

    assert dispatcher.close(timeout_s=2) is True
    assert errors == []
    assert sorted(callbacks) == sorted(
        fragment.segment_id for fragment in fragments.values()
    )
    assert fragments["camera-00"].publication_diagnostics[
        "publication_worker_index"
    ] == 1
    assert fragments["camera-04"].publication_diagnostics[
        "publication_worker_index"
    ] == 0
    snapshot = dispatcher.snapshot()
    assert snapshot["worker_count"] == 2
    assert snapshot["active_peak"] == 2
    assert metrics.snapshot()["publication_active_peak"] == 2


def test_publication_dispatcher_keeps_same_source_serial_and_callback_ordered(
    tmp_path: Path,
) -> None:
    active = 0
    active_peak = 0
    active_lock = threading.Lock()
    publication_order: list[str] = []
    callback_order: list[str] = []

    class OrderedPublisher:
        @staticmethod
        def publish(fragment):
            nonlocal active, active_peak
            with active_lock:
                active += 1
                active_peak = max(active_peak, active)
            try:
                publication_order.append(fragment.segment_id)
                time.sleep(0.01)
                fragment.final_dir.mkdir(parents=True)
                return fragment.final_dir
            finally:
                with active_lock:
                    active -= 1

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=8,
        worker_count=2,
        thread_name="test-publication-same-source",
    )
    fragments = [
        SimpleNamespace(
            segment_id=f"segment-{index}",
            final_dir=tmp_path / f"segment-{index}",
        )
        for index in range(4)
    ]
    for fragment in fragments:
        dispatcher.submit(
            source_id="camera-00",
            publisher=OrderedPublisher(),
            fragment=fragment,
            on_published=lambda item, _path: callback_order.append(item.segment_id),
        )

    assert dispatcher.close(timeout_s=2) is True
    expected = [fragment.segment_id for fragment in fragments]
    assert publication_order == expected
    assert callback_order == expected
    assert active_peak == 1
    assert {
        fragment.publication_diagnostics["publication_worker_index"]
        for fragment in fragments
    } == {1}


def test_publication_dispatcher_keeps_capacity_global_across_source_shards(
    tmp_path: Path,
) -> None:
    started = 0
    started_lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()
    third_submitted = threading.Event()

    class BlockingPublisher:
        @staticmethod
        def publish(fragment):
            nonlocal started
            with started_lock:
                started += 1
                if started == 2:
                    both_started.set()
            assert release.wait(timeout=2)
            fragment.final_dir.mkdir(parents=True)
            return fragment.final_dir

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=2,
        worker_count=2,
        thread_name="test-publication-global-capacity",
    )

    def submit(source_id: str, segment_id: str) -> None:
        dispatcher.submit(
            source_id=source_id,
            publisher=BlockingPublisher(),
            fragment=SimpleNamespace(
                segment_id=segment_id,
                final_dir=tmp_path / segment_id,
            ),
        )

    submit("camera-00", "segment-0")
    submit("camera-04", "segment-1")
    assert both_started.wait(timeout=1)
    third_thread = threading.Thread(
        target=lambda: (submit("camera-00", "segment-2"), third_submitted.set()),
        daemon=True,
    )
    third_thread.start()
    assert not third_submitted.wait(timeout=0.1)
    snapshot = dispatcher.snapshot()
    assert snapshot["outstanding"] == 2
    assert snapshot["outstanding_peak"] == 2
    assert snapshot["active"] == 2

    release.set()
    assert third_submitted.wait(timeout=1)
    third_thread.join(timeout=1)
    assert dispatcher.close(timeout_s=2) is True
    snapshot = dispatcher.snapshot()
    assert snapshot["submitted_total"] == 3
    assert snapshot["completed_total"] == 3
    assert snapshot["outstanding"] == 0


def test_publication_dispatcher_cross_shard_failure_does_not_block_peer(
    tmp_path: Path,
) -> None:
    bad_started = threading.Event()
    peer_completed = threading.Event()
    callbacks: list[str] = []

    class IsolatedFailurePublisher:
        @staticmethod
        def publish(fragment):
            if fragment.segment_id == "bad":
                bad_started.set()
                assert peer_completed.wait(timeout=2)
                raise OSError("injected-shard-failure")
            assert bad_started.wait(timeout=1)
            fragment.final_dir.mkdir(parents=True)
            peer_completed.set()
            return fragment.final_dir

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=4,
        worker_count=2,
        thread_name="test-publication-shard-failure",
    )
    dispatcher.submit(
        source_id="camera-04",
        publisher=IsolatedFailurePublisher(),
        fragment=SimpleNamespace(segment_id="bad", final_dir=tmp_path / "bad"),
        on_publish_error=lambda item, error: callbacks.append(
            f"error:{item.segment_id}:{error}"
        ),
    )
    dispatcher.submit(
        source_id="camera-00",
        publisher=IsolatedFailurePublisher(),
        fragment=SimpleNamespace(segment_id="good", final_dir=tmp_path / "good"),
        on_published=lambda item, _path: callbacks.append(f"ok:{item.segment_id}"),
    )

    assert dispatcher.close(timeout_s=2) is True
    assert peer_completed.is_set()
    assert sorted(callbacks) == [
        "error:bad:injected-shard-failure",
        "ok:good",
    ]
    assert dispatcher.snapshot()["failed_total"] == 1
    assert dispatcher.snapshot()["completed_total"] == 1


def test_publication_dispatcher_multi_worker_shutdown_drains_all_shards(
    tmp_path: Path,
) -> None:
    published: list[str] = []

    class SlowPublisher:
        @staticmethod
        def publish(fragment):
            time.sleep(0.01)
            fragment.final_dir.mkdir(parents=True)
            return fragment.final_dir

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=8,
        worker_count=2,
        thread_name="test-publication-multi-shutdown",
    )
    for index in range(6):
        dispatcher.submit(
            source_id="camera-00" if index % 2 == 0 else "camera-04",
            publisher=SlowPublisher(),
            fragment=SimpleNamespace(
                segment_id=f"segment-{index}",
                final_dir=tmp_path / f"segment-{index}",
            ),
            on_published=lambda item, _path: published.append(item.segment_id),
        )

    assert dispatcher.close(timeout_s=2) is True
    assert sorted(published) == [f"segment-{index}" for index in range(6)]
    snapshot = dispatcher.snapshot()
    assert snapshot["worker_count"] == 2
    assert snapshot["submitted_total"] == 6
    assert snapshot["completed_total"] == 6
    assert snapshot["outstanding"] == 0
    assert snapshot["active"] == 0
    assert all(not thread.is_alive() for thread in dispatcher._threads)


def test_publication_dispatcher_prepares_one_bounded_group_before_fifo_commit(
    tmp_path: Path,
) -> None:
    first_commit_started = threading.Event()
    release_first_commit = threading.Event()
    events: list[tuple[str, str]] = []
    callbacks: list[str] = []

    class GroupPublisher:
        @staticmethod
        def stage_publication(fragment):
            events.append(("stage", fragment.segment_id))
            return fragment

        @staticmethod
        def commit_publication(fragment):
            events.append(("commit", fragment.segment_id))
            if fragment.segment_id == "segment-1":
                first_commit_started.set()
                assert release_first_commit.wait(timeout=2)
            fragment.final_dir.mkdir(parents=True)
            return fragment.final_dir

    metrics = SinkMetrics()
    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=8,
        prepare_group_limit=3,
        metrics=metrics,
        thread_name="test-publication-group",
    )
    fragments = [
        SimpleNamespace(
            segment_id=f"segment-{index}",
            final_dir=tmp_path / f"segment-{index}",
        )
        for index in range(1, 6)
    ]

    def submit(fragment) -> None:
        dispatcher.submit(
            source_id="camera-01",
            publisher=GroupPublisher(),
            fragment=fragment,
            on_published=lambda item, _path: callbacks.append(item.segment_id),
            on_publish_error=lambda _item, error: pytest.fail(str(error)),
        )

    submit(fragments[0])
    assert first_commit_started.wait(timeout=1)
    for fragment in fragments[1:]:
        submit(fragment)
    release_first_commit.set()

    assert dispatcher.close(timeout_s=2) is True
    assert events == [
        ("stage", "segment-1"),
        ("commit", "segment-1"),
        ("stage", "segment-2"),
        ("stage", "segment-3"),
        ("stage", "segment-4"),
        ("commit", "segment-2"),
        ("commit", "segment-3"),
        ("commit", "segment-4"),
        ("stage", "segment-5"),
        ("commit", "segment-5"),
    ]
    assert callbacks == [fragment.segment_id for fragment in fragments]
    assert fragments[1].publication_diagnostics[
        "publication_prepare_group_size"
    ] == 3
    assert fragments[1].publication_diagnostics[
        "publication_prepare_group_position"
    ] == 1
    assert fragments[3].publication_diagnostics[
        "publication_prepare_group_position"
    ] == 3
    assert fragments[4].publication_diagnostics[
        "publication_prepare_group_size"
    ] == 1
    for fragment in fragments:
        diagnostics = fragment.publication_diagnostics
        assert diagnostics["publication_prepare_service_ms"] >= 0
        assert diagnostics["publication_commit_wait_ms"] >= 0
        assert diagnostics["publication_dispatch_total_ms"] >= (
            diagnostics["publication_capacity_wait_ms"]
            + diagnostics["publication_queue_residence_ms"]
            + diagnostics["publication_prepare_service_ms"]
            + diagnostics["publication_commit_wait_ms"]
            + diagnostics["publication_worker_service_ms"]
            - 1.0
        )
    snapshot = dispatcher.snapshot()
    assert snapshot["prepare_group_limit"] == 3
    assert snapshot["prepare_group_total"] == 3
    assert snapshot["prepare_group_size_max"] == 3
    assert snapshot["prepare_service_ms_total"] >= 0
    assert snapshot["commit_wait_ms_total"] >= 0


def test_publication_group_stage_error_keeps_fifo_error_continuation(
    tmp_path: Path,
) -> None:
    first_commit_started = threading.Event()
    release_first_commit = threading.Event()
    events: list[tuple[str, str]] = []
    callbacks: list[str] = []

    class GroupPublisher:
        @staticmethod
        def stage_publication(fragment):
            events.append(("stage", fragment.segment_id))
            if fragment.segment_id == "bad":
                raise OSError("injected-stage-failure")
            return fragment

        @staticmethod
        def commit_publication(fragment):
            events.append(("commit", fragment.segment_id))
            if fragment.segment_id == "first":
                first_commit_started.set()
                assert release_first_commit.wait(timeout=2)
            fragment.final_dir.mkdir(parents=True)
            return fragment.final_dir

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=4,
        prepare_group_limit=3,
        thread_name="test-publication-group-error",
    )
    fragments = {
        segment_id: SimpleNamespace(
            segment_id=segment_id,
            final_dir=tmp_path / segment_id,
        )
        for segment_id in ("first", "good-1", "bad", "good-2")
    }

    def submit(segment_id: str) -> None:
        dispatcher.submit(
            source_id="camera-01",
            publisher=GroupPublisher(),
            fragment=fragments[segment_id],
            on_published=lambda item, _path: callbacks.append(
                f"ok:{item.segment_id}"
            ),
            on_publish_error=lambda item, error: callbacks.append(
                f"error:{item.segment_id}:{error}"
            ),
        )

    submit("first")
    assert first_commit_started.wait(timeout=1)
    for segment_id in ("good-1", "bad", "good-2"):
        submit(segment_id)
    release_first_commit.set()

    assert dispatcher.close(timeout_s=2) is True
    assert events == [
        ("stage", "first"),
        ("commit", "first"),
        ("stage", "good-1"),
        ("stage", "bad"),
        ("stage", "good-2"),
        ("commit", "good-1"),
        ("commit", "good-2"),
    ]
    assert callbacks == [
        "ok:first",
        "ok:good-1",
        "error:bad:injected-stage-failure",
        "ok:good-2",
    ]
    assert dispatcher.snapshot()["completed_total"] == 3
    assert dispatcher.snapshot()["failed_total"] == 1
    assert dispatcher.snapshot()["outstanding"] == 0
    assert fragments["bad"].publication_diagnostics[
        "publication_prepare_group_position"
    ] == 2
    assert fragments["bad"].publication_diagnostics[
        "publication_worker_service_ms"
    ] == 0


def test_final_parent_group_fsyncs_each_durable_item_before_fifo_renames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent cohort is not the rejected multi-item preparation group.

    Every item must finish its own regular-file and staging-directory fences
    before the next item is staged.  Only then may the already-durable,
    invisible directories be renamed in FIFO order and their distinct final
    parents be fenced once each.  Callbacks remain blocked until the whole
    cohort fence is complete.
    """

    def make_publisher(source_id: str, session_id: str) -> AtomicSegmentPublisher:
        return AtomicSegmentPublisher(
            cache_root=tmp_path / "cache",
            namespace="midterm",
            runtime_epoch_id="epoch-a",
            source_id=source_id,
            session_id=session_id,
        )

    blocker_publisher = make_publisher("camera-00", "s0000000000000000")
    publisher_a = make_publisher("camera-01", "s1111111111111111")
    publisher_b = make_publisher("camera-02", "s2222222222222222")

    def make_fragment(publisher: AtomicSegmentPublisher, fragment_id: int):
        fragment = publisher.prepare(fragment_id)
        fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
        fragment.rows.extend(
            [
                {"source_id": publisher._source_id, "pts": fragment_id * 10 + 1},
                {"source_id": publisher._source_id, "pts": fragment_id * 10 + 2},
            ]
        )
        return fragment

    blocker = make_fragment(blocker_publisher, 1)
    fragments = [
        make_fragment(publisher_a, 1),
        make_fragment(publisher_b, 1),
        make_fragment(publisher_a, 2),
    ]
    publishers = [publisher_a, publisher_b, publisher_a]
    events: list[tuple[str, str]] = []
    callbacks: list[str] = []
    blocker_fsync_entered = threading.Event()
    release_blocker_fsync = threading.Event()
    blocker_callback_entered = threading.Event()
    release_blocker_callback = threading.Event()
    real_stage = publishing.AtomicSegmentPublisher.stage_publication
    real_fsync = publishing.os.fsync
    real_replace = publishing.os.replace

    def record_stage(self, fragment):
        events.append(("stage", fragment.segment_id))
        return real_stage(self, fragment)

    def record_fsync(fd: int) -> None:
        path = str(Path(f"/proc/self/fd/{fd}").resolve())
        events.append(("fsync", path))
        if path == str(blocker.staging_dir / "metadata.json"):
            blocker_fsync_entered.set()
            assert release_blocker_fsync.wait(timeout=2)
        real_fsync(fd)

    def record_replace(source, destination) -> None:
        events.append(("replace", f"{source}->{destination}"))
        real_replace(source, destination)

    monkeypatch.setattr(
        publishing.AtomicSegmentPublisher,
        "stage_publication",
        record_stage,
    )
    monkeypatch.setattr(publishing.os, "fsync", record_fsync)
    monkeypatch.setattr(publishing.os, "replace", record_replace)

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=8,
        prepare_group_limit=1,
        final_parent_group_limit=3,
        worker_count=1,
        thread_name="test-final-parent-group",
    )
    dispatcher.submit(
        source_id="camera-00",
        publisher=blocker_publisher,
        fragment=blocker,
        on_published=lambda _item, _path: (
            events.clear(),
            blocker_callback_entered.set(),
            release_blocker_callback.wait(timeout=2),
        ),
    )
    assert blocker_fsync_entered.wait(timeout=1)
    for publisher, fragment in zip(publishers, fragments, strict=True):
        dispatcher.submit(
            source_id=publisher._source_id,
            publisher=publisher,
            fragment=fragment,
            on_published=lambda item, _path: (
                events.append(("callback", item.segment_id)),
                callbacks.append(item.segment_id),
            ),
            on_publish_error=lambda _item, error: pytest.fail(str(error)),
        )
    release_blocker_fsync.set()
    assert blocker_callback_entered.wait(timeout=1)
    release_blocker_callback.set()

    assert dispatcher.close(timeout_s=3) is True
    segment_ids = [fragment.segment_id for fragment in fragments]
    event_names = [kind for kind, _value in events]
    stage_indexes = {
        value: index
        for index, (kind, value) in enumerate(events)
        if kind == "stage"
    }
    partial_fsync_indexes = [
        index
        for index, (kind, value) in enumerate(events)
        if kind == "fsync" and Path(value).name.endswith(".partial")
    ]
    replace_indexes = [
        index for index, (kind, _value) in enumerate(events) if kind == "replace"
    ]
    parent_fsync_events = [
        (index, value)
        for index, (kind, value) in enumerate(events)
        if kind == "fsync" and Path(value).name == "segments"
    ]
    callback_indexes = [
        index for index, (kind, _value) in enumerate(events) if kind == "callback"
    ]

    assert list(stage_indexes) == segment_ids
    assert len(partial_fsync_indexes) == 3
    assert stage_indexes[segment_ids[0]] < partial_fsync_indexes[0]
    assert partial_fsync_indexes[0] < stage_indexes[segment_ids[1]]
    assert stage_indexes[segment_ids[1]] < partial_fsync_indexes[1]
    assert partial_fsync_indexes[1] < stage_indexes[segment_ids[2]]
    assert stage_indexes[segment_ids[2]] < partial_fsync_indexes[2]
    assert partial_fsync_indexes[-1] < replace_indexes[0]
    assert [
        Path(value.split("->", 1)[1]).name
        for kind, value in events
        if kind == "replace"
    ] == segment_ids
    assert len(parent_fsync_events) == 2
    assert replace_indexes[-1] < parent_fsync_events[0][0]
    assert parent_fsync_events[-1][0] < callback_indexes[0]
    assert callbacks == segment_ids
    assert event_names.count("callback") == 3
    assert all(fragment.final_dir.is_dir() for fragment in fragments)

    for position, fragment in enumerate(fragments, start=1):
        diagnostics = fragment.publication_diagnostics
        assert diagnostics["publication_final_parent_group_size"] == 3
        assert diagnostics["publication_final_parent_group_position"] == position
        assert diagnostics["publication_final_parent_group_unique_parents"] == 2
        assert diagnostics["publication_final_parent_group_fsync_count"] == 2
        assert diagnostics["publication_final_parent_group_fsync_saved"] == 1
        assert diagnostics["publication_final_parent_fence_wait_ms"] >= 0
        assert diagnostics["publish_commit_wall_ms"] >= diagnostics[
            "publish_commit_ms"
        ]

    snapshot = dispatcher.snapshot()
    assert snapshot["final_parent_group_limit"] == 3
    assert snapshot["final_parent_group_total"] == 2
    assert snapshot["final_parent_group_size_max"] == 3
    assert snapshot["final_parent_fsync_total"] == 3
    assert snapshot["final_parent_fsync_saved_total"] == 1


def test_final_parent_group_preserves_middle_rename_failure_and_fifo_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def make_publisher(source_id: str, session_id: str) -> AtomicSegmentPublisher:
        return AtomicSegmentPublisher(
            cache_root=tmp_path / "cache",
            namespace="midterm",
            runtime_epoch_id="epoch-a",
            source_id=source_id,
            session_id=session_id,
        )

    blocker_publisher = make_publisher("camera-00", "s0000000000000000")
    publishers = [
        make_publisher("camera-01", "s1111111111111111"),
        make_publisher("camera-02", "s2222222222222222"),
        make_publisher("camera-03", "s3333333333333333"),
    ]

    def make_fragment(publisher: AtomicSegmentPublisher, fragment_id: int):
        fragment = publisher.prepare(fragment_id)
        fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
        fragment.rows.append(
            {"source_id": publisher._source_id, "pts": fragment_id + 1}
        )
        return fragment

    blocker = make_fragment(blocker_publisher, 1)
    fragments = [make_fragment(publisher, 1) for publisher in publishers]
    bad = fragments[1]
    blocker_fsync_entered = threading.Event()
    release_blocker_fsync = threading.Event()
    callbacks: list[str] = []
    real_fsync = publishing.os.fsync
    real_replace = publishing.os.replace

    def blocking_fsync(fd: int) -> None:
        path = str(Path(f"/proc/self/fd/{fd}").resolve())
        if path == str(blocker.staging_dir / "metadata.json"):
            blocker_fsync_entered.set()
            assert release_blocker_fsync.wait(timeout=2)
        real_fsync(fd)

    def fail_middle_rename(source, destination) -> None:
        if Path(source) == bad.staging_dir:
            raise OSError("injected-middle-rename-failure")
        real_replace(source, destination)

    monkeypatch.setattr(publishing.os, "fsync", blocking_fsync)
    monkeypatch.setattr(publishing.os, "replace", fail_middle_rename)

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=8,
        prepare_group_limit=1,
        final_parent_group_limit=3,
        worker_count=1,
        thread_name="test-final-parent-group-error",
    )
    dispatcher.submit(
        source_id="camera-00",
        publisher=blocker_publisher,
        fragment=blocker,
    )
    assert blocker_fsync_entered.wait(timeout=1)
    for publisher, fragment in zip(publishers, fragments, strict=True):
        dispatcher.submit(
            source_id=publisher._source_id,
            publisher=publisher,
            fragment=fragment,
            on_published=lambda item, _path: callbacks.append(
                f"ok:{item.segment_id}"
            ),
            on_publish_error=lambda item, error: callbacks.append(
                f"error:{item.segment_id}:{error}"
            ),
        )
    release_blocker_fsync.set()

    assert dispatcher.close(timeout_s=3) is True
    assert callbacks == [
        f"ok:{fragments[0].segment_id}",
        f"error:{bad.segment_id}:injected-middle-rename-failure",
        f"ok:{fragments[2].segment_id}",
    ]
    assert fragments[0].final_dir.is_dir()
    assert bad.staging_dir.is_dir()
    assert not bad.final_dir.exists()
    assert fragments[2].final_dir.is_dir()
    snapshot = dispatcher.snapshot()
    assert snapshot["completed_total"] == 3
    assert snapshot["failed_total"] == 1
    assert snapshot["outstanding"] == 0
    assert snapshot["active"] == 0


@pytest.mark.parametrize(
    "overrides",
    (
        {"prepare_group_limit": 2},
        {"worker_count": 2},
    ),
)
def test_final_parent_group_rejects_combined_grouping_or_worker_variables(
    overrides: dict[str, int],
) -> None:
    with pytest.raises(ValueError, match="final_parent_group_limit"):
        publishing.BoundedPublicationDispatcher(
            capacity=8,
            final_parent_group_limit=3,
            **overrides,
        )


def test_final_parent_group_rejects_commit_slot_publisher(
    tmp_path: Path,
) -> None:
    publisher = AtomicSegmentPublisher(
        cache_root=tmp_path / "cache",
        namespace="midterm",
        runtime_epoch_id="epoch-a",
        source_id="camera-01",
        session_id="s1111111111111111",
        commit_slot_count=1,
    )
    fragment = publisher.prepare(1)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.append({"source_id": "camera-01", "pts": 1})
    staged = publisher.stage_publication(fragment)

    with pytest.raises(
        RuntimeError,
        match="final_parent_group_limit cannot be combined with commit slots",
    ):
        publisher._begin_final_parent_commit(staged)

    assert fragment.staging_dir.is_dir()
    assert not fragment.final_dir.exists()


def test_final_parent_group_parent_fsync_failure_isolated_by_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publishers = [
        AtomicSegmentPublisher(
            cache_root=tmp_path / "cache",
            namespace="midterm",
            runtime_epoch_id="epoch-a",
            source_id=source_id,
            session_id=session_id,
        )
        for source_id, session_id in (
            ("camera-01", "s1111111111111111"),
            ("camera-02", "s2222222222222222"),
            ("camera-01", "s1111111111111111"),
        )
    ]
    # Reuse one publisher for both camera-01 fragments so their two renames
    # share one explicit final-parent fence.
    publishers[2] = publishers[0]
    fragments = []
    for fragment_id, publisher in enumerate(publishers, start=1):
        fragment = publisher.prepare(fragment_id)
        fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
        fragment.rows.append(
            {"source_id": publisher._source_id, "pts": fragment_id}
        )
        fragments.append(fragment)

    start_worker = threading.Event()
    real_run = publishing.BoundedPublicationDispatcher._run
    real_fsync_directory = publishing._fsync_directory
    parent_fsync_attempts: list[Path] = []
    callbacks: list[str] = []

    def gated_run(self, worker_index: int) -> None:
        assert start_worker.wait(timeout=2)
        real_run(self, worker_index)

    def fail_camera_two_parent(path: Path) -> None:
        path = Path(path)
        if path.name == "segments":
            parent_fsync_attempts.append(path)
        if path == publishers[1]._segments_root:
            raise OSError("injected-final-parent-fsync-failure")
        real_fsync_directory(path)

    monkeypatch.setattr(
        publishing.BoundedPublicationDispatcher,
        "_run",
        gated_run,
    )
    monkeypatch.setattr(publishing, "_fsync_directory", fail_camera_two_parent)

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=8,
        final_parent_group_limit=3,
        thread_name="test-final-parent-fsync-error",
    )
    for publisher, fragment in zip(publishers, fragments, strict=True):
        dispatcher.submit(
            source_id=publisher._source_id,
            publisher=publisher,
            fragment=fragment,
            on_published=lambda item, _path: callbacks.append(
                f"ok:{item.segment_id}"
            ),
            on_publish_error=lambda item, error: callbacks.append(
                f"error:{item.segment_id}:{error}"
            ),
        )
    start_worker.set()

    assert dispatcher.close(timeout_s=3) is True
    assert callbacks == [
        f"ok:{fragments[0].segment_id}",
        (
            f"error:{fragments[1].segment_id}:"
            "injected-final-parent-fsync-failure"
        ),
        f"ok:{fragments[2].segment_id}",
    ]
    assert parent_fsync_attempts == [
        publishers[0]._segments_root,
        publishers[1]._segments_root,
    ]
    assert all(fragment.final_dir.is_dir() for fragment in fragments)
    assert not (
        publishers[1]._segments_root / publishing.SEGMENT_PUBLICATION_JOURNAL_FILE
    ).exists()
    camera_one_records = (
        publishers[0]._segments_root / publishing.SEGMENT_PUBLICATION_JOURNAL_FILE
    ).read_text(encoding="utf-8").splitlines()
    assert len(camera_one_records) == 2
    assert dispatcher.snapshot()["completed_total"] == 2
    assert dispatcher.snapshot()["failed_total"] == 1


def test_final_parent_group_shutdown_timeout_preserves_later_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publishers = [
        AtomicSegmentPublisher(
            cache_root=tmp_path / "cache",
            namespace="midterm",
            runtime_epoch_id="epoch-a",
            source_id=f"camera-{index:02d}",
            session_id=f"s{index:016d}",
        )
        for index in range(1, 4)
    ]
    fragments = []
    for index, publisher in enumerate(publishers, start=1):
        fragment = publisher.prepare(index)
        fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
        fragment.rows.append({"source_id": publisher._source_id, "pts": index})
        fragments.append(fragment)

    start_worker = threading.Event()
    fence_entered = threading.Event()
    release_fence = threading.Event()
    real_run = publishing.BoundedPublicationDispatcher._run
    real_fsync_directory = publishing._fsync_directory
    callbacks: list[str] = []

    def gated_run(self, worker_index: int) -> None:
        assert start_worker.wait(timeout=2)
        real_run(self, worker_index)

    def block_first_final_parent(path: Path) -> None:
        path = Path(path)
        if path == publishers[0]._segments_root:
            fence_entered.set()
            assert release_fence.wait(timeout=2)
        real_fsync_directory(path)

    monkeypatch.setattr(
        publishing.BoundedPublicationDispatcher,
        "_run",
        gated_run,
    )
    monkeypatch.setattr(publishing, "_fsync_directory", block_first_final_parent)

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=3,
        final_parent_group_limit=3,
        thread_name="test-final-parent-shutdown",
    )
    for publisher, fragment in zip(publishers, fragments, strict=True):
        dispatcher.submit(
            source_id=publisher._source_id,
            publisher=publisher,
            fragment=fragment,
            on_published=lambda item, _path: callbacks.append(item.segment_id),
        )
    start_worker.set()
    assert fence_entered.wait(timeout=1)

    assert dispatcher.close(timeout_s=0.01) is False
    timed_out = dispatcher.snapshot()
    assert timed_out["accepting"] is False
    assert timed_out["outstanding"] == 3
    assert timed_out["shutdown_timeout_total"] == 1

    release_fence.set()
    assert dispatcher.close(timeout_s=3) is True
    assert callbacks == [fragment.segment_id for fragment in fragments]
    final = dispatcher.snapshot()
    assert final["outstanding"] == 0
    assert final["active"] == 0
    assert final["completed_total"] == 3
    assert all(not thread.is_alive() for thread in dispatcher._threads)


def test_bounded_publication_dispatcher_surfaces_error_and_drains_remaining(
    tmp_path: Path,
) -> None:
    errors: list[tuple[str, str]] = []
    published: list[str] = []
    metrics = SinkMetrics()

    class FailingPublisher:
        @staticmethod
        def publish(fragment):
            if fragment.segment_id == "bad":
                raise OSError("injected-publication-failure")
            fragment.final_dir.mkdir(parents=True)
            return fragment.final_dir

    dispatcher = publishing.BoundedPublicationDispatcher(
        capacity=2,
        metrics=metrics,
        thread_name="test-publication-error",
    )
    fragments: dict[str, SimpleNamespace] = {}
    for segment_id in ("bad", "good"):
        fragment = SimpleNamespace(
            segment_id=segment_id,
            final_dir=tmp_path / segment_id,
        )
        fragments[segment_id] = fragment
        dispatcher.submit(
            source_id="camera-01",
            publisher=FailingPublisher(),
            fragment=fragment,
            on_published=lambda item, _path: published.append(item.segment_id),
            on_publish_error=lambda item, error: errors.append(
                (item.segment_id, str(error))
            ),
        )

    assert dispatcher.close(timeout_s=2) is True
    assert errors == [("bad", "injected-publication-failure")]
    assert published == ["good"]
    assert dispatcher.snapshot()["failed_total"] == 1
    assert dispatcher.snapshot()["completed_total"] == 1
    assert dispatcher.snapshot()["outstanding"] == 0
    assert metrics.snapshot()["publication_failed_total"] == 1
    assert fragments["bad"].publication_diagnostics[
        "publication_worker_service_ms"
    ] >= 0
    assert fragments["good"].publication_diagnostics[
        "publication_dispatch_total_ms"
    ] >= 0


def test_async_publication_error_marks_active_source_pipeline_failed(
    tmp_path: Path,
) -> None:
    pipeline = object.__new__(SourcePipeline)
    pipeline._metrics = SinkMetrics()
    pipeline._failed = threading.Event()
    pipeline._closed = False
    pipeline.source_id = "camera-01"
    pipeline.runtime_epoch_id = "epoch-a"
    pipeline.session_id = "session-a"
    fragment = SimpleNamespace(
        segment_id="segment-a",
        staging_dir=tmp_path / "segment-a.partial",
    )

    pipeline._on_publish_error(fragment, OSError("injected-publication-failure"))

    metrics = pipeline._metrics.snapshot()
    assert pipeline.failed is True
    assert metrics["segment_publish_errors_total"] == 1
    assert metrics["pipeline_errors_total"] == 1
    assert metrics["pipeline_errors_active"] == 1


def test_fragment_ledger_assigns_boundary_frames_to_new_fragment(
    tmp_path: Path,
) -> None:
    ledger = FragmentLedger(_publisher(tmp_path))
    ledger.queue_frame(1, {"source_id": "camera-01", "pts": 1})
    first_location = ledger.open_fragment(0, 1)
    Path(first_location).write_bytes(b"first")

    # Appsrc can queue the boundary frame before splitmuxsink asynchronously
    # asks for the new location.
    ledger.queue_frame(2, {"source_id": "camera-01", "pts": 2})
    second_location = ledger.open_fragment(1, 2)
    Path(second_location).write_bytes(b"second")
    first_dir = ledger.close_fragment(first_location)
    ledger.record_eos({"source_id": "camera-01", "schema": "EndOfStream"})
    second_dir = ledger.close_fragment(second_location)

    first_rows = [
        json.loads(line)
        for line in (first_dir / "metadata.json")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    second_rows = [
        json.loads(line)
        for line in (second_dir / "metadata.json")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row.get("pts") for row in first_rows] == [1]
    assert [row.get("pts") for row in second_rows] == [2, None]
    assert second_rows[-1]["schema"] == "EndOfStream"
    assert ledger.pending_count() == 0


def test_fragment_ledger_defers_async_close_until_next_boundary(
    tmp_path: Path,
) -> None:
    ledger = FragmentLedger(_publisher(tmp_path))
    ledger.queue_frame(1, {"source_id": "camera-01", "pts": 1})
    first_location = ledger.open_fragment(0, 1)
    Path(first_location).write_bytes(b"first")

    # splitmux async-finalize may close the old muxer before asking for the
    # next fragment location. This ordering is valid and must not fail the
    # source pipeline.
    assert ledger.close_fragment(first_location) is None
    assert ledger.pending_count() == 1

    ledger.queue_frame(2, {"source_id": "camera-01", "pts": 2})
    second_location = ledger.open_fragment(1, 2)
    Path(second_location).write_bytes(b"second")
    first_final = (
        tmp_path
        / "cache"
        / "midterm"
        / "epochs"
        / "epoch-a"
        / "camera-01"
        / "segments"
        / "s0123456789abcdef-00000000"
    )
    assert first_final.is_dir()
    assert ledger.pending_count() == 1

    ledger.record_eos({"source_id": "camera-01", "schema": "EndOfStream"})
    second_final = ledger.close_fragment(second_location)
    assert second_final is not None and second_final.is_dir()
    assert ledger.pending_count() == 0


def test_fragment_ledger_removes_only_unpublished_staging_on_abort(
    tmp_path: Path,
) -> None:
    ledger = FragmentLedger(_publisher(tmp_path))
    ledger.queue_frame(1, {"source_id": "camera-01", "pts": 1})
    first_location = ledger.open_fragment(0, 1)
    Path(first_location).write_bytes(b"incomplete-mov")

    result = ledger.abandon_pending()

    assert result == {"fragments": 1, "bytes": len(b"incomplete-mov")}
    assert ledger.pending_count() == 0
    assert not Path(first_location).exists()
    assert list((tmp_path / "cache").rglob("*.partial")) == []


def test_epoch_resolver_tracks_state_changes_and_keeps_stable_fallback(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "epoch.json"
    resolver = EpochResolver(
        state_path=state_path,
        generated_epoch_id="fallback-epoch",
    )
    assert resolver.current() == "fallback-epoch"
    state_path.write_text(json.dumps({"runtime_epoch_id": "epoch-a"}), encoding="utf-8")
    assert resolver.current() == "epoch-a"
    state_path.write_text("{", encoding="utf-8")
    assert resolver.current() == "epoch-a"
    state_path.write_text(json.dumps({"runtime_epoch_id": "epoch-b"}), encoding="utf-8")
    assert resolver.current() == "epoch-b"


def test_explicit_epoch_has_precedence_over_state_file(tmp_path: Path) -> None:
    state_path = tmp_path / "epoch.json"
    state_path.write_text(
        json.dumps({"runtime_epoch_id": "epoch-state"}), encoding="utf-8"
    )
    resolver = EpochResolver(
        explicit_epoch_id="epoch-explicit",
        state_path=state_path,
        generated_epoch_id="fallback-epoch",
    )
    assert resolver.current() == "epoch-explicit"


@pytest.mark.parametrize("value", ["", ".", "..", "a/b", "../escape", "white space"])
def test_unsafe_exact_path_components_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        safe_component(value, field="source_id")


def test_metrics_expose_pipeline_and_thread_counts() -> None:
    metrics = SinkMetrics()
    health = HealthState(metrics)
    metrics.set("pipelines", 40)
    metrics.set("pending_fragments", 2)
    health.mark_started()

    text = metrics.prometheus_text()
    document = health.document()

    assert "rolling_cache_sink_pipelines 40" in text
    assert "rolling_cache_sink_python_threads" in text
    assert "rolling_cache_sink_process_threads" in text
    assert document["ready"] is True
    assert document["pipelines"] == 40
    assert document["pending_fragments"] == 2
    metrics.set("pipeline_errors_active", 1)
    assert health.document()["ready"] is False


def test_published_segment_is_discoverable_by_current_media_worker(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    fragment = publisher.prepare(3)
    fragment.video_path.write_bytes(b"encoded-h264-in-mov" * 128)
    fragment.rows.extend(
        [
            {"source_id": "camera-01", "pts": 1_000_000_000, "objects": []},
            {"source_id": "camera-01", "pts": 2_000_000_000, "objects": []},
        ]
    )
    publisher.publish(fragment)

    media_worker_root = REPO_ROOT / "services" / "media-worker"
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    sys.path.insert(0, str(media_worker_root))
    try:
        from app import rolling_cache
        from app.segment_index import RollingSegmentIndex

        segments = rolling_cache.find_segments(
            tmp_path / "cache",
            source_id="camera-01",
            runtime_epoch_id="epoch-a",
        )
        index = RollingSegmentIndex(
            tmp_path / "cache",
            refresh_interval_s=0.0,
            reconcile_interval_s=60.0,
            stability_age_s=0.0,
        )
        indexed = index.find_segments(
            source_id="camera-01",
            runtime_epoch_id="epoch-a",
        )
        index_snapshot = index.snapshot()
    finally:
        sys.path.remove(str(media_worker_root))

    assert len(segments) == 1
    assert segments[0].segment_id == "s0123456789abcdef-00000003"
    assert segments[0].first_pts == 1_000_000_000
    assert segments[0].last_pts == 2_000_000_000
    assert segments[0].frame_count == 2
    assert segments[0].video_path.name == "video.mov"
    assert [segment.segment_id for segment in indexed] == ["s0123456789abcdef-00000003"]
    assert int(index_snapshot["manifest_parses"]) == 1
    assert int(index_snapshot["full_row_parses"]) == 0
    assert int(index_snapshot["row_cache_entries"]) == 0


def test_static_pipeline_is_long_lived_h264_passthrough_splitmux() -> None:
    source = (APP_ROOT / "gst_sink.py").read_text(encoding="utf-8")
    entrypoint = (
        REPO_ROOT / "scripts" / "runtime" / "rolling_cache_sink_entrypoint.sh"
    ).read_text(encoding="utf-8")

    assert "splitmuxsink name=segmenter" in source
    assert "async-finalize=true" in source
    assert "h264parse name=parser config-interval=0" in source
    assert "_ensure_mux_timestamp" in source
    assert "max-size-time=" in source
    assert "x264enc" not in source
    assert "nvv4l2h264enc" not in source
    assert source.count("GLib.MainLoop()") == 1
    assert "video_files.py" not in entrypoint
    assert "/opt/rolling-cache-sink/app/main.py" in entrypoint


def test_parser_buffer_without_pts_gets_monotonic_mux_timestamp() -> None:
    pipeline = object.__new__(SourcePipeline)
    pipeline._gst = SimpleNamespace(
        CLOCK_TIME_NONE=-1,
        PadProbeReturn=SimpleNamespace(OK="ok"),
    )
    pipeline._metrics = SinkMetrics()
    pipeline.source_id = "camera-01"
    pipeline.session_id = "session-01"
    pipeline._last_mux_pts = 1_000_000_000
    pipeline._last_mux_duration = 40_000_000
    pipeline._mux_timestamp_offset = 0
    buffer = SimpleNamespace(pts=-1, dts=-1, duration=-1)
    info = SimpleNamespace(get_buffer=lambda: buffer)

    result = pipeline._ensure_mux_timestamp(None, info)

    assert result == "ok"
    assert buffer.pts == 1_040_000_000
    assert buffer.dts == buffer.pts
    assert pipeline._metrics.snapshot()["mux_pts_synthesized_total"] == 1


def test_parser_rational_cadence_does_not_accumulate_timestamp_correction() -> None:
    pipeline = object.__new__(SourcePipeline)
    pipeline._gst = SimpleNamespace(
        CLOCK_TIME_NONE=-1,
        PadProbeReturn=SimpleNamespace(OK="ok"),
    )
    pipeline._metrics = SinkMetrics()
    pipeline._last_mux_pts = None
    pipeline._last_mux_duration = 41_708_333
    pipeline._mux_timestamp_offset = 0

    pts_values = (0, 41_708_333, 83_416_667, 125_125_000)
    buffers = []
    for pts in pts_values:
        buffer = SimpleNamespace(
            pts=pts,
            dts=pts,
            duration=41_708_333,
        )
        pipeline._ensure_mux_timestamp(
            None,
            SimpleNamespace(get_buffer=lambda buffer=buffer: buffer),
        )
        buffers.append(buffer)

    assert [buffer.pts for buffer in buffers] == list(pts_values)
    assert pipeline._mux_timestamp_offset == 0
    assert (
        pipeline._metrics.snapshot().get("mux_pts_regressions_corrected_total", 0)
        == 0
    )


def test_parser_real_pts_regression_shifts_once_then_preserves_cadence() -> None:
    pipeline = object.__new__(SourcePipeline)
    pipeline._gst = SimpleNamespace(
        CLOCK_TIME_NONE=-1,
        PadProbeReturn=SimpleNamespace(OK="ok"),
    )
    pipeline._metrics = SinkMetrics()
    pipeline.source_id = "camera-01"
    pipeline.session_id = "session-01"
    pipeline._last_mux_pts = 1_000_000_000
    pipeline._last_mux_duration = 40_000_000
    pipeline._mux_timestamp_offset = 0

    regressed = SimpleNamespace(
        pts=965_000_000,
        dts=965_000_000,
        duration=40_000_000,
    )
    pipeline._ensure_mux_timestamp(
        None,
        SimpleNamespace(get_buffer=lambda: regressed),
    )
    following = SimpleNamespace(
        pts=1_005_000_000,
        dts=1_005_000_000,
        duration=40_000_000,
    )
    pipeline._ensure_mux_timestamp(
        None,
        SimpleNamespace(get_buffer=lambda: following),
    )

    assert regressed.pts == 1_000_000_001
    assert regressed.dts == regressed.pts
    assert following.pts == 1_040_000_001
    assert following.dts == following.pts
    assert pipeline._mux_timestamp_offset == 35_000_001
    assert (
        pipeline._metrics.snapshot()["mux_pts_regressions_corrected_total"]
        == 1
    )
    assert pipeline._metrics.snapshot()["mux_pts_correction_ns_total"] == 35_000_001


def test_input_pts_jitter_clamps_only_current_frame_without_cumulative_offset() -> None:
    pipeline = object.__new__(SourcePipeline)
    pipeline._gst = SimpleNamespace(CLOCK_TIME_NONE=-1)
    pipeline._metrics = SinkMetrics()
    pipeline._last_gst_pts = 1_000_000_000

    clamped_pts, clamped_dts = pipeline._normalize_input_timestamps(
        965_000_000,
        965_000_000,
    )
    pipeline._last_gst_pts = clamped_pts
    resumed_pts, resumed_dts = pipeline._normalize_input_timestamps(
        1_005_000_000,
        1_005_000_000,
    )

    assert (clamped_pts, clamped_dts) == (1_000_000_001, 1_000_000_001)
    assert (resumed_pts, resumed_dts) == (1_005_000_000, 1_005_000_000)
    metrics = pipeline._metrics.snapshot()
    assert metrics["input_pts_regressions_clamped_total"] == 1
    assert metrics["input_pts_clamp_ns_total"] == 35_000_001


def test_mux_cadence_ignores_wall_clock_bursts_without_changing_source_pts() -> None:
    pipeline = object.__new__(SourcePipeline)
    pipeline._gst = SimpleNamespace(CLOCK_TIME_NONE=-1)
    pipeline._metrics = SinkMetrics()
    pipeline._last_gst_pts = None
    pipeline._last_mux_duration = 41_708_333

    first = pipeline._next_cadence_pts(1_000_000_000, 41_708_333)
    pipeline._last_gst_pts = first
    second = pipeline._next_cadence_pts(1_553_000_000, 41_708_334)
    pipeline._last_gst_pts = second
    third = pipeline._next_cadence_pts(1_553_001_000, 41_708_333)

    assert (first, second, third) == (
        1_000_000_000,
        1_041_708_334,
        1_083_416_667,
    )
    assert pipeline._metrics.snapshot()["mux_cadence_frames_total"] == 2


def test_fragment_boundary_removes_later_mux_correction_from_input_pts() -> None:
    pipeline = object.__new__(SourcePipeline)
    pipeline._gst = SimpleNamespace(CLOCK_TIME_NONE=-1)
    pipeline._adjust_timestamp_offset = None
    pipeline._adjust_timestamp_mux_offset = None
    pipeline._mux_timestamp_offset = 0
    opened: list[tuple[int, int]] = []
    pipeline._ledger = SimpleNamespace(
        first_queued_gst_pts=lambda: 1_000_000_000,
        open_fragment=lambda fragment_id, pts: opened.append((fragment_id, pts))
        or "fragment.mov",
    )
    pipeline._metrics = SinkMetrics()

    first_sample = SimpleNamespace(
        get_buffer=lambda: SimpleNamespace(pts=100_000_000),
    )
    assert pipeline._format_location(None, 0, first_sample) == "fragment.mov"

    pipeline._mux_timestamp_offset = 35_000_001
    second_sample = SimpleNamespace(
        # input PTS 2s -> adjusted 1.1s -> parser correction +35ms
        get_buffer=lambda: SimpleNamespace(pts=1_135_000_001),
    )
    assert pipeline._format_location(None, 1, second_sample) == "fragment.mov"

    assert opened == [(0, 1_000_000_000), (1, 2_000_000_000)]


def test_config_keeps_four_second_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    for name in (
        "ROLLING_CACHE_SEGMENT_SECONDS",
        "ROLLING_CACHE_RUNTIME_EPOCH_ID",
        "RUNTIME_EPOCH_STATE_PATH",
        "SOURCE_ID",
        "SOURCE_ID_PREFIX",
        "ROLLING_CACHE_PUBLICATION_WORKERS",
        "ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS",
        "ROLLING_CACHE_PUBLICATION_FINAL_PARENT_GROUP_LIMIT",
        "ROLLING_CACHE_PUBLICATION_FILE_SYNC_MODE",
        "ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    config = SinkConfig.from_env()
    assert config.segment_seconds == 4.0
    assert config.http_port == 8080
    assert config.publication_workers == 1
    assert config.publication_commit_slots == 0
    assert config.publication_final_parent_group_limit == 1
    assert config.publication_file_sync_mode == "fsync"
    assert config.publication_metadata_layout == "split"


def test_config_accepts_bounded_publication_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    monkeypatch.setenv("ROLLING_CACHE_PUBLICATION_WORKERS", "2")

    assert SinkConfig.from_env().publication_workers == 2


@pytest.mark.parametrize("value", ("0", "5", "not-an-integer"))
def test_config_rejects_unbounded_publication_workers(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    monkeypatch.setenv("ROLLING_CACHE_PUBLICATION_WORKERS", value)

    with pytest.raises(ValueError, match="ROLLING_CACHE_PUBLICATION_WORKERS"):
        SinkConfig.from_env()


def test_config_accepts_bounded_publication_commit_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    monkeypatch.setenv("ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS", "2")

    assert SinkConfig.from_env().publication_commit_slots == 2


@pytest.mark.parametrize("value", ("-1", "5", "not-an-integer"))
def test_config_rejects_unbounded_publication_commit_slots(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    monkeypatch.setenv("ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS", value)

    with pytest.raises(
        ValueError,
        match="ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS",
    ):
        SinkConfig.from_env()


def test_config_accepts_bounded_publication_final_parent_group_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    monkeypatch.setenv(
        "ROLLING_CACHE_PUBLICATION_FINAL_PARENT_GROUP_LIMIT",
        "16",
    )

    assert SinkConfig.from_env().publication_final_parent_group_limit == 16


@pytest.mark.parametrize("value", ("0", "33", "not-an-integer"))
def test_config_rejects_unbounded_publication_final_parent_group_limit(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    monkeypatch.setenv(
        "ROLLING_CACHE_PUBLICATION_FINAL_PARENT_GROUP_LIMIT",
        value,
    )

    with pytest.raises(
        ValueError,
        match="ROLLING_CACHE_PUBLICATION_FINAL_PARENT_GROUP_LIMIT",
    ):
        SinkConfig.from_env()


def test_config_accepts_fdatasync_publication_file_sync_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    monkeypatch.setenv("ROLLING_CACHE_PUBLICATION_FILE_SYNC_MODE", "fdatasync")

    assert SinkConfig.from_env().publication_file_sync_mode == "fdatasync"


def test_config_rejects_unknown_publication_file_sync_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    monkeypatch.setenv("ROLLING_CACHE_PUBLICATION_FILE_SYNC_MODE", "syncfs")

    with pytest.raises(
        ValueError,
        match="ROLLING_CACHE_PUBLICATION_FILE_SYNC_MODE",
    ):
        SinkConfig.from_env()


def test_config_accepts_single_inode_publication_metadata_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    monkeypatch.setenv(
        "ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT",
        "single_inode",
    )

    assert SinkConfig.from_env().publication_metadata_layout == "single_inode"


def test_config_accepts_metadata_only_publication_metadata_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    monkeypatch.setenv(
        "ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT",
        "metadata_only",
    )

    assert SinkConfig.from_env().publication_metadata_layout == "metadata_only"


def test_config_rejects_unknown_publication_metadata_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZMQ_ENDPOINT", "sub+connect:tcp://fanout:5560")
    monkeypatch.setenv("ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT", "syncfs")

    with pytest.raises(
        ValueError,
        match="ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT",
    ):
        SinkConfig.from_env()
