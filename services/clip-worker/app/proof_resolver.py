"""Typed, bounded port around the legacy frame-domain proof implementation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import threading
from typing import Any, Protocol

from app.contracts import InvalidProof, NotReadyProof, ProofResolution, ReadyProof


LegacyResolveResult = tuple[dict | None, str | None, str, str | None]
_LOOKUP_SEMAPHORES: dict[int, threading.BoundedSemaphore] = {}
_LOOKUP_SEMAPHORES_LOCK = threading.Lock()


@contextmanager
def proof_lookup_concurrency_gate(limit: int):
    """Process-local bounded gate shared by legacy proof lookup helpers."""
    effective_limit = max(1, int(limit or 1))
    with _LOOKUP_SEMAPHORES_LOCK:
        semaphore = _LOOKUP_SEMAPHORES.get(effective_limit)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(effective_limit)
            _LOOKUP_SEMAPHORES[effective_limit] = semaphore
    semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


class LegacyProofCallable(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> LegacyResolveResult: ...


@dataclass
class BoundedProofResolver:
    """Adapter that types one legacy proof call and bounds concurrent calls.

    The wrapped callable retains the validated UUID/PTS/session algorithm. This
    adapter owns no Redis client, Replay client, slot, database connection, or
    ACK policy and cannot create a Replay job.
    """

    resolve_legacy: LegacyProofCallable
    max_concurrent: int = 1
    invalid_error_tokens: tuple[str, ...] = (
        "missing source_id in record_request",
        "missing_stream_session_id",
    )
    _semaphore: threading.BoundedSemaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.max_concurrent = max(1, int(self.max_concurrent or 1))
        self._semaphore = threading.BoundedSemaphore(self.max_concurrent)

    def resolve(self, *args: Any, **kwargs: Any) -> ProofResolution:
        with self._semaphore:
            request, keyframe_uuid, keyframe_source, error = self.resolve_legacy(
                *args,
                **kwargs,
            )
        if request is not None and keyframe_uuid and error is None:
            return ReadyProof(
                request_json=json.dumps(
                    request,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                keyframe_uuid=str(keyframe_uuid),
                keyframe_source=str(keyframe_source or ""),
            )
        error_text = str(error or "proof_not_ready")
        outcome_type = (
            InvalidProof
            if any(token in error_text for token in self.invalid_error_tokens)
            else NotReadyProof
        )
        return outcome_type(
            error=error_text,
            keyframe_uuid=(str(keyframe_uuid) if keyframe_uuid else None),
            keyframe_source=str(keyframe_source or ""),
        )
