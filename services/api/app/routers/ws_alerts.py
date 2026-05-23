"""WebSocket endpoint — push real-time alerts from Redis Stream to clients."""

from __future__ import annotations

import asyncio
import json
import logging
import os

import redis.asyncio as redis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
ALERT_STREAM = os.getenv("ALERT_STREAM", "security.alerts")
PING_INTERVAL = int(os.getenv("WS_PING_INTERVAL", "30"))
POLL_TIMEOUT_MS = int(os.getenv("WS_POLL_TIMEOUT_MS", "5000"))


async def _alert_stream_reader(websocket: WebSocket, stream: str) -> None:
    """Read from Redis stream *stream* and send alerts to *websocket*."""
    client = redis.Redis.from_url(REDIS_URL, decode_responses=False)

    # Create consumer group (idempotent)
    group = "ws-alert-readers"
    consumer = f"ws-{id(websocket)}"
    try:
        await client.xgroup_create(stream, group, id="$", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    ping_task: asyncio.Task | None = None

    async def send_ping():
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await websocket.send_json({"message_type": "ping"})
            except Exception:
                break

    try:
        ping_task = asyncio.create_task(send_ping())

        while True:
            try:
                result = await asyncio.wait_for(
                    client.xreadgroup(
                        group, consumer, {stream: ">"},
                        count=10, block=POLL_TIMEOUT_MS,
                    ),
                    timeout=POLL_TIMEOUT_MS / 1000.0 + 1,
                )
            except asyncio.TimeoutError:
                continue

            if not result:
                continue

            for _stream_name, entries in result:
                for msg_id, fields in entries:
                    data_raw = fields.get(b"data")
                    if not data_raw:
                        await client.xack(stream, group, msg_id)
                        continue

                    try:
                        alert = json.loads(data_raw)
                    except json.JSONDecodeError:
                        await client.xack(stream, group, msg_id)
                        continue

                    await websocket.send_json({
                        "message_type": "alert",
                        "event": alert,
                    })

                    await client.xack(stream, group, msg_id)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("alert stream reader error")
    finally:
        if ping_task:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        try:
            await client.aclose()
        except Exception:
            pass


@router.websocket("/api/v1/ws/alerts")
async def ws_alerts(websocket: WebSocket) -> None:
    await websocket.accept()
    await _alert_stream_reader(websocket, ALERT_STREAM)
