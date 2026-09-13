"""Per-camera evidence ledger reconciliation.

A system-wide success rate cannot tell you that camera-17 has produced nothing
for twenty minutes. The ledger answers that per camera, and its value depends
entirely on the buckets adding up: if a task can vanish between "created" and
the outcome columns, the report can show a healthy camera that is in fact
silent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import uuid

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "runtime" / "report_evidence_camera_ledger.py"
_SCHEMA = "evidence_ledger_fixture"


def _ledger_module():
    spec = importlib.util.spec_from_file_location(
        "report_evidence_camera_ledger", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_DDL = f"""
DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE;
CREATE SCHEMA {_SCHEMA};
CREATE TABLE {_SCHEMA}.evidence_tasks (
    event_id uuid PRIMARY KEY,
    source_id text,
    replay_source_id text,
    materialization_status text,
    materialization_expired_reason text,
    materialization_failure_reason text,
    error_message text,
    materialization_ready_at timestamptz,
    materialization_deadline_at timestamptz,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);
"""


@pytest.fixture()
def ledger_db():
    import os

    database_url = os.getenv("EVIDENCE_FAIRNESS_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("EVIDENCE_FAIRNESS_TEST_DATABASE_URL is not configured")
    psycopg = pytest.importorskip("psycopg")

    conn = psycopg.connect(database_url, autocommit=True)
    conn.execute(_DDL)
    conn.execute(f"SET search_path TO {_SCHEMA}")

    def add(source_id: str, status: str, *, count: int = 1, reason: str = "") -> None:
        for _ in range(count):
            conn.execute(
                """
                INSERT INTO evidence_tasks (
                    event_id, source_id, replay_source_id,
                    materialization_status, materialization_expired_reason,
                    materialization_ready_at, materialization_deadline_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    now() - interval '30 seconds',
                    now() + interval '120 seconds'
                )
                """,
                (str(uuid.uuid4()), source_id, source_id, status, reason),
            )

    try:
        yield conn, add
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        conn.close()


def test_ledger_balances_every_accepted_task(ledger_db) -> None:
    """created == outstanding + published + terminal_unsuccessful + skipped."""

    ledger = _ledger_module()
    conn, add = ledger_db

    add("camera-01", "materialized", count=5)
    add("camera-01", "materialization_pending", count=2)
    add("camera-02", "materialization_expired", count=3,
        reason="business_deadline_expired")
    add("camera-02", "materialization_skipped", count=4)
    add("camera-03", "materialization_failed", count=1)

    report = ledger.collect(conn, since_minutes=60)
    by_camera = {row["source_id"]: row for row in report["cameras"]}

    assert not report["unbalanced_cameras"]
    for row in report["cameras"]:
        assert row["accounted"] == row["tasks_created"]

    assert by_camera["camera-01"]["published"] == 5
    assert by_camera["camera-01"]["outstanding"] == 2
    assert by_camera["camera-02"]["deadline_expired"] == 3
    assert by_camera["camera-02"]["admission_skipped"] == 4
    assert by_camera["camera-03"]["failed"] == 1


def test_ledger_names_cameras_that_produced_nothing(ledger_db) -> None:
    """The specific question a global success rate cannot answer."""

    ledger = _ledger_module()
    conn, add = ledger_db

    add("camera-01", "materialized", count=20)
    # This camera is the symptom: tasks exist, none ever became evidence.
    add("camera-17", "materialization_expired", count=4,
        reason="business_deadline_expired")

    report = ledger.collect(conn, since_minutes=60)

    assert report["silent_cameras"] == ["camera-17"]
    by_camera = {row["source_id"]: row for row in report["cameras"]}
    assert by_camera["camera-17"]["deadline_expired"] == 4
    reasons = {r["reason"] for r in by_camera["camera-17"]["reasons"]}
    assert "business_deadline_expired" in reasons


def test_ledger_reports_an_unknown_lifecycle_state_instead_of_hiding_it(
    ledger_db,
) -> None:
    """A state the report does not know about must surface, not vanish.

    Silently dropping it would let the ledger show a camera as fully accounted
    for while some of its tasks are unrepresented -- the exact blindness the
    ledger exists to remove.
    """

    ledger = _ledger_module()
    conn, add = ledger_db

    add("camera-01", "materialized", count=2)
    add("camera-01", "some_future_state", count=3)

    report = ledger.collect(conn, since_minutes=60)
    by_camera = {row["source_id"]: row for row in report["cameras"]}

    assert report["unbalanced_cameras"] == ["camera-01"]
    assert by_camera["camera-01"]["unaccounted"] == 3


def test_admission_skips_are_reported_separately_from_accepted_work(
    ledger_db,
) -> None:
    """A success rate over admitted tasks alone would hide the refusals."""

    ledger = _ledger_module()
    conn, add = ledger_db

    add("camera-01", "materialized", count=1)
    add("camera-01", "materialization_skipped", count=9)

    report = ledger.collect(conn, since_minutes=60)
    row = report["cameras"][0]

    # 1 of 1 admitted succeeded, but 9 requests never became evidence at all.
    assert row["published"] == 1
    assert row["admission_skipped"] == 9
    assert row["terminal_unsuccessful"] == 0
    assert row["tasks_created"] == 10
