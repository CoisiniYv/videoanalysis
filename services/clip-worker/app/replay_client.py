"""Replay API client — keyframe lookup and replay job creation."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


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
        ts_ms: int,
        window_s: float = 5.0,
    ) -> Optional[str]:
        """GET /api/v1/keyframes/find — find nearest keyframe UUID.

        Args:
            source_id: Replay source identifier.
            ts_ms: Timestamp in milliseconds (event_ts_ms).
            window_s: Search window in seconds around *ts_ms*.

        Returns:
            keyframe_uuid string, or None if not found.
        """
        ts_seconds = ts_ms / 1000.0
        try:
            resp = httpx.get(
                f"{self._base_url}/api/v1/keyframes/find",
                params={
                    "source_id": source_id,
                    "ts": ts_seconds,
                    "window_s": window_s,
                },
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                logger.warning("no keyframe found for source_id=%s ts=%.3f", source_id, ts_seconds)
                return None
            resp.raise_for_status()
            data = resp.json()
            return data.get("keyframe_uuid") or data.get("uuid")
        except Exception:
            logger.exception(
                "Replay keyframes/find failed source_id=%s ts_ms=%s",
                source_id,
                ts_ms,
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
        payload: Dict[str, Any] = {
            "source_id": source_id,
            "keyframe_uuid": keyframe_uuid,
            "offset": {"seconds": pre_seconds},
            "stop_condition": {"seconds": pre_seconds + post_seconds},
            "sink": {"url": sink_endpoint},
        }
        if labels:
            payload["labels"] = labels

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
