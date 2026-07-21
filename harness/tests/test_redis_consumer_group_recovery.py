"""Redis consumer-group recovery contracts for long-lived workers."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest
from redis.exceptions import ResponseError


ROOT = Path(__file__).resolve().parents[2]
CONSUMER_PATHS = (
    ROOT / "services" / "event-worker" / "app" / "redis_consumer.py",
    ROOT / "services" / "face-worker" / "app" / "redis_consumer.py",
)


def _load_consumer(path: Path):
    module_name = f"redis_consumer_recovery_{path.parent.parent.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RedisStreamConsumer


class _DroppedGroupRedis:
    def __init__(self) -> None:
        self.read_calls = 0
        self.group_creates: list[tuple[str, str, str, bool]] = []

    def xreadgroup(self, group, consumer, streams, *, count, block):
        self.read_calls += 1
        if self.read_calls == 1:
            raise ResponseError(
                "NOGROUP No such key 'security.test' or consumer group 'workers'"
            )
        assert group == "workers"
        assert consumer == "worker-1"
        assert streams == {"security.test": ">"}
        assert count == 7
        assert block == 123
        return [[b"security.test", [(b"42-0", {b"data": b"ok"})]]]

    def xgroup_create(self, stream, group, *, id, mkstream):
        self.group_creates.append((stream, group, id, mkstream))


@pytest.mark.parametrize("consumer_path", CONSUMER_PATHS)
def test_read_new_recreates_a_dropped_group_from_retained_rows(
    consumer_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = _DroppedGroupRedis()
    consumer_type = _load_consumer(consumer_path)
    consumer = consumer_type(
        redis,
        "security.test",
        "workers",
        "worker-1",
        start_id="$",
    )

    with caplog.at_level(logging.INFO):
        messages = consumer.read_new(count=7, block_ms=123)

    assert messages == [("42-0", {b"data": b"ok"})]
    assert redis.read_calls == 2
    # Recovery replays retained rows. Database writes are idempotent, whereas
    # recreating at '$' would silently lose the already-published backlog.
    assert redis.group_creates == [("security.test", "workers", "0", True)]
    assert "consumer group missing" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
