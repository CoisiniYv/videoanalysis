"""Atomic rolling-segment publication independent of GStreamer."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import threading
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from config import safe_component


SEGMENT_MANIFEST_FILE = "segment_manifest.json"
SEGMENT_MANIFEST_SCHEMA_VERSION = "rolling-segment-manifest-v1"
SEGMENT_PUBLICATION_JOURNAL_FILE = ".segment-publications.jsonl"
SEGMENT_PUBLICATION_JOURNAL_LOCK_FILE = ".segment-publications.lock"
SEGMENT_PUBLICATION_SCHEMA_VERSION = "rolling-segment-publication-v1"
SEGMENT_PUBLICATION_JOURNAL_MAX_BYTES = 16 * 1024 * 1024
MAX_SEGMENT_PUBLICATION_RECORD_BYTES = 128 * 1024

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

    def finish(self) -> dict[str, float]:
        total_ms = max(0.0, (self._clock_ns() - self._started_ns) / 1_000_000.0)
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
    ) -> None:
        epoch = safe_component(runtime_epoch_id, field="runtime_epoch_id")
        source = safe_component(source_id, field="source_id")
        session = safe_component(session_id, field="session_id")
        namespace = safe_component(namespace, field="namespace")
        epoch_root = cache_root / namespace / "epochs" / epoch
        self._staging_root = epoch_root / ".rolling-cache-staging" / source / session
        self._segments_root = epoch_root / source / "segments"
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

    def publish(self, fragment: Fragment) -> Path:
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
            with timings.measure("metadata_fsync_ms"):
                os.fsync(handle.fileno())
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
            with timings.measure("manifest_fsync_ms"):
                os.fsync(handle.fileno())
        with timings.measure("manifest_stat_ms"):
            manifest_stat = manifest_path.stat()
        with timings.measure("staging_dir_fsync_ms"):
            _fsync_directory(fragment.staging_dir)

        with timings.measure("parent_prepare_ms"):
            fragment.final_dir.parent.mkdir(parents=True, exist_ok=True)
            if fragment.final_dir.exists():
                raise FileExistsError(
                    f"rolling_segment_final_collision:{fragment.segment_id}"
                )
        with timings.measure("rename_ms"):
            os.replace(fragment.staging_dir, fragment.final_dir)
        with timings.measure("parent_dir_fsync_ms"):
            _fsync_directory(fragment.final_dir.parent)
        with timings.measure("journal_append_ms"):
            try:
                _append_publication_record(
                    segments_root=fragment.final_dir.parent,
                    manifest=manifest,
                    manifest_stat=manifest_stat,
                    metadata_stat=metadata_stat,
                    video_stat=video_stat,
                )
            except Exception as exc:
                # The atomic segment directory is authoritative. The journal is a
                # bounded discovery accelerator, so a crash or append failure in
                # this post-commit window must leave the segment usable; the media
                # worker's periodic directory reconciliation recovers it.
                LOGGER.warning(
                    "segment publication journal append failed source=%s epoch=%s "
                    "segment=%s path=%s error=%s",
                    self._source_id,
                    self._runtime_epoch_id,
                    fragment.segment_id,
                    fragment.final_dir,
                    exc,
                )
        phase_diagnostics = timings.finish()
        fragment.publication_diagnostics = {
            "schema_version": "rolling-segment-publication-timing-v1",
            "first_pts": min(pts_values),
            "last_pts": max(pts_values),
            **{
                (name if name.startswith("publish_") else f"publish_{name}"): value
                for name, value in phase_diagnostics.items()
            },
        }
        return fragment.final_dir


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


class FragmentLedger:
    """Assign frame rows to splitmux fragments and finalize closed fragments."""

    def __init__(
        self,
        publisher: AtomicSegmentPublisher,
        *,
        on_published: Callable[[Fragment, Path], None] | None = None,
        on_publish_error: Callable[[Fragment, Exception], None] | None = None,
    ) -> None:
        self._publisher = publisher
        self._on_published = on_published
        self._on_publish_error = on_publish_error
        self._lock = threading.RLock()
        self._by_location: dict[str, Fragment] = {}
        self._ordered: list[Fragment] = []
        self._queued_rows: list[tuple[int, int, dict[str, Any]]] = []
        self._next_row_id = 0
        self._eos_rows: list[dict[str, Any]] = []
        self._pending_close_locations: set[str] = set()

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
            return len(self._by_location)

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
