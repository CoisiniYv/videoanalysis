"""Regression contracts for event/task latency and image-evidence persistence."""

from __future__ import annotations

import importlib.util
import os
import time
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = ROOT / "services" / "event-worker" / "app" / "worker.py"
PERSON_WORKER_PATH = ROOT / "services" / "event-worker" / "app" / "person_worker.py"
REPOSITORY_PATH = ROOT / "services" / "event-worker" / "app" / "repository.py"
FACE_REPOSITORY_PATH = ROOT / "services" / "face-worker" / "app" / "repository.py"
FACE_CONSUMER_PATH = ROOT / "services" / "face-worker" / "app" / "redis_consumer.py"
EVENT_CONSUMER_PATH = ROOT / "services" / "event-worker" / "app" / "redis_consumer.py"
COMPOSE_PATH = ROOT / "infra" / "docker-compose.midterm.yml"


def test_event_delivery_gets_a_bounded_turn_before_person_observations() -> None:
    source = WORKER_PATH.read_text(encoding="utf-8")
    loop = source.split("while not shutdown_requested:", 1)[1]

    event_pending = loop.index("pending = consumer.read_pending")
    event_new = loop.index("new_msgs = consumer.read_new")
    person_turn = loop.index("if person_consumer is not None")

    assert event_pending < event_new < person_turn
    assert "if not did_work:" in loop
    assert "scheduling=events_first_bounded" in source


def test_midterm_person_observations_have_a_dedicated_worker() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    event_environment = compose["services"]["event-worker"]["environment"]
    service = compose["services"]["person-observation-worker"]
    person_environment = service["environment"]

    assert event_environment["EVENT_BATCH_SIZE"] == "${EVENT_BATCH_SIZE:-100}"
    assert event_environment["PERSON_OBSERVATION_CONSUMER_ENABLED"] == "false"
    assert person_environment["PERSON_OBSERVATION_CONSUMER_ENABLED"] == "true"
    assert person_environment["PERSON_OBSERVATION_BATCH_SIZE"] == (
        "${PERSON_OBSERVATION_WORKER_BATCH_SIZE:-500}"
    )
    assert service["command"] == ["python", "person_main.py"]
    assert event_environment["EVIDENCE_TASK_CREATION_ENABLED"] == (
        "${EVIDENCE_TASK_CREATION_ENABLED:-true}"
    )
    assert event_environment["EVIDENCE_TASK_EVENT_NOT_BEFORE_TS_MS"] == (
        "${EVIDENCE_TASK_EVENT_NOT_BEFORE_TS_MS:-0}"
    )

    source = PERSON_WORKER_PATH.read_text(encoding="utf-8")
    assert "run_person_observation_worker" in source
    assert "scheduling=dedicated" in source
    assert "_process_person_observation_batch" in source
    assert "AlertPolicyService" not in source


def test_high_rate_observation_workers_bound_docker_json_logs() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    expected = {
        "driver": "json-file",
        "options": {
            "max-size": "${MIDTERM_HIGH_RATE_LOG_MAX_SIZE:-50m}",
            "max-file": "${MIDTERM_HIGH_RATE_LOG_MAX_FILE:-3}",
        },
    }

    for service_name in ("person-observation-worker", "face-worker"):
        assert compose["services"][service_name]["logging"] == expected


def test_image_evidence_write_is_atomic_and_json_parameters_are_typed() -> None:
    source = REPOSITORY_PATH.read_text(encoding="utf-8")
    image_path = source.split("def _create_image_only_evidence_task", 1)[1]
    image_path = image_path.split("def create_evidence_task", 1)[0]

    assert "with self._conn.transaction(), self._conn.cursor" in image_path
    for parameter in ("image_status", "image_reason", "task_status"):
        assert f"%({parameter})s::text" in image_path


def test_face_observation_hot_path_batches_transaction_pipeline_and_ack() -> None:
    repository = FACE_REPOSITORY_PATH.read_text(encoding="utf-8")
    consumer = FACE_CONSUMER_PATH.read_text(encoding="utf-8")

    assert "def insert_observations" in repository
    assert "with self._conn.transaction():" in repository
    assert "with self._conn.pipeline():" in repository
    assert "def ack_many" in consumer
    assert "xack(self._stream, self._group, *msg_ids)" in consumer


