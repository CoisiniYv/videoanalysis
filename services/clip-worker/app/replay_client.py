"""Replay API client — keyframe lookup and replay job creation."""

from __future__ import annotations

import json
import logging
import math
import os
from uuid import UUID
from typing import Any, Dict, Optional

import httpx

from app.contracts import (
    ReplayPlan,
    ReplaySubmission,
    ReplaySubmissionCode,
)

logger = logging.getLogger(__name__)


def _ts_ms_to_unix_seconds(ts_ms: int) -> int:
    """Convert epoch milliseconds to Unix seconds for Replay keyframe lookup."""
    return int(ts_ms // 1000)


def _uuid7_timestamp_ms(value: str) -> int | None:
    """Extract the millisecond timestamp from Savant UUIDv7-style frame IDs."""
    try:
        return int((UUID(value).int >> 80) & ((1 << 48) - 1))
    except (TypeError, ValueError, AttributeError):
        return None


class ReplayClient:
    """HTTP client for Savant Replay Service REST API."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self.last_job_request: Dict[str, Any] | None = None

    def status(self) -> Optional[Dict[str, Any]]:
        """GET /api/v1/status"""
        try:
            resp = httpx.get(
                f"{self._base_url}/api/v1/status",
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            logger.exception("Replay /api/v1/status failed")
            return None

    def find_keyframe(
        self,
        source_id: str,
        ts_ms: int = 0,
        window_s: float = 10.0,
        selection: str = "nearest",
    ) -> Optional[str]:
        """POST /api/v1/keyframes/find — find nearest keyframe UUID.

        When *ts_ms* > 0 the lookup is anchored to the event timestamp:
        ``from`` = event_time - window_s, ``to`` = event_time + window_s.
        When *ts_ms* is 0 the lookup is unbounded (``from``/``to`` omitted).

        Args:
            source_id: Replay source identifier.
            ts_ms: Event timestamp in epoch milliseconds (event_ts_ms).
            window_s: Search window in seconds around *ts_ms*.
            selection: ``nearest``, ``at_or_after``, or
                ``strict_at_or_after`` for UUIDv7 timestamp selection within
                the returned keyframe list.

        Returns:
            keyframe_uuid string, or None if not found.
        """
        from_unix_s = None
        to_unix_s = None
        if ts_ms > 0:
            from_unix_s = _ts_ms_to_unix_seconds(int(ts_ms - window_s * 1000))
            to_unix_s = _ts_ms_to_unix_seconds(int(ts_ms + window_s * 1000))
            logger.info(
                "keyframe_lookup_anchored source_id=%s event_ts_ms=%s "
                "window_s=%s from_unix_s=%s to_unix_s=%s",
                source_id, ts_ms, window_s, from_unix_s, to_unix_s,
            )
        else:
            logger.warning(
                "keyframe_lookup_unbounded source_id=%s ts_ms=%s; this must be "
                "explicitly enabled by the caller",
                source_id,
                ts_ms,
            )

        body: Dict[str, Any] = {
            "source_id": source_id,
            "from": from_unix_s,
            "to": to_unix_s,
            "limit": 20 if ts_ms > 0 else 1,
        }

        try:
            resp = httpx.post(
                f"{self._base_url}/api/v1/keyframes/find",
                json=body,
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                logger.warning(
                    "no keyframe found for source_id=%s ts_ms=%s window_s=%s",
                    source_id, ts_ms, window_s,
                )
                return None
            resp.raise_for_status()
            data = resp.json()
            # Response format: {"keyframes": ["source_id", ["uuid1", ...]]}
            # keyframes[0] = source_id, keyframes[1][0] = first UUID
            kfs = data.get("keyframes", [])
            if isinstance(kfs, list) and len(kfs) > 1:
                uuid_list = kfs[1]
                if isinstance(uuid_list, list) and len(uuid_list) > 0:
                    return _select_keyframe_uuid(
                        uuid_list,
                        ts_ms=ts_ms,
                        selection=selection,
                    )
            # Fallback: try older formats
            if isinstance(kfs, list) and len(kfs) > 0:
                first = kfs[0]
                return first if isinstance(first, str) and "-" in first else None
            return data.get("keyframe_uuid") or data.get("uuid")
        except Exception:
            logger.exception(
                "Replay keyframes/find failed source_id=%s ts_ms=%s",
                source_id, ts_ms,
            )
            return None

    def create_job(
        self,
        source_id: str,
        keyframe_uuid: str,
        pre_seconds: float,
        post_seconds: float,
        sink_endpoint: str,
        labels: Optional[Dict[str, str]] = None,
        stop_condition_mode: str = "frame_count",
        fallback_reason: str | None = None,
        fps: int = 30,
        force_constant_cadence: bool | None = None,
        offset_seconds_override: float | None = None,
        duration_seconds_override: float | None = None,
        ts_sync: bool | None = None,
    ) -> Optional[str]:
        """PUT /api/v1/job — create a re-streaming job.

        Args:
            source_id: Replay source identifier.
            keyframe_uuid: Keyframe to start from.
            pre_seconds: Offset before keyframe.
            post_seconds: Duration after offset.
            sink_endpoint: ZMQ endpoint for video-file-sink.
            labels: Optional metadata labels (e.g. event_id).

        Returns:
            job_id string, or None on failure.
        """
        replay_ts_sync = (
            _env_bool("REPLAY_TS_SYNC", False) if ts_sync is None else bool(ts_sync)
        )
        payload = build_job_payload(
            source_id=source_id,
            keyframe_uuid=keyframe_uuid,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
            sink_endpoint=sink_endpoint,
            labels=labels,
            stop_condition_mode=stop_condition_mode,
            fallback_reason=fallback_reason,
            fps=fps,
            force_constant_cadence=(
                _env_bool("REPLAY_FORCE_CONSTANT_CADENCE", True)
                if force_constant_cadence is None
                else bool(force_constant_cadence)
            ),
            offset_seconds_override=offset_seconds_override,
            duration_seconds_override=duration_seconds_override,
            ts_sync=replay_ts_sync,
        )
        self.last_job_request = payload

        try:
            return self._submit_job_payload(payload)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Replay API rejected primary job payload source_id=%s keyframe=%s "
                "status=%s response=%s",
                source_id,
                keyframe_uuid,
                exc.response.status_code if exc.response is not None else "",
                (exc.response.text if exc.response is not None else "")[:500],
            )

            fallback_payloads: list[Dict[str, Any]] = []
            primary_constant_cadence = _env_bool("REPLAY_FORCE_CONSTANT_CADENCE", True)
            if not primary_constant_cadence:
                fallback_payloads.append(
                    build_job_payload(
                        source_id=source_id,
                        keyframe_uuid=keyframe_uuid,
                        pre_seconds=pre_seconds,
                        post_seconds=post_seconds,
                        sink_endpoint=sink_endpoint,
                        labels=labels,
                        stop_condition_mode=stop_condition_mode,
                        fallback_reason=(
                            "replay_api_rejected_without_constant_cadence"
                        ),
                        fps=fps,
                        force_constant_cadence=True,
                        offset_seconds_override=offset_seconds_override,
                        duration_seconds_override=duration_seconds_override,
                        ts_sync=replay_ts_sync,
                    )
                )
                if stop_condition_mode == "ts_delta_sec":
                    fallback_payloads.append(
                        build_job_payload(
                            source_id=source_id,
                            keyframe_uuid=keyframe_uuid,
                            pre_seconds=pre_seconds,
                            post_seconds=post_seconds,
                            sink_endpoint=sink_endpoint,
                            labels=labels,
                            stop_condition_mode="frame_count",
                            fallback_reason=(
                                "replay_api_rejected_ts_delta_sec_constant_cadence"
                            ),
                            fps=fps,
                            force_constant_cadence=True,
                            offset_seconds_override=offset_seconds_override,
                            duration_seconds_override=duration_seconds_override,
                            ts_sync=replay_ts_sync,
                        )
                    )
            elif stop_condition_mode == "ts_delta_sec":
                fallback_payloads.append(
                    build_job_payload(
                        source_id=source_id,
                        keyframe_uuid=keyframe_uuid,
                        pre_seconds=pre_seconds,
                        post_seconds=post_seconds,
                        sink_endpoint=sink_endpoint,
                        labels=labels,
                        stop_condition_mode="frame_count",
                        fallback_reason="replay_api_rejected_ts_delta_sec",
                        fps=fps,
                        force_constant_cadence=True,
                        offset_seconds_override=offset_seconds_override,
                        duration_seconds_override=duration_seconds_override,
                        ts_sync=replay_ts_sync,
                    )
                )

            seen_payloads: set[str] = set()
            for fallback_payload in fallback_payloads:
                signature = str(fallback_payload)
                if signature in seen_payloads:
                    continue
                seen_payloads.add(signature)
                try:
                    logger.warning(
                        "Retrying Replay job request with fallback payload=%s",
                        fallback_payload,
                    )
                    return self._submit_job_payload(fallback_payload)
                except httpx.HTTPStatusError as fallback_exc:
                    logger.warning(
                        "Replay fallback job payload rejected source_id=%s "
                        "keyframe=%s status=%s response=%s",
                        source_id,
                        keyframe_uuid,
                        (
                            fallback_exc.response.status_code
                            if fallback_exc.response is not None
                            else ""
                        ),
                        (
                            fallback_exc.response.text
                            if fallback_exc.response is not None
                            else ""
                        )[:500],
                    )
                    continue
                except Exception:
                    logger.exception(
                        "Replay fallback job request failed source_id=%s keyframe=%s",
                        source_id,
                        keyframe_uuid,
                    )
                    continue

            logger.error(
                "Replay job creation failed after fallbacks source_id=%s keyframe=%s",
                source_id,
                keyframe_uuid,
            )
            return None
        except Exception:
            logger.exception(
                "Replay job creation failed source_id=%s keyframe=%s",
                source_id,
                keyframe_uuid,
            )
            return None

    def _submit_job_payload(self, payload: Dict[str, Any]) -> Optional[str]:
        self.last_job_request = payload
        logger.info("Replay job request payload=%s", payload)
        resp = httpx.put(
            f"{self._base_url}/api/v1/job",
            json=payload,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("new_job") or data.get("job_id") or data.get("id")

    def submit_plan(self, plan: ReplayPlan) -> ReplaySubmission:
        """Submit one immutable V2 plan and retain an explicit uncertainty type."""
        payload = plan.payload()
        resulting_stream_id = str(
            payload.get("configuration", {}).get("resulting_stream_id") or ""
        )
        request_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            job_id = self._submit_job_payload(payload)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            code = (
                ReplaySubmissionCode.PERMANENT_REJECTED
                if 400 <= status < 500 and status not in {408, 409, 425, 429}
                else ReplaySubmissionCode.UNCERTAIN
            )
            return ReplaySubmission(
                code=code,
                resulting_stream_id=resulting_stream_id,
                request_json=request_json,
                reason=f"Replay HTTP status {status}",
            )
        except Exception as exc:
            return ReplaySubmission(
                code=ReplaySubmissionCode.UNCERTAIN,
                resulting_stream_id=resulting_stream_id,
                request_json=request_json,
                reason=f"{type(exc).__name__}:{exc}",
            )
        if not job_id:
            return ReplaySubmission(
                code=ReplaySubmissionCode.UNCERTAIN,
                resulting_stream_id=resulting_stream_id,
                request_json=request_json,
                reason="Replay accepted request without a job identifier",
            )
        return ReplaySubmission(
            code=ReplaySubmissionCode.CREATED,
            job_id=str(job_id),
            resulting_stream_id=resulting_stream_id,
            request_json=request_json,
        )

    def recover_submission(
        self,
        *,
        slot_token: str,
        resulting_stream_id: str,
    ) -> ReplaySubmission | None:
        """Find an active Replay job for one logical slot without creating it."""
        try:
            response = httpx.get(
                f"{self._base_url}/api/v1/job",
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            logger.exception(
                "Replay job recovery query failed slot_token=%s stream=%s",
                slot_token,
                resulting_stream_id,
            )
            return None
        jobs = payload.get("jobs") if isinstance(payload, dict) else payload
        if not isinstance(jobs, list):
            return None
        for job in jobs:
            if not isinstance(job, dict):
                continue
            labels = _nested_job_labels(job)
            candidate_token = str(labels.get("replay_slot_token") or "")
            candidate_stream = _nested_resulting_stream_id(job)
            if slot_token and candidate_token == slot_token:
                matched = True
            else:
                matched = bool(
                    resulting_stream_id
                    and candidate_stream == resulting_stream_id
                )
            if not matched:
                continue
            job_id = _nested_job_id(job)
            if not job_id:
                continue
            return ReplaySubmission(
                code=ReplaySubmissionCode.CREATED,
                job_id=job_id,
                resulting_stream_id=candidate_stream or resulting_stream_id,
                request_json=json.dumps(
                    job,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                reason="recovered_active_replay_job",
            )
        return None


def _nested_job_labels(job: Dict[str, Any]) -> Dict[str, Any]:
    direct = job.get("labels")
    if isinstance(direct, dict):
        return direct
    configuration = job.get("configuration")
    if isinstance(configuration, dict):
        labels = configuration.get("labels")
        if isinstance(labels, dict):
            return labels
    request = job.get("request") or job.get("job")
    if isinstance(request, dict):
        return _nested_job_labels(request)
    return {}


def _nested_resulting_stream_id(job: Dict[str, Any]) -> str:
    direct = str(job.get("resulting_stream_id") or "")
    if direct:
        return direct
    configuration = job.get("configuration")
    if isinstance(configuration, dict):
        value = str(configuration.get("resulting_stream_id") or "")
        if value:
            return value
    request = job.get("request") or job.get("job")
    if isinstance(request, dict):
        return _nested_resulting_stream_id(request)
    return ""


def _nested_job_id(job: Dict[str, Any]) -> str:
    for key in ("new_job", "job_id", "id", "name"):
        value = str(job.get(key) or "")
        if value:
            return value
    request = job.get("job")
    if isinstance(request, dict):
        return _nested_job_id(request)
    return ""


def _select_keyframe_uuid(
    uuid_list: list[Any],
    *,
    ts_ms: int,
    selection: str = "nearest",
) -> str | None:
    candidates = [value for value in uuid_list if isinstance(value, str)]
    if not candidates:
        return None
    if ts_ms <= 0:
        return candidates[0]

    scored: list[tuple[int, str]] = []
    unscored: list[str] = []
    for value in candidates:
        uuid_ts_ms = _uuid7_timestamp_ms(value)
        if uuid_ts_ms is None:
            unscored.append(value)
            continue
        scored.append((uuid_ts_ms, value))
    if scored:
        if selection == "at_or_after":
            future = [item for item in scored if item[0] >= ts_ms]
            if future:
                return min(future)[1]
            return max(scored)[1]
        if selection == "strict_at_or_after":
            future = [item for item in scored if item[0] >= ts_ms]
            if future:
                return min(future)[1]
            return None
        # Prefer the closest keyframe to the requested Replay timeline anchor.
        # For equal distance, prefer the later keyframe so a bounded clip is
        # less likely to end before the event.
        return min((abs(ts - ts_ms), -ts, value) for ts, value in scored)[2]
    return unscored[0]


def _effective_fps(fps: int) -> int:
    return fps if fps > 0 else 30


def _frame_duration_nanos(fps: int) -> int:
    """Nanoseconds per frame for real-time Replay pacing."""
    return int(1_000_000_000 // _effective_fps(fps))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


RELIABLE_SINK_OPTIONS: Dict[str, Any] = {
    "send_timeout": {"secs": 5, "nanos": 0},
    "send_retries": 5,
    "receive_timeout": {"secs": 5, "nanos": 0},
    "receive_retries": 5,
    "send_hwm": 10000,
    "receive_hwm": 10000,
    "inflight_ops": 100,
}

MIN_DELIVERY_DURATION_S = 30
DELIVERY_DURATION_EXTRA_SLACK_S = 10


def _max_delivery_duration_seconds(expected_seconds: float) -> int:
    # This is a watchdog ceiling, not the expected export latency. With
    # REPLAY_TS_SYNC=false, evidence export should run as fast as Replay and the
    # sink can accept frames; with ts_sync enabled it may still take wall-clock
    # media duration plus slack.
    return max(
        MIN_DELIVERY_DURATION_S,
        int(
            math.ceil(
                max(float(expected_seconds), 0.0) + DELIVERY_DURATION_EXTRA_SLACK_S
            )
        ),
    )


def build_job_payload(
    *,
    source_id: str,
    keyframe_uuid: str,
    pre_seconds: float,
    post_seconds: float,
    sink_endpoint: str,
    labels: Optional[Dict[str, str]] = None,
    stop_condition_mode: str = "frame_count",
    fallback_reason: str | None = None,
    fps: int = 30,
    force_constant_cadence: bool | None = None,
    offset_seconds_override: float | None = None,
    duration_seconds_override: float | None = None,
    ts_sync: bool | None = None,
) -> Dict[str, Any]:
    """Build the Replay REST job request body used by clip-worker."""
    event_id = labels.get("event_id", "unknown") if labels else "unknown"
    runtime_epoch_id = (
        str(labels.get("runtime_epoch_id") or "").strip() if labels else ""
    )
    resulting_stream_id = (
        f"replay-{runtime_epoch_id}-event-{event_id}"
        if runtime_epoch_id
        else f"replay-event-{event_id}"
    )
    effective_fps = _effective_fps(fps)
    frame_duration_nanos = _frame_duration_nanos(effective_fps)
    expected_seconds = (
        float(duration_seconds_override)
        if duration_seconds_override is not None
        else float(pre_seconds) + float(post_seconds)
    )
    total_frames = int(round(expected_seconds * effective_fps))
    stop_condition: Dict[str, Any]
    if stop_condition_mode == "ts_delta_sec":
        stop_condition = {
            "ts_delta_sec": {
                "max_delta_sec": expected_seconds,
            }
        }
    else:
        stop_condition = {"frame_count": total_frames}
    frame_duration = {"secs": 0, "nanos": frame_duration_nanos}
    max_delivery_duration_s = _max_delivery_duration_seconds(expected_seconds)
    use_constant_cadence = (
        _env_bool("REPLAY_FORCE_CONSTANT_CADENCE", True)
        if force_constant_cadence is None
        else bool(force_constant_cadence)
    )
    use_ts_sync = (
        _env_bool("REPLAY_TS_SYNC", False) if ts_sync is None else bool(ts_sync)
    )
    configuration: Dict[str, Any] = {
        "ts_sync": use_ts_sync,
        "skip_intermediary_eos": False,
        "send_eos": True,
        "stop_on_incorrect_ts": False,
        "stored_stream_id": source_id,
        "resulting_stream_id": resulting_stream_id,
        "routing_labels": "bypass",
        "max_idle_duration": {"secs": 10, "nanos": 0},
        "max_delivery_duration": {"secs": max_delivery_duration_s, "nanos": 0},
        "send_metadata_only": False,
        "labels": labels or {},
    }
    if use_constant_cadence:
        configuration.update(
            {
                # Constant cadence is kept as an API-compatibility fallback. The
                # evidence window is still bounded by stop_condition, preferably
                # ts_delta_sec; frame_count must not be treated as the evidence clock.
                "ts_discrepancy_fix_duration": frame_duration,
                "min_duration": frame_duration,
                "max_duration": frame_duration,
            }
        )
    payload = {
        "sink": {
            "url": sink_endpoint,
            "options": RELIABLE_SINK_OPTIONS.copy(),
        },
        "configuration": configuration,
        "stop_condition": stop_condition,
        "anchor_keyframe": keyframe_uuid,
        "anchor_wait_duration": {"secs": 1, "nanos": 0},
        "offset": {
            "seconds": (
                float(pre_seconds)
                if offset_seconds_override is None
                else float(offset_seconds_override)
            )
        },
        "attributes": [],
    }
    if fallback_reason is not None:
        payload["fallback_reason"] = fallback_reason
    return payload
