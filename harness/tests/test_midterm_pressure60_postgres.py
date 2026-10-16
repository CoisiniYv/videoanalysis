"""Real-PostgreSQL contract for pressure camera rule provisioning.

Set ``PRESSURE60_RULE_TEST_DATABASE_URL`` to a disposable PostgreSQL database
when running this integration test.  The fixture applies the active camera
configuration migrations itself, so the test exercises the same TEXT camera
IDs and ``insert_pressure_cameras`` SQL used by the runner.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/runtime/run_midterm_pressure60.py"
MIGRATIONS = (
    "002_events.sql",
    "004_camera_config.sql",
    "008_unified_event_evidence_algorithm_rules.sql",
    "010_algorithm_config_api.sql",
    "012_operator_camera_schema_compat.sql",
    "016_camera_rule_zone_id_text_compat.sql",
)


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "pressure60_postgres_integration_runner", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def migrated_database():
    database_url = os.getenv("PRESSURE60_RULE_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("disposable pressure-camera PostgreSQL URL is not configured")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    from psycopg import sql

    connection = psycopg.connect(database_url, autocommit=True, row_factory=dict_row)
    schema = f"pressure_rule_test_{uuid.uuid4().hex}"
    try:
        connection.execute(
            sql.SQL("CREATE SCHEMA {} AUTHORIZATION CURRENT_USER").format(
                sql.Identifier(schema)
            )
        )
        connection.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )
        for migration_name in MIGRATIONS:
            connection.execute(
                (ROOT / "db/migrations" / migration_name).read_text(encoding="utf-8")
            )
        yield connection
    finally:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )
        connection.close()


def test_insert_pressure_cameras_reads_back_text_camera_ids(migrated_database, tmp_path):
    module = _load_runner()
    # Reuse operator-style TEXT IDs rather than UUID-shaped generated IDs.  A
    # uuid[] regression would otherwise pass this test by accident.
    for index in range(2):
        location = f"pressure-{index:02d}"
        migrated_database.execute(
            """
            INSERT INTO cameras (id, source_id, name, rtsp_url, site_id, location)
            VALUES (%s, %s, %s, %s, 'pressure', %s)
            """,
            (
                f"pressure-camera-{index:02d}",
                f"old-pressure-source-{index:02d}",
                f"old pressure camera {index:02d}",
                "rtsp://old-pressure/live",
                location,
            ),
        )
    cfg = SimpleNamespace(
        run_id="real-postgres-rule-test",
        stream_count=2,
        rtsp_uri="rtsp://camera/live",
        rtsp_republish_output_base="",
        behavior_rules=(),
        pressure_algorithm_cooldown_s=60,
        artifact_dir=tmp_path,
        savant_ablation_stage="full-evidence",
        evidence_group_size=10,
        evidence_policy_groups=((2, 2),),
    )

    report = module.insert_pressure_cameras(migrated_database, cfg)

    assert report["enabled_rule_readback"]["status"] == "passed"
    assert report["enabled_rule_readback"]["mismatches"] == []
    assert report["created_camera_ids"] == []
    camera_ids = report["reused_camera_ids"]
    assert set(camera_ids) == {"pressure-camera-00", "pressure-camera-01"}
    rows = migrated_database.execute(
        "SELECT camera_id FROM camera_rules WHERE camera_id=ANY(%s::text[])",
        (camera_ids,),
    ).fetchall()
    assert {row["camera_id"] for row in rows} == set(camera_ids)
