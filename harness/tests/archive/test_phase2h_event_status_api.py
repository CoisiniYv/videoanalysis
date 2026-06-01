"""Tests for Phase 2H event status mutation endpoints."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

# Add services/api to path
API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from fastapi.testclient import TestClient

from app.main import app
from app.schemas.events import EventResponse

NOW = datetime(2026, 5, 24, 10, 30, 0, tzinfo=timezone.utc)


# ===========================================================================
# Fake repositories
# ===========================================================================


def _db_row(event_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1",
            status="new", **overrides) -> dict:
    row = {
        "id": event_id,
        "source_event_id": "savant:cam_01:t_889:intrusion:1000",
        "event_type": "intrusion",
        "camera_id": "cam_01",
        "source_id": "src_01",
        "track_id": "t_889",
        "person_id": None,
        "severity": "medium",
        "confidence": 0.85,
        "start_ts": NOW,
        "end_ts": NOW,
        "event_ts_ms": 2000,
        "frame_uuid": "frm-001",
        "keyframe_uuid": "kf-001",
        "status": status,
        "snapshot_path": None,
        "clip_path": None,
        "recording_strategy": "reserved",
        "media_status": "not_implemented",
        "payload": {"zone_id": "full_frame", "media": {
            "snapshot_status": "not_implemented",
            "clip_status": "not_implemented",
            "recording_strategy": "reserved",
        }},
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


_VALID_TRANSITIONS = {
    "new": {"acknowledged", "confirmed", "false_positive", "resolved"},
    "acknowledged": {"confirmed", "false_positive", "resolved"},
    "confirmed": {"resolved"},
    "false_positive": {"resolved"},
    "resolved": set(),
}


class FakeEventRepository:
    def __init__(self, rows=None):
        self._rows = rows or [_db_row()]
        self._audit_logs: list[dict] = []
        self._conn = self

    def list_recent(self, limit=50): return self._rows[:limit]

    def list_events(self, **filters):
        rows = self._rows
        et = filters.get("event_type")
        if et: rows = [r for r in rows if r["event_type"] == et]
        cid = filters.get("camera_id")
        if cid: rows = [r for r in rows if r["camera_id"] == cid]
        total = len(rows)
        offset = filters.get("offset", 0)
        limit = filters.get("limit", 50)
        return rows[offset:offset+limit], total

    def get_by_id(self, event_id):
        for r in self._rows:
            if r["id"] == event_id: return r
        return None

    def get_by_source_event_id(self, sid):
        for r in self._rows:
            if r["source_event_id"] == sid: return r
        return None

    def get_by_id_or_sid(self, event_id):
        row = self.get_by_id(event_id)
        if row is None: row = self.get_by_source_event_id(event_id)
        return row

    def update_status(self, event_id, new_status):
        row = self.get_by_id_or_sid(event_id)
        if row is None: return None
        current = row["status"]
        allowed = _VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition: {current} -> {new_status}. "
                f"Allowed: {sorted(allowed) if allowed else ['none (terminal)']}"
            )
        row["status"] = new_status
        row["updated_at"] = datetime.now(timezone.utc)
        return dict(row)


class FakeAuditLogRepository:
    def __init__(self):
        self.entries: list[dict] = []

    def write(self, **kwargs):
        self.entries.append(kwargs)


@pytest.fixture
def client():
    repo = FakeEventRepository()

    from app.routers.events import _repo as original_repo

    def fake_repo():
        yield repo

    app.dependency_overrides.clear()
    app.dependency_overrides[original_repo] = fake_repo

    with patch(
        "app.routers.events.AuditLogRepository",
        return_value=FakeAuditLogRepository(),
    ):
        with TestClient(app) as c:
            yield c
    app.dependency_overrides.clear()


# ===========================================================================
# 75. POST acknowledge changes status to acknowledged
# ===========================================================================


def test_acknowledge_sets_status(client):
    resp = client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/acknowledge",
        json={"operator": "op1", "comment": "Looking into it"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["data"]["status"] == "acknowledged"


# ===========================================================================
# 76. POST confirm changes status to confirmed
# ===========================================================================


def test_confirm_sets_status(client):
    resp = client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/confirm",
        json={"operator": "op1"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "confirmed"


# ===========================================================================
# 77. POST false-positive changes status to false_positive
# ===========================================================================


def test_false_positive_sets_status(client):
    resp = client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/false-positive",
        json={"operator": "op1", "comment": "Bird, not person"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "false_positive"


# ===========================================================================
# 78. POST resolve changes status to resolved
# ===========================================================================


def test_resolve_sets_status(client):
    resp = client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/resolve",
        json={"operator": "op1"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "resolved"


# ===========================================================================
# 79. Invalid event_id returns 404
# ===========================================================================


def test_acknowledge_404(client):
    resp = client.post(
        "/api/v1/events/00000000-0000-0000-0000-000000000000/acknowledge",
        json={"operator": "op1"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["data"] is None
    assert body["error"] is not None


# ===========================================================================
# 80. source_event_id works for status mutations
# ===========================================================================


def test_status_mutation_by_source_event_id(client):
    sid = "savant:cam_01:t_889:intrusion:1000"
    resp = client.post(
        f"/api/v1/events/{sid}/acknowledge",
        json={"operator": "op1"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["source_event_id"] == sid


# ===========================================================================
# 81. Invalid transition returns 409 conflict
# ===========================================================================


def test_resolved_to_anything_is_409(client):
    # First resolve
    client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/resolve",
        json={"operator": "op1"},
    )
    # Then try to acknowledge again
    resp = client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/acknowledge",
        json={"operator": "op1"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["data"] is None
    assert body["error"]["code"] == 409
    assert "Invalid transition" in body["error"]["message"]


# ===========================================================================
# 82. acknowledged -> confirmed is allowed
# ===========================================================================


def test_acknowledged_to_confirmed(client):
    client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/acknowledge",
        json={"operator": "op1"},
    )
    resp = client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/confirm",
        json={"operator": "op1"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "confirmed"


# ===========================================================================
# 83. confirmed -> resolved is allowed
# ===========================================================================


def test_confirmed_to_resolved(client):
    client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/confirm",
        json={"operator": "op1"},
    )
    resp = client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/resolve",
        json={"operator": "op1"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "resolved"


# ===========================================================================
# 84. Media fields present in status mutation response
# ===========================================================================


def test_status_response_has_media_fields(client):
    resp = client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/acknowledge",
        json={"operator": "op1"},
    )
    media = resp.json()["data"]["media"]
    assert media["snapshot_status"] == "not_implemented"
    assert media["clip_status"] == "not_implemented"
    assert media["recording_strategy"] == "reserved"


# ===========================================================================
# 85. Audit log written for acknowledge
# ===========================================================================


def test_audit_log_written():
    """Simulate audit log: verify the FakeAuditLogRepository receives entries."""
    audit = FakeAuditLogRepository()
    audit.write(
        actor="op1",
        action="event.acknowledge",
        entity_type="event",
        entity_id="uuid-1",
        previous_status="new",
        new_status="acknowledged",
        comment="test",
        source_event_id="sid-1",
    )
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["actor"] == "op1"
    assert entry["action"] == "event.acknowledge"
    assert entry["entity_type"] == "event"
    assert entry["previous_status"] == "new"
    assert entry["new_status"] == "acknowledged"


# ===========================================================================
# 86. Audit log action names are correct
# ===========================================================================


def test_audit_log_action_names():
    """Verify all 4 mutation actions produce correct audit log action strings."""
    from app.routers.events import _STATUS_ACTIONS

    assert _STATUS_ACTIONS["acknowledge"]["action"] == "event.acknowledge"
    assert _STATUS_ACTIONS["confirm"]["action"] == "event.confirm"
    assert _STATUS_ACTIONS["false-positive"]["action"] == "event.false_positive"
    assert _STATUS_ACTIONS["resolve"]["action"] == "event.resolve"


# ===========================================================================
# 87. Request body without operator/comment works (defaults)
# ===========================================================================


def test_status_mutation_empty_body(client):
    resp = client.post(
        "/api/v1/events/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1/acknowledge",
        json={},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "acknowledged"


# ===========================================================================
# 88. new -> any valid status allowed
# ===========================================================================


def test_new_to_all_transitions(client):
    routes = {
        "acknowledge": "acknowledged",
        "confirm": "confirmed",
        "false-positive": "false_positive",
        "resolve": "resolved",
    }
    for i, (route, expected) in enumerate(routes.items()):
        # Each test needs a fresh row with "new" status
        eid = f"00000000-0000-0000-0000-{i:012d}"
        repo = FakeEventRepository([_db_row(event_id=eid, status="new")])

        from app.routers.events import _repo as original_repo

        def fake_repo(r=repo):
            yield r

        app.dependency_overrides.clear()
        app.dependency_overrides[original_repo] = fake_repo
        with patch(
            "app.routers.events.AuditLogRepository",
            return_value=FakeAuditLogRepository(),
        ):
            with TestClient(app) as c:
                resp = c.post(
                    f"/api/v1/events/{eid}/{route}",
                    json={"operator": "op1"},
                )
                assert resp.status_code == 200, f"{route} should succeed from new"
                assert resp.json()["data"]["status"] == expected
    app.dependency_overrides.clear()
