"""Spec 34 Phase 3A Clip Coordinator delivery/outcome/ACK contracts."""

from __future__ import annotations

import ast
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLIP_ROOT = ROOT / "services" / "clip-worker"

# The ``pytest`` console script does not guarantee that the repository root is
# on sys.path.  Keep the contract test hermetic so clip-worker's shared
# ``libs.*`` imports work both with ``pytest`` and ``python -m pytest``.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _activate():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    root = str(CLIP_ROOT)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    import app.contracts as contracts
    import app.coordinator as coordinator
    import app.evidence_state_repository as evidence_state
    import app.replay_admission_repository as replay_admission
    import app.request_consumer as request_consumer

    return contracts, coordinator, evidence_state, replay_admission, request_consumer


class FakeRedis:
    def __init__(self) -> None:
        self.new_entries: list[tuple[object, object]] = []
        self.pending_entries: list[tuple[object, object]] = []
        self.pending_counts: dict[str, int] = {}
        self.acked: list[str] = []
        self.group_created = False

    def xgroup_create(self, *_args: Any, **_kwargs: Any) -> None:
        self.group_created = True

    def xreadgroup(self, *_args: Any, **_kwargs: Any):
        entries = list(self.new_entries)
        self.new_entries.clear()
        return [(b"security.record_requests", entries)] if entries else []

    def xpending_range(self, *_args: Any, **_kwargs: Any):
        return [
            {"message_id": message_id, "times_delivered": count}
            for message_id, count in self.pending_counts.items()
        ]

    def xautoclaim(self, *_args: Any, **_kwargs: Any):
        entries = list(self.pending_entries)
        self.pending_entries.clear()
        return ("0-0", entries, [])

    def xack(self, _stream: str, _group: str, message_id: str) -> int:
        self.acked.append(str(message_id))
        return 1

    def xpending(self, *_args: Any, **_kwargs: Any):
        return {"pending": len(self.pending_entries)}

    def xinfo_groups(self, *_args: Any, **_kwargs: Any):
        return [{"name": b"clip-workers", "pending": 0, "lag": 0}]


def _consumer(fake: FakeRedis):
    _contracts, _coordinator, _state, _admission, module = _activate()
    return module.RequestConsumer(
        fake,
        module.ConsumerSettings(
            stream="security.record_requests",
            group="clip-workers",
            consumer="clip-1",
            poll_timeout_ms=0,
            pending_claim_min_idle_ms=0,
            pending_claim_count=10,
        ),
    )


def _delivery(contracts, *, message_id: str = "1-0"):
    return contracts.DeliveryEnvelope(
        stream="security.record_requests",
        group="clip-workers",
        consumer="clip-1",
        message_id=message_id,
        fields=((b"data", json.dumps({"event_id": "event-1"}).encode()),),
    )


def test_request_consumer_normalizes_new_and_reclaimed_deliveries() -> None:
    fake = FakeRedis()
    consumer = _consumer(fake)
    consumer.ensure_group()
    fake.new_entries = [
        (b"1-0", {b"data": json.dumps({"event_id": "event-new"}).encode()})
    ]
    new_delivery = consumer.read_new()[0]

    fake.pending_counts = {"9-0": 3}
    fake.pending_entries = [
        (b"9-0", {b"data": json.dumps({"event_id": "event-old"}).encode()})
    ]
    reclaimed = consumer.reclaim()[0]

    assert fake.group_created is True
    assert new_delivery.message_id == "1-0"
    assert new_delivery.reclaimed is False
    assert consumer.parse_json(new_delivery) == {"event_id": "event-new"}
    assert reclaimed.reclaimed is True
    assert reclaimed.delivery_count == 3
    assert reclaimed.retry_count == 2
    assert consumer.parse_json(reclaimed) == {"event_id": "event-old"}
    assert consumer.diagnostics() == {"pending": 0, "lag": 0}


@pytest.mark.parametrize(
    ("code", "disposition", "durable", "expected_ack"),
    (
        ("malformed", "ack", True, True),
        ("duplicate_terminal", "ack", True, True),
        ("replay_created", "ack", True, True),
        ("permanent_failure", "ack", True, True),
        ("retry_pending", "hold", True, False),
        ("capacity_pending", "hold", True, False),
        ("transient_failure", "hold", False, False),
        ("permanent_failure", "ack", False, False),
    ),
)
def test_coordinator_has_one_durable_outcome_ack_policy(
    code: str,
    disposition: str,
    durable: bool,
    expected_ack: bool,
) -> None:
    contracts, coordinator, _state, _admission, _consumer_module = _activate()
    fake = FakeRedis()
    consumer = _consumer(fake)
    delivery = _delivery(contracts)
    outcome = contracts.ProcessingOutcome(
        code=contracts.ProcessingCode(code),
        ack_disposition=contracts.AckDisposition(disposition),
        durable=durable,
        event_id="event-1",
    )
    instance = coordinator.ClipCoordinator(consumer, lambda _delivery: outcome)

    result = instance.process_one(delivery)

    assert result.ack_performed is expected_ack
    assert fake.acked == (["1-0"] if expected_ack else [])


