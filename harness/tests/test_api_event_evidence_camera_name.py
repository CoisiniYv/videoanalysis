from __future__ import annotations

import sys
from pathlib import Path


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.schemas.events import EventEvidenceResponse, EventResponse  # noqa: E402
from app.services.evidence_detail_resolver import resolve_event_evidence_detail  # noqa: E402


def _event() -> EventResponse:
    return EventResponse.from_db_row(
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "source_event_id": "source-event-1",
            "event_type": "intrusion",
            "camera_id": "camera-1",
            "source_id": "source-1",
            "payload": {"camera_name": "lab"},
        }
    )


def test_event_evidence_response_exposes_camera_name_without_losing_ids() -> None:
    event = _event()

    payload = EventEvidenceResponse.from_event_and_tasks(
        event=event,
        evidence_tasks=[],
        evidence_detail=resolve_event_evidence_detail(event),
    ).model_dump()

    assert payload["camera_name"] == "lab"
    assert payload["event"]["source_id"] == "source-1"
    assert payload["event"]["camera_id"] == "camera-1"
    assert payload["evidence_detail"]["camera_name"] == "lab"
    assert payload["evidence_detail"]["source_id"] == "source-1"


def test_event_response_exposes_camera_name_for_list_recent_and_detail_routes() -> None:
    payload = _event().model_dump()

    assert payload["camera_name"] == "lab"
    assert payload["source_id"] == "source-1"
    assert payload["camera_id"] == "camera-1"
