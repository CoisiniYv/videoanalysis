"""EventExporter interface and DryRunEventExporter.

``DryRunEventExporter`` is a no-op exporter that serialises each
``SecurityEvent`` to a single-line JSON string and prints it.
It does **not** connect to Redis, PostgreSQL, or any external service.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from custom.models.events import SecurityEvent


class EventExporter(ABC):
    """Abstract interface for exporting ``SecurityEvent`` objects."""

    @abstractmethod
    def export(self, event: SecurityEvent) -> None:
        """Export *event* to the configured sink."""


class DryRunEventExporter(EventExporter):
    """Exporter that logs each event as a single-line JSON dry-run entry.

    The output is structured so it can be grepped::

        stage=phase2c_security_event_dry_run
        source_event_id=...
        event_type=intrusion
        security_event_json={...}
    """

    def export(self, event: SecurityEvent) -> None:
        """Print the event as a structured single-line log entry.

        Does not write to Redis, PostgreSQL, or any external system.
        """
        event_json = event.to_json()
        print(
            f"stage=phase2c_security_event_dry_run "
            f"source_event_id={event.source_event_id} "
            f"event_type={event.event_type} "
            f"camera_id={event.camera_id} "
            f"track_id={event.track_id} "
            f"security_event_json={event_json}",
            flush=True,
        )
