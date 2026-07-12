"""One-message characterization tests for Clip Coordinator V2."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "harness/tests/test_clip_worker_queue_safety.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_helpers():
    spec = importlib.util.spec_from_file_location("clip_v2_helpers", HELPERS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeState:
    def __init__(self) -> None:
        self.terminal = ""
        self.target = True
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def terminal_state(self, _event_id: str) -> str:
        return self.terminal

    def target_exists(self, **_kwargs: Any) -> bool:
        return self.target

    def diagnostics(self, _event_id: str) -> dict[str, object]:
        return {}

    def mark_pending(self, event_id: str, **kwargs: Any) -> bool:
        self.calls.append(("pending", {"event_id": event_id, **kwargs}))
        return True

    def mark_failed(self, event_id: str, **kwargs: Any) -> bool:
        self.calls.append(("failed", {"event_id": event_id, **kwargs}))
        return True

    def mark_terminal_deferred(self, event_id: str, **kwargs: Any) -> bool:
        self.calls.append(("deferred", {"event_id": event_id, **kwargs}))
        return True

    def mark_skipped(self, event_id: str, **kwargs: Any) -> bool:
        self.calls.append(("skipped", {"event_id": event_id, **kwargs}))
        return True


class FakeAdmission:
    def __init__(self, reservation_type) -> None:
        self.reservation_type = reservation_type
        self.state = None
        self.commits: list[dict[str, Any]] = []

    def load(self, _event_id: str):
        return self.state

    def active_counts(self, **_kwargs: Any) -> dict[str, int]:
        return {
            "replay_active_global_count": 0,
            "replay_active_shard_count": 0,
            "replay_active_source_count": 0,
        }

    def acquire_fenced(self, **kwargs: Any):
        self.state = self.reservation_type(
            event_id=str(kwargs["event_id"]),
            acquired=True,
            reason="",
            counts=(),
            quota_decision=(),
            owner=str(kwargs["owner"]),
            token=str(kwargs["slot_token"]),
            generation=1,
            create_state="reserved",
            plan_hash=str(kwargs["plan_hash"]),
            slot_status="active",
        )
        return self.state

    def takeover(self, reservation, *, owner: str, delivery_id: str):
        del delivery_id
        self.state = replace(
            reservation,
            owner=owner,
            generation=reservation.generation + 1,
        )
        return self.state

    def mark_submitting(
        self,
        reservation,
        *,
        replay_job_request: dict | None = None,
    ) -> bool:
        assert replay_job_request
        self.state = replace(reservation, create_state="submitting")
        return True

    def mark_uncertain(self, reservation, *, reason: str) -> bool:
        del reason
        self.state = replace(reservation, create_state="uncertain")
        return True

    def abort_permanent(
        self,
        reservation,
        *,
        reason: str,
        diagnostics: dict | None = None,
    ) -> bool:
        del reason, diagnostics
        self.state = replace(
            reservation,
            create_state="aborted",
            slot_status="released",
        )
        return True

    def commit_handoff(self, reservation, **kwargs: Any) -> bool:
        self.commits.append(kwargs)
        self.state = replace(
            reservation,
            create_state="committed",
            replay_job_id=str(kwargs["replay_job_id"]),
            resulting_stream_id=str(kwargs["resulting_stream_id"]),
        )
        return True


def _activate(monkeypatch):
    helpers = _load_helpers()
    helpers._activate()
    import app.contracts as contracts
    import app.coordinator as coordinator
    import app.replay_admission_repository as admission_module
    import app.request_consumer as consumer_module
    import app.request_processor as processor_module
    import app.worker as worker

    class FakeReplay:
        instances: list["FakeReplay"] = []

        def __init__(self, url: str) -> None:
            self.url = url
            self.plans = []
            FakeReplay.instances.append(self)

        def find_keyframe(self, *_args: Any, **_kwargs: Any) -> str:
            return "lookup-keyframe"

        def submit_plan(self, plan):
            self.plans.append(plan)
            payload = plan.payload()
            return contracts.ReplaySubmission(
                code=contracts.ReplaySubmissionCode.CREATED,
                job_id="job-v2-1",
                resulting_stream_id=(
                    payload["configuration"]["resulting_stream_id"]
                ),
                request_json=plan.canonical_payload_json,
            )

        def recover_submission(self, **_kwargs: Any):
            return None

    monkeypatch.setattr(worker, "ReplayClient", FakeReplay)
    return (
        helpers,
        contracts,
        coordinator,
        admission_module,
        consumer_module,
        processor_module,
        worker,
        FakeReplay,
    )


def _delivery(contracts, request: dict[str, Any]):
    return contracts.DeliveryEnvelope(
        stream="security.record_requests",
        group="clip-workers-test",
        consumer="clip-v2-1",
        message_id="1780000000000-0",
        fields=((b"data", json.dumps(request).encode()),),
    )


def test_direct_keyframe_request_commits_then_coordinator_acks_once(monkeypatch) -> None:
    (
        helpers,
        contracts,
        coordinator,
        admission_module,
        consumer_module,
        processor_module,
        worker,
        FakeReplay,
    ) = _activate(monkeypatch)
    redis_client = helpers._FakeRedis([])
    consumer = consumer_module.RequestConsumer(
        redis_client,
        consumer_module.ConsumerSettings(
            stream="security.record_requests",
            group="clip-workers-test",
            consumer="clip-v2-1",
            poll_timeout_ms=0,
            pending_claim_count=0,
        ),
    )
    state = FakeState()
    admission = FakeAdmission(admission_module.ReplaySlotReservation)
    processor = processor_module.ClipRequestProcessorV2(
        cfg=helpers._clip_config(
            coordinator_v2_enabled=True,
            pending_claim_count=0,
        ),
        redis_client=redis_client,
        consumer=consumer,
        evidence_state=state,
        replay_admission=admission,
        legacy=worker,
    )
    request = helpers._request("401")

    outcome = coordinator.ClipCoordinator(
        consumer,
        processor,
    ).process_one(_delivery(contracts, request))

    assert outcome.code is contracts.ProcessingCode.REPLAY_CREATED
    assert outcome.durable is True
    assert outcome.ack_performed is True
    assert redis_client.acked == ["1780000000000-0"]
    assert len(admission.commits) == 1
    assert len(FakeReplay.instances) == 1
    labels = FakeReplay.instances[0].plans[0].payload()["configuration"]["labels"]
    assert labels["replay_slot_token"] == admission.state.token
    assert state.calls == []


def test_capacity_pending_is_persisted_but_never_acked(monkeypatch) -> None:
    (
        helpers,
        contracts,
        coordinator,
        admission_module,
        consumer_module,
        processor_module,
        worker,
        _FakeReplay,
    ) = _activate(monkeypatch)
    redis_client = helpers._FakeRedis([])
    consumer = consumer_module.RequestConsumer(
        redis_client,
        consumer_module.ConsumerSettings(
            stream="security.record_requests",
            group="clip-workers-test",
            consumer="clip-v2-1",
            poll_timeout_ms=0,
            pending_claim_count=0,
        ),
    )
    state = FakeState()
    admission = FakeAdmission(admission_module.ReplaySlotReservation)
    admission.active_counts = lambda **_kwargs: {
        "replay_active_global_count": 1,
        "replay_active_shard_count": 1,
        "replay_active_source_count": 1,
    }
    processor = processor_module.ClipRequestProcessorV2(
        cfg=helpers._clip_config(
            coordinator_v2_enabled=True,
            pending_claim_count=0,
            evidence_materialization_max_concurrency=1,
        ),
        redis_client=redis_client,
        consumer=consumer,
        evidence_state=state,
        replay_admission=admission,
        legacy=worker,
    )

    outcome = coordinator.ClipCoordinator(
        consumer,
        processor,
    ).process_one(_delivery(contracts, helpers._request("402")))

    assert outcome.code is contracts.ProcessingCode.CAPACITY_PENDING
    assert outcome.durable is True
    assert outcome.ack_performed is False
    assert redis_client.acked == []
    assert state.calls[0][0] == "pending"


def test_reclaimed_active_slot_bypasses_its_own_capacity_count(monkeypatch) -> None:
    (
        helpers,
        contracts,
        coordinator,
        admission_module,
        consumer_module,
        processor_module,
        worker,
        FakeReplay,
    ) = _activate(monkeypatch)
    redis_client = helpers._FakeRedis([])
    consumer = consumer_module.RequestConsumer(
        redis_client,
        consumer_module.ConsumerSettings(
            stream="security.record_requests",
            group="clip-workers-test",
            consumer="clip-v2-2",
            poll_timeout_ms=0,
            pending_claim_count=0,
        ),
    )
    state = FakeState()
    admission = FakeAdmission(admission_module.ReplaySlotReservation)
    request = helpers._request("402-reclaim")
    token = processor_module.logical_replay_slot_token(request["event_id"])
    admission.state = admission_module.ReplaySlotReservation(
        event_id=request["event_id"],
        acquired=True,
        reason="",
        counts=(),
        quota_decision=(),
        owner="clip-v2-1",
        token=token,
        generation=1,
        create_state="submitting",
        plan_hash="",
        slot_status="active",
    )
    admission.active_counts = lambda **_kwargs: {
        "replay_active_global_count": 1,
        "replay_active_shard_count": 1,
        "replay_active_source_count": 1,
    }

    def recover(self, *, slot_token: str, resulting_stream_id: str):
        assert slot_token == token
        return contracts.ReplaySubmission(
            code=contracts.ReplaySubmissionCode.CREATED,
            job_id="job-recovered",
            resulting_stream_id=resulting_stream_id,
            reason="recovered_active_replay_job",
        )

    monkeypatch.setattr(FakeReplay, "recover_submission", recover)
    processor = processor_module.ClipRequestProcessorV2(
        cfg=helpers._clip_config(
            coordinator_v2_enabled=True,
            pending_claim_count=0,
            evidence_materialization_max_concurrency=1,
            evidence_materialization_max_concurrency_per_source=1,
        ),
        redis_client=redis_client,
        consumer=consumer,
        evidence_state=state,
        replay_admission=admission,
        legacy=worker,
    )
    delivery = replace(
        _delivery(contracts, request),
        consumer="clip-v2-2",
        delivery_count=2,
        reclaimed=True,
    )

    outcome = coordinator.ClipCoordinator(
        consumer,
        processor,
    ).process_one(delivery)

    assert outcome.code is contracts.ProcessingCode.REPLAY_CREATED
    assert outcome.ack_performed is True
    assert outcome.replay_job_id == "job-recovered"
    assert len(admission.commits) == 1
    assert state.calls == []


def test_post_savant_frame_domain_contract_is_preserved_in_v2(monkeypatch) -> None:
    (
        helpers,
        contracts,
        coordinator,
        admission_module,
        consumer_module,
        processor_module,
        worker,
        FakeReplay,
    ) = _activate(monkeypatch)
    request = {
        **helpers._request("403"),
        "replay_source_kind": "post_savant",
        "event_frame_pts": 10_000_000_000,
        "requested_start_pts": 5_000_000_000,
        "requested_end_pts": 15_000_000_000,
        "stream_session_id": "session-1",
        "runtime_epoch_id": "epoch-1",
        "keyframe_uuid": "start-keyframe",
        "keyframe_pts": 5_000_000_000,
    }
    redis_client = helpers._FakeRedis(
        [],
        frame_annotations=[
            helpers._frame_annotation(
                frame_uuid="start-keyframe",
                frame_pts=5_000_000_000,
                stream_id="1779999995000-0",
                stream_session_id="session-1",
                keyframe_uuid="start-keyframe",
                keyframe_pts=5_000_000_000,
            ),
            helpers._frame_annotation(
                frame_uuid="post-window-frame",
                frame_pts=15_000_000_000,
                stream_id="1780000005000-0",
                stream_session_id="session-2",
                keyframe_uuid="post-session-keyframe",
                keyframe_pts=14_900_000_000,
            ),
        ],
    )
    consumer = consumer_module.RequestConsumer(
        redis_client,
        consumer_module.ConsumerSettings(
            stream="security.record_requests",
            group="clip-workers-test",
            consumer="clip-v2-1",
            poll_timeout_ms=0,
            pending_claim_count=0,
        ),
    )
    admission = FakeAdmission(admission_module.ReplaySlotReservation)
    processor = processor_module.ClipRequestProcessorV2(
        cfg=helpers._clip_config(
            coordinator_v2_enabled=True,
            pending_claim_count=0,
            post_savant_frame_proof_wait_budget_s=0.0,
            post_savant_frame_proof_poll_interval_s=0.0,
            post_savant_allow_cross_session_post_window_proof=True,
        ),
        redis_client=redis_client,
        consumer=consumer,
        evidence_state=FakeState(),
        replay_admission=admission,
        legacy=worker,
    )

    outcome = coordinator.ClipCoordinator(
        consumer,
        processor,
    ).process_one(_delivery(contracts, request))

    assert outcome.durable is True
    assert outcome.ack_performed is True
    labels = FakeReplay.instances[0].plans[0].payload()["configuration"]["labels"]
    assert labels["stream_session_id"] == "session-1"
    assert labels["start_window_stream_session_id"] == "session-1"
    assert labels["post_window_stream_session_id"] == "session-2"
    assert labels["post_window_cross_session_proof_used"] == "true"
    assert labels["frame_domain_session_policy"] == (
        "post_window_cross_session_pts_verified"
    )
    assert labels["replay_slot_token"] == admission.state.token


def test_v2_composition_loop_reads_processes_and_acks_one_delivery(monkeypatch) -> None:
    (
        helpers,
        _contracts,
        _coordinator,
        admission_module,
        _consumer_module,
        _processor_module,
        worker,
        FakeReplay,
    ) = _activate(monkeypatch)
    redis_client = helpers._FakeRedis([helpers._request("404")])
    state = FakeState()
    admission = FakeAdmission(admission_module.ReplaySlotReservation)
    monkeypatch.setattr(worker, "EvidenceStateRepository", lambda _conn: state)
    monkeypatch.setattr(worker, "ReplayAdmissionRepository", lambda _conn: admission)
    monkeypatch.setattr(worker, "expire_materialization_deadlines", lambda _conn: 0)
    worker.shutdown_requested = False

    worker.run_worker(
        helpers._clip_config(
            coordinator_v2_enabled=True,
            pending_claim_count=0,
            run_once=True,
        ),
        redis_client,
        object(),
    )

    assert redis_client.acked == ["1-0"]
    assert len(admission.commits) == 1
    assert len(FakeReplay.instances) == 1
