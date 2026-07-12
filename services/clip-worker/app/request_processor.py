"""One-message business processor for Clip Coordinator V2."""

from __future__ import annotations

from collections import defaultdict
import logging
import time
from typing import Any, Mapping

from app.config import Config
from app.contracts import (
    AckDisposition,
    ActiveReplayJob,
    DeliveryEnvelope,
    ProcessingCode,
    ProcessingOutcome,
    ReadyProof,
)
from app.evidence_state_repository import EvidenceStateRepository
from app.legacy_observability import request_correlation
from app.proof_resolver import BoundedProofResolver
from app.replay_admission_repository import ReplayAdmissionRepository
from app.replay_client import ReplayClient, build_job_payload
from app.replay_executor import (
    FencedReplayExecutor,
    FencedReplayRequest,
    NoopCrashInjector,
)
from app.replay_planner import (
    assert_replay_plan_parity,
    build_replay_plan,
    logical_replay_slot_token,
    normalize_record_request,
    request_identity,
)
from app.replay_shards import ReplayShardConfigError
from app.request_consumer import RequestConsumer


logger = logging.getLogger(__name__)


class ClipRequestProcessorV2:
    """Run exactly one record request and return one explicit outcome."""

    def __init__(
        self,
        *,
        cfg: Config,
        redis_client: Any,
        consumer: RequestConsumer,
        evidence_state: EvidenceStateRepository,
        replay_admission: ReplayAdmissionRepository,
        legacy: Any,
        crash_injector: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.redis_client = redis_client
        self.consumer = consumer
        self.evidence_state = evidence_state
        self.replay_admission = replay_admission
        self.legacy = legacy
        self.crash_injector = crash_injector or NoopCrashInjector()
        self.replay_clients: dict[str, ReplayClient] = {}
        self.jobs_created = 0
        self.active_jobs: list[ActiveReplayJob] = []
        self.event_type_counts: dict[str, int] = defaultdict(int)
        self.last_job_by_camera: dict[str, int] = defaultdict(int)
        self.proof_resolver = BoundedProofResolver(
            legacy._prepare_post_savant_replay_request,
            max_concurrent=cfg.frame_annotation_lookup_concurrency,
        )

    def __call__(self, delivery: DeliveryEnvelope) -> ProcessingOutcome:
        req = self.consumer.parse_json(delivery)
        if req is None:
            return ProcessingOutcome(
                code=ProcessingCode.MALFORMED,
                ack_disposition=AckDisposition.ACK,
                durable=False,
                reason="record_request_json_invalid",
                diagnostics=(("delivery_id", delivery.message_id),),
            )

        normalized = normalize_record_request(
            req,
            default_pre_seconds=self.cfg.default_pre_seconds,
            default_post_seconds=self.cfg.default_post_seconds,
        )
        event_id = normalized.event_id
        request_id = request_identity(req)
        source_id = normalized.source_id
        source_event_id = normalized.source_event_id
        camera_id = normalized.camera_id
        event_type = normalized.event_type
        pre_seconds = normalized.pre_seconds
        post_seconds = normalized.post_seconds
        event_ts_ms = normalized.event_ts_ms
        retry_count = delivery.retry_count
        pending_ms = self.legacy._message_age_ms(delivery.message_id)
        claimed_at = self.legacy._utc_now_iso()
        phase_diagnostics: dict[str, object] = {
            "record_request_stream_id": delivery.message_id,
            "record_request_pending_ms": pending_ms,
            "clip_worker_claimed_at": claimed_at,
            "redis_delivery_retry_count": retry_count,
            "record_request_batch_size": 1,
            "coordinator_version": "v2",
        }
        phase_diagnostics["correlation"] = request_correlation(
            req,
            event_id=event_id,
            request_id=request_id,
            delivery_id=delivery.message_id,
            retry_count=retry_count,
            consumer=delivery.consumer,
        )

        if not event_id:
            return ProcessingOutcome(
                code=ProcessingCode.MALFORMED,
                ack_disposition=AckDisposition.ACK,
                durable=False,
                request_id=request_id,
                reason="record_request_missing_event_id",
                diagnostics=tuple(sorted(phase_diagnostics.items())),
            )

        terminal_state = self.evidence_state.terminal_state(event_id)
        if terminal_state:
            return self._outcome(
                ProcessingCode.DUPLICATE_TERMINAL,
                event_id,
                request_id,
                reason=f"terminal_state:{terminal_state}",
                ack=True,
                durable=True,
            )
        target_exists = self.evidence_state.target_exists(
            event_id=event_id,
            source_event_id=source_event_id,
        )
        if target_exists is False:
            return self._outcome(
                ProcessingCode.STALE_TARGET,
                event_id,
                request_id,
                reason="missing_db_event_and_evidence_task",
                ack=True,
                durable=True,
            )

        existing_slot = self.replay_admission.load(event_id)
        if existing_slot is not None and existing_slot.slot_status == "timeout":
            persisted = self.evidence_state.mark_failed(
                event_id,
                error_message="replay_slot_timeout",
                evidence_state="materialization_failed",
                evidence_reason="replay_slot_timeout",
                request_id=request_id,
                diagnostics=phase_diagnostics,
            )
            return self._persisted_terminal(
                event_id,
                request_id,
                reason="replay_slot_timeout",
                persisted=persisted,
            )
        if existing_slot is not None and existing_slot.slot_status == "released":
            return self._outcome(
                ProcessingCode.RETRY_PENDING,
                event_id,
                request_id,
                reason="replay_slot_released_waiting_terminal_projection",
                durable=True,
            )
        if (
            existing_slot is not None
            and existing_slot.slot_status == "active"
            and existing_slot.create_state == "committed"
            and existing_slot.replay_job_id
        ):
            return ProcessingOutcome(
                code=ProcessingCode.REPLAY_CREATED,
                ack_disposition=AckDisposition.ACK,
                durable=True,
                event_id=event_id,
                request_id=request_id,
                reason="replay_handoff_already_committed",
                replay_job_id=existing_slot.replay_job_id,
                slot_token=existing_slot.token,
            )
        recovering_existing_slot = bool(
            existing_slot is not None
            and existing_slot.slot_status == "active"
        )

        try:
            replay_route = self.legacy._resolve_replay_route(
                self.cfg,
                source_id=source_id,
                replay_clients=self.replay_clients,
            )
        except ReplayShardConfigError as exc:
            diagnostics = {
                **phase_diagnostics,
                "source_id": source_id,
                "replay_shard_error": str(exc),
                "replay_shards": self.cfg.replay_shards.to_dict(),
            }
            persisted = self.evidence_state.mark_failed(
                event_id,
                error_message=f"replay shard routing failed: {exc}",
                evidence_state="failed",
                evidence_reason="replay_shard_routing_failed",
                request_id=request_id,
                diagnostics=diagnostics,
            )
            return self._persisted_terminal(
                event_id,
                request_id,
                reason="replay_shard_routing_failed",
                persisted=persisted,
            )

        replay = replay_route.client
        replay_shard = replay_route.diagnostics()
        record_request_shard_id = str(
            req.get("record_request_shard_id")
            or req.get("replay_shard_id")
            or ""
        ).strip()
        phase_diagnostics.update(
            {
                "replay_shard": replay_shard,
                "record_request_shard_id": record_request_shard_id,
                "consumer_resolved_shard_id": replay_route.shard.shard_id,
                "shard_mapping_version": replay_route.mapping_version,
                "record_request_shard_mapping_version": str(
                    req.get("shard_mapping_version")
                    or req.get("replay_shard_mapping_version")
                    or ""
                ).strip(),
            }
        )

        now = time.monotonic()
        self.active_jobs = [
            job for job in self.active_jobs if job.until_monotonic > now
        ]
        replay_counts = self.replay_admission.active_counts(
            shard_id=replay_route.shard.shard_id,
            source_id=source_id,
        )
        if replay_counts is None:
            replay_counts = self.legacy._active_replay_counts(
                self.active_jobs,
                shard_id=replay_route.shard.shard_id,
                source_id=source_id,
            )
            phase_diagnostics["replay_slot_count_source"] = "local_fallback"
        else:
            phase_diagnostics["replay_slot_count_source"] = "postgres"
        phase_diagnostics.update(replay_counts)

        cooldown_gate_ts_ms = self.legacy._cooldown_gate_ts_ms(req)
        if recovering_existing_slot:
            phase_diagnostics[
                "replay_slot_recovery_bypassed_schedule_gate"
            ] = True
        else:
            schedule_gate = self.legacy._clip_gate_decision(
                self.cfg,
                jobs_created=self.jobs_created,
                active_jobs=self.active_jobs,
                active_counts=replay_counts,
                shard_id=replay_route.shard.shard_id,
                source_id=source_id,
                camera_id=camera_id,
                cooldown_gate_ts_ms=cooldown_gate_ts_ms,
                last_job_by_camera=self.last_job_by_camera,
                event_type_counts=self.event_type_counts,
                event_type=event_type,
            )
            if not schedule_gate.allowed:
                return self._schedule_denied(
                    schedule_gate,
                    event_id=event_id,
                    request_id=request_id,
                    retry_count=retry_count,
                    replay_shard=replay_shard,
                    diagnostics=phase_diagnostics,
                )

        anchor = self._resolve_anchor(
            delivery=delivery,
            req=req,
            replay=replay,
            replay_shard=replay_shard,
            event_id=event_id,
            request_id=request_id,
            source_id=source_id,
            source_event_id=source_event_id,
            camera_id=camera_id,
            event_ts_ms=event_ts_ms,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
            keyframe_uuid=normalized.keyframe_uuid,
            keyframe_source=normalized.keyframe_source,
            post_savant_media_request=normalized.post_savant_media_request,
            phase_diagnostics=phase_diagnostics,
        )
        if isinstance(anchor, ProcessingOutcome):
            return anchor
        replay_anchor_req, keyframe_uuid, proof_diagnostics = anchor

        stop_condition_mode = self.cfg.replay_stop_condition_mode
        fallback_reason = (
            None
            if stop_condition_mode == "ts_delta_sec"
            else "configured_frame_count_fallback"
        )
        replay_stop_strategy = str(req.get("replay_stop_strategy") or "")
        offset_seconds_override = self.legacy._replay_offset_seconds(
            replay_stop_strategy=replay_stop_strategy,
            anchor_strategy=self.cfg.replay_anchor_strategy,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
            event_frame_uuid=str(replay_anchor_req.get("frame_uuid") or ""),
            keyframe_uuid=keyframe_uuid,
            explicit_offset_seconds=self.legacy._to_float(
                replay_anchor_req.get("replay_offset_seconds")
            ),
        )
        explicit_duration = self.legacy._to_float(
            replay_anchor_req.get("replay_duration_seconds")
        )
        if explicit_duration is None:
            explicit_duration = self.legacy._to_float(
                replay_anchor_req.get("duration_seconds_override")
            )
        duration_seconds_override = self.legacy._replay_duration_seconds(
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
            offset_seconds_override=offset_seconds_override,
            explicit_duration_seconds=explicit_duration,
        )
        timing = self.legacy._effective_replay_slot_timing(
            self.cfg,
            replay_anchor_req,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
            offset_seconds_override=offset_seconds_override,
            duration_seconds_override=duration_seconds_override,
        )
        slot_token = logical_replay_slot_token(event_id)
        label_request = {
            **replay_anchor_req,
            "record_request_shard_id": record_request_shard_id,
            "consumer_resolved_shard_id": replay_route.shard.shard_id,
            "shard_mapping_version": replay_route.mapping_version,
            "replay_slot_token": slot_token,
        }
        labels = self.legacy._replay_job_labels(
            event_id,
            label_request,
            replay_offset_seconds=offset_seconds_override,
            replay_duration_seconds=duration_seconds_override,
        )
        plan = build_replay_plan(
            source_id=source_id,
            keyframe_uuid=keyframe_uuid,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
            sink_endpoint=replay_route.shard.replay_job_sink_url,
            labels=labels,
            stop_condition_mode=stop_condition_mode,
            fallback_reason=fallback_reason,
            fps=self.cfg.replay_fps,
            force_constant_cadence=self.cfg.replay_force_constant_cadence,
            offset_seconds_override=offset_seconds_override,
            duration_seconds_override=duration_seconds_override,
            ts_sync=self.cfg.replay_ts_sync,
        )
        if self.cfg.planner_shadow_enabled:
            assert_replay_plan_parity(
                plan,
                build_job_payload(
                    source_id=source_id,
                    keyframe_uuid=keyframe_uuid,
                    pre_seconds=pre_seconds,
                    post_seconds=post_seconds,
                    sink_endpoint=replay_route.shard.replay_job_sink_url,
                    labels=labels,
                    stop_condition_mode=stop_condition_mode,
                    fallback_reason=fallback_reason,
                    fps=self.cfg.replay_fps,
                    force_constant_cadence=(
                        self.cfg.replay_force_constant_cadence
                    ),
                    offset_seconds_override=offset_seconds_override,
                    duration_seconds_override=duration_seconds_override,
                    ts_sync=self.cfg.replay_ts_sync,
                ),
            )
        phase_diagnostics.update(proof_diagnostics)
        phase_diagnostics.update(
            {
                "replay_plan_schema_version": plan.schema_version,
                "replay_plan_hash": plan.plan_hash,
                "replay_plan_shadow_enabled": self.cfg.planner_shadow_enabled,
                "replay_slot_token": slot_token,
            }
        )
        sink_instance = self.legacy._sink_instance_from_url(
            replay_route.shard.replay_job_sink_url
        )
        result = FencedReplayExecutor(
            self.replay_admission,
            replay,
            crash_injector=self.crash_injector,
        ).execute(
            FencedReplayRequest(
                delivery=delivery,
                event_id=event_id,
                request_id=request_id,
                owner=delivery.consumer,
                slot_token=slot_token,
                source_id=source_id,
                camera_id=camera_id,
                replay_shard=replay_shard,
                sink_instance=sink_instance,
                plan=plan,
                timing=timing,
                max_global=self.cfg.evidence_materialization_max_concurrency,
                max_per_shard=(
                    self.cfg.evidence_materialization_max_concurrency_per_shard
                ),
                max_per_source=(
                    self.cfg.evidence_materialization_max_concurrency_per_source
                ),
                diagnostics=phase_diagnostics,
            )
        )
        if result.code is ProcessingCode.CAPACITY_PENDING and not result.durable:
            persisted = self.evidence_state.mark_pending(
                event_id,
                error_message=result.reason,
                evidence_state="queued",
                evidence_reason=result.reason,
                request_id=request_id,
                attempt_count=retry_count,
                diagnostics=phase_diagnostics,
                replay_shard=replay_shard,
            )
            return ProcessingOutcome(
                **{
                    **result.__dict__,
                    "durable": persisted,
                }
            )
        if result.code is ProcessingCode.REPLAY_CREATED and result.durable:
            self.jobs_created += 1
            hold_s = float(timing.timeout_budget_s)
            self.active_jobs.append(
                ActiveReplayJob(
                    until_monotonic=time.monotonic() + hold_s,
                    shard_id=replay_route.shard.shard_id,
                    source_id=source_id,
                    event_type=event_type,
                )
            )
            if event_type:
                self.event_type_counts[event_type] += 1
            if camera_id:
                self.last_job_by_camera[camera_id] = cooldown_gate_ts_ms
        return result

    def _resolve_anchor(
        self,
        *,
        delivery: DeliveryEnvelope,
        req: dict[str, Any],
        replay: Any,
        replay_shard: dict[str, object],
        event_id: str,
        request_id: str,
        source_id: str,
        source_event_id: str,
        camera_id: str,
        event_ts_ms: int,
        pre_seconds: int,
        post_seconds: int,
        keyframe_uuid: str | None,
        keyframe_source: str,
        post_savant_media_request: bool,
        phase_diagnostics: dict[str, object],
    ) -> tuple[dict[str, Any], str, dict[str, object]] | ProcessingOutcome:
        proof_diagnostics: dict[str, object] = {}
        replay_anchor_req: dict[str, Any] | None = req
        if post_savant_media_request:
            started = time.monotonic()
            started_at = self.legacy._utc_now_iso()
            persisted_diagnostics = self.evidence_state.diagnostics(event_id)
            try:
                proof_retry_count = max(
                    0,
                    int(persisted_diagnostics.get("proof_retry_count") or 0),
                )
            except (TypeError, ValueError):
                proof_retry_count = 0
            wait_budget = None
            poll_interval = None
            attempts = None
            if delivery.reclaimed:
                wait_budget = 0.0
                poll_interval = 0.0
                attempts = 1
            else:
                stream_diagnostics = self.consumer.diagnostics()
                fast_path = self.legacy._post_savant_proof_fast_path_reason(
                    self.cfg,
                    record_request_batch_size=1,
                    stream_diagnostics=stream_diagnostics,
                )
                if fast_path:
                    wait_budget = 0.0
                    poll_interval = 0.0
                    attempts = 1
                    phase_diagnostics["proof_wait_fast_path_reason"] = fast_path
                    phase_diagnostics["proof_wait_fast_path_stream"] = (
                        stream_diagnostics
                    )

            def on_wait(
                attempt: int,
                _attempts: int,
                error: str,
                diagnostics: dict[str, object],
            ) -> None:
                proof_diagnostics.clear()
                proof_diagnostics.update(diagnostics)
                proof_diagnostics.update(phase_diagnostics)
                proof_diagnostics.update(
                    {
                        "proof_wait_started_at": started_at,
                        "proof_retry_count": proof_retry_count,
                        "redis_delivery_retry_count": delivery.retry_count,
                    }
                )
                self.evidence_state.mark_pending(
                    event_id,
                    error_message=error,
                    evidence_state="waiting_proof",
                    evidence_reason=error,
                    request_id=request_id,
                    attempt_count=attempt,
                    diagnostics=proof_diagnostics,
                    replay_shard=replay_shard,
                )

            resolution = self.proof_resolver.resolve(
                self.redis_client,
                replay,
                self.cfg,
                req,
                source_id=source_id,
                camera_id=camera_id or source_id,
                keyframe_uuid=keyframe_uuid,
                keyframe_source=keyframe_source,
                pre_seconds=pre_seconds,
                post_seconds=post_seconds,
                on_wait=on_wait,
                wait_budget_s_override=wait_budget,
                poll_interval_s_override=poll_interval,
                attempts_override=attempts,
            )
            wait_s = time.monotonic() - started
            proof_diagnostics.update(
                {
                    "proof_wait_seconds": wait_s,
                    "proof_wait_ms": int(wait_s * 1000),
                    "proof_wait_started_at": started_at,
                    "proof_wait_finished_at": self.legacy._utc_now_iso(),
                    "proof_retry_count": proof_retry_count,
                    "redis_delivery_retry_count": delivery.retry_count,
                }
            )
            if isinstance(resolution, ReadyProof):
                replay_anchor_req = resolution.request()
                keyframe_uuid = resolution.keyframe_uuid
            else:
                error = resolution.error
                retryable = self.legacy._post_savant_anchor_error_retryable(
                    req,
                    error,
                )
                if (
                    retryable
                    and proof_retry_count
                    < max(0, int(self.cfg.deferred_retry_max_attempts))
                ):
                    next_retry = proof_retry_count + 1
                    proof_diagnostics["proof_retry_count"] = next_retry
                    persisted = self.evidence_state.mark_pending(
                        event_id,
                        error_message=(
                            "deferred_retry "
                            "reason=missing_post_savant_frame_proof "
                            f"retry_count={next_retry} error={error}"
                        ),
                        evidence_state="waiting_proof",
                        evidence_reason=error,
                        request_id=request_id,
                        attempt_count=next_retry,
                        diagnostics=proof_diagnostics,
                        replay_shard=replay_shard,
                    )
                    return self._outcome(
                        ProcessingCode.RETRY_PENDING,
                        event_id,
                        request_id,
                        reason=error,
                        durable=persisted,
                    )
                final_error = error
                if retryable:
                    final_error = (
                        "retry_budget_exhausted "
                        "reason=missing_post_savant_frame_proof "
                        f"retries={proof_retry_count} error={error}"
                    )
                persisted = self.evidence_state.mark_failed(
                    event_id,
                    error_message=final_error,
                    evidence_state="failed",
                    evidence_reason=final_error,
                    request_id=request_id,
                    attempt_count=proof_retry_count or None,
                    diagnostics=proof_diagnostics,
                    replay_shard=replay_shard,
                )
                return self._persisted_terminal(
                    event_id,
                    request_id,
                    reason=final_error,
                    persisted=persisted,
                )
        elif not keyframe_uuid:
            if (
                not self.legacy._should_lookup_replay_anchor(
                    self.cfg.replay_anchor_strategy
                )
                and not self.cfg.allow_unbounded_keyframe_fallback
            ):
                return self._fail(
                    event_id,
                    request_id,
                    self.legacy.MISSING_KEYFRAME_ERROR,
                    replay_shard,
                )
            if not source_id:
                return self._fail(
                    event_id,
                    request_id,
                    "missing source_id in record_request",
                    replay_shard,
                )
            if not event_ts_ms:
                return self._fail(
                    event_id,
                    request_id,
                    f"missing event_ts_ms in record_request source_id={source_id}",
                    replay_shard,
                )
            lookup_ts_ms = self.legacy._replay_anchor_lookup_ts_ms(
                req,
                pre_seconds=pre_seconds,
                post_seconds=post_seconds,
                anchor_strategy=self.cfg.replay_anchor_strategy,
            )
            selection = self.legacy._replay_anchor_selection(
                self.cfg.replay_anchor_strategy
            )
            attempts = max(1, int(self.cfg.keyframe_lookup_retries) + 1)
            for attempt in range(attempts):
                keyframe_uuid = replay.find_keyframe(
                    source_id,
                    lookup_ts_ms,
                    window_s=max(
                        self.cfg.keyframe_lookup_window_s,
                        pre_seconds + post_seconds,
                    ),
                    selection=selection,
                )
                if keyframe_uuid:
                    break
                if attempt + 1 < attempts:
                    time.sleep(max(0.0, self.cfg.keyframe_lookup_retry_sleep_s))

        if not keyframe_uuid or replay_anchor_req is None:
            return self._fail(
                event_id,
                request_id,
                (
                    f"no keyframe found for source_id={source_id}"
                    + (f" event_ts_ms={event_ts_ms}" if event_ts_ms else "")
                ),
                replay_shard,
            )
        return replay_anchor_req, str(keyframe_uuid), proof_diagnostics

    def _schedule_denied(
        self,
        decision: Any,
        *,
        event_id: str,
        request_id: str,
        retry_count: int,
        replay_shard: dict[str, object],
        diagnostics: dict[str, object],
    ) -> ProcessingOutcome:
        if decision.terminal_defer:
            persisted = self.evidence_state.mark_terminal_deferred(
                event_id,
                error_message=decision.error_message,
                evidence_state="materialization_deferred",
                evidence_reason=decision.reason,
                request_id=request_id,
                diagnostics=diagnostics,
                replay_shard=replay_shard,
                quota_decision=decision.quota_decision,
                degrade_decision=decision.degrade_decision,
            )
            return self._persisted_terminal(
                event_id,
                request_id,
                reason=decision.reason,
                persisted=persisted,
            )
        if decision.reason.startswith("max_concurrent"):
            persisted = self.evidence_state.mark_pending(
                event_id,
                error_message=decision.error_message,
                evidence_state="queued",
                evidence_reason=decision.reason,
                request_id=request_id,
                attempt_count=retry_count,
                diagnostics=diagnostics,
                replay_shard=replay_shard,
                quota_decision=decision.quota_decision,
            )
            return self._outcome(
                ProcessingCode.CAPACITY_PENDING,
                event_id,
                request_id,
                reason=decision.reason,
                durable=persisted,
            )
        if decision.reason == "cooldown":
            if retry_count >= max(0, int(self.cfg.deferred_retry_max_attempts)):
                return self._fail(
                    event_id,
                    request_id,
                    (
                        "retry_budget_exhausted reason=cooldown "
                        f"retries={retry_count} error={decision.error_message}"
                    ),
                    replay_shard,
                )
            persisted = self.evidence_state.mark_pending(
                event_id,
                error_message=decision.error_message,
                evidence_state="materialization_pending",
                evidence_reason=decision.reason,
                request_id=request_id,
                attempt_count=retry_count + 1,
                diagnostics=diagnostics,
                replay_shard=replay_shard,
                quota_decision=decision.quota_decision,
                degrade_decision=decision.degrade_decision,
            )
            return self._outcome(
                ProcessingCode.RETRY_PENDING,
                event_id,
                request_id,
                reason=decision.reason,
                durable=persisted,
            )
        persisted = self.evidence_state.mark_skipped(
            event_id,
            error_message=decision.error_message,
            replay_shard=replay_shard,
        )
        return self._persisted_terminal(
            event_id,
            request_id,
            reason=decision.reason,
            persisted=persisted,
        )

    def _fail(
        self,
        event_id: str,
        request_id: str,
        reason: str,
        replay_shard: Mapping[str, object],
    ) -> ProcessingOutcome:
        persisted = self.evidence_state.mark_failed(
            event_id,
            error_message=reason,
            evidence_state="failed",
            evidence_reason=reason,
            request_id=request_id,
            replay_shard=dict(replay_shard),
        )
        return self._persisted_terminal(
            event_id,
            request_id,
            reason=reason,
            persisted=persisted,
        )

    @staticmethod
    def _persisted_terminal(
        event_id: str,
        request_id: str,
        *,
        reason: str,
        persisted: bool,
    ) -> ProcessingOutcome:
        return ProcessingOutcome(
            code=ProcessingCode.PERMANENT_FAILURE,
            ack_disposition=(
                AckDisposition.ACK if persisted else AckDisposition.HOLD
            ),
            durable=persisted,
            event_id=event_id,
            request_id=request_id,
            reason=reason,
        )

    @staticmethod
    def _outcome(
        code: ProcessingCode,
        event_id: str,
        request_id: str,
        *,
        reason: str,
        ack: bool = False,
        durable: bool = False,
    ) -> ProcessingOutcome:
        return ProcessingOutcome(
            code=code,
            ack_disposition=(AckDisposition.ACK if ack else AckDisposition.HOLD),
            durable=durable,
            event_id=event_id,
            request_id=request_id,
            reason=reason,
        )
