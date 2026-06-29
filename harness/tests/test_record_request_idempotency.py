"""Record request idempotency contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVENT_WORKER_DIR = str(ROOT / "services" / "event-worker")
if EVENT_WORKER_DIR in sys.path:
    sys.path.remove(EVENT_WORKER_DIR)
sys.path.insert(0, EVENT_WORKER_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.record_request import RecordRequestPublisher  # noqa: E402


class _FakeRedis:
    def __init__(self, *, fail_xadd: bool = False) -> None:
        self.fail_xadd = fail_xadd
        self.streams: dict[str, list[tuple[bytes, dict[bytes, bytes]]]] = {}
        self.keys: dict[str, str] = {}
        self.set_calls: list[dict[str, Any]] = []
        self.exists_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.xadd_calls: list[dict[str, Any]] = []
        self.xrange_calls: list[tuple[Any, ...]] = []

    def set(self, name: str, value: str, *, ex: int, nx: bool) -> bool:
        self.set_calls.append({"name": name, "value": value, "ex": ex, "nx": nx})
        if nx and name in self.keys:
            return False
        self.keys[name] = value
        return True

    def get(self, name: str) -> str | None:
        return self.keys.get(name)

    def exists(self, name: str) -> int:
        self.exists_calls.append(name)
        return 1 if name in self.keys else 0

    def delete(self, name: str) -> int:
        self.delete_calls.append(name)
        existed = name in self.keys
        self.keys.pop(name, None)
        return 1 if existed else 0

    def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> bytes:
        self.xadd_calls.append(
            {
                "stream": stream,
                "fields": fields,
                "maxlen": maxlen,
                "approximate": approximate,
            }
        )
        if self.fail_xadd:
            raise RuntimeError("xadd failed")
        entry_id = f"{len(self.streams.get(stream, [])) + 1}-0".encode("utf-8")
        encoded = {
            str(key).encode("utf-8"): str(value).encode("utf-8")
            for key, value in fields.items()
        }
        self.streams.setdefault(stream, []).append((entry_id, encoded))
        return entry_id

    def xrange(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.xrange_calls.append((*args, kwargs))
        raise AssertionError("record request dedupe must not scan Redis streams")


def _event(source_event_id: str = "src:evt:1") -> dict[str, Any]:
    return {
        "event_type": "intrusion",
        "source_event_id": source_event_id,
        "camera_id": "cam_lab",
        "source_id": "lab",
        "event_ts_ms": 1_765_000_000_000,
        "clip_required": True,
        "evidence_policy": {"clip_required": True, "pre_seconds": 5, "post_seconds": 5},
        "payload": {"media": {"clip_required": True, "source_id": "lab"}},
    }


def _published_records(fake: _FakeRedis) -> list[dict[str, Any]]:
    rows = fake.streams.get("security.record_requests", [])
    return [json.loads(fields[b"data"].decode("utf-8")) for _entry_id, fields in rows]


def test_publish_sets_o1_dedupe_key_and_suppresses_duplicate_without_xrange() -> None:
    fake = _FakeRedis()
    publisher = RecordRequestPublisher(
        fake,
        "security.record_requests",
        dedupe_ttl_seconds=60,
    )

    first = publisher.publish(_event(), "00000000-0000-4000-8000-000000000001")
    second = publisher.publish(_event(), "00000000-0000-4000-8000-000000000001")

    assert first == "1-0"
    assert second is None
    assert len(fake.xadd_calls) == 1
    assert len(fake.set_calls) == 2
    assert fake.set_calls[0]["ex"] == 60
    assert fake.set_calls[0]["nx"] is True
    assert fake.xrange_calls == []
    assert [record["source_event_id"] for record in _published_records(fake)] == [
        "src:evt:1"
    ]


def test_has_request_uses_dedupe_key_exists_not_xrange() -> None:
    fake = _FakeRedis()
    publisher = RecordRequestPublisher(fake, "security.record_requests")

    assert publisher.has_request("src:evt:1", "savant_replay") is False
    publisher.publish(_event(), "00000000-0000-4000-8000-000000000001")

    assert publisher.has_request("src:evt:1", "savant_replay") is True
    assert len(fake.exists_calls) == 2
    assert fake.xrange_calls == []


def test_publish_failure_releases_dedupe_key_so_retry_can_publish() -> None:
    failing = _FakeRedis(fail_xadd=True)
    publisher = RecordRequestPublisher(failing, "security.record_requests")

    assert publisher.publish(_event(), "00000000-0000-4000-8000-000000000001") is None
    assert failing.keys == {}
    assert len(failing.delete_calls) == 1

    retry = _FakeRedis()
    retry.keys = failing.keys
    retry_publisher = RecordRequestPublisher(retry, "security.record_requests")

    assert retry_publisher.publish(
        _event(),
        "00000000-0000-4000-8000-000000000001",
    ) == "1-0"
    assert len(retry.xadd_calls) == 1
