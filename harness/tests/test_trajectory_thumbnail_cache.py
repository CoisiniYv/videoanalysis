from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from app.main import MediaStaticFiles  # noqa: E402
from app.services.trajectory_thumbnail_cache import (  # noqa: E402
    cache_trajectory_thumbnails,
)


def test_cache_keeps_only_latest_hundred_per_person_without_deleting_canonical(
    tmp_path: Path,
    monkeypatch,
) -> None:
    media_root = tmp_path / "media"
    canonical_root = media_root / "face_trajectories" / "2026" / "07" / "12"
    cache_root = media_root / "face_trajectory_cache"
    canonical_root.mkdir(parents=True)
    monkeypatch.setenv("MEDIA_ROOT", str(media_root))
    monkeypatch.setenv("FACE_TRAJECTORY_CACHE_ROOT", str(cache_root))
    monkeypatch.setenv("FACE_TRAJECTORY_CACHE_LIMIT_PER_PERSON", "100")

    uris = []
    for index in range(105):
        source = canonical_root / f"{index:03d}.jpg"
        source.write_bytes(b"\xff\xd8" + bytes([index % 251]) * 8 + b"\xff\xd9")
        uris.append("/media/" + source.relative_to(media_root).as_posix())

    mappings = cache_trajectory_thumbnails(7, uris)

    assert len(mappings) == 100
    assert len(list((cache_root / "person_7").glob("*.jpg"))) == 100
    assert len(list(canonical_root.glob("*.jpg"))) == 105
    assert all(
        url.startswith("/media/face_trajectory_cache/person_7/")
        for url in mappings.values()
    )


def test_cache_rejects_media_paths_outside_canonical_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8outside\xff\xd9")
    monkeypatch.setenv("MEDIA_ROOT", str(media_root))
    monkeypatch.setenv(
        "FACE_TRAJECTORY_CACHE_ROOT",
        str(media_root / "face_trajectory_cache"),
    )

    assert cache_trajectory_thumbnails(7, [str(outside)]) == {}


def test_cached_thumbnail_is_served_with_immutable_browser_cache(
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    thumbnail = media_root / "face_trajectory_cache" / "person_7" / "demo.jpg"
    thumbnail.parent.mkdir(parents=True)
    thumbnail.write_bytes(b"\xff\xd8demo\xff\xd9")
    static_app = FastAPI()
    static_app.mount("/media", MediaStaticFiles(directory=media_root), name="media")

    with TestClient(static_app) as client:
        response = client.get("/media/face_trajectory_cache/person_7/demo.jpg")

    assert response.status_code == 200
    assert response.headers["cache-control"] == ("public, max-age=31536000, immutable")
