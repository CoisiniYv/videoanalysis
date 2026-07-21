#!/usr/bin/env python3
"""Single-owner retention and byte-quota maintenance for rolling segments."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


GENERATION_FILE = ".rolling-cache-generation"
OWNER_LOCK_FILE = ".rolling-cache-maintenance-owner.lock"
MUTATION_LOCK_FILE = ".rolling-cache-mutation.lock"
READ_PIN_DIR = ".read-pins"
VIDEO_NAMES = ("video.mov", "raw_clip.mov", "video.mp4", "raw_clip.mp4", "video.mkv")


@dataclass(frozen=True)
class SegmentCandidate:
    directory: Path
    metadata_path: Path
    video_path: Path
    size_bytes: int
    newest_mtime_s: float


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)


def _find_video(directory: Path) -> Path | None:
    for name in VIDEO_NAMES:
        path = directory / name
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    for pattern in ("*.mov", "*.mp4", "*.mkv", "*.webm"):
        for path in directory.glob(pattern):
            try:
                if path.is_file() and path.stat().st_size > 0:
                    return path
            except OSError:
                continue
    return None


def _directory_size(directory: Path) -> int:
    total = 0
    try:
        paths = list(directory.rglob("*"))
    except OSError:
        return 0
    for path in paths:
        try:
            if path.is_file() and not path.is_symlink():
                total += int(path.stat().st_size)
        except OSError:
            continue
    return total


def discover_segments(
    root: Path,
    *,
    now_s: float,
    stability_age_s: float,
) -> list[SegmentCandidate]:
    candidates: list[SegmentCandidate] = []
    if not root.exists():
        return candidates
    for metadata_path in root.rglob("metadata.json"):
        if READ_PIN_DIR in metadata_path.parts or "materialized" in metadata_path.parts:
            continue
        directory = metadata_path.parent.resolve(strict=False)
        video_path = _find_video(directory)
        if video_path is None:
            continue
        try:
            metadata_stat = metadata_path.stat()
            video_stat = video_path.stat()
        except OSError:
            continue
        newest_mtime_s = max(metadata_stat.st_mtime, video_stat.st_mtime)
        if now_s - newest_mtime_s < stability_age_s:
            continue
        candidates.append(
            SegmentCandidate(
                directory=directory,
                metadata_path=metadata_path.resolve(strict=False),
                video_path=video_path.resolve(strict=False),
                size_bytes=_directory_size(directory),
                newest_mtime_s=newest_mtime_s,
            )
        )
    return candidates


def load_active_read_pins(
    root: Path,
    *,
    now_s: float,
    corrupt_pin_ttl_s: float,
) -> tuple[set[Path], dict[str, int | bool]]:
    pin_dir = root / READ_PIN_DIR
    active: set[Path] = set()
    stats: dict[str, int | bool] = {
        "pin_files": 0,
        "active_pins": 0,
        "expired_pins_removed": 0,
        "corrupt_pins_removed": 0,
        "conservative_block_all": False,
    }
    if not pin_dir.exists():
        return active, stats
    for marker in pin_dir.glob("*.json"):
        stats["pin_files"] = int(stats["pin_files"]) + 1
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            expires_at = float(payload.get("expires_at_epoch_s") or 0.0)
            segments = payload.get("segments")
            if not isinstance(segments, list):
                raise ValueError("segments_not_list")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            try:
                age_s = max(0.0, now_s - marker.stat().st_mtime)
            except OSError:
                continue
            if age_s >= corrupt_pin_ttl_s:
                marker.unlink(missing_ok=True)
                stats["corrupt_pins_removed"] = int(stats["corrupt_pins_removed"]) + 1
            else:
                stats["conservative_block_all"] = True
            continue
        if expires_at <= now_s:
            marker.unlink(missing_ok=True)
            stats["expired_pins_removed"] = int(stats["expired_pins_removed"]) + 1
            continue
        stats["active_pins"] = int(stats["active_pins"]) + 1
        for relative in segments:
            if not isinstance(relative, str) or not relative:
                continue
            resolved = (root / relative).resolve(strict=False)
            try:
                resolved.relative_to(root.resolve(strict=False))
            except ValueError:
                stats["conservative_block_all"] = True
                continue
            active.add(resolved)
    return active, stats


def _advance_generation(root: Path) -> int:
    path = root / GENERATION_FILE
    try:
        generation = int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        generation = 0
    generation += 1
    temp = root / f".{GENERATION_FILE}.{os.getpid()}.tmp"
    try:
        temp.write_text(f"{generation}\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return generation


def _delete_segment(candidate: SegmentCandidate, *, root: Path) -> int:
    directory = candidate.directory.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    try:
        directory.relative_to(root_resolved)
    except ValueError as exc:
        raise RuntimeError(f"refusing to delete outside rolling root: {directory}") from exc
    if directory == root_resolved:
        raise RuntimeError("refusing to delete rolling root")
    size = candidate.size_bytes
    shutil.rmtree(directory)
    parent = directory.parent
    while parent != root_resolved:
        if parent.name == READ_PIN_DIR:
            break
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return size


def _candidate_is_unchanged_and_stable(
    candidate: SegmentCandidate,
    *,
    now_s: float,
    stability_age_s: float,
) -> bool:
    """Revalidate a segment after acquiring the deletion lock.

    Discovery is intentionally performed without the root-wide mutation lock
    so remux readers are not blocked by a full retention-tree walk.  Before a
    delete, the small immutable identity is checked again while the lock is
    held.  A segment that changed between discovery and deletion is deferred
    to the next maintenance pass.
    """

    try:
        metadata_stat = candidate.metadata_path.stat()
        current_video = _find_video(candidate.directory)
        if current_video is None:
            return False
        current_video = current_video.resolve(strict=False)
        if current_video != candidate.video_path:
            return False
        video_stat = current_video.stat()
    except OSError:
        return False
    newest_mtime_s = max(metadata_stat.st_mtime, video_stat.st_mtime)
    if newest_mtime_s != candidate.newest_mtime_s:
        return False
    return now_s - newest_mtime_s >= stability_age_s


def cleanup_once(
    root: Path,
    *,
    retention_s: float,
    max_bytes: int,
    read_pin_ttl_s: float,
    stability_age_s: float,
    now_s: float | None = None,
    dry_run: bool = False,
) -> dict[str, int | float | bool | str]:
    root = root.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    now = time.time() if now_s is None else float(now_s)
    discovery_started = time.monotonic()
    candidates = discover_segments(
        root,
        now_s=now,
        stability_age_s=stability_age_s,
    )
    discovery_duration_ms = int((time.monotonic() - discovery_started) * 1000)
    total_bytes = sum(candidate.size_bytes for candidate in candidates)
    mutation_lock = root / MUTATION_LOCK_FILE
    lock_started = time.monotonic()
    with mutation_lock.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        lock_acquired = time.monotonic()
        pinned, pin_stats = load_active_read_pins(
            root,
            now_s=now,
            corrupt_pin_ttl_s=max(1.0, read_pin_ttl_s),
        )
        deleted_bytes = 0
        retention_deleted = 0
        quota_deleted = 0
        skipped_pinned = 0
        deleted_paths: set[Path] = set()
        block_all = bool(pin_stats["conservative_block_all"])

        if retention_s > 0:
            cutoff = now - retention_s
            for candidate in sorted(candidates, key=lambda item: item.newest_mtime_s):
                if candidate.newest_mtime_s >= cutoff:
                    continue
                if block_all or candidate.directory in pinned:
                    skipped_pinned += 1
                    continue
                if not _candidate_is_unchanged_and_stable(
                    candidate,
                    now_s=now,
                    stability_age_s=stability_age_s,
                ):
                    continue
                if not dry_run:
                    deleted_bytes += _delete_segment(candidate, root=root)
                else:
                    deleted_bytes += candidate.size_bytes
                deleted_paths.add(candidate.directory)
                retention_deleted += 1

        remaining_bytes = max(0, total_bytes - deleted_bytes)
        if max_bytes > 0 and remaining_bytes > max_bytes:
            for candidate in sorted(candidates, key=lambda item: item.newest_mtime_s):
                if remaining_bytes <= max_bytes:
                    break
                if candidate.directory in deleted_paths:
                    continue
                if block_all or candidate.directory in pinned:
                    skipped_pinned += 1
                    continue
                if not _candidate_is_unchanged_and_stable(
                    candidate,
                    now_s=now,
                    stability_age_s=stability_age_s,
                ):
                    continue
                if not dry_run:
                    removed = _delete_segment(candidate, root=root)
                else:
                    removed = candidate.size_bytes
                deleted_bytes += removed
                remaining_bytes = max(0, remaining_bytes - removed)
                deleted_paths.add(candidate.directory)
                quota_deleted += 1

        generation = 0
        if deleted_paths and not dry_run:
            generation = _advance_generation(root)
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_released = time.monotonic()

    return {
        "status": "ok",
        "root": str(root),
        "retention_s": retention_s,
        "max_bytes": max_bytes,
        "segments_seen": len(candidates),
        "bytes_seen": total_bytes,
        "retention_deleted": retention_deleted,
        "quota_deleted": quota_deleted,
        "deleted_bytes": deleted_bytes,
        "remaining_bytes": max(0, total_bytes - deleted_bytes),
        "skipped_pinned": skipped_pinned,
        "active_pins": int(pin_stats["active_pins"]),
        "expired_pins_removed": int(pin_stats["expired_pins_removed"]),
        "corrupt_pins_removed": int(pin_stats["corrupt_pins_removed"]),
        "conservative_block_all": block_all,
        "generation": generation,
        "dry_run": dry_run,
        "discovery_duration_ms": discovery_duration_ms,
        "lock_wait_ms": int((lock_acquired - lock_started) * 1000),
        "lock_hold_ms": int((lock_released - lock_acquired) * 1000),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.getenv("ROLLING_CACHE_ROOT", "/media/rolling-cache")),
    )
    parser.add_argument(
        "--retention-s",
        type=float,
        default=_env_float("ROLLING_CACHE_RETENTION_SECONDS", 300.0),
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=_env_int("ROLLING_CACHE_MAX_BYTES", 0),
    )
    parser.add_argument(
        "--interval-s",
        type=float,
        default=_env_float(
            "ROLLING_CACHE_CLEANUP_INTERVAL_SECONDS",
            30.0,
            minimum=1.0,
        ),
    )
    parser.add_argument(
        "--read-pin-ttl-s",
        type=float,
        default=_env_float(
            "ROLLING_CACHE_READ_PIN_TTL_SECONDS",
            600.0,
            minimum=1.0,
        ),
    )
    parser.add_argument(
        "--stability-age-s",
        type=float,
        default=_env_float(
            "ROLLING_CACHE_MAINTENANCE_STABILITY_AGE_SECONDS",
            5.0,
        ),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _acquire_owner_lock(root: Path) -> TextIO | None:
    root.mkdir(parents=True, exist_ok=True)
    lock_fh = (root / OWNER_LOCK_FILE).open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.close()
        return None
    lock_fh.seek(0)
    lock_fh.truncate()
    lock_fh.write(f"pid={os.getpid()} acquired_at={time.time()}\n")
    lock_fh.flush()
    return lock_fh


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve(strict=False)
    owner_lock = _acquire_owner_lock(root)
    if owner_lock is None:
        print(
            json.dumps(
                {
                    "status": "standby",
                    "reason": "maintenance_owner_lock_held",
                    "root": str(root),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    try:
        while True:
            started = time.monotonic()
            result = cleanup_once(
                root,
                retention_s=max(0.0, args.retention_s),
                max_bytes=max(0, args.max_bytes),
                read_pin_ttl_s=max(1.0, args.read_pin_ttl_s),
                stability_age_s=max(0.0, args.stability_age_s),
                dry_run=args.dry_run,
            )
            result["duration_ms"] = int((time.monotonic() - started) * 1000)
            result["owner_pid"] = os.getpid()
            print(json.dumps(result, sort_keys=True), flush=True)
            if args.once:
                return 0
            time.sleep(max(1.0, args.interval_s))
    finally:
        fcntl.flock(owner_lock.fileno(), fcntl.LOCK_UN)
        owner_lock.close()


if __name__ == "__main__":
    sys.exit(main())
