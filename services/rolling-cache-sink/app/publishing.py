"""Atomic rolling-segment publication independent of GStreamer."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import queue
import shutil
import threading
import time
import zlib
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from config import (
    MAX_ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS,
    MAX_ROLLING_CACHE_PUBLICATION_WORKERS,
    safe_component,
)


SEGMENT_MANIFEST_FILE = "segment_manifest.json"
SEGMENT_MANIFEST_SCHEMA_VERSION = "rolling-segment-manifest-v1"
SEGMENT_PUBLICATION_JOURNAL_FILE = ".segment-publications.jsonl"
SEGMENT_PUBLICATION_JOURNAL_LOCK_FILE = ".segment-publications.lock"
SEGMENT_PUBLICATION_COMMIT_LOCK_FILE = ".segment-publication-commit.lock"
SEGMENT_PUBLICATION_COMMIT_SLOT_FILE = ".segment-publication-commit-slot-{slot}.lock"
SEGMENT_PUBLICATION_SCHEMA_VERSION = "rolling-segment-publication-v1"
SEGMENT_PUBLICATION_JOURNAL_MAX_BYTES = 16 * 1024 * 1024
MAX_SEGMENT_PUBLICATION_RECORD_BYTES = 128 * 1024
SEGMENT_PUBLICATION_OUTSTANDING_LIMIT = 128
SEGMENT_PUBLICATION_PREPARE_GROUP_LIMIT = 1
SEGMENT_PUBLICATION_COMMIT_ARBITRATION_ENABLED = False

LOGGER = logging.getLogger("rolling_cache_sink.publisher")


@dataclass
class Fragment:
    segment_id: str
    staging_dir: Path
    final_dir: Path
    video_path: Path
    first_gst_pts: int | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    publication_diagnostics: dict[str, Any] = field(default_factory=dict)


class _PublicationPhaseTimings:
    """Account publication phases without changing their execution order."""

    def __init__(self, *, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._clock_ns = clock_ns
        self._started_ns = self._clock_ns()
        self._durations_ms: dict[str, float] = {}

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        started_ns = self._clock_ns()
        try:
            yield
        finally:
            duration_ms = max(0.0, (self._clock_ns() - started_ns) / 1_000_000.0)
            self._durations_ms[name] = self._durations_ms.get(name, 0.0) + duration_ms

    def elapsed_ms(self) -> float:
        return max(0.0, (self._clock_ns() - self._started_ns) / 1_000_000.0)

    def record(self, name: str, duration_ms: float) -> None:
        duration_ms = max(0.0, float(duration_ms))
        self._durations_ms[name] = self._durations_ms.get(name, 0.0) + duration_ms

    def finish(self, *, total_ms: float | None = None) -> dict[str, float]:
        if total_ms is None:
            total_ms = self.elapsed_ms()
        total_ms = max(0.0, float(total_ms))
        accounted_ms = sum(self._durations_ms.values())
        result = {
            name: round(value, 3)
            for name, value in self._durations_ms.items()
        }
        result.update(
            {
                "publish_total_ms": round(total_ms, 3),
                "publish_accounted_ms": round(accounted_ms, 3),
                "publish_unattributed_ms": round(
                    max(0.0, total_ms - accounted_ms),
                    3,
                ),
            }
        )
        return result


@dataclass(frozen=True)
class _StagedPublication:
    fragment: Fragment
    timings: _PublicationPhaseTimings
    manifest: dict[str, Any]
    manifest_stat: os.stat_result
    metadata_stat: os.stat_result
    video_stat: os.stat_result
    first_pts: int
    last_pts: int
    stage_service_ms: float


class AtomicSegmentPublisher:
    """Publish video and native JSONL metadata as one directory rename.

    Staging is a sibling of source directories, so recursive scans rooted at a
    source can never observe a half-written metadata file. Both staging and
    final paths live below the same epoch directory, making ``os.replace`` an
    atomic same-filesystem operation.
    """

    def __init__(
        self,
        *,
        cache_root: Path,
        namespace: str,
        runtime_epoch_id: str,
        source_id: str,
        session_id: str,
        commit_arbitration_enabled: bool = (
            SEGMENT_PUBLICATION_COMMIT_ARBITRATION_ENABLED
        ),
        commit_slot_count: int = 0,
        file_sync_mode: str = "fsync",
    ) -> None:
        epoch = safe_component(runtime_epoch_id, field="runtime_epoch_id")
        source = safe_component(source_id, field="source_id")
        session = safe_component(session_id, field="session_id")
        namespace = safe_component(namespace, field="namespace")
        epoch_root = cache_root / namespace / "epochs" / epoch
        self._staging_root = epoch_root / ".rolling-cache-staging" / source / session
        self._segments_root = epoch_root / source / "segments"
        self._commit_arbitration_enabled = bool(commit_arbitration_enabled)
        requested_commit_slot_count = int(commit_slot_count)
        if not 0 <= requested_commit_slot_count <= (
            MAX_ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS
        ):
            raise ValueError(
                "commit_slot_count must be between 0 and "
                f"{MAX_ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS}, got "
                f"{requested_commit_slot_count}"
            )
        if self._commit_arbitration_enabled and requested_commit_slot_count > 1:
            raise ValueError(
                "commit_arbitration_enabled cannot be combined with "
                "commit_slot_count > 1"
            )
        self._commit_slot_count = (
            1 if self._commit_arbitration_enabled else requested_commit_slot_count
        )
        self._commit_slot_index = (
            zlib.crc32(source.encode("utf-8")) % self._commit_slot_count
            if self._commit_slot_count > 0
            else -1
        )
        requested_file_sync_mode = str(file_sync_mode).strip().lower()
        if requested_file_sync_mode not in {"fsync", "fdatasync"}:
            raise ValueError(
                "file_sync_mode must be fsync or fdatasync, got "
                f"{requested_file_sync_mode!r}"
            )
        if requested_file_sync_mode == "fdatasync" and not callable(
            getattr(os, "fdatasync", None)
        ):
            raise RuntimeError("fdatasync is unavailable on this platform")
        self._file_sync_mode = requested_file_sync_mode
        if self._commit_slot_count == 0:
            self._commit_lock_path: Path | None = None
        elif self._commit_slot_count == 1:
            self._commit_lock_path = (
                epoch_root / SEGMENT_PUBLICATION_COMMIT_LOCK_FILE
            )
        else:
            self._commit_lock_path = epoch_root / (
                SEGMENT_PUBLICATION_COMMIT_SLOT_FILE.format(
                    slot=self._commit_slot_index
                )
            )
        self._session_id = session
        self._runtime_epoch_id = epoch
        self._source_id = source

    def prepare(self, fragment_id: int) -> Fragment:
        segment_id = f"{self._session_id}-{int(fragment_id):08d}"
        staging_dir = self._staging_root / f"{segment_id}.partial"
        final_dir = self._segments_root / segment_id
        if staging_dir.exists() or final_dir.exists():
            raise FileExistsError(f"rolling_segment_collision:{segment_id}")
        staging_dir.mkdir(parents=True, exist_ok=False)
        return Fragment(
            segment_id=segment_id,
            staging_dir=staging_dir,
            final_dir=final_dir,
            video_path=staging_dir / "video.mov",
        )

    def stage_publication(self, fragment: Fragment) -> _StagedPublication:
        """Write and flush one segment's metadata without making it durable."""

        timings = _PublicationPhaseTimings()
        with timings.measure("validate_ms"):
            if not fragment.video_path.is_file():
                raise RuntimeError(
                    f"rolling_segment_video_missing:{fragment.segment_id}"
                )
            video_stat = fragment.video_path.stat()
            if video_stat.st_size <= 0:
                raise RuntimeError(
                    f"rolling_segment_video_missing:{fragment.segment_id}"
                )
            pts_values = [
                pts
                for pts in (_row_pts(row) for row in fragment.rows)
                if pts is not None
            ]
            if not pts_values:
                raise RuntimeError(
                    f"rolling_segment_metadata_has_no_pts:{fragment.segment_id}"
                )
            source_pts_values = [
                pts
                for pts in (_source_row_pts(row) for row in fragment.rows)
                if pts is not None
            ]

        metadata_path = fragment.staging_dir / "metadata.json"
        with metadata_path.open("x", encoding="utf-8") as handle:
            with timings.measure("metadata_write_ms"):
                for row in fragment.rows:
                    handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
                    handle.write("\n")
                handle.flush()
        with timings.measure("metadata_stat_ms"):
            metadata_stat = metadata_path.stat()
        video_size_bytes = video_stat.st_size
        metadata_size_bytes = metadata_stat.st_size
        manifest = {
            "schema_version": SEGMENT_MANIFEST_SCHEMA_VERSION,
            "segment_id": fragment.segment_id,
            "source_id": self._source_id,
            "runtime_epoch_id": self._runtime_epoch_id,
            "video_file": fragment.video_path.name,
            "metadata_file": metadata_path.name,
            "first_pts": min(pts_values),
            "last_pts": max(pts_values),
            "frame_count": len(pts_values),
            "source_first_pts": (
                min(source_pts_values) if source_pts_values else None
            ),
            "source_last_pts": (
                max(source_pts_values) if source_pts_values else None
            ),
            "video_size_bytes": video_size_bytes,
            "metadata_size_bytes": metadata_size_bytes,
        }
        manifest_path = fragment.staging_dir / SEGMENT_MANIFEST_FILE
        with manifest_path.open("x", encoding="utf-8") as handle:
            with timings.measure("manifest_write_ms"):
                handle.write(
                    json.dumps(manifest, separators=(",", ":"), ensure_ascii=False)
                )
                handle.write("\n")
                handle.flush()
        with timings.measure("manifest_stat_ms"):
            manifest_stat = manifest_path.stat()

        return _StagedPublication(
            fragment=fragment,
            timings=timings,
            manifest=manifest,
            manifest_stat=manifest_stat,
            metadata_stat=metadata_stat,
            video_stat=video_stat,
            first_pts=min(pts_values),
            last_pts=max(pts_values),
            stage_service_ms=timings.elapsed_ms(),
        )

    def commit_publication(self, staged: _StagedPublication) -> Path:
        """Durably commit one staged segment through the original fence order."""

        fragment = staged.fragment
        timings = staged.timings
        commit_started_ns = time.monotonic_ns()
        commit_lock_hold_ms = 0.0
        timings.record("commit_lock_wait_ms", 0.0)
        metadata_path = fragment.staging_dir / "metadata.json"
        manifest_path = fragment.staging_dir / SEGMENT_MANIFEST_FILE
        try:
            lock_context = (
                self._commit_lock_path.open("a+b")
                if self._commit_lock_path is not None
                else nullcontext()
            )
            with lock_context as lock_handle:
                lock_hold_started_ns: int | None = None
                if lock_handle is not None:
                    lock_wait_started_ns = time.monotonic_ns()
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                    finally:
                        timings.record(
                            "commit_lock_wait_ms",
                            (
                                time.monotonic_ns() - lock_wait_started_ns
                            )
                            / 1_000_000.0,
                        )
                    lock_hold_started_ns = time.monotonic_ns()
                try:
                    file_sync = (
                        os.fdatasync
                        if self._file_sync_mode == "fdatasync"
                        else os.fsync
                    )
                    with metadata_path.open("rb") as handle:
                        with timings.measure("metadata_fsync_ms"):
                            file_sync(handle.fileno())
                    with manifest_path.open("rb") as handle:
                        with timings.measure("manifest_fsync_ms"):
                            file_sync(handle.fileno())
                    with timings.measure("staging_dir_fsync_ms"):
                        _fsync_directory(fragment.staging_dir)

                    with timings.measure("parent_prepare_ms"):
                        fragment.final_dir.parent.mkdir(parents=True, exist_ok=True)
                        if fragment.final_dir.exists():
                            raise FileExistsError(
                                "rolling_segment_final_collision:"
                                f"{fragment.segment_id}"
                            )
                    with timings.measure("rename_ms"):
                        os.replace(fragment.staging_dir, fragment.final_dir)
                    with timings.measure("parent_dir_fsync_ms"):
                        _fsync_directory(fragment.final_dir.parent)
                    with timings.measure("journal_append_ms"):
                        try:
                            _append_publication_record(
                                segments_root=fragment.final_dir.parent,
                                manifest=staged.manifest,
                                manifest_stat=staged.manifest_stat,
                                metadata_stat=staged.metadata_stat,
                                video_stat=staged.video_stat,
                            )
                        except Exception as exc:
                            # The atomic segment directory is authoritative. The
                            # journal is a bounded discovery accelerator, so a
                            # crash or append failure in this post-commit window
                            # leaves the segment usable; periodic reconciliation
                            # recovers it.
                            LOGGER.warning(
                                "segment publication journal append failed "
                                "source=%s epoch=%s segment=%s path=%s error=%s",
                                self._source_id,
                                self._runtime_epoch_id,
                                fragment.segment_id,
                                fragment.final_dir,
                                exc,
                            )
                finally:
                    if (
                        lock_handle is not None
                        and lock_hold_started_ns is not None
                    ):
                        try:
                            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                        finally:
                            commit_lock_hold_ms = max(
                                0.0,
                                (
                                    time.monotonic_ns() - lock_hold_started_ns
                                )
                                / 1_000_000.0,
                            )
        finally:
            commit_service_ms = max(
                0.0,
                (time.monotonic_ns() - commit_started_ns) / 1_000_000.0,
            )
            phase_diagnostics = timings.finish(
                total_ms=staged.stage_service_ms + commit_service_ms
            )
            fragment.publication_diagnostics = {
                "schema_version": "rolling-segment-publication-timing-v1",
                "first_pts": staged.first_pts,
                "last_pts": staged.last_pts,
                "publish_stage_ms": round(staged.stage_service_ms, 3),
                "publish_commit_ms": round(commit_service_ms, 3),
                "publish_commit_lock_hold_ms": round(commit_lock_hold_ms, 3),
                "publish_commit_slot_count": self._commit_slot_count,
                "publish_commit_slot_index": self._commit_slot_index,
                "publish_file_sync_mode": self._file_sync_mode,
                "publish_file_fdatasync_enabled": int(
                    self._file_sync_mode == "fdatasync"
                ),
                **{
                    (
                        name if name.startswith("publish_") else f"publish_{name}"
                    ): value
                    for name, value in phase_diagnostics.items()
                },
            }
        return fragment.final_dir

    def publish(self, fragment: Fragment) -> Path:
        """Compatibility path for callers that publish one segment inline."""

        return self.commit_publication(self.stage_publication(fragment))


