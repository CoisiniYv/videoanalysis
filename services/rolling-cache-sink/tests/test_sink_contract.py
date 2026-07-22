from __future__ import annotations

import json
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
    assert fragment.publication_diagnostics["publish_stage_ms"] >= 0
    assert fragment.publication_diagnostics["publish_commit_ms"] >= 0


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
    ):
        monkeypatch.delenv(name, raising=False)
    config = SinkConfig.from_env()
    assert config.segment_seconds == 4.0
    assert config.http_port == 8080