def test_person_observation_hot_path_batches_transaction_pipeline_and_ack() -> None:
    repository = REPOSITORY_PATH.read_text(encoding="utf-8")
    consumer = EVENT_CONSUMER_PATH.read_text(encoding="utf-8")
    worker = WORKER_PATH.read_text(encoding="utf-8")

    assert "def insert_person_bbox_observations" in repository
    assert "jsonb_to_recordset(%(rows)s::jsonb)" in repository
    assert "_INSERT_PERSON_BBOX_OBSERVATIONS_SQL" in repository
    assert "with self._conn.transaction(), self._conn.cursor" in repository
    assert "def ack_many" in consumer
    assert "xack(self._stream, self._group, *msg_ids)" in consumer
    assert "insert_person_bbox_observations" in worker


def _load_event_repository_module():
    spec = importlib.util.spec_from_file_location(
        "event_worker_repository_atomicity_test",
        REPOSITORY_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_face_repository_module():
    spec = importlib.util.spec_from_file_location(
        "face_worker_repository_batch_test",
        FACE_REPOSITORY_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.integration
def test_real_postgres_face_batch_insert_is_idempotent_and_atomic() -> None:
    database_url = os.getenv("FACE_WORKER_REPOSITORY_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("FACE_WORKER_REPOSITORY_TEST_DATABASE_URL not set")

    repository_module = _load_face_repository_module()
    source_observation_id = f"face-batch-atomic:{uuid.uuid4()}"
    row = {
        "source_observation_id": source_observation_id,
        "camera_id": "face-batch-atomic-camera",
        "source_id": "face-batch-atomic-source",
        "track_id": "face-batch-atomic-track",
        "timestamp_ms": int(time.time() * 1000),
        "face_bbox": [10.0, 20.0, 110.0, 120.0],
        "landmarks": [
            [35.0, 50.0],
            [85.0, 50.0],
            [60.0, 72.0],
            [42.0, 96.0],
            [78.0, 96.0],
        ],
        "embedding": [0.0] * 511 + [1.0],
        "embedding_dim": 512,
        "embedding_norm": 1.0,
        "face_confidence": 0.9,
        "quality": 0.8,
        "payload": {"batch_atomic_test": True},
    }

    class RollbackProbe(Exception):
        pass

    connection = psycopg.connect(database_url, autocommit=True)
    try:
        repository = repository_module.FaceObservationRepository(connection)
        with pytest.raises(RollbackProbe):
            with connection.transaction():
                results = repository.insert_observations([row, dict(row)])
                assert results[0]
                assert results[1] is None
                count = connection.execute(
                    "SELECT count(*) FROM face_observations "
                    "WHERE source_observation_id = %(source_observation_id)s",
                    {"source_observation_id": source_observation_id},
                ).fetchone()[0]
                assert count == 1
                raise RollbackProbe
        residual = connection.execute(
            "SELECT count(*) FROM face_observations "
            "WHERE source_observation_id = %(source_observation_id)s",
            {"source_observation_id": source_observation_id},
        ).fetchone()[0]
        assert residual == 0
    finally:
        connection.close()


@pytest.mark.integration
def test_real_postgres_person_batch_insert_is_idempotent_and_atomic() -> None:
    database_url = os.getenv("EVENT_WORKER_REPOSITORY_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("EVENT_WORKER_REPOSITORY_TEST_DATABASE_URL not set")

    repository_module = _load_event_repository_module()
    source_observation_id = f"person-batch-atomic:{uuid.uuid4()}"
    row = {
        "source_observation_id": source_observation_id,
        "camera_id": "person-batch-atomic-camera",
        "source_id": "person-batch-atomic-source",
        "track_id": "person-batch-atomic-track",
        "timestamp_ms": int(time.time() * 1000),
        "frame_pts": 123456789,
        "frame_num": 42,
        "person_bbox": [10.0, 20.0, 110.0, 220.0],
        "person_confidence": 0.9,
        "gate_status": "accepted",
        "payload": {"batch_atomic_test": True},
    }

    class RollbackProbe(Exception):
        pass

    connection = psycopg.connect(database_url, autocommit=True)
    try:
        repository = repository_module.EventRepository(connection)
        with pytest.raises(RollbackProbe):
            with connection.transaction():
                results = repository.insert_person_bbox_observations([row, dict(row)])
                assert results[0]
                assert results[1] is None
                count = connection.execute(
                    "SELECT count(*) FROM person_bbox_observations "
                    "WHERE source_observation_id = %(source_observation_id)s",
                    {"source_observation_id": source_observation_id},
                ).fetchone()[0]
                assert count == 1
                raise RollbackProbe
        residual = connection.execute(
            "SELECT count(*) FROM person_bbox_observations "
            "WHERE source_observation_id = %(source_observation_id)s",
            {"source_observation_id": source_observation_id},
        ).fetchone()[0]
        assert residual == 0
    finally:
        connection.close()


@pytest.mark.integration
def test_real_postgres_image_task_bundle_and_event_projection_are_atomic() -> None:
    database_url = os.getenv("EVENT_WORKER_REPOSITORY_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("EVENT_WORKER_REPOSITORY_TEST_DATABASE_URL not set")

    repository_module = _load_event_repository_module()
    event_id = str(uuid.uuid4())
    source_event_id = f"atomic-image-test:{event_id}"
    now_ms = int(time.time() * 1000)
    event = {
        "source_event_id": source_event_id,
        "event_type": "watchlist_hit",
        "camera_id": "atomic-image-camera",
        "source_id": "atomic-image-source",
        "event_ts_ms": now_ms,
        "start_ts_ms": now_ms,
        "end_ts_ms": now_ms,
        "snapshot_required": True,
        "clip_required": False,
        "runtime_epoch_id": "atomic-image-epoch",
        "evidence_policy": {
            "snapshot_required": True,
            "clip_required": False,
            "pre_seconds": 5,
            "post_seconds": 5,
            "evidence_mode": "image_only",
            "playback_kind": "image",
        },
        "payload": {
            "runtime_epoch_id": "atomic-image-epoch",
            "media": {
                "runtime_epoch_id": "atomic-image-epoch",
                "evidence_mode": "image_only",
                "playback_kind": "image",
                "frame_pts": now_ms * 1_000_000,
            },
        },
    }

    class RollbackProbe(Exception):
        pass

    connection = psycopg.connect(database_url, autocommit=True)
    try:
        with pytest.raises(RollbackProbe):
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO events (
                        id, source_event_id, event_type, camera_id, source_id,
                        start_ts_ms, end_ts_ms, event_ts_ms, snapshot_required,
                        clip_required, evidence_policy, payload
                    ) VALUES (
                        %(event_id)s::uuid, %(source_event_id)s, 'watchlist_hit',
                        %(camera_id)s, %(source_id)s, %(event_ts_ms)s,
                        %(event_ts_ms)s, %(event_ts_ms)s, true, false,
                        %(evidence_policy)s::jsonb, %(payload)s::jsonb
                    )
                    """,
                    {
                        "event_id": event_id,
                        "source_event_id": source_event_id,
                        "camera_id": event["camera_id"],
                        "source_id": event["source_id"],
                        "event_ts_ms": now_ms,
                        "evidence_policy": psycopg.types.json.Jsonb(
                            event["evidence_policy"]
                        ),
                        "payload": psycopg.types.json.Jsonb(event["payload"]),
                    },
                )
                repository = repository_module.EventRepository(connection)
                task_id = repository.create_evidence_task(event, event_id)
                assert task_id
                cursor.execute(
                    """
                    SELECT et.task_type, et.materialization_status,
                           eb.media_status,
                           e.payload->'media'->>'materialization_status' AS projected
                    FROM events e
                    JOIN evidence_tasks et ON et.event_id = e.id
                    JOIN evidence_bundles eb ON eb.event_id = e.id
                    WHERE e.id = %(event_id)s::uuid
                    """,
                    {"event_id": event_id},
                )
                task_type, task_status, bundle_status, projected = cursor.fetchone()
                assert task_type == "image_only"
                assert task_status == "materialization_pending"
                assert bundle_status == "image_pending"
                assert projected == "materialization_pending"
                raise RollbackProbe
    finally:
        connection.close()
