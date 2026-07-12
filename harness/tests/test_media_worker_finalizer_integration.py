"""Disposable-PostgreSQL canary for the Phase 2 finalizer boundary."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

import psycopg
from psycopg.rows import dict_row
import pytest


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"


def _worker():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    root = str(MEDIA_WORKER_ROOT)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    import app.worker as worker

    return worker


@pytest.fixture()
def database():
    database_url = os.getenv("MATERIALIZATION_REPOSITORY_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("disposable lifecycle-v2 PostgreSQL URL is not configured")
    conn = psycopg.connect(database_url, autocommit=True, row_factory=dict_row)
    event_id = str(uuid.uuid4())
    source_event_id = f"phase2-finalizer:{event_id}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events (
                id, source_event_id, event_type, camera_id, source_id,
                event_ts_ms, frame_uuid, clip_required, payload
            ) VALUES (
                %(event_id)s::uuid, %(source_event_id)s, 'intrusion',
                'camera-phase2', 'source-phase2', 1783828800000,
                'event-frame', true,
                jsonb_build_object(
                    'runtime_epoch_id', 'epoch-phase2',
                    'media', jsonb_build_object(
                        'event_frame_pts', 100000000000,
                        'replay_job_id', 'job-phase2',
                        'replay_job_request', jsonb_build_object(
                            'configuration', jsonb_build_object(
                                'labels', jsonb_build_object(
                                    'event_id', %(event_id)s,
                                    'requested_start_pts', '95000000000',
                                    'requested_end_pts', '105000000000',
                                    'event_frame_uuid', 'event-frame',
                                    'event_frame_pts', '100000000000',
                                    'runtime_epoch_id', 'epoch-phase2'
                                )
                            )
                        )
                    )
                )
            )
            """,
            {"event_id": event_id, "source_event_id": source_event_id},
        )
        cur.execute(
            """
            INSERT INTO evidence_tasks (
                task_id, event_id, source_event_id, camera_id, source_id,
                event_type, event_ts_ms, task_type, clip_required,
                status, materialization_status, materialization_phase,
                materialization_phase_updated_at, materialization_owner,
                materialization_ready_at, materialization_deadline_at,
                runtime_epoch_id
            ) VALUES (
                %(task_id)s, %(event_id)s::uuid, %(source_event_id)s,
                'camera-phase2', 'source-phase2', 'intrusion', 1783828800000,
                'snapshot_clip', true, 'materialization_pending',
                'materialization_pending', 'waiting_ready', now(),
                'media_finalizer', now() - interval '1 second',
                now() + interval '1 hour', 'epoch-phase2'
            )
            """,
            {
                "task_id": f"phase2-finalizer-{event_id}",
                "event_id": event_id,
                "source_event_id": source_event_id,
            },
        )
    yield conn, event_id
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM events WHERE id = %(event_id)s::uuid",
            {"event_id": event_id},
        )
    conn.close()


def _metadata_rows() -> list[dict]:
    return [
        {
            "type": "VideoFrame",
            "pts": pts * 1_000_000_000,
            "frame_uuid": "event-frame" if pts == 100 else f"frame-{pts}",
            "objects": [
                {
                    "label": "person",
                    "track_id": 7,
                    "bbox": {"xc": 100, "yc": 100, "width": 40, "height": 80},
                }
            ],
        }
        for pts in range(95, 106)
    ]


