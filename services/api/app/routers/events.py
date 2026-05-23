"""Event query endpoints — /api/v1/events/*"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.db import get_conn
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.events import EventRepository
from app.schemas.events import EventListResponse, EventResponse, StatusUpdateRequest

router = APIRouter(prefix="/api/v1/events", tags=["events"])


# ---------------------------------------------------------------------------
# Dependency: request_id
# ---------------------------------------------------------------------------


def _request_id(request: Request) -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Dependency: repository
# ---------------------------------------------------------------------------


def _repo() -> EventRepository:
    conn = get_conn()
    try:
        yield EventRepository(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Response wrappers
# ---------------------------------------------------------------------------


def _ok(data: object, request_id: str) -> dict:
    return {"data": data, "error": None, "request_id": request_id}


def _err(message: str, request_id: str, status_code: int = 404) -> dict:
    return {
        "data": None,
        "error": {"message": message, "code": status_code},
        "request_id": request_id,
    }


# ---------------------------------------------------------------------------
# GET /api/v1/events/recent
# ---------------------------------------------------------------------------


@router.get("/recent")
def events_recent(
    limit: int = Query(50, ge=1, le=200),
    repo: EventRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    rows = repo.list_recent(limit=limit)
    events = [EventResponse.from_db_row(r) for r in rows]
    return _ok(
        EventListResponse(
            events=events, total=len(events), limit=limit, offset=0
        ).model_dump(),
        request_id,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/events
# ---------------------------------------------------------------------------


@router.get("")
def events_list(
    event_type: Optional[str] = Query(None),
    camera_id: Optional[str] = Query(None),
    track_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    start: Optional[str] = Query(None, description="ISO 8601 start of time range"),
    end: Optional[str] = Query(None, description="ISO 8601 end of time range"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: EventRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    rows, total = repo.list_events(
        event_type=event_type,
        camera_id=camera_id,
        track_id=track_id,
        status=status,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )
    events = [EventResponse.from_db_row(r) for r in rows]
    return _ok(
        EventListResponse(
            events=events, total=total, limit=limit, offset=offset
        ).model_dump(),
        request_id,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/events/{event_id}
# ---------------------------------------------------------------------------


@router.get("/{event_id}")
def events_get(
    event_id: str,
    repo: EventRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    # Try UUID first, then source_event_id
    row = repo.get_by_id(event_id)
    if row is None:
        row = repo.get_by_source_event_id(event_id)

    if row is None:
        return JSONResponse(
            status_code=404,
            content=_err(f"event not found: {event_id}", request_id),
        )

    return _ok(EventResponse.from_db_row(row).model_dump(), request_id)


# ---------------------------------------------------------------------------
# Status mutation helper
# ---------------------------------------------------------------------------

_STATUS_ACTIONS = {
    "acknowledge":      {"status": "acknowledged",   "action": "event.acknowledge"},
    "confirm":          {"status": "confirmed",      "action": "event.confirm"},
    "false-positive":   {"status": "false_positive", "action": "event.false_positive"},
    "resolve":          {"status": "resolved",       "action": "event.resolve"},
}


def _apply_status_mutation(
    event_id: str,
    mutation: str,
    body: StatusUpdateRequest,
    repo: EventRepository,
    request_id: str,
) -> dict:
    info = _STATUS_ACTIONS[mutation]

    # Resolve event
    row = repo.get_by_id_or_sid(event_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content=_err(f"event not found: {event_id}", request_id),
        )

    previous_status = row["status"]

    # Update status (validates transition)
    try:
        updated = repo.update_status(event_id, info["status"])
    except ValueError as exc:
        return JSONResponse(
            status_code=409,
            content=_err(str(exc), request_id, status_code=409),
        )

    if updated is None:
        return JSONResponse(
            status_code=404,
            content=_err(f"event not found: {event_id}", request_id),
        )

    # Write audit log
    audit = AuditLogRepository(repo._conn)
    audit.write(
        actor=body.operator or "unknown",
        action=info["action"],
        entity_type="event",
        entity_id=str(updated["id"]),
        previous_status=previous_status,
        new_status=info["status"],
        comment=body.comment or "",
        source_event_id=updated.get("source_event_id", ""),
    )

    return _ok(EventResponse.from_db_row(updated).model_dump(), request_id)


# ---------------------------------------------------------------------------
# POST /api/v1/events/{event_id}/acknowledge
# ---------------------------------------------------------------------------


@router.post("/{event_id}/acknowledge")
def events_acknowledge(
    event_id: str,
    body: StatusUpdateRequest,
    repo: EventRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    return _apply_status_mutation(event_id, "acknowledge", body, repo, request_id)


# ---------------------------------------------------------------------------
# POST /api/v1/events/{event_id}/confirm
# ---------------------------------------------------------------------------


@router.post("/{event_id}/confirm")
def events_confirm(
    event_id: str,
    body: StatusUpdateRequest,
    repo: EventRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    return _apply_status_mutation(event_id, "confirm", body, repo, request_id)


# ---------------------------------------------------------------------------
# POST /api/v1/events/{event_id}/false-positive
# ---------------------------------------------------------------------------


@router.post("/{event_id}/false-positive")
def events_false_positive(
    event_id: str,
    body: StatusUpdateRequest,
    repo: EventRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    return _apply_status_mutation(event_id, "false-positive", body, repo, request_id)


# ---------------------------------------------------------------------------
# POST /api/v1/events/{event_id}/resolve
# ---------------------------------------------------------------------------


@router.post("/{event_id}/resolve")
def events_resolve(
    event_id: str,
    body: StatusUpdateRequest,
    repo: EventRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    return _apply_status_mutation(event_id, "resolve", body, repo, request_id)
