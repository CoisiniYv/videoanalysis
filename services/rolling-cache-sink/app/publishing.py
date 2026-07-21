"""Atomic rolling-segment publication independent of GStreamer."""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config import safe_component


@dataclass
class Fragment:
    segment_id: str
    staging_dir: Path
    final_dir: Path
    video_path: Path
    first_gst_pts: int | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)


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
        if not fragment.video_path.is_file() or fragment.video_path.stat().st_size <= 0:
            raise RuntimeError(f"rolling_segment_video_missing:{fragment.segment_id}")
        if not any(_row_pts(row) is not None for row in fragment.rows):
            raise RuntimeError(
                f"rolling_segment_metadata_has_no_pts:{fragment.segment_id}"
            )

        metadata_path = fragment.staging_dir / "metadata.json"
        with metadata_path.open("x", encoding="utf-8") as handle:
            for row in fragment.rows:
                handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(fragment.staging_dir)

        fragment.final_dir.parent.mkdir(parents=True, exist_ok=True)
        if fragment.final_dir.exists():
            raise FileExistsError(
                f"rolling_segment_final_collision:{fragment.segment_id}"
            )
        os.replace(fragment.staging_dir, fragment.final_dir)
        _fsync_directory(fragment.final_dir.parent)
        return fragment.final_dir


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
