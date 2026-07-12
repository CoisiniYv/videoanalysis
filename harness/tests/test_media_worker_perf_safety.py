"""Media-worker performance safety tests for midterm runtime."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
EVENT_ID = "11111111-1111-4111-8111-111111111111"
CURRENT_EPOCH = "midterm-20260612T010203Z-a1b2c3d4"
OLD_EPOCH = "midterm-20260612T000000Z-00aa11bb"


def _activate(service: str, module_name: str):
    service_root = str(REPO_ROOT / "services" / service)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if service_root in sys.path:
        sys.path.remove(service_root)
    sys.path.insert(0, service_root)
    return importlib.import_module(module_name)


def test_processed_sink_dirs_survive_restart(tmp_path: Path) -> None:
    worker = _activate("media-worker", "app.worker")
    root = tmp_path / "replay-sink-output" / "midterm" / "epochs" / CURRENT_EPOCH
    sink_dir = root / f"replay-event-{EVENT_ID}-00000000"
    sink_dir.mkdir(parents=True)
    (sink_dir / "metadata.json").write_text(
        json.dumps({"labels": {"event_id": EVENT_ID}, "source_id": "source-1"})
        + "\n",
        encoding="utf-8",
    )
    (sink_dir / "clip.mov").write_bytes(b"video")

    state_path = tmp_path / "media-worker-state.json"
    worker._save_processed_sink_state(state_path, {str(sink_dir)})
    processed_dirs: set[str] = set()

    updated = worker._process_sink_output(
        object(),
        str(root),
        processed_dirs,
        processed_state_path=state_path,
    )

    assert updated == 0
    assert processed_dirs == {str(sink_dir)}


def test_active_epoch_scan_uses_incremental_children_and_ignores_old_epoch(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    worker = _activate("media-worker", "app.worker")
    root = tmp_path / "replay-sink-output" / "midterm"
    state_path = root / ".current_epoch.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"runtime_epoch_id": CURRENT_EPOCH}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RUNTIME_EPOCH_STATE_PATH", str(state_path))

    current_dir = root / "epochs" / CURRENT_EPOCH / f"replay-event-{EVENT_ID}"
    old_dir = root / "epochs" / OLD_EPOCH / f"replay-event-{EVENT_ID}"
    for path, source_id in ((current_dir, "current-source"), (old_dir, "old-source")):
        path.mkdir(parents=True)
        (path / "metadata.json").write_text(
            json.dumps({"labels": {"event_id": EVENT_ID}, "source_id": source_id})
            + "\n",
            encoding="utf-8",
        )

    active_root = worker._active_epoch_sink_output_dir(str(root))
    rows, stats = worker._scan_metadata_files(active_root, processed_dirs=set())

    assert [row["source_id"] for row in rows] == ["current-source"]
    assert stats["scan_mode"] == "active_epoch_incremental"
    assert stats["rglob_fallback_used"] is False
    assert stats["metadata_files_visited"] == 1


def test_metadata_scan_limit_prefers_newest_candidates(tmp_path: Path) -> None:
    worker = _activate("media-worker", "app.worker")
    root = tmp_path / "sink"
    old_dir = root / "old"
    new_dir = root / "new"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    old_meta = old_dir / "metadata.json"
    new_meta = new_dir / "metadata.json"
    old_meta.write_text(
        json.dumps({"labels": {"event_id": EVENT_ID}, "source_id": "old-source"})
        + "\n",
        encoding="utf-8",
    )
    new_meta.write_text(
        json.dumps({"labels": {"event_id": EVENT_ID}, "source_id": "new-source"})
        + "\n",
        encoding="utf-8",
    )
    old_ns = 1_800_000_000_000_000_000
    new_ns = old_ns + 10_000_000_000
    old_meta.touch()
    new_meta.touch()
    old_dir.touch()
    new_dir.touch()
    os.utime(old_meta, ns=(old_ns, old_ns))
    os.utime(new_meta, ns=(new_ns, new_ns))
    os.utime(old_dir, ns=(old_ns, old_ns))
    os.utime(new_dir, ns=(new_ns, new_ns))

    rows, stats = worker._scan_metadata_files(
        str(root),
        processed_dirs=set(),
        max_metadata_files=1,
    )

    assert [row["source_id"] for row in rows] == ["new-source"]
    assert stats["metadata_files_truncated"] is True
    assert stats["metadata_files_visited"] == 1


def test_frame_cache_dropped_debug_sidecar_is_opt_in() -> None:
    policy = _activate("media-worker", "app.production_sidecar_policy")

    assert policy.load_frame_cache_sidecar_config({})["write_dropped_debug_sidecar"] is False
    assert (
        policy.load_frame_cache_sidecar_config(
            {"FRAME_CACHE_WRITE_DROPPED_DEBUG_SIDECAR": "true"}
        )["write_dropped_debug_sidecar"]
        is True
    )


def test_success_prune_removes_frame_cache_dropped_debug_sidecar(tmp_path: Path) -> None:
    worker = _activate("media-worker", "app.worker")
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    raw_clip = bundle_dir / "raw_clip.mov"
    raw_clip.write_bytes(b"video")
    debug_sidecar = bundle_dir / "annotations.frame_cache.identity.dropped.debug.jsonl"
    debug_sidecar.write_text("{}\n", encoding="utf-8")
    production_sidecar = bundle_dir / "annotations.frame_cache.identity.jsonl"
    production_sidecar.write_text("{}\n", encoding="utf-8")

    result = worker._prune_success_evidence_sidecars(bundle_dir)

    assert raw_clip.is_file()
    assert not debug_sidecar.exists()
    assert not production_sidecar.exists()
    assert sorted(result["deleted"]) == [
        "annotations.frame_cache.identity.dropped.debug.jsonl",
        "annotations.frame_cache.identity.jsonl",
    ]
    assert result["errors"] == 0


def test_evidence_db_index_expanded_rows_can_be_disabled(monkeypatch: Any) -> None:
    worker = _activate("media-worker", "app.worker")

    monkeypatch.setenv("EVIDENCE_DB_INDEX_EXPANDED_ROWS_ENABLED", "false")
    assert worker._evidence_db_index_expanded_rows_enabled() is False

    monkeypatch.setenv("EVIDENCE_DB_INDEX_EXPANDED_ROWS_ENABLED", "true")
    assert worker._evidence_db_index_expanded_rows_enabled() is True


def test_rolling_cache_finalizer_uses_metadata_frame_count_and_duration(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    worker = _activate("media-worker", "app.worker")
    event_id = "22222222-2222-4222-8222-222222222222"
    sink_dir = tmp_path / "rolling-cache-materialized" / event_id
    sink_dir.mkdir(parents=True)
    video_path = sink_dir / "video.mov"
    video_path.write_bytes(b"raw clip bytes")
    video_stat = video_path.stat()
    metadata_file = sink_dir / "metadata.json"
    metadata_file.write_text(
        json.dumps(
            {
                "job_id": f"rolling-cache-event-{event_id}",
                "rolling_cache": {
                    "selected_frame_count": 42,
                    "output_duration_s": 1.75,
                    "immutable_probe": {
                        "schema_version": "rolling-cache-immutable-probe-v1",
                        "status": "ready",
                        "duration_s": 1.75,
                        "identity": {
                            "device": video_stat.st_dev,
                            "inode": video_stat.st_ino,
                            "size": video_stat.st_size,
                            "mtime_ns": video_stat.st_mtime_ns,
                        },
                    },
                    "time_domain_crop_applied": True,
                    "actual_start_pts": 1_000_000_000,
                    "actual_end_pts": 2_750_000_000,
                    "requested_start_pts": 1_000_000_000,
                    "requested_end_pts": 2_750_000_000,
                },
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "evidence"
    captured: dict[str, object] = {}

    def fake_build_post_savant_evidence_bundle(**kwargs: object):
        captured.update(kwargs)
        reader = kwargs["decoded_frame_count_reader"]
        assert callable(reader)
        assert reader(Path("unused.mov")) == 42
        assert kwargs["decoded_video_duration_s"] == 1.75
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_clip = output_dir / "raw_clip.mov"
        raw_clip.write_bytes(b"raw clip bytes")
        sink_metadata = output_dir / "sink_metadata.json"
        sink_metadata.write_text("{}\n", encoding="utf-8")
        annotations = output_dir / "annotations.frame_cache.identity.jsonl"
        annotations.write_text("{}\n", encoding="utf-8")
        summary_path = output_dir / "summary.json"
        summary = {
            "annotation_status": "complete",
            "annotation_source": "frame_annotation_cache",
            "sidecar_frame_count": 42,
            "decoded_video_frame_count": 42,
            "raw_clip_duration": 1.75,
            "time_window": {
                "requested_duration_s": 1.75,
                "actual_start_pts": 1_000_000_000,
                "actual_end_pts": 2_750_000_000,
            },
            "object_counts": {"person": 1, "face": 0, "known_face": 0},
            "production_ready": True,
            "runtime_epoch_id": CURRENT_EPOCH,
            "video_crop": {"materialization_mode": "rolling_cache_copy"},
        }
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        return worker._EvidenceBundleView(
            output_dir=output_dir,
            raw_clip_path=raw_clip,
            sink_metadata_path=sink_metadata,
            production_sidecar_path=annotations,
            summary_path=summary_path,
            summary=summary,
        )

    monkeypatch.setenv("EVIDENCE_RUNTIME_EPOCH_STRICT", "false")
    monkeypatch.setattr(
        worker,
        "_load_event_context",
        lambda _conn, _event_id: {
            "event_id": event_id,
            "source_event_id": "source-event",
            "event_type": "intrusion",
            "camera_id": "camera-1",
            "source_id": "source-1",
            "created_at": "2026-06-12T01:00:00Z",
            "event_ts_ms": 1_000,
            "payload": {"media": {}},
        },
    )
    monkeypatch.setattr(
        worker,
        "load_native_metadata",
        lambda _path: [
            {
                "type": "VideoFrame",
                "frame_uuid": "frame-1",
                "frame_pts": 1_000_000_000,
                "duration": 41_666_666,
                "objects": [{"label": "person"}],
            }
        ],
    )
    monkeypatch.setattr(
        worker,
        "read_decoded_video_frame_count",
        lambda _path: (_ for _ in ()).throw(AssertionError("ffprobe frame count path should not run")),
    )
    probed_paths: list[str] = []

    def fake_probe_duration(path: str) -> float:
        probed_paths.append(path)
        return 1.75

    monkeypatch.setattr(worker, "_probe_video_duration_seconds", fake_probe_duration)
    monkeypatch.setattr(
        worker,
        "build_post_savant_evidence_bundle",
        fake_build_post_savant_evidence_bundle,
    )

    bundle = worker._finalize_post_savant_evidence_bundle(
        object(),
        event_id=event_id,
        meta_dir=str(sink_dir),
        metadata_file=str(metadata_file),
        evidence_output_dir=str(output_root),
    )

    assert captured["decoded_video_duration_s"] == 1.75
    assert probed_paths == []
    assert bundle["raw_clip"].endswith("raw_clip.mov")
    assert bundle["annotation_lines"] == 42


def test_rolling_cache_known_frame_count_skips_video_decode(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    worker = _activate("media-worker", "app.worker")
    raw_clip = tmp_path / "raw_clip.mov"
    raw_clip.write_bytes(b"video")
    monkeypatch.setattr(
        worker,
        "read_decoded_video_frame_count",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("known rolling-cache frame count must skip decode")
        ),
    )

    count, duration_ms = worker._decoded_frame_count_with_timing(
        raw_clip,
        raw_clip_available=True,
        known_frame_count=240,
    )

    assert count == 240
    assert duration_ms == 0


def test_rolling_cache_ready_check_uses_known_duration_without_ffprobe(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    worker = _activate("media-worker", "app.worker")
    video = tmp_path / "video.mov"
    metadata = tmp_path / "metadata.json"
    video.write_bytes(b"video")
    metadata.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(
        worker,
        "_probe_video_duration_seconds",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("ready check should trust rolling-cache duration")
        ),
    )

    ready, reason = worker._sink_output_ready_for_finalizer(
        video_file=str(video),
        metadata_file=str(metadata),
        known_duration_s=2.5,
    )
    assert ready is True
    assert reason == "ready"


def test_rolling_cache_candidates_do_not_claim_before_ready_at() -> None:
    worker = _activate("media-worker", "app.worker")

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, params: dict[str, Any]) -> None:
            self.sql = sql
            self.params = params

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    class _Conn:
        def __init__(self) -> None:
            self.cursor_obj = _Cursor()

        def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
            return self.cursor_obj

    conn = _Conn()
    cfg = SimpleNamespace(
        rolling_cache_sources=("source-1",),
        rolling_cache_materialization_max_per_poll=16,
        rolling_cache_segment_seconds=4,
        rolling_cache_materialization_ready_segment_grace_seconds=1.0,
    )

    assert worker._rolling_cache_candidate_tasks(conn, cfg) == []
    assert "rolling_cache_ready_at <= now()" in conn.cursor_obj.sql
    assert "et.materialization_ready_at <= now()" in conn.cursor_obj.sql
    assert "segment_ready_delay_s" not in conn.cursor_obj.params
    assert worker.ROLLING_CACHE_TASK_STATUSES == (
        "manifest_ready",
        "materialization_pending",
    )


def test_rolling_cache_coverage_miss_uses_pending_not_terminal_deferred() -> None:
    worker = _activate("media-worker", "app.worker")

    assert "materialization_pending" in worker.ROLLING_CACHE_TASK_STATUSES
    assert "materialization_deferred" not in worker.ROLLING_CACHE_TASK_STATUSES


def test_rolling_cache_claim_preserves_existing_processing_deadline() -> None:
    worker = _activate("media-worker", "app.worker")

    class _Cursor:
        rowcount = 1

        def __init__(self) -> None:
            self.executions: list[tuple[str, dict[str, Any]]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, params: dict[str, Any]) -> None:
            self.sql = sql
            self.params = params
            self.executions.append((sql, params))

        def fetchall(self) -> list[tuple[object, ...]]:
            return []

        def fetchone(self) -> tuple[int]:
            return (1,)

    class _Conn:
        def __init__(self) -> None:
            self.cursor_obj = _Cursor()

        def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
            return self.cursor_obj

    conn = _Conn()
    ready_at = "2026-07-06T01:02:03+00:00"

    lease = worker._claim_rolling_cache_task(
        conn,
        event_id=EVENT_ID,
        ready_at=ready_at,
        processing_deadline_s=45.0,
    )
    assert lease is not None
    task_query, task_params = next(
        execution
        for execution in conn.cursor_obj.executions
        if "UPDATE evidence_tasks" in execution[0]
    )
    assert "materialization_ready_at =" not in task_query
    assert "materialization_deadline_at =" not in task_query
    assert "ready_at" not in task_params
    assert "processing_deadline_s" not in task_params


def test_rolling_cache_retry_preserves_ready_at_and_sets_next_attempt() -> None:
    worker = _activate("media-worker", "app.worker")

    class _Cursor:
        rowcount = 1

        def __init__(self) -> None:
            self.executions: list[tuple[str, dict[str, Any]]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, params: dict[str, Any]) -> None:
            self.sql = sql
            self.params = params
            self.executions.append((sql, params))

    class _Conn:
        def __init__(self) -> None:
            self.cursor_obj = _Cursor()

        def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
            return self.cursor_obj

    conn = _Conn()
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="media-worker:test",
        token="lease-token",
        generation=1,
        phase=worker.MaterializationPhase.REMUX_RUNNING.value,
    )
    assert worker._defer_rolling_cache_task(
        conn,
        event_id=EVENT_ID,
        reason="rolling_cache_requested_window_not_fully_covered:post_gap_ns=100",
        retry_after_s=1.5,
        lease=lease,
    )

    task_query, task_params = next(
        execution
        for execution in conn.cursor_obj.executions
        if "materialization_next_attempt_at" in execution[0]
    )
    assert "materialization_ready_at =" not in task_query
    assert task_params["delay_s"] >= 1.5
    assert task_params["reason_code"] == "coverage_not_complete"


def test_global_rolling_recovery_expires_deadline_miss() -> None:
    worker = _activate("media-worker", "app.worker")

    class _Cursor:
        rowcount = 2

        def __init__(self) -> None:
            self.executions: list[tuple[str, dict[str, Any]]] = []
            self.sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, params: dict[str, Any]) -> None:
            self.sql = sql
            self.params = params
            self.executions.append((sql, params))

        def fetchall(self) -> list[tuple[str]]:
            if "information_schema.columns" in self.sql:
                return []
            if "RETURNING event_id" in self.sql:
                return [(EVENT_ID,), ("22222222-2222-4222-8222-222222222222",)]
            return []

    class _Conn:
        def __init__(self) -> None:
            self.cursor_obj = _Cursor()

        def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
            return self.cursor_obj

    conn = _Conn()
    cfg = SimpleNamespace(rolling_cache_sources=("source-1",))

    assert worker._recover_rolling_cache_lifecycle(conn, cfg) == 2
    task_query, task_params = next(
        execution
        for execution in conn.cursor_obj.executions
        if "UPDATE evidence_tasks" in execution[0]
    )
    assert "SET status = 'materialization_expired'" in task_query
    assert "materialization_failure_reason = NULL" in task_query
    assert "materialization_expired_reason = 'business_deadline_expired'" in task_query
    assert task_params["sources_empty"] is True
    assert task_params["sources"] == []


def test_rolling_cache_coverage_retry_waits_for_observed_gap() -> None:
    worker = _activate("media-worker", "app.worker")

    retry_after_s = worker._rolling_cache_coverage_retry_after_s(
        RuntimeError("rolling_cache_requested_window_not_fully_covered:post_gap_ns=1873500104")
    )
    assert abs(retry_after_s - 2.873500104) < 0.000001
    assert worker._rolling_cache_coverage_retry_after_s(RuntimeError("no_overlapping_segments")) == 2.0


def test_known_sink_duration_requires_matching_immutable_probe(tmp_path: Path) -> None:
    worker = _activate("media-worker", "app.worker")
    video_path = tmp_path / "video.mov"
    video_path.write_bytes(b"video")
    stat = video_path.stat()
    metadata = {
        "rolling_cache": {
            "output_duration_s": "3.25",
            "immutable_probe": {
                "status": "ready",
                "duration_s": "3.25",
                "identity": {
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                },
            },
        }
    }

    assert (
        worker._known_sink_output_duration_seconds(
            metadata,
            video_path,
        )
        == 3.25
    )
    video_path.write_bytes(b"replacement")
    assert worker._known_sink_output_duration_seconds(metadata, video_path) is None
    assert (
        worker._known_sink_output_duration_seconds(
            {"rolling_cache": {"output_duration_s": "3.25"}},
            video_path,
        )
        is None
    )


def test_identity_mismatch_reprobes_once_and_reuses_finalizer_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    worker = _activate("media-worker", "app.worker")
    video_path = tmp_path / "video.mov"
    metadata_path = tmp_path / "metadata.json"
    video_path.write_bytes(b"video")
    metadata_path.write_text("{}\n", encoding="utf-8")
    metadata = {
        "rolling_cache": {
            "immutable_probe": {
                "status": "ready",
                "duration_s": 2.0,
                "identity": {"size": 999, "mtime_ns": 1},
            }
        }
    }
    probes: list[str] = []
    monkeypatch.setattr(
        worker,
        "_probe_video_duration_seconds",
        lambda path: probes.append(path) or 2.5,
    )

    ready, reason = worker._sink_output_ready_for_finalizer(
        video_file=str(video_path),
        metadata_file=str(metadata_path),
        known_duration_s=worker._known_sink_output_duration_seconds(
            metadata,
            video_path,
        ),
        metadata=metadata,
    )
    assert (ready, reason) == (True, "ready")
    assert probes == [str(video_path)]
    assert worker._known_sink_output_duration_seconds(metadata, video_path) == 2.5

    ready, reason = worker._sink_output_ready_for_finalizer(
        video_file=str(video_path),
        metadata_file=str(metadata_path),
        known_duration_s=worker._known_sink_output_duration_seconds(
            metadata,
            video_path,
        ),
        metadata=metadata,
    )
    assert (ready, reason) == (True, "ready")
    assert probes == [str(video_path)]


def test_frame_cache_reader_uses_bounded_stream_range_and_filters_identity() -> None:
    writer = _activate("media-worker", "app.frame_cache_sidecar_writer")

    class FakeRedis:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def xrevrange(
            self,
            name: str,
            max: str = "+",
            min: str = "-",
            count: int | None = None,
        ) -> list[tuple[str, dict[str, str]]]:
            self.calls.append({"name": name, "max": max, "min": min, "count": count})
            return [
                (
                    "1781197210000-0",
                    {"data": json.dumps(_frame_annotation("wrong-session", stream_session_id="old-session"))},
                ),
                (
                    "1781197209000-0",
                    {"data": json.dumps(_frame_annotation("wrong-source", source_id="source-2"))},
                ),
                (
                    "1781197208000-0",
                    {"data": json.dumps(_frame_annotation("wrong-camera", camera_id="camera-2"))},
                ),
                (
                    "1781197207000-0",
                    {"data": json.dumps(_frame_annotation("current-frame"))},
                ),
            ]

    redis = FakeRedis()
    messages, summary = writer._read_frame_annotations(
        redis_client=redis,
        config={
            "stream_name": "security.frame_annotations",
            "lookback_count": 20000,
            "range_count": 5,
            "max_scan": 20000,
            "pre_seconds": 5,
            "post_seconds": 5,
        },
        event={
            "event_id": EVENT_ID,
            "event_type": "intrusion",
            "created_at": "2026-06-12T01:00:00Z",
            "source_id": "source-1",
            "camera_id": "camera-1",
            "frame_uuid": "current-frame",
            "frame_pts": 100_000_000_000,
            "payload": {
                "runtime_epoch_id": CURRENT_EPOCH,
                "stream_session_id": "session-1",
            },
        },
    )

    assert [message["frame_uuid"] for message in messages] == ["current-frame"]
    assert redis.calls == [
        {
            "name": "security.frame_annotations",
            "max": summary["range_max"],
            "min": summary["range_min"],
            "count": 5,
        }
    ]
    assert summary["bounded_range_used"] is True
    assert summary["read_mode"] == "bounded_stream_id_range"
    assert summary["range_max"] != "+"
    assert summary["range_min"] != "-"
    assert summary["stream_session_filter_mode"] == "strict"
    assert summary["messages_filtered_stream_session"] == 1
    assert summary["messages_stream_session_mismatch"] == 0
    assert summary["messages_filtered_source"] == 1
    assert summary["messages_filtered_camera"] == 1
    assert summary["messages_retained"] == 1
    assert summary["pages_read"] == 1
    assert summary["entries_scanned_total"] == 4
    assert summary["stop_reason"] == "range_exhausted"


def test_frame_cache_reader_paginates_bounded_range_until_source_anchor() -> None:
    writer = _activate("media-worker", "app.frame_cache_sidecar_writer")
    event_ms = 1781226000000

    class FakeRedis:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def xrevrange(
            self,
            name: str,
            max: str = "+",
            min: str = "-",
            count: int | None = None,
        ) -> list[tuple[str, dict[str, str]]]:
            self.calls.append({"name": name, "max": max, "min": min, "count": count})
            if len(self.calls) == 1:
                return [
                    (
                        f"{event_ms + 19000}-0",
                        {"data": json.dumps(_frame_annotation("wrong-source-1", source_id="source-2"))},
                    ),
                    (
                        f"{event_ms + 18000}-0",
                        {"data": json.dumps(_frame_annotation("wrong-source-2", source_id="source-3"))},
                    ),
                    (
                        f"{event_ms + 17000}-0",
                        {"data": json.dumps(_frame_annotation("wrong-source-3", source_id="source-4"))},
                    ),
                ]
            return [
                (
                    f"{event_ms + 6000}-0",
                    {
                        "data": json.dumps(
                            _frame_annotation(
                                "post-frame",
                                frame_pts=105_000_000_000,
                                timestamp_ms=event_ms + 6000,
                            )
                        )
                    },
                ),
                (
                    f"{event_ms}-0",
                    {
                        "data": json.dumps(
                            _frame_annotation(
                                "anchor-frame",
                                frame_pts=100_000_000_000,
                                timestamp_ms=event_ms,
                            )
                        )
                    },
                ),
                (
                    f"{event_ms - 6000}-0",
                    {
                        "data": json.dumps(
                            _frame_annotation(
                                "pre-frame",
                                frame_pts=95_000_000_000,
                                timestamp_ms=event_ms - 6000,
                            )
                        )
                    },
                ),
            ]

    redis = FakeRedis()
    messages, summary = writer._read_frame_annotations(
        redis_client=redis,
        config={
            "stream_name": "security.frame_annotations",
            "range_count": 3,
            "max_scan": 9,
            "pre_seconds": 5,
            "post_seconds": 5,
        },
        event={
            "event_id": EVENT_ID,
            "event_type": "intrusion",
            "created_at": "2026-06-12T01:00:00Z",
            "source_id": "source-1",
            "camera_id": "camera-1",
            "frame_uuid": "anchor-frame",
            "frame_pts": 100_000_000_000,
            "payload": {
                "runtime_epoch_id": CURRENT_EPOCH,
                "stream_session_id": "session-1",
            },
        },
    )

    assert [message["frame_uuid"] for message in messages] == [
        "pre-frame",
        "anchor-frame",
        "post-frame",
    ]
    assert len(redis.calls) == 2
    assert redis.calls[0]["count"] == 3
    assert str(redis.calls[1]["max"]).startswith("(")
    assert summary["pages_read"] == 2
    assert summary["entries_scanned_total"] == 6
    assert summary["messages_filtered_source"] == 3
    assert summary["messages_retained"] == 3
    assert summary["anchor_found"] is True
    assert summary["stop_reason"] == "event_window_satisfied"


def test_frame_cache_reader_reuses_bucketed_range_cache() -> None:
    writer = _activate("media-worker", "app.frame_cache_sidecar_writer")
    writer._RANGE_CACHE.clear()
    event_ms = 1781226000000

    class FakeRedis:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def xrevrange(
            self,
            name: str,
            max: str = "+",
            min: str = "-",
            count: int | None = None,
        ) -> list[tuple[str, dict[str, str]]]:
            self.calls.append({"name": name, "max": max, "min": min, "count": count})
            return [
                (
                    f"{event_ms}-0",
                    {
                        "data": json.dumps(
                            _frame_annotation(
                                "source-1-frame",
                                source_id="source-1",
                                camera_id="camera-1",
                                timestamp_ms=event_ms,
                            )
                        )
                    },
                ),
                (
                    f"{event_ms}-1",
                    {
                        "data": json.dumps(
                            _frame_annotation(
                                "source-2-frame",
                                source_id="source-2",
                                camera_id="camera-2",
                                timestamp_ms=event_ms,
                            )
                        )
                    },
                ),
            ]

    redis = FakeRedis()
    config = {
        "stream_name": "security.frame_annotations",
        "range_count": 2,
        "max_scan": 30,
        "pre_seconds": 5,
        "post_seconds": 5,
        "range_cache_bucket_ms": 10000,
        "range_cache_ttl_s": 60,
        "range_cache_max_entries": 4,
    }

    first, first_summary = writer._read_frame_annotations(
        redis_client=redis,
        config=config,
        event={
            "event_id": EVENT_ID,
            "event_type": "intrusion",
            "created_at": "2026-06-12T01:00:00Z",
            "source_id": "source-1",
            "camera_id": "camera-1",
            "frame_uuid": "source-1-frame",
            "frame_pts": 100_000_000_000,
            "payload": {"runtime_epoch_id": CURRENT_EPOCH},
        },
    )
    second, second_summary = writer._read_frame_annotations(
        redis_client=redis,
        config=config,
        event={
            "event_id": EVENT_ID,
            "event_type": "intrusion",
            "created_at": "2026-06-12T01:00:00Z",
            "source_id": "source-2",
            "camera_id": "camera-2",
            "frame_uuid": "source-2-frame",
            "frame_pts": 100_000_000_000,
            "payload": {"runtime_epoch_id": CURRENT_EPOCH},
        },
    )

    assert [item["frame_uuid"] for item in first] == ["source-1-frame"]
    assert [item["frame_uuid"] for item in second] == ["source-2-frame"]
    assert len(redis.calls) == 1
    assert redis.calls[0]["count"] == 30
    assert first_summary["range_cache_hit"] is False
    assert second_summary["range_cache_hit"] is True


def test_frame_cache_reader_event_window_mode_retains_same_source_session_mismatch() -> None:
    writer = _activate("media-worker", "app.frame_cache_sidecar_writer")

    class FakeRedis:
        def xrevrange(
            self,
            _name: str,
            max: str = "+",
            min: str = "-",
            count: int | None = None,
        ) -> list[tuple[str, dict[str, str]]]:
            return [
                (
                    "1781197207000-0",
                    {
                        "data": json.dumps(
                            _frame_annotation(
                                "old-session-frame",
                                frame_pts=99_875_000_000,
                                stream_session_id="old-session",
                            )
                        )
                    },
                ),
                (
                    "1781197208000-0",
                    {"data": json.dumps(_frame_annotation("current-frame"))},
                ),
            ]

    messages, summary = writer._read_frame_annotations(
        redis_client=FakeRedis(),
        config={
            "stream_name": "security.frame_annotations",
            "range_count": 5,
            "stream_session_filter_mode": "event_window",
        },
        event={
            "event_id": EVENT_ID,
            "event_type": "intrusion",
            "created_at": "2026-06-12T01:00:00Z",
            "source_id": "source-1",
            "camera_id": "camera-1",
            "frame_uuid": "current-frame",
            "frame_pts": 100_000_000_000,
            "payload": {
                "runtime_epoch_id": CURRENT_EPOCH,
                "stream_session_id": "session-1",
            },
        },
    )

    assert [message["frame_uuid"] for message in messages] == [
        "old-session-frame",
        "current-frame",
    ]
    assert summary["stream_session_filter_mode"] == "event_window"
    assert summary["stream_session_filter_strict"] is False
    assert summary["messages_filtered_stream_session"] == 0
    assert summary["messages_stream_session_mismatch"] == 1
    assert summary["messages_retained"] == 2


def test_frame_cache_reader_allows_verified_cross_session_post_window() -> None:
    writer = _activate("media-worker", "app.frame_cache_sidecar_writer")

    class FakeRedis:
        def xrevrange(
            self,
            _name: str,
            max: str = "+",
            min: str = "-",
            count: int | None = None,
        ) -> list[tuple[str, dict[str, str]]]:
            return [
                (
                    "1781197210000-0",
                    {
                        "data": json.dumps(
                                _frame_annotation(
                                    "post-window-frame",
                                    frame_pts=105_000_000_000,
                                    stream_session_id="session-2",
                                )
                        )
                    },
                ),
                (
                    "1781197207000-0",
                    {"data": json.dumps(_frame_annotation("start-window-frame"))},
                ),
            ]

    messages, summary = writer._read_frame_annotations(
        redis_client=FakeRedis(),
        config={
            "stream_name": "security.frame_annotations",
            "lookback_count": 20000,
            "range_count": 5,
            "max_scan": 20000,
            "pre_seconds": 5,
            "post_seconds": 5,
        },
        event={
            "event_id": EVENT_ID,
            "event_type": "intrusion",
            "created_at": "2026-06-12T01:00:00Z",
            "source_id": "source-1",
            "camera_id": "camera-1",
            "frame_uuid": "start-window-frame",
            "frame_pts": 100_000_000_000,
            "payload": {
                "runtime_epoch_id": CURRENT_EPOCH,
                "stream_session_id": "session-1",
                "media": {
                    "replay_job_request": {
                        "configuration": {
                            "labels": {
                                "stream_session_id": "session-1",
                                "start_window_stream_session_id": "session-1",
                                "post_window_stream_session_id": "session-2",
                                "post_window_cross_session_proof_used": "true",
                                "frame_domain_session_policy": (
                                    "post_window_cross_session_pts_verified"
                                ),
                            }
                        }
                    }
                },
            },
        },
    )

    assert [message["frame_uuid"] for message in messages] == [
        "start-window-frame",
        "post-window-frame",
    ]
    assert summary["expected_stream_session_id"] == "session-1"
    assert summary["expected_stream_session_ids"] == ["session-1", "session-2"]
    assert summary["messages_filtered_stream_session"] == 0
    assert summary["messages_retained"] == 2


def test_frame_cache_window_uses_effective_start_for_truncated_pre_window() -> None:
    worker = _activate("media-worker", "app.worker")

    window = worker._frame_cache_time_domain_window(
        event_context={
            "frame_pts": 10_000_000_000,
            "payload": {"media": {"requested_start_pts": 5_000_000_000}},
        },
        replay_labels={
            "event_frame_pts": "10000000000",
            "original_requested_start_pts": "5000000000",
            "effective_start_pts": "9250000000",
            "requested_start_pts": "9250000000",
            "requested_end_pts": "15000000000",
            "pre_window_truncated": "true",
            "pre_window_policy": "truncated_to_current_session",
            "requested_pre_window_seconds": "5.0",
            "effective_pre_window_seconds": "0.75",
            "pre_window_truncated_seconds": "4.25",
        },
    )

    assert window["original_requested_start_pts"] == 5_000_000_000
    assert window["effective_start_pts"] == 9_250_000_000
    assert window["requested_start_pts"] == 9_250_000_000
    assert window["requested_end_pts"] == 15_000_000_000
    assert window["requested_duration_s"] == 5.75
    assert window["expected_event_t_s"] == 0.75
    assert window["pre_window_truncated"] is True
    assert window["pre_window_policy"] == "truncated_to_current_session"
    assert window["requested_pre_window_seconds"] == 5.0
    assert window["effective_pre_window_seconds"] == 0.75
    assert window["pre_window_truncated_seconds"] == 4.25


def test_missing_frame_metadata_keeps_playable_clip_degraded_not_failed() -> None:
    worker = _activate("media-worker", "app.worker")

    summary = {
        "production_ready": False,
        "annotation_status": "missing_frame_metadata",
        "duration_guard_status": "passed",
        "duration_guard_failed": False,
        "epoch_guard_status": "passed",
        "epoch_guard_failed": False,
        "sink_window_guard_status": "passed",
        "sink_window_guard_failed": False,
    }

    clip_status = worker._summary_clip_status(summary)

    assert clip_status == "generated_unverified"
    assert worker._evidence_state_for_clip_status(clip_status) == "materialized"


def test_frame_cache_anchor_lag_is_measured_from_latest_exported_pts() -> None:
    worker = _activate("media-worker", "app.worker")

    lag = worker._frame_cache_anchor_lag_seconds(
        {
            "annotation_status": "missing_frame_metadata",
            "trigger_face_row_frame_pts": 20_000_000_000,
            "frame_cache_reader_summary": {"latest_frame_pts": 12_500_000_000},
        }
    )

    assert lag == 7.5


def test_frame_cache_sidecar_retries_when_exporter_is_behind(
    monkeypatch: Any,
) -> None:
    worker = _activate("media-worker", "app.worker")
    calls = []
    sleeps = []

    def fake_writer(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return (
                {
                    "annotation_status": "missing_frame_metadata",
                    "trigger_face_row_frame_pts": 20_000_000_000,
                    "frame_cache_reader_summary": {
                        "latest_frame_pts": 12_000_000_000
                    },
                },
                {"annotations_path": "annotations.jsonl"},
            )
        return (
            {
                "annotation_status": "complete",
                "trigger_face_row_frame_pts": 20_000_000_000,
                "frame_cache_reader_summary": {
                    "latest_frame_pts": 20_000_000_000
                },
            },
            {"annotations_path": "annotations.jsonl"},
        )

    monkeypatch.setattr(worker, "write_frame_cache_identity_sidecar", fake_writer)
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    monkeypatch.setenv("FRAME_CACHE_ANCHOR_WAIT_MAX_S", "30")
    summary, _ = worker._write_frame_cache_sidecar_after_anchor(
        event={"event_id": EVENT_ID, "source_id": "source-1"},
        config={"range_cache_ttl_s": 900.0, "range_cache_max_entries": 64},
    )

    assert len(calls) == 2
    assert sleeps == [12.0]
    assert summary["annotation_status"] == "complete"
    assert summary["annotation_anchor_wait"]["attempts"] == 1
    assert summary["annotation_anchor_wait"]["initial_lag_s"] == 8.0
    assert summary["annotation_anchor_wait"]["range_cache_bypassed_for_retry"] is True
    assert isinstance(summary["sidecar_build_ms"], int)
    assert summary["sidecar_build_ms"] >= 0
    assert calls[0]["config"]["range_cache_ttl_s"] == 900.0
    assert calls[1]["config"]["range_cache_ttl_s"] == 0.0
    assert calls[1]["config"]["range_cache_max_entries"] == 0
    assert calls[1]["config"]["stream_id_range_unbounded"] is True


def test_late_annotation_retry_uses_unbounded_stream_id_lookback() -> None:
    writer = _activate("media-worker", "app.frame_cache_sidecar_writer")

    range_max, range_min, mode = writer._frame_cache_stream_range(
        {"event_ts_ms": 1_700_000_000_000},
        {"stream_id_range_unbounded": True},
    )

    assert (range_max, range_min) == ("+", "-")
    assert mode == "late_annotation_retry_lookback"


def test_person_bbox_db_recovery_uses_exact_timeline_pts() -> None:
    worker = _activate("media-worker", "app.worker")

    rows = worker._build_person_bbox_db_annotation_rows(
        [
            {"frame_uuid": "frame-1", "pts": 10_000_000_000},
            {"frame_uuid": "frame-2", "pts": 10_250_000_000},
        ],
        [
            {
                "source_observation_id": "person-1",
                "track_id": "7",
                "frame_pts": 10_250_000_000,
                "frame_num": 42,
                "person_bbox": [10, 20, 30, 40],
                "person_confidence": 0.9,
            },
            {
                "source_observation_id": "wrong-pts",
                "track_id": "8",
                "frame_pts": 10_260_000_000,
                "frame_num": 43,
                "person_bbox": [1, 2, 3, 4],
                "person_confidence": 0.8,
            },
        ],
    )

    assert len(rows) == 1
    assert rows[0]["clip_frame_index"] == 1
    assert rows[0]["frame_uuid"] == "frame-2"
    assert rows[0]["t_ms"] == 250
    assert rows[0]["objects"][0]["annotation_role"] == "person_context"
    assert rows[0]["objects"][0]["bbox"]["xyxy"] == [10.0, 20.0, 30.0, 40.0]


def test_timeline_reconciliation_unverified_keeps_playable_clip_degraded() -> None:
    worker = _activate("media-worker", "app.worker")

    summary = {
        "production_ready": False,
        "annotation_status": "timeline_reconciliation_unverified",
        "duration_guard_status": "relaxed",
        "duration_guard_failed": False,
        "epoch_guard_status": "relaxed",
        "epoch_guard_failed": False,
        "sink_window_guard_status": "relaxed",
        "sink_window_guard_failed": False,
    }

    clip_status = worker._summary_clip_status(summary)

    assert clip_status == "generated_unverified"
    assert worker._evidence_state_for_clip_status(clip_status) == "materialized"


def _frame_annotation(
    frame_uuid: str,
    *,
    runtime_epoch_id: str = CURRENT_EPOCH,
    stream_session_id: str = "session-1",
    source_id: str = "source-1",
    camera_id: str = "camera-1",
    frame_pts: int = 100_000_000_000,
    timestamp_ms: int = 1781197200000,
) -> dict[str, object]:
    return {
        "message_type": "frame_annotation",
        "source_id": source_id,
        "camera_id": camera_id,
        "frame_uuid": frame_uuid,
        "frame_pts": frame_pts,
        "timestamp_ms": timestamp_ms,
        "runtime_epoch_id": runtime_epoch_id,
        "stream_session_id": stream_session_id,
        "objects": [],
    }


def test_media_worker_does_not_resurrect_epoch_superseded_tasks() -> None:
    source = (REPO_ROOT / "services" / "media-worker" / "app" / "worker.py").read_text(
        encoding="utf-8"
    )

    assert "EPOCH_SUPERSEDED_INCOMPLETE_REASON" in source
    assert "materialization_failure_reason" in source
    assert "superseded_reason" in source


def test_rolling_cache_uses_independent_ready_poll_interval(monkeypatch: Any) -> None:
    config = _activate("media-worker", "app.config")
    worker_source = (
        REPO_ROOT / "services" / "media-worker" / "app" / "worker.py"
    ).read_text(encoding="utf-8")

    monkeypatch.setenv("ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S", "0.5")
    cfg = config.load_config()

    assert cfg.rolling_cache_materialization_poll_interval_s == 0.5
    assert "next_rolling_cache_poll_at" in worker_source
    assert "next_general_poll_at" in worker_source
    assert "runner=rolling_cache_runner" in worker_source
    assert "class _RollingCacheMaterializationRunner" in worker_source
    assert "future.done()" in worker_source


def test_rolling_cache_runtime_errors_are_terminal_not_deferred() -> None:
    source = (REPO_ROOT / "services" / "media-worker" / "app" / "worker.py").read_text(
        encoding="utf-8"
    )
    repository_source = (
        REPO_ROOT
        / "services"
        / "media-worker"
        / "app"
        / "materialization_repository.py"
    ).read_text(encoding="utf-8")

    assert "def _fail_rolling_cache_task" in source
    assert "def _rolling_cache_failure_reason" in source
    assert "return fail_rolling_task(pg_conn, lease, reason=reason)" in source
    assert "status = 'materialization_failed'" in repository_source
    assert "materialization_failure_reason = %(reason_code)s" in repository_source
    assert "reason=_rolling_cache_failure_reason(exc)" in source
