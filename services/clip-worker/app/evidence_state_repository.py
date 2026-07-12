"""Evidence state port for Clip Coordinator V2."""

from __future__ import annotations

from typing import Any

from app import repository


class EvidenceStateRepository:
    """Expose named state operations without arbitrary status SQL at callers."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def terminal_state(self, event_id: str) -> str:
        return repository.terminal_evidence_state(self.connection, event_id)

    def target_exists(self, *, event_id: str, source_event_id: str) -> bool | None:
        return repository.record_request_target_exists(
            self.connection,
            event_id=event_id,
            source_event_id=source_event_id,
        )

    def diagnostics(self, event_id: str) -> dict[str, object]:
        return repository.get_evidence_diagnostics(self.connection, event_id)

    def mark_pending(self, event_id: str, **kwargs: Any) -> bool:
        return repository.update_clip_status(
            self.connection,
            event_id,
            "pending",
            **kwargs,
        )

    def mark_failed(self, event_id: str, **kwargs: Any) -> bool:
        return repository.update_clip_status(
            self.connection,
            event_id,
            "failed",
            **kwargs,
        )

    def mark_replay_created(self, event_id: str, **kwargs: Any) -> bool:
        return repository.update_clip_status(
            self.connection,
            event_id,
            "replay_job_created",
            **kwargs,
        )

    def mark_terminal_deferred(self, event_id: str, **kwargs: Any) -> bool:
        return repository.update_clip_status(
            self.connection,
            event_id,
            "materialization_deferred",
            **kwargs,
        )

    def mark_skipped(self, event_id: str, **kwargs: Any) -> bool:
        return repository.update_clip_status(
            self.connection,
            event_id,
            "skipped_by_poc_limit",
            **kwargs,
        )
