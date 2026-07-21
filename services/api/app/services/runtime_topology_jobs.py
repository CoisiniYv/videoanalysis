"""Background topology-apply jobs for the 8090 operator portal."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.runtime_topology import (
    RuntimeTopologyError,
    apply_runtime_topology_config,
)


DEFAULT_STATUS_PATH = "/data/video-analytics/media/.runtime/topology_apply_status.json"
_LOCK = threading.Lock()
_STATUS_LOCK = threading.Lock()
_ACTIVE_THREAD: threading.Thread | None = None


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _status_path() -> Path:
    return Path(os.getenv("RUNTIME_TOPOLOGY_APPLY_STATUS_PATH", DEFAULT_STATUS_PATH))


def _read_status() -> dict[str, Any]:
    path = _status_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {
            "status": "idle",
            "phase": "idle",
            "percent": 0,
            "message": "尚未启动完整链路任务",
        }
    return payload if isinstance(payload, dict) else {}


def _write_status(payload: dict[str, Any]) -> dict[str, Any]:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {**payload, "updated_at": _now_text()}
    # Stop and background-apply requests may report progress concurrently.
    # A fixed .tmp path lets one writer rename the other writer's file and
    # caused the 8090 stop endpoint to return 500.  Serialize publication and
    # keep a per-write temp identity so os.replace remains atomic.
    with _STATUS_LOCK:
        temp = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp.write_text(
                json.dumps(doc, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return doc


def get_runtime_topology_apply_status() -> dict[str, Any]:
    status = _read_status()
    if status.get("status") != "running":
        return status
    thread_alive = _ACTIVE_THREAD is not None and _ACTIVE_THREAD.is_alive()
    worker_pid = int(status.get("worker_pid") or 0)
    if thread_alive and worker_pid == os.getpid():
        return status
    return _write_status(
        {
            **status,
            "status": "failed",
            "phase": "failed",
            "message": "API 在启动过程中重启，后台任务已中断，请核对运行状态后重试",
            "finished_at": _now_text(),
            "error": {
                "message": "runtime topology apply worker was interrupted",
                "status_code": 503,
            },
        }
    )


def mark_runtime_topology_stopped(result: dict[str, Any]) -> dict[str, Any]:
    return _write_status(
        {
            "job_id": None,
            "status": "stopped",
            "phase": "stopped",
            "percent": 0,
            "message": "摄像头采集与双分支推理已停止；已产生的 evidence 继续收尾",
            "started_at": None,
            "finished_at": _now_text(),
            "rolling_cache": None,
            "error": None,
            "result": {
                "disabled_camera_count": result.get("disabled_camera_count"),
                "stop_mode": result.get("stop_mode"),
                "drain_workers_left_running": result.get("drain_workers_left_running"),
            },
        }
    )


def start_runtime_topology_apply_job(
    *,
    force: bool,
    camera_selection: dict[str, Any] | None,
) -> dict[str, Any]:
    global _ACTIVE_THREAD
    with _LOCK:
        if _ACTIVE_THREAD is not None and _ACTIVE_THREAD.is_alive():
            current = _read_status()
            raise RuntimeTopologyError(
                "runtime topology apply is already running",
                status_code=409,
                details={"apply_status": current},
            )
        job_id = str(uuid.uuid4())
        initial = _write_status(
            {
                "job_id": job_id,
                "status": "running",
                "phase": "queued",
                "percent": 1,
                "message": "启动任务已进入后台队列",
                "started_at": _now_text(),
                "worker_pid": os.getpid(),
                "rolling_cache": None,
                "error": None,
                "result": None,
            }
        )
        _ACTIVE_THREAD = threading.Thread(
            target=_run_apply_job,
            kwargs={
                "job_id": job_id,
                "force": force,
                "camera_selection": camera_selection,
            },
            name=f"runtime-topology-apply-{job_id[:8]}",
            daemon=True,
        )
        _ACTIVE_THREAD.start()
        return initial


def _run_apply_job(
    *,
    job_id: str,
    force: bool,
    camera_selection: dict[str, Any] | None,
) -> None:
    base = {
        "job_id": job_id,
        "status": "running",
        "started_at": _read_status().get("started_at") or _now_text(),
        "worker_pid": os.getpid(),
        "error": None,
        "result": None,
    }

    def progress(update: dict[str, Any]) -> None:
        _write_status({**base, **update, "status": "running"})

    try:
        result = apply_runtime_topology_config(
            force=force,
            camera_selection=camera_selection,
            progress_callback=progress,
        )
        _write_status(
            {
                **base,
                "status": "succeeded",
                "phase": "complete",
                "percent": 100,
                "message": "完整双分支、rolling-cache 和 evidence 链已就绪",
                "rolling_cache": {
                    "prefill_seconds": int(
                        ((result.get("status") or {}).get("plan") or {}).get(
                            "rolling_prefill_seconds"
                        )
                        or 0
                    ),
                    "remaining_seconds": 0,
                },
                "finished_at": _now_text(),
                "result": {
                    "mode": result.get("mode"),
                    "runtime_epoch_id": result.get("runtime_epoch_id"),
                    "camera_selection": result.get("camera_selection"),
                },
            }
        )
    except RuntimeTopologyError as exc:
        _write_status(
            {
                **base,
                "status": "failed",
                "phase": "failed",
                "percent": int(_read_status().get("percent") or 0),
                "message": str(exc),
                "finished_at": _now_text(),
                "error": {
                    "message": str(exc),
                    "status_code": exc.status_code,
                    "details": exc.details,
                },
            }
        )
    except Exception as exc:  # pragma: no cover - final containment boundary
        _write_status(
            {
                **base,
                "status": "failed",
                "phase": "failed",
                "percent": int(_read_status().get("percent") or 0),
                "message": f"unexpected topology apply error: {exc}",
                "finished_at": _now_text(),
                "error": {"message": str(exc), "status_code": 500},
            }
        )
