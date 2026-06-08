"""C2.15 watchlist emitter runtime regression tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
FACE_WORKER_ROOT = ROOT / "services" / "face-worker"


class FakeGalleryStore:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def search_gallery(
        self,
        embedding: list[float],
        *,
        top_k: int,
        min_similarity: float | None,
        person_ids: list[int] | None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "embedding": embedding,
                "top_k": top_k,
                "min_similarity": min_similarity,
                "person_ids": person_ids,
            }
        )
        return self.rows


class FakeRedis:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> bytes:
        self.messages.append(
            {
                "stream": stream,
                "fields": fields,
                "maxlen": maxlen,
                "approximate": approximate,
            }
        )
        return b"1-0"


def test_watchlist_emitter_publishes_threshold_hit_without_unsafe_payload() -> None:
    WatchlistMatchEmitter = _load_watchlist_match_emitter()
    try:
        emitter = object.__new__(WatchlistMatchEmitter)
        fake_store = FakeGalleryStore(
            [
                {
                    "person_id": 6,
                    "person_name": "Finch",
                    "external_person_id": "demo:f4_3:finch",
                    "id": 5,
                    "similarity": 0.6879,
                }
            ]
        )
        fake_redis = FakeRedis()
        emitter._cfg = SimpleNamespace(  # type: ignore[attr-defined]
            watchlist_top_k=5,
            watchlist_threshold=0.65,
            watchlist_event_stream="security.events",
        )
        emitter._store = fake_store  # type: ignore[attr-defined]
        emitter._redis = fake_redis  # type: ignore[attr-defined]
        emitter._current_target_person_ids = (  # type: ignore[attr-defined]
            lambda: [5, 6]
        )

        emitted = emitter.emit_for_observation(
            {
                "source_observation_id": "face:c2_replay_first_rtsp:734:35903",
                "camera_id": "c2_replay_first_rtsp",
                "source_id": "c2_replay_first_rtsp",
                "track_id": "734",
                "timestamp_ms": 1780943537211,
                "embedding": [1.0] + [0.0] * 511,
                "face_bbox": {"format": "xyxy", "values": [10, 20, 30, 40]},
                "landmarks": [],
                "quality": 0.9,
                "face_confidence": 0.8,
                "person_bbox": {"format": "xyxy", "values": [1, 2, 50, 80]},
                "payload": {
                    "media": {
                        "frame_uuid": "019e8a1c-frame",
                        "frame_pts": 123456789,
                        "frame_num": 42,
                    }
                },
            }
        )

        assert emitted == 1
        assert fake_store.calls == [
            {
                "embedding": [1.0] + [0.0] * 511,
                "top_k": 5,
                "min_similarity": 0.65,
                "person_ids": [5, 6],
            }
        ]
        assert len(fake_redis.messages) == 1
        message = fake_redis.messages[0]
        assert message["stream"] == "security.events"
        assert message["fields"]["event_type"] == "watchlist_hit"

        event = json.loads(message["fields"]["data"])
        assert event["event_type"] == "watchlist_hit"
        assert event["person_id"] == 6
        assert event["source_id"] == "c2_replay_first_rtsp"
        assert event["payload"]["match"]["similarity"] == 0.6879
        assert event["payload"]["match"]["threshold"] == 0.65
        assert event["payload"]["match"]["source_observation_id"] == (
            "face:c2_replay_first_rtsp:734:35903"
        )
        assert event["payload"]["matched_person"]["external_person_id"] == (
            "demo:f4_3:finch"
        )

        unsafe_keys = _collect_keys(event) & {
            "embedding",
            "image_base64",
            "image_bytes",
            "crop_bytes",
            "crop_base64",
            "face_crop_bytes",
            "face_crop_base64",
        }
        assert unsafe_keys == set()
    finally:
        _clear_face_worker_app_imports()


def _load_watchlist_match_emitter() -> type:
    if str(FACE_WORKER_ROOT) not in sys.path:
        sys.path.insert(0, str(FACE_WORKER_ROOT))
    from app.worker import WatchlistMatchEmitter

    return WatchlistMatchEmitter


def _clear_face_worker_app_imports() -> None:
    if str(FACE_WORKER_ROOT) in sys.path:
        sys.path.remove(str(FACE_WORKER_ROOT))
    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            sys.modules.pop(module_name, None)


def _collect_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        keys = set(value)
        for child in value.values():
            keys.update(_collect_keys(child))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for child in value:
            keys.update(_collect_keys(child))
        return keys
    return set()