def _builder(worker, fake_crop):
    def build(**kwargs: object):
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_clip = output_dir / "raw_clip.mov"
        sink_metadata = output_dir / "sink_metadata.json"
        annotations = output_dir / "annotations.frame_cache.identity.jsonl"
        summary_path = output_dir / "summary.json"
        video_crop = fake_crop(output_video_path=raw_clip)
        sink_metadata.write_text(
            json.dumps(_metadata_rows()[0]) + "\n",
            encoding="utf-8",
        )
        annotations.write_text(
            json.dumps(
                {
                    "frame_uuid": "event-frame",
                    "frame_pts": 100_000_000_000,
                    "objects": _metadata_rows()[0]["objects"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        summary = {
            "schema_version": "2.0-midterm",
            "evidence_topology": "post_savant_replay",
            "annotation_status": "complete",
            "annotation_source": "post_savant_sink_metadata",
            "production_ready": True,
            "sidecar_frame_count": 1,
            "frame_count": 1,
            "object_counts": {"person": 1, "face": 0, "known_face": 0},
            "video_crop": video_crop,
            "time_window": {
                "time_domain_crop_applied": True,
                "requested_start_pts": 95_000_000_000,
                "requested_end_pts": 105_000_000_000,
                "requested_duration_s": 10.0,
            },
            "limitations": [],
        }
        summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")
        return worker._EvidenceBundleView(
            output_dir=output_dir,
            raw_clip_path=raw_clip,
            sink_metadata_path=sink_metadata,
            production_sidecar_path=annotations,
            summary_path=summary_path,
            summary=summary,
        )

    return build


def test_real_finalizer_stages_commits_indexes_and_cleans(
    database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    conn, event_id = database
    worker = _worker()
    sink_root = tmp_path / "sink"
    meta_dir = sink_root / f"event-{event_id}"
    meta_dir.mkdir(parents=True)
    (meta_dir / "video.mov").write_bytes(b"source video")
    metadata_file = meta_dir / "metadata.json"
    metadata_file.write_text(
        "".join(json.dumps(row) + "\n" for row in _metadata_rows()),
        encoding="utf-8",
    )
    evidence_root = tmp_path / "evidence"

    def fake_crop(**kwargs: object) -> dict:
        output = Path(kwargs["output_video_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"raw clip")
        return {
            "measurement_schema_version": "phase2-finalizer-v1",
            "method": "fixture_copy",
            "materialization_mode": "fixture",
            "crop_video_to_time_window": True,
            "input_bytes": 12,
            "input_duration_seconds": 10.0,
            "output_bytes": 8,
            "source_metadata_frame_count": 11,
            "source_metadata_duration_seconds": 10.0,
            "decoded_frame_count": 10,
        }

    monkeypatch.setenv("FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED", "true")
    monkeypatch.setenv("POST_SAVANT_DURATION_GUARD_SLACK_SEC", "1")
    monkeypatch.setenv("EVIDENCE_RUNTIME_EPOCH_STRICT", "false")
    monkeypatch.setenv("EVIDENCE_DB_INDEX_WRITE_ENABLED", "true")
    monkeypatch.setenv("EVIDENCE_DB_INDEX_EXPANDED_ROWS_ENABLED", "true")
    monkeypatch.setattr(worker, "load_native_metadata", lambda _path: _metadata_rows())
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 10.0)
    monkeypatch.setattr(
        worker,
        "build_post_savant_evidence_bundle",
        _builder(worker, fake_crop),
    )

    guard = worker._MaterializationGuard(1)
    permit = worker._HeldMaterializationPermit.acquire(guard, required=True)
    result = worker._finalize_one(
        conn,
        event_id=event_id,
        meta={"job_id": "job-phase2"},
        meta_dir=str(meta_dir),
        metadata_file=str(metadata_file),
        sink_dir=str(sink_root),
        evidence_output_dir=str(evidence_root),
        finalizer_worker_id="finalizer-integration",
        source_id="source-phase2",
        replay_shard_id="default",
        phase_diagnostics={
            "finalizer_started_at": datetime.now(timezone.utc).isoformat()
        },
        guardrails={},
        permit=permit,
        materialization_timeout_s=30,
        cleanup_replay_sink_output_enabled=True,
        cleanup_replay_sink_output_statuses=("ready",),
        schedule_row={},
        materialization_pacer=None,
        scan_stats={"scan_duration_ms": 0, "metadata_files_visited": 1},
    )

    canonical = evidence_root / event_id
    assert result.updated == 1
    assert result.processed is True
    assert result.cleanup_status == "deleted"
    assert guard.snapshot()["active"] == 0
    assert (canonical / "raw_clip.mov").read_bytes() == b"raw clip"
    assert not meta_dir.exists()
    assert not any((evidence_root / ".incoming").rglob("raw_clip.mov"))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT materialization_status, materialization_phase, cleanup_audit,
                   clip_path
            FROM evidence_tasks
            WHERE event_id = %(event_id)s::uuid
            """,
            {"event_id": event_id},
        )
        task = cur.fetchone()
        cur.execute(
            "SELECT count(*) AS count FROM evidence_bundles WHERE event_id = %(event_id)s::uuid",
            {"event_id": event_id},
        )
        bundle_count = cur.fetchone()["count"]
        cur.execute(
            "SELECT media_status, payload FROM events WHERE id = %(event_id)s::uuid",
            {"event_id": event_id},
        )
        event = cur.fetchone()
        cur.execute(
            """
            SELECT (
                et::text LIKE '%%/.incoming/%%'
                OR e::text LIKE '%%/.incoming/%%'
                OR COALESCE(eb::text, '') LIKE '%%/.incoming/%%'
                OR EXISTS (
                    SELECT 1
                    FROM evidence_artifacts ea
                    WHERE ea.event_id = %(event_id)s::uuid
                      AND ea::text LIKE '%%/.incoming/%%'
                )
                OR EXISTS (
                    SELECT 1
                    FROM evidence_frame_timeline eft
                    WHERE eft.event_id = %(event_id)s::uuid
                      AND eft::text LIKE '%%/.incoming/%%'
                )
                OR EXISTS (
                    SELECT 1
                    FROM evidence_overlay_segments eos
                    WHERE eos.event_id = %(event_id)s::uuid
                      AND eos::text LIKE '%%/.incoming/%%'
                )
            ) AS incoming_path_leaked
            FROM evidence_tasks et
            JOIN events e ON e.id = et.event_id
            LEFT JOIN evidence_bundles eb ON eb.event_id = et.event_id
            WHERE et.event_id = %(event_id)s::uuid
            """,
            {"event_id": event_id},
        )
        incoming_path_leaked = cur.fetchone()["incoming_path_leaked"]
    assert task["materialization_status"] == "materialized"
    assert task["materialization_phase"] == "terminal"
    assert task["cleanup_audit"]["sink_output"]["status"] == "deleted"
    assert task["clip_path"] == str(canonical / "raw_clip.mov")
    assert bundle_count == 1
    assert event["media_status"] == "materialized"
    assert event["payload"]["media"]["raw_clip_path"] == str(
        canonical / "raw_clip.mov"
    )
    assert incoming_path_leaked is False
