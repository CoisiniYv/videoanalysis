"""Read-through SSD cache for immutable trajectory thumbnails.

The media tree remains the canonical, long-term store.  This cache only keeps
the most recently accessed thumbnails for each registered person and may be
deleted at any time.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Iterable


LOGGER = logging.getLogger(__name__)
DEFAULT_CACHE_LIMIT_PER_PERSON = 100
SUPPORTED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def _media_root() -> Path:
    return Path(os.getenv("MEDIA_ROOT", "/data/video-analytics/media")).resolve()


def _cache_root() -> Path:
    return Path(
        os.getenv(
            "FACE_TRAJECTORY_CACHE_ROOT",
            str(_media_root() / "face_trajectory_cache"),
        )
    ).resolve()


def _cache_limit() -> int:
    raw = os.getenv(
        "FACE_TRAJECTORY_CACHE_LIMIT_PER_PERSON",
        str(DEFAULT_CACHE_LIMIT_PER_PERSON),
    )
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_CACHE_LIMIT_PER_PERSON


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _canonical_path(uri: str) -> Path | None:
    text = str(uri or "").strip()
    if not text:
        return None
    media_root = _media_root()
    if text.startswith("/media/"):
        candidate = media_root / text.removeprefix("/media/")
    else:
        candidate = Path(text)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not _is_within(resolved, media_root):
        return None
    if resolved.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
        return None
    return resolved


def _media_url(path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(_media_root())
    except ValueError:
        return None
    return "/media/" + relative.as_posix()


def _prune(person_dir: Path, limit: int) -> None:
    try:
        files = sorted(
            (
                path
                for path in person_dir.iterdir()
                if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        LOGGER.exception("trajectory thumbnail cache scan failed path=%s", person_dir)
        return
    for stale in files[limit:]:
        try:
            stale.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            LOGGER.exception("trajectory thumbnail cache prune failed path=%s", stale)


def cache_trajectory_thumbnails(
    person_id: int,
    source_uris: Iterable[str | None],
) -> dict[str, str]:
    """Cache accessed thumbnails and return canonical-uri to cache-url mappings.

    At most ``FACE_TRAJECTORY_CACHE_LIMIT_PER_PERSON`` files survive for each
    person.  Pruning only touches the SSD cache directory; canonical media is
    never modified.
    """

    cache_root = _cache_root()
    media_root = _media_root()
    if not _is_within(cache_root, media_root):
        LOGGER.warning(
            "trajectory thumbnail cache root must be mounted under media root: %s",
            cache_root,
        )
        return {}

    person_dir = cache_root / f"person_{int(person_id)}"
    try:
        person_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        LOGGER.exception("trajectory thumbnail cache mkdir failed path=%s", person_dir)
        return {}

    limit = _cache_limit()
    access_ns = time.time_ns()
    mappings: dict[str, str] = {}
    seen: set[str] = set()
    cached_count = 0
    for raw_uri in source_uris:
        if cached_count >= limit:
            break
        uri = str(raw_uri or "").strip()
        if not uri or uri in seen:
            continue
        seen.add(uri)
        source = _canonical_path(uri)
        if source is None or _is_within(source, cache_root):
            continue
        try:
            stat = source.stat()
            digest = hashlib.sha256(
                f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
            ).hexdigest()
            suffix = source.suffix.lower()
            target = person_dir / f"{digest}{suffix}"
            if not target.exists():
                temporary = person_dir / f".{digest}.{uuid.uuid4().hex}.tmp"
                try:
                    shutil.copyfile(source, temporary)
                    temporary.replace(target)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            touched_ns = access_ns - cached_count
            os.utime(target, ns=(touched_ns, touched_ns))
            url = _media_url(target)
            if url:
                mappings[uri] = url
                cached_count += 1
        except OSError:
            LOGGER.exception(
                "trajectory thumbnail cache populate failed person_id=%s source=%s",
                person_id,
                source,
            )

    _prune(person_dir, limit)
    return mappings