def _identity_payload(stat: os.stat_result) -> dict[str, int]:
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _publication_record_bytes(
    *,
    manifest: dict[str, Any],
    manifest_stat: os.stat_result,
    metadata_stat: os.stat_result,
    video_stat: os.stat_result,
) -> bytes:
    record: dict[str, Any] = {
        "schema_version": SEGMENT_PUBLICATION_SCHEMA_VERSION,
        "source_id": manifest["source_id"],
        "runtime_epoch_id": manifest["runtime_epoch_id"],
        "segment_id": manifest["segment_id"],
        "manifest": manifest,
        "identities": {
            "manifest": _identity_payload(manifest_stat),
            "metadata": _identity_payload(metadata_stat),
            "video": _identity_payload(video_stat),
        },
    }
    canonical = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    record["crc32"] = f"{zlib.crc32(canonical) & 0xFFFFFFFF:08x}"
    encoded = (
        json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > MAX_SEGMENT_PUBLICATION_RECORD_BYTES:
        raise RuntimeError(
            "rolling_segment_publication_record_too_large:"
            f"{manifest.get('segment_id')}:{len(encoded)}"
        )
    return encoded


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("rolling_segment_publication_journal_short_write")
        view = view[written:]


def _append_publication_record(
    *,
    segments_root: Path,
    manifest: dict[str, Any],
    manifest_stat: os.stat_result,
    metadata_stat: os.stat_result,
    video_stat: os.stat_result,
) -> None:
    """Append one bounded discovery hint after the atomic segment commit.

    The journal intentionally does not fsync each record: the segment directory
    was already fsynced and remains the correctness source of truth. A host
    crash may lose a recent hint, which periodic reconciliation is required to
    recover. A separate flock serializes append/rotation and lets readers avoid
    observing an in-process partial write.
    """

    encoded = _publication_record_bytes(
        manifest=manifest,
        manifest_stat=manifest_stat,
        metadata_stat=metadata_stat,
        video_stat=video_stat,
    )
    journal_path = segments_root / SEGMENT_PUBLICATION_JOURNAL_FILE
    lock_path = segments_root / SEGMENT_PUBLICATION_JOURNAL_LOCK_FILE
    with lock_path.open("a+b") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            try:
                current_size = int(journal_path.stat().st_size)
            except OSError:
                current_size = 0
            if current_size + len(encoded) <= SEGMENT_PUBLICATION_JOURNAL_MAX_BYTES:
                fd = os.open(
                    journal_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o640,
                )
                try:
                    _write_all(fd, encoded)
                finally:
                    os.close(fd)
                return

            temp_path = segments_root / (
                f".{SEGMENT_PUBLICATION_JOURNAL_FILE}.{os.getpid()}."
                f"{threading.get_ident()}.tmp"
            )
            fd = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o640,
            )
            try:
                _write_all(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.replace(temp_path, journal_path)
                _fsync_directory(segments_root)
            finally:
                temp_path.unlink(missing_ok=True)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class _QueuedPublication:
    source_id: str
    worker_index: int
    publisher: Any
    fragment: Fragment
    on_published: Callable[[Fragment, Path], None] | None
    on_publish_error: Callable[[Fragment, Exception], None] | None
    requested_at_ns: int
    accepted_at_ns: int
    capacity_wait_ms: float
    outstanding_at_submit: int
    queue_depth_at_submit: int


@dataclass(frozen=True)
class _PreparedPublication:
    queued: _QueuedPublication
    staged: Any
    split_publication: bool
    stage_error: Exception | None
    queue_residence_ms: float
    prepare_service_ms: float
    stage_completed_at_ns: int
    group_size: int
    group_position: int


def _diagnostic_duration_ms(fragment: Any, name: str) -> float:
    diagnostics = getattr(fragment, "publication_diagnostics", {}) or {}
    try:
        return max(0.0, float(diagnostics.get(name, 0.0)))
    except (TypeError, ValueError):
        return 0.0


class BoundedPublicationDispatcher:
    """Durably publish through bounded, source-stable FIFO worker shards.

    ``crc32(source_id) % worker_count`` keeps every source on one FIFO while
    allowing unrelated sources on different shards to overlap slow durability
    work. The finite outstanding semaphore is global across all shard queues,
    preserving explicit callback backpressure under a sustained storage stall.
    """

    _STOP = object()

    def __init__(
        self,
        *,
        capacity: int = SEGMENT_PUBLICATION_OUTSTANDING_LIMIT,
        prepare_group_limit: int = SEGMENT_PUBLICATION_PREPARE_GROUP_LIMIT,
        worker_count: int = 1,
        metrics: Any | None = None,
        thread_name: str = "rolling-cache-publication",
        clock_ns: Callable[[], int] = time.monotonic_ns,
        wall_clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.prepare_group_limit = min(
            self.capacity,
            max(1, int(prepare_group_limit)),
        )
        self.worker_count = int(worker_count)
        if not 1 <= self.worker_count <= MAX_ROLLING_CACHE_PUBLICATION_WORKERS:
            raise ValueError(
                "worker_count must be between 1 and "
                f"{MAX_ROLLING_CACHE_PUBLICATION_WORKERS}, got {self.worker_count}"
            )
        self._metrics = metrics
        self._clock_ns = clock_ns
        self._wall_clock_ns = wall_clock_ns
        self._queues: tuple[queue.Queue[_QueuedPublication | object], ...] = tuple(
            queue.Queue(maxsize=self.capacity) for _ in range(self.worker_count)
        )
        self._slots = threading.BoundedSemaphore(self.capacity)
        self._state_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._accepting = True
        self._outstanding = 0
        self._outstanding_peak = 0
        self._active = 0
        self._active_peak = 0
        self._queue_depth_peak = 0
        self._submitted_total = 0
        self._completed_total = 0
        self._failed_total = 0
        self._queue_wait_ms_total = 0.0
        self._queue_wait_ms_max = 0.0
        self._queue_wait_events_total = 0
        self._prepare_group_total = 0
        self._prepare_group_size_max = 0
        self._prepare_service_ms_total = 0.0
        self._prepare_service_ms_max = 0.0
        self._commit_wait_ms_total = 0.0
        self._commit_wait_ms_max = 0.0
        self._commit_lock_wait_ms_total = 0.0
        self._commit_lock_wait_ms_max = 0.0
        self._commit_lock_wait_events_total = 0
        self._commit_lock_hold_ms_total = 0.0
        self._commit_lock_hold_ms_max = 0.0
        self._queue_residence_ms_total = 0.0
        self._queue_residence_ms_max = 0.0
        self._queue_residence_events_total = 0
        self._worker_service_ms_total = 0.0
        self._worker_service_ms_max = 0.0
        self._dispatch_total_ms_total = 0.0
        self._dispatch_total_ms_max = 0.0
        self._shutdown_timeout_total = 0
        self._outstanding_peak_at_epoch_ms = 0
        self._outstanding_peak_source_id = "none"
        self._outstanding_peak_segment_id = "none"
        self._outstanding_peak_queue_depth = 0
        self._threads = tuple(
            threading.Thread(
                target=self._run,
                args=(worker_index,),
                name=(
                    thread_name
                    if self.worker_count == 1
                    else f"{thread_name}-{worker_index}"
                ),
                daemon=True,
            )
            for worker_index in range(self.worker_count)
        )
        for thread in self._threads:
            thread.start()
        self._sync_metrics(self.snapshot())

    def submit(
        self,
        *,
        source_id: str,
        publisher: Any,
        fragment: Fragment,
        on_published: Callable[[Fragment, Path], None] | None = None,
        on_publish_error: Callable[[Fragment, Exception], None] | None = None,
    ) -> None:
        source_id = str(source_id)
        worker_index = (
            zlib.crc32(source_id.encode("utf-8")) % self.worker_count
        )
        with self._state_lock:
            if not self._accepting:
                raise RuntimeError("rolling_segment_publication_dispatcher_closed")

        requested_at_ns = self._clock_ns()
        self._slots.acquire()
        accepted_at_ns = self._clock_ns()
        wait_ms = max(0.0, (accepted_at_ns - requested_at_ns) / 1_000_000.0)
        with self._state_lock:
            if not self._accepting:
                self._slots.release()
                raise RuntimeError("rolling_segment_publication_dispatcher_closed")
            self._outstanding += 1
            self._submitted_total += 1
            queue_depth = max(0, self._outstanding - self._active)
            if self._outstanding > self._outstanding_peak:
                self._outstanding_peak = self._outstanding
                self._outstanding_peak_at_epoch_ms = max(
                    0,
                    int(self._wall_clock_ns() // 1_000_000),
                )
                self._outstanding_peak_source_id = str(source_id)
                self._outstanding_peak_segment_id = str(fragment.segment_id)
                self._outstanding_peak_queue_depth = queue_depth
            self._queue_depth_peak = max(self._queue_depth_peak, queue_depth)
            self._queue_wait_ms_total += wait_ms
            self._queue_wait_ms_max = max(self._queue_wait_ms_max, wait_ms)
            if wait_ms >= 1.0:
                self._queue_wait_events_total += 1
            self._idle.clear()
            state = self._snapshot_locked()
        task = _QueuedPublication(
            source_id=source_id,
            worker_index=worker_index,
            publisher=publisher,
            fragment=fragment,
            on_published=on_published,
            on_publish_error=on_publish_error,
            requested_at_ns=requested_at_ns,
            accepted_at_ns=accepted_at_ns,
            capacity_wait_ms=wait_ms,
            outstanding_at_submit=int(state["outstanding"]),
            queue_depth_at_submit=queue_depth,
        )
        try:
            self._queues[worker_index].put_nowait(task)
        except Exception:
            with self._state_lock:
                self._outstanding -= 1
                self._submitted_total -= 1
                if self._outstanding == 0:
                    self._idle.set()
                state = self._snapshot_locked()
            self._slots.release()
            self._sync_metrics(state)
            raise
        self._sync_metrics(state)

    def snapshot(self) -> dict[str, int | float | bool]:
        with self._state_lock:
            return self._snapshot_locked()

    def peak_snapshot(self) -> dict[str, int | str]:
        with self._state_lock:
            return {
                "outstanding": self._outstanding_peak,
                "queue_depth": self._outstanding_peak_queue_depth,
                "at_epoch_ms": self._outstanding_peak_at_epoch_ms,
                "source_id": self._outstanding_peak_source_id,
                "segment_id": self._outstanding_peak_segment_id,
            }

    def close(self, *, timeout_s: float) -> bool:
        timeout_s = max(0.0, float(timeout_s))
        started_at = time.monotonic()
        with self._state_lock:
            self._accepting = False
            state = self._snapshot_locked()
        self._sync_metrics(state)
        if not self._idle.wait(timeout_s):
            with self._state_lock:
                self._shutdown_timeout_total += 1
                state = self._snapshot_locked()
            self._sync_metrics(state)
            return False
        live_threads = [thread for thread in self._threads if thread.is_alive()]
        if not live_threads:
            return True
        for worker_index, thread in enumerate(self._threads):
            if not thread.is_alive():
                continue
            try:
                self._queues[worker_index].put_nowait(self._STOP)
            except queue.Full:
                with self._state_lock:
                    self._shutdown_timeout_total += 1
                    state = self._snapshot_locked()
                self._sync_metrics(state)
                return False
        for thread in live_threads:
            remaining_s = max(0.0, timeout_s - (time.monotonic() - started_at))
            thread.join(remaining_s)
        if any(thread.is_alive() for thread in live_threads):
            with self._state_lock:
                self._shutdown_timeout_total += 1
                state = self._snapshot_locked()
            self._sync_metrics(state)
            return False
        return True

    def _run(self, worker_index: int) -> None:
        worker_queue = self._queues[worker_index]
        while True:
            first = worker_queue.get()
            if first is self._STOP:
                worker_queue.task_done()
                return
            assert isinstance(first, _QueuedPublication)
            group = [first]
            while len(group) < self.prepare_group_limit:
                try:
                    queued = worker_queue.get_nowait()
                except queue.Empty:
                    break
                assert isinstance(queued, _QueuedPublication)
                group.append(queued)

            with self._state_lock:
                self._active += 1
                self._active_peak = max(self._active_peak, self._active)
                self._prepare_group_total += 1
                self._prepare_group_size_max = max(
                    self._prepare_group_size_max,
                    len(group),
                )
                state = self._snapshot_locked()
            self._sync_metrics(state)

            prepared: list[_PreparedPublication] = []
            group_size = len(group)
            for group_position, queued in enumerate(group, start=1):
                stage_started_at_ns = self._clock_ns()
                queue_residence_ms = max(
                    0.0,
                    (stage_started_at_ns - queued.accepted_at_ns) / 1_000_000.0,
                )
                stage_error: Exception | None = None
                staged: Any = queued.fragment
                stage_method = getattr(
                    queued.publisher,
                    "stage_publication",
                    None,
                )
                commit_method = getattr(
                    queued.publisher,
                    "commit_publication",
                    None,
                )
                split_publication = callable(stage_method) and callable(
                    commit_method
                )
                try:
                    if split_publication:
                        staged = stage_method(queued.fragment)
                except Exception as exc:
                    stage_error = exc
                stage_completed_at_ns = self._clock_ns()
                prepared.append(
                    _PreparedPublication(
                        queued=queued,
                        staged=staged,
                        split_publication=split_publication,
                        stage_error=stage_error,
                        queue_residence_ms=queue_residence_ms,
                        prepare_service_ms=max(
                            0.0,
                            (
                                stage_completed_at_ns - stage_started_at_ns
                            )
                            / 1_000_000.0,
                        ),
                        stage_completed_at_ns=stage_completed_at_ns,
                        group_size=group_size,
                        group_position=group_position,
                    )
                )

            for item in prepared:
                self._commit_prepared(item)

            with self._state_lock:
                self._active = max(0, self._active - 1)
                state = self._snapshot_locked()
            self._sync_metrics(state)

    def _commit_prepared(self, item: _PreparedPublication) -> None:
        queued = item.queued
        commit_started_at_ns = self._clock_ns()
        commit_wait_ms = max(
            0.0,
            (commit_started_at_ns - item.stage_completed_at_ns) / 1_000_000.0,
        )
        failed = item.stage_error is not None
        error = item.stage_error
        final_dir: Path | None = None
        worker_completed_at_ns = commit_started_at_ns
        if not failed:
            try:
                if item.split_publication:
                    final_dir = queued.publisher.commit_publication(item.staged)
                else:
                    final_dir = queued.publisher.publish(queued.fragment)
            except Exception as exc:
                failed = True
                error = exc
            worker_completed_at_ns = self._clock_ns()

        worker_service_ms = max(
            0.0,
            (worker_completed_at_ns - commit_started_at_ns) / 1_000_000.0,
        )
        dispatch_total_ms = max(
            0.0,
            (worker_completed_at_ns - queued.requested_at_ns) / 1_000_000.0,
        )
        self._attach_dispatch_diagnostics(
            item,
            commit_wait_ms=commit_wait_ms,
            worker_service_ms=worker_service_ms,
            dispatch_total_ms=dispatch_total_ms,
        )
        commit_lock_wait_ms = _diagnostic_duration_ms(
            queued.fragment,
            "publish_commit_lock_wait_ms",
        )
        commit_lock_hold_ms = _diagnostic_duration_ms(
            queued.fragment,
            "publish_commit_lock_hold_ms",
        )

        if failed:
            assert error is not None
            if queued.on_publish_error is not None:
                try:
                    queued.on_publish_error(queued.fragment, error)
                except Exception:
                    LOGGER.exception(
                        "segment publication error callback failed "
                        "source=%s segment=%s",
                        queued.source_id,
                        queued.fragment.segment_id,
                    )
        elif queued.on_published is not None:
            assert final_dir is not None
            try:
                queued.on_published(queued.fragment, final_dir)
            except Exception:
                failed = True
                LOGGER.exception(
                    "segment publication success callback failed "
                    "source=%s segment=%s",
                    queued.source_id,
                    queued.fragment.segment_id,
                )

        with self._state_lock:
            self._outstanding = max(0, self._outstanding - 1)
            if failed:
                self._failed_total += 1
            else:
                self._completed_total += 1
            self._prepare_service_ms_total += item.prepare_service_ms
            self._prepare_service_ms_max = max(
                self._prepare_service_ms_max,
                item.prepare_service_ms,
            )
            self._commit_wait_ms_total += commit_wait_ms
            self._commit_wait_ms_max = max(
                self._commit_wait_ms_max,
                commit_wait_ms,
            )
            self._commit_lock_wait_ms_total += commit_lock_wait_ms
            self._commit_lock_wait_ms_max = max(
                self._commit_lock_wait_ms_max,
                commit_lock_wait_ms,
            )
            if commit_lock_wait_ms >= 1.0:
                self._commit_lock_wait_events_total += 1
            self._commit_lock_hold_ms_total += commit_lock_hold_ms
            self._commit_lock_hold_ms_max = max(
                self._commit_lock_hold_ms_max,
                commit_lock_hold_ms,
            )
            self._queue_residence_ms_total += item.queue_residence_ms
            self._queue_residence_ms_max = max(
                self._queue_residence_ms_max,
                item.queue_residence_ms,
            )
            if item.queue_residence_ms >= 1.0:
                self._queue_residence_events_total += 1
            self._worker_service_ms_total += worker_service_ms
            self._worker_service_ms_max = max(
                self._worker_service_ms_max,
                worker_service_ms,
            )
            self._dispatch_total_ms_total += dispatch_total_ms
            self._dispatch_total_ms_max = max(
                self._dispatch_total_ms_max,
                dispatch_total_ms,
            )
            if self._outstanding == 0:
                self._idle.set()
            state = self._snapshot_locked()
        self._slots.release()
        self._queues[queued.worker_index].task_done()
        self._sync_metrics(state)

    def _attach_dispatch_diagnostics(
        self,
        item: _PreparedPublication,
        *,
        commit_wait_ms: float,
        worker_service_ms: float,
        dispatch_total_ms: float,
    ) -> None:
        queued = item.queued
        diagnostics = dict(
            getattr(queued.fragment, "publication_diagnostics", {}) or {}
        )
        diagnostics.update(
            {
                "publication_capacity_wait_ms": round(
                    queued.capacity_wait_ms,
                    3,
                ),
                "publication_queue_residence_ms": round(
                    item.queue_residence_ms,
                    3,
                ),
                "publication_prepare_service_ms": round(
                    item.prepare_service_ms,
                    3,
                ),
                "publication_commit_wait_ms": round(
                    commit_wait_ms,
                    3,
                ),
                "publication_worker_service_ms": round(
                    worker_service_ms,
                    3,
                ),
                "publication_dispatch_total_ms": round(
                    dispatch_total_ms,
                    3,
                ),
                "publication_outstanding_at_submit": (
                    queued.outstanding_at_submit
                ),
                "publication_queue_depth_at_submit": queued.queue_depth_at_submit,
                "publication_worker_index": queued.worker_index,
                "publication_prepare_group_size": item.group_size,
                "publication_prepare_group_position": item.group_position,
            }
        )
        queued.fragment.publication_diagnostics = diagnostics

    def _snapshot_locked(self) -> dict[str, int | float | bool]:
        return {
            "accepting": self._accepting,
            "capacity": self.capacity,
            "worker_count": self.worker_count,
            "queue_depth": max(0, self._outstanding - self._active),
            "queue_depth_peak": self._queue_depth_peak,
            "outstanding": self._outstanding,
            "outstanding_peak": self._outstanding_peak,
            "active": self._active,
            "active_peak": self._active_peak,
            "submitted_total": self._submitted_total,
            "completed_total": self._completed_total,
            "failed_total": self._failed_total,
            "queue_wait_ms_total": round(self._queue_wait_ms_total, 3),
            "queue_wait_ms_max": round(self._queue_wait_ms_max, 3),
            "queue_wait_events_total": self._queue_wait_events_total,
            "prepare_group_limit": self.prepare_group_limit,
            "prepare_group_total": self._prepare_group_total,
            "prepare_group_size_max": self._prepare_group_size_max,
            "prepare_service_ms_total": round(
                self._prepare_service_ms_total,
                3,
            ),
            "prepare_service_ms_max": round(
                self._prepare_service_ms_max,
                3,
            ),
            "commit_wait_ms_total": round(self._commit_wait_ms_total, 3),
            "commit_wait_ms_max": round(self._commit_wait_ms_max, 3),
            "commit_lock_wait_ms_total": round(
                self._commit_lock_wait_ms_total,
                3,
            ),
            "commit_lock_wait_ms_max": round(
                self._commit_lock_wait_ms_max,
                3,
            ),
            "commit_lock_wait_events_total": (
                self._commit_lock_wait_events_total
            ),
            "commit_lock_hold_ms_total": round(
                self._commit_lock_hold_ms_total,
                3,
            ),
            "commit_lock_hold_ms_max": round(
                self._commit_lock_hold_ms_max,
                3,
            ),
            "queue_residence_ms_total": round(
                self._queue_residence_ms_total,
                3,
            ),
            "queue_residence_ms_max": round(self._queue_residence_ms_max, 3),
            "queue_residence_events_total": self._queue_residence_events_total,
            "worker_service_ms_total": round(self._worker_service_ms_total, 3),
            "worker_service_ms_max": round(self._worker_service_ms_max, 3),
            "dispatch_total_ms_total": round(self._dispatch_total_ms_total, 3),
            "dispatch_total_ms_max": round(self._dispatch_total_ms_max, 3),
            "outstanding_peak_at_epoch_ms": self._outstanding_peak_at_epoch_ms,
            "shutdown_timeout_total": self._shutdown_timeout_total,
        }

    def _sync_metrics(self, state: dict[str, int | float | bool]) -> None:
        if self._metrics is None:
            return
        for name, value in state.items():
            if name == "accepting":
                self._metrics.set("publication_accepting", int(bool(value)))
                continue
            self._metrics.set(f"publication_{name}", float(value))


class FragmentLedger:
    """Assign frame rows to splitmux fragments and finalize closed fragments."""

    def __init__(
        self,
        publisher: AtomicSegmentPublisher,
        *,
        publication_dispatcher: BoundedPublicationDispatcher | None = None,
        on_published: Callable[[Fragment, Path], None] | None = None,
        on_publish_error: Callable[[Fragment, Exception], None] | None = None,
    ) -> None:
        self._publisher = publisher
        self._publication_dispatcher = publication_dispatcher
        self._on_published = on_published
        self._on_publish_error = on_publish_error
        self._lock = threading.RLock()
        self._by_location: dict[str, Fragment] = {}
        self._ordered: list[Fragment] = []
        self._queued_rows: list[tuple[int, int, dict[str, Any]]] = []
        self._next_row_id = 0
        self._eos_rows: list[dict[str, Any]] = []
        self._pending_close_locations: set[str] = set()
        self._pending_publications = 0

    def open_fragment(self, fragment_id: int, first_gst_pts: int) -> str:
        fragment = self._publisher.prepare(fragment_id)
        fragment.first_gst_pts = int(first_gst_pts)
        location = str(fragment.video_path)
        with self._lock:
            self._by_location[location] = fragment
            self._ordered.append(fragment)
            pending_close_locations = list(self._pending_close_locations)
        # async-finalize may report the previous fragment closed before this
        # format-location callback establishes its end boundary. Finalize those
        # deferred closes now that the next fragment is known.
        for pending_location in pending_close_locations:
            self.close_fragment(pending_location)
        return location

    def queue_frame(self, gst_pts: int, row: dict[str, Any]) -> int:
        with self._lock:
            row_id = self._next_row_id
            self._next_row_id += 1
            self._queued_rows.append((row_id, int(gst_pts), row))
            return row_id

    def discard_frame(self, row_id: int) -> None:
        with self._lock:
            self._queued_rows = [
                queued for queued in self._queued_rows if queued[0] != row_id
            ]

    def first_queued_gst_pts(self) -> int | None:
        with self._lock:
            if not self._queued_rows:
                return None
            return self._queued_rows[0][1]

    def record_eos(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._eos_rows.append(row)
            pending_close_locations = list(self._pending_close_locations)
        for pending_location in pending_close_locations:
            self.close_fragment(pending_location)

    def close_fragment(self, location: str) -> Path | None:
        with self._lock:
            fragment = self._by_location.get(location)
            if fragment is None:
                raise KeyError(f"unknown_splitmux_fragment:{location}")
            try:
                index = self._ordered.index(fragment)
            except ValueError as exc:
                raise KeyError(f"unordered_splitmux_fragment:{location}") from exc
            next_fragment = (
                self._ordered[index + 1] if index + 1 < len(self._ordered) else None
            )
            if next_fragment is None and not self._eos_rows:
                self._pending_close_locations.add(location)
                return None
            start_pts = fragment.first_gst_pts
            end_pts = next_fragment.first_gst_pts if next_fragment is not None else None
            if start_pts is None:
                raise RuntimeError(
                    f"splitmux_fragment_start_pts_missing:{fragment.segment_id}"
                )
            selected: list[tuple[int, int, dict[str, Any]]] = []
            remaining: list[tuple[int, int, dict[str, Any]]] = []
            for queued in self._queued_rows:
                _, gst_pts, _ = queued
                if gst_pts >= start_pts and (end_pts is None or gst_pts < end_pts):
                    selected.append(queued)
                else:
                    remaining.append(queued)
            fragment.rows.extend(row for _, _, row in sorted(selected))
            if next_fragment is None:
                fragment.rows.extend(self._eos_rows)
                self._eos_rows = []
            self._queued_rows = remaining
            self._pending_close_locations.discard(location)
            self._by_location.pop(location, None)
            self._ordered.remove(fragment)
            if self._publication_dispatcher is not None:
                self._pending_publications += 1
        if self._publication_dispatcher is not None:
            try:
                self._publication_dispatcher.submit(
                    source_id=next(
                        (
                            str(row.get("source_id") or "")
                            for row in fragment.rows
                            if row.get("source_id")
                        ),
                        "unknown",
                    ),
                    publisher=self._publisher,
                    fragment=fragment,
                    on_published=self._async_published,
                    on_publish_error=self._async_publish_error,
                )
            except Exception as exc:
                self._publication_finished()
                if self._on_publish_error is not None:
                    self._on_publish_error(fragment, exc)
                raise
            return fragment.final_dir
        try:
            final_dir = self._publisher.publish(fragment)
        except Exception as exc:
            if self._on_publish_error is not None:
                self._on_publish_error(fragment, exc)
            raise
        if self._on_published is not None:
            self._on_published(fragment, final_dir)
        return final_dir

    def pending_count(self) -> int:
        with self._lock:
            return len(self._by_location) + self._pending_publications

    def _async_published(self, fragment: Fragment, final_dir: Path) -> None:
        try:
            if self._on_published is not None:
                self._on_published(fragment, final_dir)
        finally:
            self._publication_finished()

    def _async_publish_error(self, fragment: Fragment, error: Exception) -> None:
        try:
            if self._on_publish_error is not None:
                self._on_publish_error(fragment, error)
        finally:
            self._publication_finished()

    def _publication_finished(self) -> None:
        with self._lock:
            self._pending_publications = max(0, self._pending_publications - 1)

    def abandon_pending(self) -> dict[str, int]:
        """Remove unpublished fragment staging after a pipeline is stopped.

        Published segments have already been atomically moved out of staging
        and are not present in this ledger. Only incomplete, undiscoverable
        fragments are removed, preventing failed source sessions from leaking
        partial MOV files for the lifetime of the service.
        """
        with self._lock:
            fragments = list(self._by_location.values())
            self._by_location.clear()
            self._ordered.clear()
            self._queued_rows.clear()
            self._eos_rows.clear()
            self._pending_close_locations.clear()
        removed_bytes = 0
        removed = 0
        for fragment in fragments:
            try:
                if fragment.video_path.is_file():
                    removed_bytes += fragment.video_path.stat().st_size
                shutil.rmtree(fragment.staging_dir, ignore_errors=False)
                removed += 1
            except FileNotFoundError:
                continue
        return {"fragments": removed, "bytes": removed_bytes}


def _row_pts(row: dict[str, Any]) -> int | None:
    value = row.get("rolling_cache_mux_pts")
    if value is None:
        value = row.get("pts", row.get("frame_pts"))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _source_row_pts(row: dict[str, Any]) -> int | None:
    value = row.get("pts", row.get("frame_pts"))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
