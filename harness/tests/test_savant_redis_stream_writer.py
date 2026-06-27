"""Redis Stream async writer tests for Savant hot-path exporters."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = ROOT / "modules" / "savant_security"
sys.path.insert(0, str(MODULE_DIR))

from custom.services.redis_stream_writer import AsyncRedisStreamWriter  # noqa: E402


class FakeRedisClient:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict, int | None, bool]] = []

    def xadd(self, stream, fields, maxlen=None, approximate=True):
        self.entries.append((stream, fields, maxlen, approximate))
        return "1-0"


class FakeRedisModule:
    client = FakeRedisClient()
    kwargs = {}

    class Redis:
        @staticmethod
        def from_url(url, **kwargs):
            FakeRedisModule.kwargs = {"url": url, **kwargs}
            return FakeRedisModule.client


def test_async_writer_sets_timeouts_and_drops_when_queue_full() -> None:
    writer = AsyncRedisStreamWriter(
        redis_url="redis://redis:6379/0",
        stream="security.test",
        maxlen=10,
        component="test_writer",
        socket_timeout_ms=25,
        connect_timeout_ms=30,
        queue_maxsize=1,
        redis_module=FakeRedisModule,
        start_worker=False,
    )

    assert FakeRedisModule.kwargs["socket_timeout"] == 0.025
    assert FakeRedisModule.kwargs["socket_connect_timeout"] == 0.03
    assert writer.enqueue({"a": "1"}) is True
    assert writer.enqueue({"b": "2"}) is False
    assert writer.dropped_count == 1
    writer.close()


def test_async_writer_drains_to_redis_stream_in_background() -> None:
    FakeRedisModule.client = FakeRedisClient()
    writer = AsyncRedisStreamWriter(
        redis_url="redis://redis:6379/0",
        stream="security.test",
        maxlen=10,
        component="test_writer",
        redis_module=FakeRedisModule,
    )

    assert writer.enqueue({"a": "1"}) is True
    assert writer.flush(timeout_s=1.0) is True
    assert FakeRedisModule.client.entries == [
        ("security.test", {"a": "1"}, 10, True)
    ]
    writer.close()
