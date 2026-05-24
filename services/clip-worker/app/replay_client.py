"""Replay API client — keyframe lookup and replay job creation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


def _ts_ms_to_iso(ts_ms: int) -> str:
    """Convert epoch milliseconds to ISO 8601 UTC string."""
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat()


class ReplayClient:
    """HTTP client for Savant Replay Service REST API."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

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
    ) -> Optional[str]:
        """POST /api/v1/keyframes/find — find nearest keyframe UUID.

        When *ts_ms* > 0 the lookup is anchored to the event timestamp:
        ``from`` = event_time - window_s, ``to`` = event_time + window_s.
        When *ts_ms* is 0 the lookup is unbounded (``from``/``to`` = null).

        Args:
            source_id: Replay source identifier.
            ts_ms: Event timestamp in epoch milliseconds (event_ts_ms).
            window_s: Search window in seconds around *ts_ms*.

        Returns:
            keyframe_uuid string, or None if not found.
        """
        from_ts = None
        to_ts = None
        if ts_ms > 0:
            from_ts = _ts_ms_to_iso(int(ts_ms - window_s * 1000))
            to_ts = _ts_ms_to_iso(int(ts_ms + window_s * 1000))
            logger.debug(
                "keyframe_lookup_anchored source_id=%s event_ts_ms=%s "
                "window_s=%s from=%s to=%s",
                source_id, ts_ms, window_s, from_ts, to_ts,
            )

        try:
            resp = httpx.post(
                f"{self._base_url}/api/v1/keyframes/find",
                json={
                    "source_id": source_id,
                    "from": from_ts,
                    "to": to_ts,
                    "limit": 1,
                },
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
                    return uuid_list[0]
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
        pre_seconds: int,
        post_seconds: int,
        sink_endpoint: str,
        labels: Optional[Dict[str, str]] = None,
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
        event_id = labels.get("event_id", "unknown") if labels else "unknown"
        total_frames = (pre_seconds + post_seconds) * 30  # assume 30fps
        payload: Dict[str, Any] = {
            "sink": {"url": sink_endpoint},
            "configuration": {
                "ts_sync": True,
                "skip_intermediary_eos": False,
                "send_eos": True,
                "stop_on_incorrect_ts": False,
                "ts_discrepancy_fix_duration": {"secs": 0, "nanos": 33333333},
                "min_duration": {"secs": 0, "nanos": 10000000},
                "max_duration": {"secs": 0, "nanos": 103333333},
                "stored_stream_id": source_id,
                "resulting_stream_id": f"replay-event-{event_id}",
                "routing_labels": "bypass",
                "max_idle_duration": {"secs": 10, "nanos": 0},
                "max_delivery_duration": {"secs": 10, "nanos": 0},
                "send_metadata_only": False,
                "labels": labels or {},
            },
            "stop_condition": {"frame_count": total_frames},
            "anchor_keyframe": keyframe_uuid,
            "anchor_wait_duration": {"secs": 1, "nanos": 0},
            "offset": {"seconds": pre_seconds},
            "attributes": [],
        }

        try:
            resp = httpx.put(
                f"{self._base_url}/api/v1/job",
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("job_id") or data.get("id")
        except Exception:
            logger.exception(
                "Replay job creation failed source_id=%s keyframe=%s",
                source_id,
                keyframe_uuid,
            )
            return None
