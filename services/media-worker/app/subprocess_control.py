"""Process-group tracking for bounded media-worker shutdown."""

from __future__ import annotations

import os
import signal
import subprocess
from threading import Lock
import time
from typing import Any


class ManagedProcessRegistry:
    """Track child process groups and terminate them TERM -> KILL on demand."""

    def __init__(self) -> None:
        self._processes: dict[int, subprocess.Popen[Any]] = {}
        self._lock = Lock()
        self._started_total = 0
        self._completed_total = 0
        self._term_total = 0
        self._kill_total = 0

    def run(
        self,
        *popenargs: object,
        input: object | None = None,
        capture_output: bool = False,
        timeout: float | None = None,
        check: bool = False,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[Any]:
        if input is not None:
            if kwargs.get("stdin") is not None:
                raise ValueError("stdin and input arguments may not both be used")
            kwargs["stdin"] = subprocess.PIPE
        if capture_output:
            if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
                raise ValueError(
                    "stdout and stderr arguments may not be used with capture_output"
                )
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
        kwargs.setdefault("start_new_session", True)
        process = subprocess.Popen(*popenargs, **kwargs)  # type: ignore[arg-type]
        self._register(process)
        try:
            try:
                stdout, stderr = process.communicate(input=input, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                self._terminate_process(process, term_timeout_s=0.25)
                stdout, stderr = process.communicate()
                exc.stdout = stdout
                exc.stderr = stderr
                raise
            completed = subprocess.CompletedProcess(
                process.args,
                process.returncode,
                stdout,
                stderr,
            )
            if check:
                completed.check_returncode()
            return completed
        finally:
            self._unregister(process)

    def _register(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._processes[process.pid] = process
            self._started_total += 1

    def _unregister(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            if self._processes.pop(process.pid, None) is not None:
                self._completed_total += 1

    @staticmethod
    def _send_group_signal(
        process: subprocess.Popen[Any],
        signal_number: int,
    ) -> bool:
        if process.poll() is not None:
            return False
        try:
            os.killpg(os.getpgid(process.pid), signal_number)
        except (ProcessLookupError, PermissionError):
            try:
                process.send_signal(signal_number)
            except ProcessLookupError:
                return False
        return True

    def _terminate_process(
        self,
        process: subprocess.Popen[Any],
        *,
        term_timeout_s: float,
    ) -> None:
        if self._send_group_signal(process, signal.SIGTERM):
            with self._lock:
                self._term_total += 1
        deadline = time.monotonic() + max(0.0, float(term_timeout_s))
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if process.poll() is None and self._send_group_signal(process, signal.SIGKILL):
            with self._lock:
                self._kill_total += 1

    def terminate_all(self, *, term_timeout_s: float) -> None:
        with self._lock:
            processes = list(self._processes.values())
        if not processes:
            return
        for process in processes:
            if self._send_group_signal(process, signal.SIGTERM):
                with self._lock:
                    self._term_total += 1
        deadline = time.monotonic() + max(0.0, float(term_timeout_s))
        while time.monotonic() < deadline:
            if all(process.poll() is not None for process in processes):
                return
            time.sleep(0.01)
        for process in processes:
            if process.poll() is None and self._send_group_signal(
                process,
                signal.SIGKILL,
            ):
                with self._lock:
                    self._kill_total += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "active": len(self._processes),
                "started_total": self._started_total,
                "completed_total": self._completed_total,
                "term_total": self._term_total,
                "kill_total": self._kill_total,
            }


_ACTIVE_REGISTRY: ManagedProcessRegistry | None = None
_ACTIVE_REGISTRY_LOCK = Lock()


def set_active_process_registry(
    registry: ManagedProcessRegistry | None,
) -> None:
    global _ACTIVE_REGISTRY
    with _ACTIVE_REGISTRY_LOCK:
        _ACTIVE_REGISTRY = registry


def run_managed_subprocess(
    *popenargs: object,
    **kwargs: object,
) -> subprocess.CompletedProcess[Any]:
    with _ACTIVE_REGISTRY_LOCK:
        registry = _ACTIVE_REGISTRY
    if registry is None:
        return subprocess.run(*popenargs, **kwargs)  # type: ignore[arg-type]
    return registry.run(*popenargs, **kwargs)
