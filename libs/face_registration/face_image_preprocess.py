"""Helpers for external submitted face image registration."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StorageResolution:
    registration_root: str
    registered_crop_path: str
    storage_fallback_used: bool
    storage_fallback_reason: str | None


def resolve_registration_storage(image_path: str) -> StorageResolution:
    """Resolve crop output path with media-root fallback semantics."""
    media_root = os.getenv("MEDIA_ROOT", "/data/video-analytics/media")
    preferred_root = os.getenv(
        "FACE_REGISTRATION_ROOT",
        os.path.join(media_root, "face_registration"),
    )
    fallback_root = os.path.join(".", "tmp", "face_registration")

    root = preferred_root
    fallback_used = False
    fallback_reason: str | None = None

    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        probe = Path(root) / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        root = fallback_root
        fallback_used = True
        fallback_reason = f"media_root_unavailable:{exc.__class__.__name__}"
        Path(root).mkdir(parents=True, exist_ok=True)

    src = Path(image_path).resolve()
    crop_name = f"{src.stem}_registered_crop{src.suffix or '.jpg'}"
    crop_path = os.path.join(root, crop_name)
    return StorageResolution(
        registration_root=root,
        registered_crop_path=crop_path,
        storage_fallback_used=fallback_used,
        storage_fallback_reason=fallback_reason,
    )


def validate_image_path(image_path: str) -> str:
    """Return absolute path if the source image exists."""
    abs_path = str(Path(image_path).resolve())
    if not os.path.exists(abs_path):
        raise FileNotFoundError(abs_path)
    if not os.path.isfile(abs_path):
        raise IsADirectoryError(abs_path)
    return abs_path


def save_registered_crop(
    image_path: str,
    destination_path: str,
    *,
    keep_crop: bool,
) -> str | None:
    """Persist a demo crop artifact by copying the source image."""
    if not keep_crop:
        return None
    Path(destination_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image_path, destination_path)
    return destination_path
