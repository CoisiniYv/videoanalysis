"""Crash/reclaim contracts for the fenced Replay executor."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLIP_ROOT = ROOT / "services" / "clip-worker"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _activate():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    path = str(CLIP_ROOT)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    from app import contracts, replay_admission_repository, replay_executor
    from app.replay_planner import build_replay_plan

    return contracts, replay_admission_repository, replay_executor, build_replay_plan


class FakeAdmission:
    def __init__(self, reservation_type) -> None:
        self.reservation_type = reservation_type
        self.state = None
        self.commits: list[dict[str, Any]] = []
        self.takeovers = 0
        self.uncertain = 0
        self.aborted = 0

    def load(self, _event_id: str):
        return self.state

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
        self.takeovers += 1
        self.state = replace(
            reservation,
            acquired=True,
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
        assert self.state == reservation
        self.state = replace(reservation, create_state="submitting")
        return True

    def mark_uncertain(self, reservation, *, reason: str) -> bool:
        del reason
        assert self.state == reservation
        self.uncertain += 1
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
        assert self.state == reservation
        self.aborted += 1
        self.state = replace(
            reservation,
            create_state="aborted",
            slot_status="released",
        )
        return True

    def commit_handoff(self, reservation, **kwargs: Any) -> bool:
        assert self.state == reservation
        self.commits.append(kwargs)
        self.state = replace(
            reservation,
            create_state="committed",
            replay_job_id=str(kwargs["replay_job_id"]),
            resulting_stream_id=str(kwargs["resulting_stream_id"]),
        )
        return True


class FakeTransport:
    def __init__(self, contracts) -> None:
        self.contracts = contracts
        self.submits = 0
        self.jobs: dict[str, object] = {}

    def submit_plan(self, plan):
        self.submits += 1
        payload = plan.payload()
        token = payload["configuration"]["labels"]["replay_slot_token"]
        submission = self.contracts.ReplaySubmission(
            code=self.contracts.ReplaySubmissionCode.CREATED,
            job_id=f"job-{self.submits}",
            resulting_stream_id=payload["configuration"]["resulting_stream_id"],
            request_json=plan.canonical_payload_json,
        )
        self.jobs[token] = submission
        return submission

    def recover_submission(self, *, slot_token: str, resulting_stream_id: str):
        del resulting_stream_id
        submission = self.jobs.get(slot_token)
        if submission is None:
            return None
        return replace(submission, reason="recovered_active_replay_job")


class CrashAt:
    def __init__(self, point) -> None:
        self.point = point
        self.triggered = False

    def hit(self, point, **_kwargs: Any) -> None:
        if point is self.point and not self.triggered:
            self.triggered = True
            raise RuntimeError(f"injected:{point.value}")


def _request(*, reclaimed: bool = False, owner: str = "clip-a"):
    contracts, reservation_module, executor_module, build_replay_plan = _activate()
    token = "slot-token-1"
    labels = {
        "event_id": "00000000-0000-4000-8000-000000000401",
        "request_id": "request-1",
        "replay_slot_token": token,
    }
    plan = build_replay_plan(
        source_id="source-1",
        keyframe_uuid="00000000-0000-7000-8000-000000000001",
        pre_seconds=5,
        post_seconds=5,
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels=labels,
        stop_condition_mode="ts_delta_sec",
        fallback_reason=None,
        fps=24,
        force_constant_cadence=True,
        offset_seconds_override=5.0,
        duration_seconds_override=10.0,
        ts_sync=False,
    )
    delivery = contracts.DeliveryEnvelope(
        stream="security.record_requests",
        group="clip-workers-midterm",
        consumer=owner,
        message_id="1-0",
        fields=(),
        delivery_count=2 if reclaimed else 1,
        reclaimed=reclaimed,
    )
    request = executor_module.FencedReplayRequest(
        delivery=delivery,
        event_id=labels["event_id"],
        request_id="request-1",
        owner=owner,
        slot_token=token,
        source_id="source-1",
        camera_id="camera-1",
        replay_shard={
            "shard_id": "replay-a",
            "replay_api_url": "http://replay-service:8080",
            "replay_job_sink_url": (
                "dealer+connect:tcp://video-file-sink:6666"
            ),
        },
        sink_instance="video-file-sink",
        plan=plan,
        timing=contracts.ReplaySlotTiming(10.0, "window", 120.0),
        max_global=8,
        max_per_shard=4,
        max_per_source=1,
    )
    admission = FakeAdmission(reservation_module.ReplaySlotReservation)
    transport = FakeTransport(contracts)
    return contracts, executor_module, request, admission, transport


def test_fresh_execution_commits_before_requesting_ack() -> None:
    contracts, executor_module, request, admission, transport = _request()
    outcome = executor_module.FencedReplayExecutor(
        admission,
        transport,
    ).execute(request)

    assert outcome.code is contracts.ProcessingCode.REPLAY_CREATED
    assert outcome.ack_disposition is contracts.AckDisposition.ACK
    assert outcome.durable is True
    assert transport.submits == 1
    assert len(admission.commits) == 1
    assert admission.state.create_state == "committed"


@pytest.mark.parametrize(
    "point",
    (
        "before_replay_create",
        "after_replay_response",
        "before_durable_commit",
        "after_durable_commit",
    ),
)
def test_crash_then_reclaim_converges_to_one_job(point: str) -> None:
    contracts, executor_module, request, admission, transport = _request()
    crash = CrashAt(contracts.CrashPoint(point))
    first = executor_module.FencedReplayExecutor(
        admission,
        transport,
        crash_injector=crash,
    )

    with pytest.raises(RuntimeError, match="injected"):
        first.execute(request)

    reclaimed = replace(
        request,
        owner="clip-b",
        delivery=replace(
            request.delivery,
            consumer="clip-b",
            delivery_count=2,
            reclaimed=True,
        ),
    )
    outcome = executor_module.FencedReplayExecutor(
        admission,
        transport,
    ).execute(reclaimed)

    assert outcome.code is contracts.ProcessingCode.REPLAY_CREATED
    assert outcome.ack_disposition is contracts.AckDisposition.ACK
    assert outcome.durable is True
    assert transport.submits == 1
    assert len(admission.commits) == 1
    if point == "after_durable_commit":
        assert admission.takeovers == 0
    else:
        assert admission.takeovers == 1


def test_unresolved_ambiguous_response_is_held_without_second_submit() -> None:
    contracts, executor_module, request, admission, transport = _request(
        reclaimed=True,
        owner="clip-b",
    )
    admission.state = admission.reservation_type(
        event_id=request.event_id,
        acquired=True,
        reason="",
        counts=(),
        quota_decision=(),
        owner="clip-a",
        token=request.slot_token,
        generation=1,
        create_state="submitting",
        plan_hash=request.plan.plan_hash,
        slot_status="active",
    )

    outcome = executor_module.FencedReplayExecutor(
        admission,
        transport,
    ).execute(request)

    assert outcome.ack_disposition is contracts.AckDisposition.HOLD
    assert outcome.reason == "replay_response_unresolved"
    assert transport.submits == 0
    assert admission.uncertain == 1
    assert admission.takeovers == 1


def test_permanent_replay_rejection_is_terminal_and_ackable() -> None:
    contracts, executor_module, request, admission, transport = _request()

    def reject(_plan):
        transport.submits += 1
        return contracts.ReplaySubmission(
            code=contracts.ReplaySubmissionCode.PERMANENT_REJECTED,
            reason="Replay HTTP status 400",
        )

    transport.submit_plan = reject
    outcome = executor_module.FencedReplayExecutor(
        admission,
        transport,
    ).execute(request)

    assert outcome.code is contracts.ProcessingCode.PERMANENT_FAILURE
    assert outcome.ack_disposition is contracts.AckDisposition.ACK
    assert outcome.durable is True
    assert transport.submits == 1
    assert admission.aborted == 1
    assert admission.uncertain == 0
    assert admission.state.create_state == "aborted"
    assert admission.state.slot_status == "released"


def test_slot_token_is_part_of_the_canonical_replay_plan() -> None:
    _contracts, _executor, request, _admission, _transport = _request()
    payload = request.plan.payload()
    assert payload["configuration"]["labels"]["replay_slot_token"] == (
        request.slot_token
    )
    assert request.plan.plan_hash


def test_replay_client_recovers_active_job_by_slot_token(monkeypatch) -> None:
    contracts, _executor, request, _admission, _transport = _request()
    from app import replay_client

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "jobs": [
                    {
                        "job_id": "job-recovered",
                        "configuration": {
                            "resulting_stream_id": request.resulting_stream_id,
                            "labels": {
                                "replay_slot_token": request.slot_token,
                            },
                        },
                    }
                ]
            }

    monkeypatch.setattr(replay_client.httpx, "get", lambda *_a, **_k: Response())
    recovered = replay_client.ReplayClient(
        "http://replay-service:8080"
    ).recover_submission(
        slot_token=request.slot_token,
        resulting_stream_id=request.resulting_stream_id,
    )

    assert recovered is not None
    assert recovered.code is contracts.ReplaySubmissionCode.CREATED
    assert recovered.job_id == "job-recovered"
    assert recovered.reason == "recovered_active_replay_job"


def test_replay_client_marks_transport_exception_uncertain(monkeypatch) -> None:
    contracts, _executor, request, _admission, _transport = _request()
    from app import replay_client

    client = replay_client.ReplayClient("http://replay-service:8080")

    def fail(_payload):
        raise TimeoutError("response lost")

    monkeypatch.setattr(client, "_submit_job_payload", fail)
    submission = client.submit_plan(request.plan)

    assert submission.code is contracts.ReplaySubmissionCode.UNCERTAIN
    assert submission.job_id == ""
    assert "response lost" in submission.reason