def test_unexpected_processor_exception_is_never_acked() -> None:
    contracts, coordinator, _state, _admission, _consumer_module = _activate()
    fake = FakeRedis()
    consumer = _consumer(fake)

    def fail(_delivery):
        raise RuntimeError("unexpected")

    result = coordinator.ClipCoordinator(consumer, fail).process_one(
        _delivery(contracts)
    )

    assert result.code is contracts.ProcessingCode.UNEXPECTED_FAILURE
    assert result.ack_disposition is contracts.AckDisposition.HOLD
    assert result.durable is False
    assert fake.acked == []


class CrashAt:
    def __init__(self, coordinator, point) -> None:
        self.coordinator = coordinator
        self.point = point

    def hit(self, point, **_kwargs: Any) -> None:
        if point is self.point:
            raise self.coordinator.InjectedCoordinatorCrash(point.value)


@pytest.mark.parametrize("point", ("before_ack", "after_ack"))
def test_ack_crash_points_preserve_expected_pending_state(point: str) -> None:
    contracts, coordinator, _state, _admission, _consumer_module = _activate()
    fake = FakeRedis()
    consumer = _consumer(fake)
    outcome = contracts.ProcessingOutcome(
        code=contracts.ProcessingCode.REPLAY_CREATED,
        ack_disposition=contracts.AckDisposition.ACK,
        durable=True,
        replay_job_id="job-1",
    )
    crash_point = contracts.CrashPoint(point)
    instance = coordinator.ClipCoordinator(
        consumer,
        lambda _delivery: outcome,
        crash_injector=CrashAt(coordinator, crash_point),
    )

    with pytest.raises(coordinator.InjectedCoordinatorCrash):
        instance.process_one(_delivery(contracts))

    assert fake.acked == ([] if point == "before_ack" else ["1-0"])


def test_coordinator_tick_reclaims_before_reading_new() -> None:
    contracts, coordinator, _state, _admission, _consumer_module = _activate()
    fake = FakeRedis()
    fake.pending_counts = {"9-0": 2}
    fake.pending_entries = [
        (b"9-0", {b"data": json.dumps({"event_id": "event-old"}).encode()})
    ]
    fake.new_entries = [
        (b"1-0", {b"data": json.dumps({"event_id": "event-new"}).encode()})
    ]
    consumer = _consumer(fake)
    seen: list[str] = []

    def process(delivery):
        seen.append(delivery.message_id)
        return contracts.ProcessingOutcome(
            code=contracts.ProcessingCode.RETRY_PENDING,
            ack_disposition=contracts.AckDisposition.HOLD,
            durable=True,
        )

    instance = coordinator.ClipCoordinator(consumer, process)
    instance.tick(reclaim=True)

    assert seen == ["9-0"]
    assert fake.new_entries


def test_repository_ports_delegate_only_named_operations(monkeypatch) -> None:
    _contracts, _coordinator, state_module, admission_module, _consumer_module = (
        _activate()
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        admission_module.repository,
        "try_acquire_replay_slot",
        lambda _conn, **kwargs: calls.append(("acquire", kwargs))
        or {
            "acquired": True,
            "reason": "",
            "counts": {"replay_active_global_count": 0},
            "quota_decision": {},
        },
    )
    monkeypatch.setattr(
        admission_module.repository,
        "record_replay_job_for_slot",
        lambda _conn, **kwargs: calls.append(("record", kwargs)) or True,
    )
    monkeypatch.setattr(
        admission_module.repository,
        "release_replay_slot",
        lambda _conn, **kwargs: calls.append(("release", kwargs)) or True,
    )
    admission = admission_module.ReplayAdmissionRepository(object())
    reservation = admission.acquire(event_id="event-1")
    assert reservation and reservation.acquired
    assert admission.record_job(
        reservation,
        replay_job_id="job-1",
        resulting_stream_id="result-1",
    )
    assert admission.release(reservation, reason="test")

    monkeypatch.setattr(
        state_module.repository,
        "update_clip_status",
        lambda _conn, event_id, status, **kwargs: calls.append(
            (status, {"event_id": event_id, **kwargs})
        )
        or True,
    )
    state = state_module.EvidenceStateRepository(object())
    assert state.mark_pending("event-1", reason="wait")
    assert state.mark_failed("event-1", reason="bad")
    assert state.mark_replay_created("event-1", replay_job_id="job-1")

    assert [name for name, _kwargs in calls] == [
        "acquire",
        "record",
        "release",
        "pending",
        "failed",
        "replay_job_created",
    ]


def test_coordinator_has_no_transport_sql_or_replay_side_effect_calls() -> None:
    path = CLIP_ROOT / "app" / "coordinator.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "xack" not in calls
    assert "xreadgroup" not in calls
    assert "create_job" not in calls
    assert "cursor" not in calls
    assert "SELECT " not in source
    assert "UPDATE " not in source


def test_v2_flag_defaults_off_and_fails_closed_until_processor_is_installed() -> None:
    helpers_path = ROOT / "harness" / "tests" / "test_clip_worker_queue_safety.py"
    spec = __import__("importlib.util").util.spec_from_file_location(
        "phase3_clip_helpers",
        helpers_path,
    )
    assert spec and spec.loader
    helpers = __import__("importlib.util").util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    helpers._activate()
    import app.worker as worker

    config = helpers._clip_config(
        coordinator_v2_enabled=True,
        pending_claim_count=0,
    )
    with pytest.raises(RuntimeError, match="requires the Phase 3 processor"):
        worker.run_worker(config, object(), object())
