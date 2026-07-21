"""Process-lifetime rolling-cache segment catalog and read-pin contract."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
import zlib
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

from app.post_savant_metadata_annotation_builder import load_native_metadata
from app.rolling_cache import RollingCacheCoverageMiss, RollingSegment, VIDEO_NAMES


GENERATION_FILE = ".rolling-cache-generation"
READ_PIN_DIR = ".read-pins"
MUTATION_LOCK_FILE = ".rolling-cache-mutation.lock"
READ_PIN_SCHEMA_VERSION = "rolling-segment-read-pin-v1"
SEGMENT_MANIFEST_FILE = "segment_manifest.json"
SEGMENT_MANIFEST_SCHEMA_VERSION = "rolling-segment-manifest-v1"
MAX_SEGMENT_MANIFEST_BYTES = 64 * 1024

_OPERATION_TIMING_FIELDS = (
    "io_slot_wait_ms",
    "lock_wait_ms",
    "lock_hold_ms",
    "refresh_ms",
    "rebuild_ms",
    "stat_ms",
    "full_row_parse_ms",
    "manifest_parse_ms",
    "sort_ms",
    "mutation_lock_wait_ms",
    "pin_publish_ms",
    "pin_release_ms",
)
_OPERATION_COUNT_FIELDS = (
    "stat_calls",
    "full_row_parses",
    "manifest_parses",
    "scanned_known",
    "new_or_changed",
    "pinned_segments",
    "row_cache_hits",
    "row_cache_misses",
    "row_cache_evictions",
)


class SegmentPinRetryableError(RollingCacheCoverageMiss):
    """A segment changed while an atomic read pin was being published."""


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def from_path(cls, path: Path) -> "FileIdentity":
        return cls.from_stat(path.stat())

    @classmethod
    def from_stat(cls, stat: os.stat_result) -> "FileIdentity":
        return cls(
            device=int(stat.st_dev),
            inode=int(stat.st_ino),
            size=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(frozen=True)
class _IndexedSegment:
    segment: RollingSegment
    manifest_identity: FileIdentity
    metadata_identity: FileIdentity
    video_identity: FileIdentity


@dataclass(frozen=True)
class _SegmentManifest:
    segment_id: str
    source_id: str
    runtime_epoch_id: str
    video_file: str
    metadata_file: str
    first_pts: int
    last_pts: int
    frame_count: int
    source_first_pts: int | None
    source_last_pts: int | None
    video_size_bytes: int
    metadata_size_bytes: int


@dataclass
class _Catalog:
    source_id: str
    runtime_epoch_id: str
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    version: int = 0
    entries: dict[Path, _IndexedSegment] = field(default_factory=dict)
    pending: set[Path] = field(default_factory=set)
    failed_identities: dict[Path, FileIdentity] = field(default_factory=dict)
    containers: dict[Path, int] = field(default_factory=dict)
    initialized: bool = False
    generation: int = 0
    root_generation: tuple[int, int] = (0, 0)
    last_refresh_at: float = 0.0
    last_reconcile_at: float = 0.0
    next_reconcile_at: float = 0.0


@dataclass(frozen=True)
class _CachedRows:
    rows: tuple[dict, ...]
    size_bytes: int


class SegmentReadPin:
    """Filesystem marker preventing retention from deleting active inputs."""

    def __init__(
        self,
        *,
        root: Path,
        segments: Iterable[RollingSegment],
        ttl_s: float,
        expected_identities: dict[
            Path,
            tuple[FileIdentity, FileIdentity],
        ]
        | None = None,
        on_open: Callable[[str], None] | None = None,
        on_close: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root.resolve(strict=False)
        self.segments = tuple(segments)
        self.ttl_s = max(1.0, float(ttl_s))
        self.token = uuid.uuid4().hex
        self.marker_path = self.root / READ_PIN_DIR / f"{self.token}.json"
        self._expected_identities = {
            path.resolve(strict=False): identities
            for path, identities in (expected_identities or {}).items()
        }
        self._on_open = on_open
        self._on_close = on_close
        self._active = False

    def __enter__(self) -> "SegmentReadPin":
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / MUTATION_LOCK_FILE
        with lock_path.open("a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
            try:
                self._activate_locked()
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        return self

    def _activate_locked(self) -> None:
        """Publish this marker while the caller holds the mutation lock."""

        pin_dir = self.root / READ_PIN_DIR
        pin_dir.mkdir(parents=True, exist_ok=True)
        relative_segments: list[str] = []
        for segment in self.segments:
            resolved = segment.directory.resolve(strict=False)
            try:
                relative = resolved.relative_to(self.root)
            except ValueError as exc:
                raise ValueError(
                    f"segment read pin escapes cache root: {resolved}"
                ) from exc
            relative_segments.append(str(relative))
        now = time.time()
        payload = {
            "schema_version": READ_PIN_SCHEMA_VERSION,
            "token": self.token,
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "created_at_epoch_s": now,
            "expires_at_epoch_s": now + self.ttl_s,
            "segments": sorted(set(relative_segments)),
        }
        temp_path = pin_dir / f".{self.token}.{os.getpid()}.tmp"
        try:
            for segment in self.segments:
                try:
                    metadata_stat = segment.metadata_path.stat()
                    video_stat = segment.video_path.stat()
                except OSError as exc:
                    raise SegmentPinRetryableError(
                        f"segment disappeared before read pin: {segment.directory}"
                    ) from exc
                if metadata_stat.st_size <= 0 or video_stat.st_size != segment.size_bytes:
                    raise SegmentPinRetryableError(
                        f"segment identity changed before read pin: {segment.directory}"
                    )
                expected = self._expected_identities.get(
                    segment.directory.resolve(strict=False)
                )
                if expected is not None and (
                    FileIdentity.from_stat(metadata_stat) != expected[0]
                    or FileIdentity.from_stat(video_stat) != expected[1]
                ):
                    raise SegmentPinRetryableError(
                        f"segment identity changed before read pin: {segment.directory}"
                    )
            temp_path.write_text(
                json.dumps(payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, self.marker_path)
        finally:
            temp_path.unlink(missing_ok=True)
        self._active = True
        if self._on_open is not None:
            self._on_open(self.token)

    def __exit__(self, *_exc: object) -> None:
        if not self._active:
            return
        lock_path = self.root / MUTATION_LOCK_FILE
        try:
            with lock_path.open("a+", encoding="utf-8") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
                self.marker_path.unlink(missing_ok=True)
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._active = False
            if self._on_close is not None:
                self._on_close(self.token)


class RollingSegmentIndex:
    """Bounded, epoch-fenced segment snapshots shared by worker lanes.

    A full source/epoch walk is performed only for the initial catalog and a
    bounded periodic reconciliation. Between reconciliations, directory
    watermarks and known file identities discover or refresh only changed
    paths. Parsed metadata rows live in a bounded LRU shared by remux and image
    materialization.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        refresh_interval_s: float = 0.5,
        reconcile_interval_s: float = 30.0,
        stability_age_s: float = 0.25,
        row_cache_max_entries: int = 256,
        row_cache_max_bytes: int = 256 * 1024 * 1024,
        io_concurrency: int = 2,
        max_catalogs: int = 256,
        max_scan_entries: int = 20_000,
        max_scan_depth: int = 6,
        min_video_bytes: int = 1024,
        read_pin_ttl_s: float = 600.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.root = Path(root).resolve(strict=False)
        self.refresh_interval_s = max(0.0, float(refresh_interval_s))
        self.reconcile_interval_s = max(
            self.refresh_interval_s,
            float(reconcile_interval_s),
        )
        self.stability_age_s = max(0.0, float(stability_age_s))
        self.row_cache_max_entries = max(1, int(row_cache_max_entries))
        self.row_cache_max_bytes = max(1, int(row_cache_max_bytes))
        self.io_concurrency = max(1, int(io_concurrency))
        self.max_catalogs = max(1, int(max_catalogs))
        self.max_scan_entries = max(1, int(max_scan_entries))
        self.max_scan_depth = max(1, int(max_scan_depth))
        self.min_video_bytes = max(1, int(min_video_bytes))
        self.read_pin_ttl_s = max(1.0, float(read_pin_ttl_s))
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._timer = timer
        self._map_lock = threading.RLock()
        # Kept as a compatibility alias for narrow lock-contention tests.  It
        # now protects only the catalog map and never filesystem work.
        self._lock = self._map_lock
        self._row_cache_lock = threading.RLock()
        self._io_slots = threading.BoundedSemaphore(self.io_concurrency)
        self._metrics_lock = threading.Lock()
        self._diagnostic_local = threading.local()
        self._catalogs: OrderedDict[tuple[str, str], _Catalog] = OrderedDict()
        self._row_cache: OrderedDict[
            tuple[str, FileIdentity], _CachedRows
        ] = OrderedDict()
        self._row_cache_size_bytes = 0
        self._row_cache_bytes = 0
        self._row_cache_entry_count = 0
        self._active_pins: set[str] = set()
        self._catalog_count = 0
        self._catalog_entry_count = 0
        self._catalog_pending_count = 0
        self._catalog_generation = 0
        self._stats = {
            "hits": 0,
            "misses": 0,
            "refreshes": 0,
            "reconciliations": 0,
            "initial_scans": 0,
            "metadata_parses": 0,
            "parse_errors": 0,
            "stale_entries": 0,
            "fallback_scans": 0,
            "row_cache_hits": 0,
            "row_cache_misses": 0,
            "row_cache_evictions": 0,
            "row_cache_byte_evictions": 0,
            "catalog_evictions": 0,
            "catalog_publish_conflicts": 0,
            "read_pins_created": 0,
            "read_pins_released": 0,
        }
        self._timing_totals = {name: 0.0 for name in _OPERATION_TIMING_FIELDS}
        self._timing_maxima = {name: 0.0 for name in _OPERATION_TIMING_FIELDS}
        self._operation_counts = {name: 0 for name in _OPERATION_COUNT_FIELDS}

    @staticmethod
    def new_operation_diagnostics() -> dict[str, float | int]:
        """Return one job-local timing document populated with stable zeroes."""

        return {
            **{f"segment_index_{name}": 0.0 for name in _OPERATION_TIMING_FIELDS},
            **{f"segment_index_{name}": 0 for name in _OPERATION_COUNT_FIELDS},
        }

    @contextmanager
    def _diagnostic_scope(
        self,
        diagnostics: dict[str, float | int] | None,
    ) -> Iterator[None]:
        if diagnostics is None:
            yield
            return
        previous = getattr(self._diagnostic_local, "current", None)
        self._diagnostic_local.current = diagnostics
        try:
            yield
        finally:
            self._diagnostic_local.current = previous

    def _record_timing(self, name: str, elapsed_ms: float) -> None:
        elapsed_ms = max(0.0, float(elapsed_ms))
        diagnostics = getattr(self._diagnostic_local, "current", None)
        if isinstance(diagnostics, dict):
            key = f"segment_index_{name}"
            diagnostics[key] = float(diagnostics.get(key) or 0.0) + elapsed_ms
        with self._metrics_lock:
            self._timing_totals[name] += elapsed_ms
            self._timing_maxima[name] = max(
                self._timing_maxima[name],
                elapsed_ms,
            )

    def _record_count(self, name: str, count: int = 1) -> None:
        count = max(0, int(count))
        diagnostics = getattr(self._diagnostic_local, "current", None)
        if isinstance(diagnostics, dict):
            key = f"segment_index_{name}"
            diagnostics[key] = int(diagnostics.get(key) or 0) + count
        with self._metrics_lock:
            self._operation_counts[name] += count

    @contextmanager
    def _timed(self, name: str) -> Iterator[None]:
        started_at = self._timer()
        try:
            yield
        finally:
            self._record_timing(name, (self._timer() - started_at) * 1000.0)

    @contextmanager
    def _locked(self, lock: object | None = None) -> Iterator[None]:
        active_lock = lock if lock is not None else self._map_lock
        wait_started_at = self._timer()
        active_lock.acquire()  # type: ignore[attr-defined]
        self._record_timing(
            "lock_wait_ms",
            (self._timer() - wait_started_at) * 1000.0,
        )
        hold_started_at = self._timer()
        try:
            yield
        finally:
            hold_ms = (self._timer() - hold_started_at) * 1000.0
            active_lock.release()  # type: ignore[attr-defined]
            self._record_timing("lock_hold_ms", hold_ms)

    def _increment_stat(self, name: str, count: int = 1) -> None:
        with self._metrics_lock:
            self._stats[name] += int(count)

    def _get_or_create_catalog(
        self,
        key: tuple[str, str],
    ) -> tuple[_Catalog, bool]:
        created = False
        with self._locked(self._map_lock):
            catalog = self._catalogs.get(key)
            if catalog is None:
                catalog = _Catalog(
                    source_id=key[0],
                    runtime_epoch_id=key[1],
                )
                self._catalogs[key] = catalog
                created = True
                with self._metrics_lock:
                    self._catalog_count += 1
                self._evict_catalogs()
            else:
                self._catalogs.move_to_end(key)
        self._increment_stat("misses" if created else "hits")
        return catalog, created

    def _copy_catalog(self, catalog: _Catalog) -> tuple[_Catalog, int]:
        with self._locked(catalog.lock):
            version = catalog.version
            source_id = catalog.source_id
            runtime_epoch_id = catalog.runtime_epoch_id
            entries = catalog.entries
            pending = catalog.pending
            failed_identities = catalog.failed_identities
            containers = catalog.containers
            initialized = catalog.initialized
            generation = catalog.generation
            root_generation = catalog.root_generation
            last_refresh_at = catalog.last_refresh_at
            last_reconcile_at = catalog.last_reconcile_at
            next_reconcile_at = catalog.next_reconcile_at
        return (
            _Catalog(
                source_id=source_id,
                runtime_epoch_id=runtime_epoch_id,
                version=version,
                entries=dict(entries),
                pending=set(pending),
                failed_identities=dict(failed_identities),
                containers=dict(containers),
                initialized=initialized,
                generation=generation,
                root_generation=root_generation,
                last_refresh_at=last_refresh_at,
                last_reconcile_at=last_reconcile_at,
                next_reconcile_at=next_reconcile_at,
            ),
            version,
        )

    def _publish_catalog(
        self,
        catalog: _Catalog,
        candidate: _Catalog,
        *,
        expected_version: int,
    ) -> bool:
        with self._locked(catalog.lock):
            if catalog.version != expected_version:
                self._increment_stat("catalog_publish_conflicts")
                return False
            old_entries = len(catalog.entries)
            old_pending = len(catalog.pending)
            old_generation = catalog.generation
            catalog.entries = candidate.entries
            catalog.pending = candidate.pending
            catalog.failed_identities = candidate.failed_identities
            catalog.containers = candidate.containers
            catalog.initialized = candidate.initialized
            catalog.generation = candidate.generation
            catalog.root_generation = candidate.root_generation
            catalog.last_refresh_at = candidate.last_refresh_at
            catalog.last_reconcile_at = candidate.last_reconcile_at
            catalog.next_reconcile_at = candidate.next_reconcile_at
            catalog.version += 1
            with self._metrics_lock:
                self._catalog_entry_count += len(catalog.entries) - old_entries
                self._catalog_pending_count += len(catalog.pending) - old_pending
                self._catalog_generation += catalog.generation - old_generation
            return True

    def find_segments(
        self,
        *,
        source_id: str,
        runtime_epoch_id: str,
        allow_fallback: bool = True,
        diagnostics: dict[str, float | int] | None = None,
    ) -> list[RollingSegment]:
        if not source_id or not runtime_epoch_id:
            return []
        key = (source_id, runtime_epoch_id)
        with self._diagnostic_scope(diagnostics):
            try:
                now = self._monotonic()
                root_generation = self._read_root_generation()
                catalog, _created = self._get_or_create_catalog(key)
                candidate, version = self._copy_catalog(catalog)
                changed = False
                if not candidate.initialized:
                    self._rebuild(candidate, now=now, initial=True)
                    changed = True
                elif now >= candidate.next_reconcile_at:
                    self._refresh(candidate, now=now, reconcile=True)
                    changed = True
                elif (
                    root_generation != candidate.root_generation
                    or now - candidate.last_refresh_at >= self.refresh_interval_s
                ):
                    self._refresh(candidate, now=now)
                    changed = True
                if changed and not self._publish_catalog(
                    catalog,
                    candidate,
                    expected_version=version,
                ):
                    candidate, _version = self._copy_catalog(catalog)
                segments = [entry.segment for entry in candidate.entries.values()]
                with self._timed("sort_ms"):
                    return sorted(
                        segments,
                        key=lambda segment: (
                            segment.first_pts,
                            segment.last_pts,
                            str(segment.metadata_path),
                        ),
                    )
            except Exception:
                if not allow_fallback:
                    raise
                self._increment_stat("fallback_scans")
                return self._fallback_manifest_scan(
                    source_id=source_id,
                    runtime_epoch_id=runtime_epoch_id,
                )

    def rows_for_segment(self, segment: RollingSegment) -> list[dict]:
        metadata_path = segment.metadata_path.resolve(strict=False)
        identity = self._file_identity(metadata_path)
        key = (str(metadata_path), identity)
        with self._locked(self._row_cache_lock):
            cached = self._row_cache.get(key)
            if cached is not None:
                self._row_cache.move_to_end(key)
                rows = list(cached.rows)
            else:
                rows = None
        if rows is not None:
            self._increment_stat("row_cache_hits")
            self._record_count("row_cache_hits")
            return rows
        self._increment_stat("row_cache_misses")
        self._record_count("row_cache_misses")
        parsed = self._parse_rows(metadata_path)
        self._remember_rows(metadata_path, identity, parsed)
        return list(parsed)

    def pin_segments(
        self,
        segments: Iterable[RollingSegment],
        *,
        ttl_s: float | None = None,
        _expected_identities: dict[
            Path,
            tuple[FileIdentity, FileIdentity],
        ]
        | None = None,
    ) -> SegmentReadPin:
        return SegmentReadPin(
            root=self.root,
            segments=tuple(segments),
            ttl_s=self.read_pin_ttl_s if ttl_s is None else ttl_s,
            expected_identities=_expected_identities,
            on_open=self._pin_opened,
            on_close=self._pin_closed,
        )

    def _catalog_segment_identities(
        self,
        *,
        source_id: str,
        runtime_epoch_id: str,
        segments: Iterable[RollingSegment],
    ) -> dict[Path, tuple[FileIdentity, FileIdentity]]:
        wanted = {
            segment.directory.resolve(strict=False)
            for segment in segments
        }
        if not wanted:
            return {}
        with self._locked(self._map_lock):
            catalog = self._catalogs.get((source_id, runtime_epoch_id))
        if catalog is None:
            return {}
        with self._locked(catalog.lock):
            return {
                indexed.segment.directory.resolve(strict=False): (
                    indexed.metadata_identity,
                    indexed.video_identity,
                )
                for indexed in catalog.entries.values()
                if indexed.segment.directory.resolve(strict=False) in wanted
            }

    @staticmethod
    def _source_window_candidates(
        segments: Iterable[RollingSegment],
        *,
        requested_start_pts: int | None,
        requested_end_pts: int | None,
    ) -> list[RollingSegment]:
        """Return a conservative source-clock subset for read-pin publication.

        Modern manifests carry immutable source-clock bounds. A 5+5 evidence
        request only needs the overlapping leaves plus one guard leaf on each
        side; pinning the full retention catalog adds no safety because the
        materializer never reads those unrelated leaves. Mixed/legacy catalogs
        retain the full-catalog behavior so missing bounds cannot narrow
        coverage accidentally.
        """

        segment_list = list(segments)
        if (
            requested_start_pts is None
            or requested_end_pts is None
            or requested_end_pts <= requested_start_pts
        ):
            return segment_list
        bounded: list[tuple[int, int, int, RollingSegment]] = []
        for sequence, segment in enumerate(segment_list):
            first = segment.source_first_pts
            last = segment.source_last_pts
            if first is None or last is None:
                return segment_list
            low, high = sorted((int(first), int(last)))
            bounded.append((low, high, sequence, segment))
        bounded.sort(key=lambda item: (item[0], item[1], item[2]))
        overlapping = [
            index
            for index, (low, high, _sequence, _segment) in enumerate(bounded)
            if high >= requested_start_pts and low <= requested_end_pts
        ]
        if not overlapping:
            return segment_list
        first_index = max(0, min(overlapping) - 1)
        last_index = min(len(bounded), max(overlapping) + 2)
        selected = {
            item[3]
            for item in bounded[first_index:last_index]
        }
        return [segment for segment in segment_list if segment in selected]

    @contextmanager
    def pin_source_segments(
        self,
        *,
        source_id: str,
        runtime_epoch_id: str,
        requested_source_start_pts: int | None = None,
        requested_source_end_pts: int | None = None,
        ttl_s: float | None = None,
        diagnostics: dict[str, float | int] | None = None,
    ) -> Iterator[list[RollingSegment]]:
        """Atomically refresh, select, and pin one source/epoch snapshot.

        Retention takes the same mutation lock exclusively. Holding it shared
        across catalog refresh and marker publication closes the otherwise
        observable gap where cleanup could delete a segment returned by
        ``find_segments()`` before ``SegmentReadPin.__enter__()`` published its
        marker.
        """

        with self._diagnostic_scope(diagnostics):
            self.root.mkdir(parents=True, exist_ok=True)
            lock_path = self.root / MUTATION_LOCK_FILE
            pin: SegmentReadPin | None = None
            io_wait_started_at = self._timer()
            self._io_slots.acquire()
            self._record_timing(
                "io_slot_wait_ms",
                (self._timer() - io_wait_started_at) * 1000.0,
            )
            try:
                with lock_path.open("a+", encoding="utf-8") as lock_fh:
                    mutation_wait_started_at = self._timer()
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
                    self._record_timing(
                        "mutation_lock_wait_ms",
                        (self._timer() - mutation_wait_started_at) * 1000.0,
                    )
                    try:
                        segments = self.find_segments(
                            source_id=source_id,
                            runtime_epoch_id=runtime_epoch_id,
                        )
                        segments = self._source_window_candidates(
                            segments,
                            requested_start_pts=requested_source_start_pts,
                            requested_end_pts=requested_source_end_pts,
                        )
                        expected_identities = self._catalog_segment_identities(
                            source_id=source_id,
                            runtime_epoch_id=runtime_epoch_id,
                            segments=segments,
                        )
                        pin = self.pin_segments(
                            segments,
                            ttl_s=ttl_s,
                            _expected_identities=expected_identities,
                        )
                        self._record_count("pinned_segments", len(segments))
                        with self._timed("pin_publish_ms"):
                            pin._activate_locked()
                    finally:
                        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._io_slots.release()
            try:
                yield segments
            finally:
                if pin is not None:
                    with self._timed("pin_release_ms"):
                        pin.__exit__(None, None, None)

    def force_reconcile(self, *, source_id: str, runtime_epoch_id: str) -> None:
        key = (source_id, runtime_epoch_id)
        catalog, _created = self._get_or_create_catalog(key)
        for _attempt in range(3):
            candidate, version = self._copy_catalog(catalog)
            self._rebuild(
                candidate,
                now=self._monotonic(),
                initial=not candidate.initialized,
            )
            if self._publish_catalog(
                catalog,
                candidate,
                expected_version=version,
            ):
                return
        raise RuntimeError("rolling_segment_index_publish_conflict")

    def snapshot(self) -> dict[str, int | float | str]:
        with self._metrics_lock:
            return {
                "mode": "incremental",
                **self._stats,
                "catalogs": self._catalog_count,
                "entries": self._catalog_entry_count,
                "pending": self._catalog_pending_count,
                "row_cache_entries": self._row_cache_entry_count,
                "row_cache_bytes": self._row_cache_bytes,
                "active_read_pins": len(self._active_pins),
                "generation": self._catalog_generation,
                **{
                    f"{name}_total": total
                    for name, total in self._timing_totals.items()
                },
                **{
                    f"{name}_max": value
                    for name, value in self._timing_maxima.items()
                },
                **self._operation_counts,
            }

    def _initial_reconcile_delay(self, catalog: _Catalog) -> float:
        identity = f"{catalog.source_id}\0{catalog.runtime_epoch_id}".encode("utf-8")
        fraction = zlib.crc32(identity) / float(2**32)
        return self.reconcile_interval_s * (0.5 + fraction)

    def _rebuild(self, catalog: _Catalog, *, now: float, initial: bool) -> None:
        with self._timed("rebuild_ms"):
            candidates: set[Path] = set()
            containers: dict[Path, int] = {}
            visited = 0
            for source_root in self._candidate_source_roots(
                source_id=catalog.source_id,
                runtime_epoch_id=catalog.runtime_epoch_id,
            ):
                if not source_root.exists():
                    continue
                for path, is_dir in self._walk(source_root):
                    visited += 1
                    if visited > self.max_scan_entries:
                        raise RuntimeError("rolling_segment_index_scan_overflow")
                    if is_dir:
                        containers[path] = self._directory_mtime(path)
                    elif (
                        path.name == SEGMENT_MANIFEST_FILE
                        and "materialized" not in path.parts
                    ):
                        candidates.add(path.resolve(strict=False))
            self._record_count("scanned_known", len(candidates))
            previous = set(catalog.entries)
            catalog.containers = containers
            catalog.pending = set()
            catalog.failed_identities = {}
            for manifest_path in candidates:
                self._refresh_candidate(catalog, manifest_path)
                # Atomically published segment directories are immutable
                # leaves. Tracking only their discovery parents keeps steady
                # refresh work independent of retained segment count.
                catalog.containers.pop(manifest_path.parent, None)
            missing = previous - candidates
            for manifest_path in missing:
                self._remove_entry(catalog, manifest_path)
            catalog.initialized = True
            catalog.generation += 1
            catalog.root_generation = self._read_root_generation()
            catalog.last_refresh_at = now
            catalog.last_reconcile_at = now
            catalog.next_reconcile_at = now + (
                self._initial_reconcile_delay(catalog)
                if initial
                else self.reconcile_interval_s
            )
            self._increment_stat(
                "initial_scans" if initial else "reconciliations"
            )

    def _refresh(
        self,
        catalog: _Catalog,
        *,
        now: float,
        reconcile: bool = False,
    ) -> None:
        with self._timed("refresh_ms"):
            self._increment_stat("reconciliations" if reconcile else "refreshes")
            # Final directories are immutable after atomic publication. Only
            # incomplete/invalid manifests need payload identity retries on a
            # steady refresh; selected segments are fenced again at read-pin
            # publication.
            pending_candidates = tuple(catalog.pending)
            self._record_count("scanned_known", len(pending_candidates))
            for manifest_path in pending_candidates:
                self._refresh_candidate(catalog, manifest_path)

            queue: list[Path] = []
            queued: set[Path] = set()

            def enqueue(path: Path) -> None:
                resolved = path.resolve(strict=False)
                if resolved not in queued:
                    queue.append(resolved)
                    queued.add(resolved)

            def remove_tree(path: Path) -> None:
                resolved = path.resolve(strict=False)
                for tracked in tuple(catalog.containers):
                    if self._is_relative_to(tracked, resolved):
                        catalog.containers.pop(tracked, None)
                known = set(catalog.entries) | set(catalog.pending)
                for manifest_path in known:
                    if not self._is_relative_to(manifest_path, resolved):
                        continue
                    catalog.pending.discard(manifest_path)
                    catalog.failed_identities.pop(manifest_path, None)
                    self._remove_entry(catalog, manifest_path)

            known_roots = self._candidate_source_roots(
                source_id=catalog.source_id,
                runtime_epoch_id=catalog.runtime_epoch_id,
            )
            for source_root in known_roots:
                if source_root.exists() and source_root not in catalog.containers:
                    enqueue(source_root)
            for container, previous_mtime in tuple(catalog.containers.items()):
                current_mtime = self._directory_mtime(container)
                if current_mtime == 0:
                    remove_tree(container)
                    continue
                if reconcile or current_mtime != previous_mtime:
                    enqueue(container)

            visited = 0
            processed: set[Path] = set()
            while queue:
                container = queue.pop(0)
                queued.discard(container)
                if container in processed:
                    continue
                processed.add(container)
                current_mtime = self._directory_mtime(container)
                if current_mtime == 0:
                    remove_tree(container)
                    continue
                catalog.containers[container] = current_mtime
                try:
                    children = list(os.scandir(container))
                except OSError:
                    catalog.containers[container] = -1
                    continue
                child_directories: set[Path] = set()
                current_manifests: set[Path] = set()
                for child in children:
                    visited += 1
                    if visited > self.max_scan_entries:
                        self._rebuild(catalog, now=now, initial=False)
                        return
                    if not child.is_dir(follow_symlinks=False):
                        continue
                    # ``container`` is already resolved and scandir does not
                    # follow symlinked directories. Re-resolving every retained
                    # leaf and statting its immutable manifest turns each new
                    # segment publication into retention-wide filesystem work.
                    child_path = Path(child.path)
                    child_directories.add(child_path)
                    manifest_path = child_path / SEGMENT_MANIFEST_FILE
                    if (
                        manifest_path in catalog.entries
                        and manifest_path not in catalog.pending
                    ):
                        current_manifests.add(manifest_path)
                        catalog.containers.pop(child_path, None)
                        continue
                    if manifest_path.is_file():
                        current_manifests.add(manifest_path)
                        catalog.containers.pop(child_path, None)
                        if (
                            manifest_path not in catalog.entries
                            or manifest_path in catalog.pending
                        ):
                            self._refresh_candidate(catalog, manifest_path)
                        continue
                    previous_child_mtime = catalog.containers.get(child_path)
                    child_mtime = self._directory_mtime(child_path)
                    if child_mtime == 0:
                        remove_tree(child_path)
                        continue
                    catalog.containers[child_path] = child_mtime
                    if (
                        reconcile
                        or previous_child_mtime is None
                        or previous_child_mtime != child_mtime
                    ):
                        enqueue(child_path)

                direct_known = {
                    manifest_path
                    for manifest_path in set(catalog.entries) | set(catalog.pending)
                    if manifest_path.parent.parent == container
                }
                self._record_count("scanned_known", len(direct_known))
                for missing_manifest in direct_known - current_manifests:
                    catalog.pending.discard(missing_manifest)
                    catalog.failed_identities.pop(missing_manifest, None)
                    self._remove_entry(catalog, missing_manifest)
            # Retention advances one root-wide generation after deleting segments.
            # Membership comparison on changed discovery parents has already
            # removed deleted immutable leaves, so acknowledge that generation.
            catalog.root_generation = self._read_root_generation()
            catalog.last_refresh_at = now
            if reconcile:
                catalog.last_reconcile_at = now
                catalog.next_reconcile_at = now + self.reconcile_interval_s

    def _refresh_candidate(self, catalog: _Catalog, manifest_path: Path) -> None:
        try:
            manifest_identity = self._file_identity(manifest_path)
        except OSError:
            catalog.pending.discard(manifest_path)
            catalog.failed_identities.pop(manifest_path, None)
            self._remove_entry(catalog, manifest_path)
            return
        if (
            manifest_identity.size <= 0
            or manifest_identity.size > MAX_SEGMENT_MANIFEST_BYTES
            or not self._is_stable(manifest_identity)
        ):
            catalog.pending.add(manifest_path)
            catalog.failed_identities.pop(manifest_path, None)
            self._remove_entry(catalog, manifest_path)
            return
        if catalog.failed_identities.get(manifest_path) == manifest_identity:
            catalog.pending.add(manifest_path)
            self._remove_entry(catalog, manifest_path)
            return

        existing = catalog.entries.get(manifest_path)
        if existing is not None and existing.manifest_identity == manifest_identity:
            metadata_path = existing.segment.metadata_path
            video_path = existing.segment.video_path
            try:
                metadata_identity = self._file_identity(metadata_path)
                video_identity = self._file_identity(video_path)
            except OSError:
                catalog.pending.add(manifest_path)
                self._remove_entry(catalog, manifest_path)
                return
            if (
                existing.metadata_identity == metadata_identity
                and existing.video_identity == video_identity
            ):
                catalog.pending.discard(manifest_path)
                catalog.failed_identities.pop(manifest_path, None)
                return
            # Published segment directories are immutable. A payload identity
            # change without a new manifest is never accepted as the same
            # catalog entry.
            self._increment_stat("parse_errors")
            catalog.pending.add(manifest_path)
            catalog.failed_identities[manifest_path] = manifest_identity
            self._remove_entry(catalog, manifest_path)
            return

        self._record_count("new_or_changed")
        try:
            manifest = self._parse_manifest(manifest_path)
        except Exception:
            self._increment_stat("parse_errors")
            catalog.pending.add(manifest_path)
            catalog.failed_identities[manifest_path] = manifest_identity
            self._remove_entry(catalog, manifest_path)
            return
        if (
            manifest.segment_id != manifest_path.parent.name
            or manifest.source_id != catalog.source_id
            or manifest.runtime_epoch_id != catalog.runtime_epoch_id
            or self._runtime_epoch_from_path(manifest_path)
            != catalog.runtime_epoch_id
        ):
            self._increment_stat("parse_errors")
            catalog.pending.add(manifest_path)
            catalog.failed_identities[manifest_path] = manifest_identity
            self._remove_entry(catalog, manifest_path)
            return

        metadata_path = (manifest_path.parent / manifest.metadata_file).resolve(
            strict=False
        )
        video_path = (manifest_path.parent / manifest.video_file).resolve(
            strict=False
        )
        try:
            metadata_identity = self._file_identity(metadata_path)
            video_identity = self._file_identity(video_path)
        except OSError:
            catalog.pending.add(manifest_path)
            catalog.failed_identities.pop(manifest_path, None)
            self._remove_entry(catalog, manifest_path)
            return
        if (
            metadata_identity.size <= 0
            or video_identity.size < self.min_video_bytes
            or not self._is_stable(metadata_identity)
            or not self._is_stable(video_identity)
        ):
            catalog.pending.add(manifest_path)
            catalog.failed_identities.pop(manifest_path, None)
            self._remove_entry(catalog, manifest_path)
            return
        if (
            manifest.metadata_size_bytes != metadata_identity.size
            or manifest.video_size_bytes != video_identity.size
        ):
            self._increment_stat("parse_errors")
            catalog.pending.add(manifest_path)
            catalog.failed_identities[manifest_path] = manifest_identity
            self._remove_entry(catalog, manifest_path)
            return

        segment = RollingSegment(
            segment_id=manifest.segment_id,
            source_id=manifest.source_id,
            runtime_epoch_id=manifest.runtime_epoch_id,
            directory=manifest_path.parent,
            video_path=video_path,
            metadata_path=metadata_path,
            first_pts=manifest.first_pts,
            last_pts=manifest.last_pts,
            frame_count=manifest.frame_count,
            size_bytes=video_identity.size,
            source_first_pts=manifest.source_first_pts,
            source_last_pts=manifest.source_last_pts,
        )
        catalog.entries[manifest_path] = _IndexedSegment(
            segment=segment,
            manifest_identity=manifest_identity,
            metadata_identity=metadata_identity,
            video_identity=video_identity,
        )
        catalog.pending.discard(manifest_path)
        catalog.failed_identities.pop(manifest_path, None)

    def _remove_entry(self, catalog: _Catalog, manifest_path: Path) -> None:
        removed = catalog.entries.pop(manifest_path, None)
        if removed is not None:
            self._increment_stat("stale_entries")
            metadata_path = removed.segment.metadata_path
        else:
            metadata_path = manifest_path.parent / "metadata.json"
        metadata_text = str(metadata_path.resolve(strict=False))
        with self._locked(self._row_cache_lock):
            for key in tuple(self._row_cache):
                if key[0] != metadata_text:
                    continue
                cached = self._row_cache.pop(key)
                self._row_cache_size_bytes -= cached.size_bytes
            cache_entries = len(self._row_cache)
            cache_bytes = self._row_cache_size_bytes
        with self._metrics_lock:
            self._row_cache_entry_count = cache_entries
            self._row_cache_bytes = cache_bytes

    def _parse_manifest(self, manifest_path: Path) -> _SegmentManifest:
        self._record_count("manifest_parses")
        with self._timed("manifest_parse_ms"):
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("rolling_segment_manifest_not_object")
        if payload.get("schema_version") != SEGMENT_MANIFEST_SCHEMA_VERSION:
            raise ValueError("rolling_segment_manifest_schema_invalid")

        def required_text(name: str) -> str:
            value = payload.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"rolling_segment_manifest_{name}_invalid")
            return value

        def required_int(name: str, *, minimum: int | None = None) -> int:
            value = payload.get(name)
            if isinstance(value, bool):
                raise ValueError(f"rolling_segment_manifest_{name}_invalid")
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"rolling_segment_manifest_{name}_invalid"
                ) from exc
            if minimum is not None and parsed < minimum:
                raise ValueError(f"rolling_segment_manifest_{name}_invalid")
            return parsed

        def optional_int(name: str) -> int | None:
            value = payload.get(name)
            if value is None:
                return None
            if isinstance(value, bool):
                raise ValueError(f"rolling_segment_manifest_{name}_invalid")
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"rolling_segment_manifest_{name}_invalid"
                ) from exc

        video_file = required_text("video_file")
        metadata_file = required_text("metadata_file")
        if video_file not in VIDEO_NAMES or Path(video_file).name != video_file:
            raise ValueError("rolling_segment_manifest_video_file_invalid")
        if metadata_file != "metadata.json":
            raise ValueError("rolling_segment_manifest_metadata_file_invalid")
        first_pts = required_int("first_pts")
        last_pts = required_int("last_pts")
        frame_count = required_int("frame_count", minimum=1)
        if last_pts < first_pts:
            raise ValueError("rolling_segment_manifest_pts_bounds_invalid")
        source_first_pts = optional_int("source_first_pts")
        source_last_pts = optional_int("source_last_pts")
        if (source_first_pts is None) != (source_last_pts is None):
            raise ValueError("rolling_segment_manifest_source_bounds_incomplete")
        if (
            source_first_pts is not None
            and source_last_pts is not None
            and source_last_pts < source_first_pts
        ):
            raise ValueError("rolling_segment_manifest_source_bounds_invalid")
        return _SegmentManifest(
            segment_id=required_text("segment_id"),
            source_id=required_text("source_id"),
            runtime_epoch_id=required_text("runtime_epoch_id"),
            video_file=video_file,
            metadata_file=metadata_file,
            first_pts=first_pts,
            last_pts=last_pts,
            frame_count=frame_count,
            source_first_pts=source_first_pts,
            source_last_pts=source_last_pts,
            video_size_bytes=required_int("video_size_bytes", minimum=1),
            metadata_size_bytes=required_int("metadata_size_bytes", minimum=1),
        )

    def _parse_rows(self, metadata_path: Path) -> list[dict]:
        self._record_count("full_row_parses")
        with self._timed("full_row_parse_ms"):
            rows = [
                row
                for row in load_native_metadata(metadata_path)
                if isinstance(row, dict)
            ]
        self._increment_stat("metadata_parses")
        return rows

    def _remember_rows(
        self,
        metadata_path: Path,
        identity: FileIdentity,
        rows: Iterable[dict],
    ) -> None:
        key = (str(metadata_path), identity)
        cached = _CachedRows(
            rows=tuple(rows),
            size_bytes=max(1, int(identity.size)),
        )
        evictions = 0
        byte_evictions = 0
        with self._locked(self._row_cache_lock):
            previous = self._row_cache.pop(key, None)
            if previous is not None:
                self._row_cache_size_bytes -= previous.size_bytes
            self._row_cache[key] = cached
            self._row_cache_size_bytes += cached.size_bytes
            self._row_cache.move_to_end(key)
            while (
                len(self._row_cache) > self.row_cache_max_entries
                or self._row_cache_size_bytes > self.row_cache_max_bytes
            ):
                byte_limited = self._row_cache_size_bytes > self.row_cache_max_bytes
                _evicted_key, evicted = self._row_cache.popitem(last=False)
                self._row_cache_size_bytes -= evicted.size_bytes
                evictions += 1
                if byte_limited:
                    byte_evictions += 1
            cache_entries = len(self._row_cache)
            cache_bytes = self._row_cache_size_bytes
        if evictions:
            self._increment_stat("row_cache_evictions", evictions)
            self._record_count("row_cache_evictions", evictions)
        if byte_evictions:
            self._increment_stat("row_cache_byte_evictions", byte_evictions)
        with self._metrics_lock:
            self._row_cache_entry_count = cache_entries
            self._row_cache_bytes = cache_bytes

    def _evict_catalogs(self) -> None:
        while len(self._catalogs) > self.max_catalogs:
            _key, catalog = self._catalogs.popitem(last=False)
            with self._locked(catalog.lock):
                entries = len(catalog.entries)
                pending = len(catalog.pending)
                generation = catalog.generation
            with self._metrics_lock:
                self._catalog_count -= 1
                self._catalog_entry_count -= entries
                self._catalog_pending_count -= pending
                self._catalog_generation -= generation
            self._increment_stat("catalog_evictions")

    def _pin_opened(self, token: str) -> None:
        with self._metrics_lock:
            self._active_pins.add(token)
            self._stats["read_pins_created"] += 1

    def _pin_closed(self, token: str) -> None:
        with self._metrics_lock:
            self._active_pins.discard(token)
            self._stats["read_pins_released"] += 1

    def _fallback_manifest_scan(
        self,
        *,
        source_id: str,
        runtime_epoch_id: str,
    ) -> list[RollingSegment]:
        catalog = _Catalog(
            source_id=source_id,
            runtime_epoch_id=runtime_epoch_id,
        )
        for source_root in self._candidate_source_roots(
            source_id=source_id,
            runtime_epoch_id=runtime_epoch_id,
        ):
            if not source_root.exists():
                continue
            for manifest_path in source_root.rglob(SEGMENT_MANIFEST_FILE):
                if "materialized" in manifest_path.parts:
                    continue
                self._refresh_candidate(
                    catalog,
                    manifest_path.resolve(strict=False),
                )
        return sorted(
            (entry.segment for entry in catalog.entries.values()),
            key=lambda segment: (
                segment.first_pts,
                segment.last_pts,
                str(segment.metadata_path),
            ),
        )

    def _candidate_source_roots(
        self,
        *,
        source_id: str,
        runtime_epoch_id: str,
    ) -> list[Path]:
        roots: list[Path] = []
        bases = [self.root]
        if self.root.name != "midterm":
            bases.append(self.root / "midterm")
        for base in bases:
            roots.append(base / "epochs" / runtime_epoch_id / source_id)
        unique: list[Path] = []
        seen: set[Path] = set()
        for path in roots:
            resolved = path.resolve(strict=False)
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return unique

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
        except ValueError:
            return False
        return True

    def _walk(self, root: Path) -> Iterable[tuple[Path, bool]]:
        stack: list[tuple[Path, int]] = [(root.resolve(strict=False), 0)]
        while stack:
            current, depth = stack.pop()
            yield current, True
            if depth >= self.max_scan_depth:
                continue
            try:
                children = list(os.scandir(current))
            except OSError:
                continue
            for child in children:
                path = Path(child.path).resolve(strict=False)
                if child.is_dir(follow_symlinks=False):
                    stack.append((path, depth + 1))
                elif child.is_file(follow_symlinks=False):
                    yield path, False

    def _find_video(self, directory: Path) -> Path | None:
        for name in VIDEO_NAMES:
            candidate = directory / name
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate.resolve(strict=False)
            except OSError:
                continue
        for pattern in ("*.mov", "*.mp4", "*.mkv", "*.webm"):
            for candidate in directory.glob(pattern):
                try:
                    if candidate.is_file() and candidate.stat().st_size > 0:
                        return candidate.resolve(strict=False)
                except OSError:
                    continue
        return None

    def _file_identity(self, path: Path) -> FileIdentity:
        self._record_count("stat_calls")
        with self._timed("stat_ms"):
            return FileIdentity.from_path(path)

    def _is_stable(self, identity: FileIdentity) -> bool:
        modified_at = identity.mtime_ns / 1_000_000_000.0
        return self._wall_clock() - modified_at >= self.stability_age_s

    def _read_root_generation(self) -> tuple[int, int]:
        path = self.root / GENERATION_FILE
        self._record_count("stat_calls")
        try:
            with self._timed("stat_ms"):
                stat = path.stat()
                text = path.read_text(encoding="utf-8").strip()
            generation = int(text or 0)
            return generation, int(stat.st_mtime_ns)
        except (OSError, ValueError):
            return 0, 0

    def _directory_mtime(self, path: Path) -> int:
        self._record_count("stat_calls")
        try:
            with self._timed("stat_ms"):
                return int(path.stat().st_mtime_ns)
        except OSError:
            return 0

    @staticmethod
    def _runtime_epoch_from_path(path: Path) -> str:
        parts = path.parts
        for index, part in enumerate(parts[:-1]):
            if part == "epochs" and index + 1 < len(parts):
                return parts[index + 1]
        return ""

    @staticmethod
    def _row_pts(row: dict) -> int | None:
        value = row.get("rolling_cache_mux_pts")
        if value is None:
            value = row.get("pts")
        if value is None:
            value = row.get("frame_pts")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _source_row_pts(row: dict) -> int | None:
        value = row.get("pts")
        if value is None:
            value = row.get("frame_pts")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
