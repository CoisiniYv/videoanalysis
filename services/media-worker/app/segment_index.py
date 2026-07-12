"""Process-lifetime rolling-cache segment catalog and read-pin contract."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from app.post_savant_metadata_annotation_builder import load_native_metadata
from app.rolling_cache import RollingSegment, VIDEO_NAMES


GENERATION_FILE = ".rolling-cache-generation"
READ_PIN_DIR = ".read-pins"
MUTATION_LOCK_FILE = ".rolling-cache-mutation.lock"
READ_PIN_SCHEMA_VERSION = "rolling-segment-read-pin-v1"


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def from_path(cls, path: Path) -> "FileIdentity":
        stat = path.stat()
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
    metadata_identity: FileIdentity
    video_identity: FileIdentity


@dataclass
class _Catalog:
    source_id: str
    runtime_epoch_id: str
    entries: dict[Path, _IndexedSegment] = field(default_factory=dict)
    pending: set[Path] = field(default_factory=set)
    failed_identities: dict[
        Path,
        tuple[FileIdentity, FileIdentity],
    ] = field(default_factory=dict)
    containers: dict[Path, int] = field(default_factory=dict)
    initialized: bool = False
    generation: int = 0
    root_generation: tuple[int, int] = (0, 0)
    last_refresh_at: float = 0.0
    last_reconcile_at: float = 0.0


class SegmentReadPin:
    """Filesystem marker preventing retention from deleting active inputs."""

    def __init__(
        self,
        *,
        root: Path,
        segments: Iterable[RollingSegment],
        ttl_s: float,
        on_open: Callable[[str], None] | None = None,
        on_close: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root.resolve(strict=False)
        self.segments = tuple(segments)
        self.ttl_s = max(1.0, float(ttl_s))
        self.token = uuid.uuid4().hex
        self.marker_path = self.root / READ_PIN_DIR / f"{self.token}.json"
        self._on_open = on_open
        self._on_close = on_close
        self._active = False

    def __enter__(self) -> "SegmentReadPin":
        self.root.mkdir(parents=True, exist_ok=True)
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
        lock_path = self.root / MUTATION_LOCK_FILE
        with lock_path.open("a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
            temp_path = pin_dir / f".{self.token}.{os.getpid()}.tmp"
            try:
                for segment in self.segments:
                    try:
                        metadata_stat = segment.metadata_path.stat()
                        video_stat = segment.video_path.stat()
                    except OSError as exc:
                        raise FileNotFoundError(
                            f"segment disappeared before read pin: {segment.directory}"
                        ) from exc
                    if metadata_stat.st_size <= 0 or video_stat.st_size != segment.size_bytes:
                        raise RuntimeError(
                            f"segment identity changed before read pin: {segment.directory}"
                        )
                temp_path.write_text(
                    json.dumps(payload, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                os.replace(temp_path, self.marker_path)
            finally:
                temp_path.unlink(missing_ok=True)
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        self._active = True
        if self._on_open is not None:
            self._on_open(self.token)
        return self

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
        max_catalogs: int = 256,
        max_scan_entries: int = 20_000,
        max_scan_depth: int = 6,
        min_video_bytes: int = 1024,
        read_pin_ttl_s: float = 600.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root).resolve(strict=False)
        self.refresh_interval_s = max(0.0, float(refresh_interval_s))
        self.reconcile_interval_s = max(
            self.refresh_interval_s,
            float(reconcile_interval_s),
        )
        self.stability_age_s = max(0.0, float(stability_age_s))
        self.row_cache_max_entries = max(1, int(row_cache_max_entries))
        self.max_catalogs = max(1, int(max_catalogs))
        self.max_scan_entries = max(1, int(max_scan_entries))
        self.max_scan_depth = max(1, int(max_scan_depth))
        self.min_video_bytes = max(1, int(min_video_bytes))
        self.read_pin_ttl_s = max(1.0, float(read_pin_ttl_s))
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._catalogs: OrderedDict[tuple[str, str], _Catalog] = OrderedDict()
        self._row_cache: OrderedDict[
            tuple[str, FileIdentity], tuple[dict, ...]
        ] = OrderedDict()
        self._active_pins: set[str] = set()
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
            "catalog_evictions": 0,
            "read_pins_created": 0,
            "read_pins_released": 0,
        }

    def find_segments(
        self,
        *,
        source_id: str,
        runtime_epoch_id: str,
        allow_fallback: bool = True,
    ) -> list[RollingSegment]:
        if not source_id or not runtime_epoch_id:
            return []
        key = (source_id, runtime_epoch_id)
        try:
            with self._lock:
                now = self._monotonic()
                catalog = self._catalogs.get(key)
                if catalog is None:
                    self._stats["misses"] += 1
                    catalog = _Catalog(
                        source_id=source_id,
                        runtime_epoch_id=runtime_epoch_id,
                    )
                    self._catalogs[key] = catalog
                    self._evict_catalogs()
                    self._rebuild(catalog, now=now, initial=True)
                else:
                    self._stats["hits"] += 1
                    self._catalogs.move_to_end(key)
                    root_generation = self._read_root_generation()
                    if (
                        root_generation != catalog.root_generation
                        or now - catalog.last_reconcile_at
                        >= self.reconcile_interval_s
                    ):
                        self._rebuild(catalog, now=now, initial=False)
                    elif now - catalog.last_refresh_at >= self.refresh_interval_s:
                        self._refresh(catalog, now=now)
                segments = [entry.segment for entry in catalog.entries.values()]
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
            from app.rolling_cache import find_segments as legacy_find_segments

            with self._lock:
                self._stats["fallback_scans"] += 1
            legacy_segments = legacy_find_segments(
                self.root,
                source_id=source_id,
                runtime_epoch_id=runtime_epoch_id,
            )
            allowed_roots = self._candidate_source_roots(
                source_id=source_id,
                runtime_epoch_id=runtime_epoch_id,
            )
            return [
                segment
                for segment in legacy_segments
                if any(
                    self._is_relative_to(segment.directory, root)
                    for root in allowed_roots
                )
            ]

    def rows_for_segment(self, segment: RollingSegment) -> list[dict]:
        metadata_path = segment.metadata_path.resolve(strict=False)
        with self._lock:
            identity = FileIdentity.from_path(metadata_path)
            key = (str(metadata_path), identity)
            rows = self._row_cache.get(key)
            if rows is not None:
                self._stats["row_cache_hits"] += 1
                self._row_cache.move_to_end(key)
                return list(rows)
            self._stats["row_cache_misses"] += 1
            parsed = self._parse_rows(metadata_path)
            self._remember_rows(metadata_path, identity, parsed)
            return list(parsed)

    def pin_segments(
        self,
        segments: Iterable[RollingSegment],
        *,
        ttl_s: float | None = None,
    ) -> SegmentReadPin:
        return SegmentReadPin(
            root=self.root,
            segments=tuple(segments),
            ttl_s=self.read_pin_ttl_s if ttl_s is None else ttl_s,
            on_open=self._pin_opened,
            on_close=self._pin_closed,
        )

    def force_reconcile(self, *, source_id: str, runtime_epoch_id: str) -> None:
        key = (source_id, runtime_epoch_id)
        with self._lock:
            catalog = self._catalogs.get(key)
            if catalog is None:
                catalog = _Catalog(source_id=source_id, runtime_epoch_id=runtime_epoch_id)
                self._catalogs[key] = catalog
            self._rebuild(catalog, now=self._monotonic(), initial=not catalog.initialized)

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "mode": "incremental",
                **self._stats,
                "catalogs": len(self._catalogs),
                "entries": sum(len(catalog.entries) for catalog in self._catalogs.values()),
                "pending": sum(len(catalog.pending) for catalog in self._catalogs.values()),
                "row_cache_entries": len(self._row_cache),
                "active_read_pins": len(self._active_pins),
                "generation": sum(catalog.generation for catalog in self._catalogs.values()),
            }

    def _rebuild(self, catalog: _Catalog, *, now: float, initial: bool) -> None:
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
                elif path.name == "metadata.json" and "materialized" not in path.parts:
                    candidates.add(path.resolve(strict=False))
        previous = set(catalog.entries)
        catalog.containers = containers
        catalog.pending = set()
        catalog.failed_identities = {}
        for metadata_path in candidates:
            self._refresh_candidate(catalog, metadata_path)
        missing = previous - candidates
        for metadata_path in missing:
            self._remove_entry(catalog, metadata_path)
        catalog.initialized = True
        catalog.generation += 1
        catalog.root_generation = self._read_root_generation()
        catalog.last_refresh_at = now
        catalog.last_reconcile_at = now
        self._stats["initial_scans" if initial else "reconciliations"] += 1

    def _refresh(self, catalog: _Catalog, *, now: float) -> None:
        self._stats["refreshes"] += 1
        known_candidates = set(catalog.entries) | set(catalog.pending)
        for metadata_path in tuple(known_candidates):
            self._refresh_candidate(catalog, metadata_path)

        queue: list[Path] = []
        known_roots = self._candidate_source_roots(
            source_id=catalog.source_id,
            runtime_epoch_id=catalog.runtime_epoch_id,
        )
        for source_root in known_roots:
            if source_root.exists() and source_root not in catalog.containers:
                queue.append(source_root)
        for container, previous_mtime in tuple(catalog.containers.items()):
            current_mtime = self._directory_mtime(container)
            if current_mtime == 0:
                catalog.containers.pop(container, None)
                continue
            if current_mtime != previous_mtime:
                queue.append(container)

        visited = 0
        queued = set(queue)
        while queue:
            container = queue.pop(0)
            queued.discard(container)
            current_mtime = self._directory_mtime(container)
            if current_mtime == 0:
                continue
            catalog.containers[container] = current_mtime
            metadata_path = container / "metadata.json"
            if metadata_path.exists():
                self._refresh_candidate(catalog, metadata_path.resolve(strict=False))
            try:
                children = list(os.scandir(container))
            except OSError:
                catalog.containers[container] = -1
                continue
            for child in children:
                visited += 1
                if visited > self.max_scan_entries:
                    self._rebuild(catalog, now=now, initial=False)
                    return
                if not child.is_dir(follow_symlinks=False):
                    continue
                child_path = Path(child.path).resolve(strict=False)
                if child_path not in catalog.containers:
                    catalog.containers[child_path] = self._directory_mtime(child_path)
                    if child_path not in queued:
                        queue.append(child_path)
                        queued.add(child_path)
        catalog.last_refresh_at = now

    def _refresh_candidate(self, catalog: _Catalog, metadata_path: Path) -> None:
        try:
            metadata_identity = FileIdentity.from_path(metadata_path)
        except OSError:
            catalog.pending.discard(metadata_path)
            catalog.failed_identities.pop(metadata_path, None)
            self._remove_entry(catalog, metadata_path)
            return
        video_path = self._find_video(metadata_path.parent)
        if video_path is None:
            catalog.pending.add(metadata_path)
            catalog.failed_identities.pop(metadata_path, None)
            self._remove_entry(catalog, metadata_path)
            return
        try:
            video_identity = FileIdentity.from_path(video_path)
        except OSError:
            catalog.pending.add(metadata_path)
            catalog.failed_identities.pop(metadata_path, None)
            self._remove_entry(catalog, metadata_path)
            return
        if (
            metadata_identity.size <= 0
            or video_identity.size < self.min_video_bytes
            or not self._is_stable(metadata_identity)
            or not self._is_stable(video_identity)
        ):
            catalog.pending.add(metadata_path)
            catalog.failed_identities.pop(metadata_path, None)
            self._remove_entry(catalog, metadata_path)
            return

        existing = catalog.entries.get(metadata_path)
        if (
            existing is not None
            and existing.metadata_identity == metadata_identity
            and existing.video_identity == video_identity
        ):
            catalog.pending.discard(metadata_path)
            catalog.failed_identities.pop(metadata_path, None)
            return
        failed_identity = (metadata_identity, video_identity)
        if catalog.failed_identities.get(metadata_path) == failed_identity:
            catalog.pending.add(metadata_path)
            self._remove_entry(catalog, metadata_path)
            return
        cache_key = (str(metadata_path), metadata_identity)
        rows = self._row_cache.get(cache_key)
        if rows is not None:
            self._stats["row_cache_hits"] += 1
            self._row_cache.move_to_end(cache_key)
            parsed = list(rows)
        else:
            self._stats["row_cache_misses"] += 1
            try:
                parsed = self._parse_rows(metadata_path)
            except Exception:
                self._stats["parse_errors"] += 1
                catalog.pending.add(metadata_path)
                catalog.failed_identities[metadata_path] = failed_identity
                self._remove_entry(catalog, metadata_path)
                return
            self._remember_rows(metadata_path, metadata_identity, parsed)
        pts_values = [
            pts
            for pts in (self._row_pts(row) for row in parsed)
            if pts is not None
        ]
        if not pts_values:
            self._stats["parse_errors"] += 1
            catalog.pending.add(metadata_path)
            catalog.failed_identities[metadata_path] = failed_identity
            self._remove_entry(catalog, metadata_path)
            return
        actual_epoch = self._runtime_epoch_from_path(metadata_path)
        if actual_epoch and actual_epoch != catalog.runtime_epoch_id:
            catalog.pending.discard(metadata_path)
            catalog.failed_identities.pop(metadata_path, None)
            self._remove_entry(catalog, metadata_path)
            return
        segment = RollingSegment(
            segment_id=metadata_path.parent.name,
            source_id=catalog.source_id,
            runtime_epoch_id=catalog.runtime_epoch_id,
            directory=metadata_path.parent,
            video_path=video_path,
            metadata_path=metadata_path,
            first_pts=int(min(pts_values)),
            last_pts=int(max(pts_values)),
            frame_count=len(pts_values),
            size_bytes=video_identity.size,
        )
        catalog.entries[metadata_path] = _IndexedSegment(
            segment=segment,
            metadata_identity=metadata_identity,
            video_identity=video_identity,
        )
        catalog.pending.discard(metadata_path)
        catalog.failed_identities.pop(metadata_path, None)

    def _remove_entry(self, catalog: _Catalog, metadata_path: Path) -> None:
        if catalog.entries.pop(metadata_path, None) is not None:
            self._stats["stale_entries"] += 1
        metadata_text = str(metadata_path)
        for key in tuple(self._row_cache):
            if key[0] == metadata_text:
                self._row_cache.pop(key, None)

    def _parse_rows(self, metadata_path: Path) -> list[dict]:
        rows = [row for row in load_native_metadata(metadata_path) if isinstance(row, dict)]
        self._stats["metadata_parses"] += 1
        return rows

    def _remember_rows(
        self,
        metadata_path: Path,
        identity: FileIdentity,
        rows: Iterable[dict],
    ) -> None:
        key = (str(metadata_path), identity)
        self._row_cache[key] = tuple(rows)
        self._row_cache.move_to_end(key)
        while len(self._row_cache) > self.row_cache_max_entries:
            self._row_cache.popitem(last=False)
            self._stats["row_cache_evictions"] += 1

    def _evict_catalogs(self) -> None:
        while len(self._catalogs) > self.max_catalogs:
            self._catalogs.popitem(last=False)
            self._stats["catalog_evictions"] += 1

    def _pin_opened(self, token: str) -> None:
        with self._lock:
            self._active_pins.add(token)
            self._stats["read_pins_created"] += 1

    def _pin_closed(self, token: str) -> None:
        with self._lock:
            self._active_pins.discard(token)
            self._stats["read_pins_released"] += 1

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

    def _is_stable(self, identity: FileIdentity) -> bool:
        modified_at = identity.mtime_ns / 1_000_000_000.0
        return self._wall_clock() - modified_at >= self.stability_age_s

    def _read_root_generation(self) -> tuple[int, int]:
        path = self.root / GENERATION_FILE
        try:
            stat = path.stat()
            text = path.read_text(encoding="utf-8").strip()
            generation = int(text or 0)
            return generation, int(stat.st_mtime_ns)
        except (OSError, ValueError):
            return 0, 0

    @staticmethod
    def _directory_mtime(path: Path) -> int:
        try:
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
        value = row.get("pts")
        if value is None:
            value = row.get("frame_pts")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
