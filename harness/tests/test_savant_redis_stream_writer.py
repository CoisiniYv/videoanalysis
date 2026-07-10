"""Redis Stream async writer tests for Savant hot-path exporters."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = ROOT / "modules" / "savant_security"
sys.path.insert(0, str(MODULE_DIR))

from custom.services.redis_stream_writer import AsyncRedisStreamWriter  # noqa: E402
from poc_deps import redis as redis_shim  # noqa: E402


class FakeRedisClient:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict, int | None, bool]] = []

    def xadd(self, stream, fields, maxlen=None, approximate=True):
        self.entries.append((stream, fields, maxlen, approximate))
        return "1-0"


class FlakyRedisClient(FakeRedisClient):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    def xadd(self, stream, fields, maxlen=None, approximate=True):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise OSError(99, "Cannot assign requested address")
        return super().xadd(stream, fields, maxlen=maxlen, approximate=approximate)


class FakeRedisModule:
    client = FakeRedisClient()
    kwargs = {}

    class Redis:
        @staticmethod
        def from_url(url, **kwargs):
            FakeRedisModule.kwargs = {"url": url, **kwargs}
            return FakeRedisModule.client


class LegacyRedisModule:
    client = FakeRedisClient()
    kwargs = {}

    class Redis:
        @staticmethod
        def from_url(url, **kwargs):
            if "socket_keepalive" in kwargs or "single_connection_client" in kwargs:
                raise TypeError("unsupported keyword")
            LegacyRedisModule.kwargs = {"url": url, **kwargs}
            return LegacyRedisModule.client


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
    assert FakeRedisModule.kwargs["socket_keepalive"] is True
    assert FakeRedisModule.kwargs["single_connection_client"] is True
    assert writer.enqueue({"a": "1"}) is True
    assert writer.enqueue({"b": "2"}) is False
    assert writer.dropped_count == 1
    writer.close()


def test_async_writer_falls_back_for_legacy_redis_client_kwargs() -> None:
    LegacyRedisModule.client = FakeRedisClient()
    LegacyRedisModule.kwargs = {}
    writer = AsyncRedisStreamWriter(
        redis_url="redis://redis:6379/0",
        stream="security.test",
        maxlen=10,
        component="test_writer",
        socket_timeout_ms=25,
        connect_timeout_ms=30,
        redis_module=LegacyRedisModule,
        start_worker=False,
    )

    assert LegacyRedisModule.kwargs == {
        "url": "redis://redis:6379/0",
        "socket_timeout": 0.025,
        "socket_connect_timeout": 0.03,
    }
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


def test_async_writer_can_override_stream_per_message() -> None:
    FakeRedisModule.client = FakeRedisClient()
    writer = AsyncRedisStreamWriter(
        redis_url="redis://redis:6379/0",
        stream="security.test",
        maxlen=10,
        component="test_writer",
        redis_module=FakeRedisModule,
    )

    assert writer.enqueue(
        {"a": "1"},
        stream="security.test.source-a",
        maxlen=5,
    ) is True
    assert writer.flush(timeout_s=1.0) is True
    assert FakeRedisModule.client.entries == [
        ("security.test.source-a", {"a": "1"}, 5, True)
    ]
    writer.close()


def test_async_writer_retries_transient_redis_write_error() -> None:
    flaky = FlakyRedisClient(failures=1)
    FakeRedisModule.client = flaky
    writer = AsyncRedisStreamWriter(
        redis_url="redis://redis:6379/0",
        stream="security.test",
        maxlen=10,
        component="test_writer",
        write_retries=2,
        retry_sleep_ms=0,
        redis_module=FakeRedisModule,
    )

    assert writer.enqueue({"a": "1"}) is True
    assert writer.flush(timeout_s=1.0) is True
    assert flaky.attempts == 2
    assert flaky.entries == [("security.test", {"a": "1"}, 10, True)]
    assert writer.write_error_count == 0
    writer.close()


def test_poc_redis_shim_accepts_persistent_connection_kwargs() -> None:
    client = redis_shim.Redis.from_url(
        "redis://redis:6379/0",
        socket_timeout=0.5,
        socket_connect_timeout=0.25,
        socket_keepalive=True,
        single_connection_client=True,
    )

    assert client.socket_timeout == 0.5
    assert client.socket_connect_timeout == 0.25
    assert client.socket_keepalive is True
    assert client.single_connection_client is True
    client.close()
