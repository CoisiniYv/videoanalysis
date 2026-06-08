"""C2 event evidence flow contract checks."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"


def test_media_worker_c2_sink_output_uses_post_savant_bundle_builder(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    worker = _activate_media_worker()
    event_id = "11111111-1111-4111-8111-111111111111"
    sink_dir = _make_sink_output(tmp_path, event_id=event_id)
    evidence_root = tmp_path / "evidence"
    pg_conn = _FakeConnection([_already_not_ready_row(), _event_row(event_id)])
    calls: list[dict[str, Any]] = []

    def fake_builder(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return _fake_builder_result(kwargs["output_dir"])

    monkeypatch.setenv("EVIDENCE_TOPOLOGY", "post_savant_replay")
    monkeypatch.setenv("MAX_FPS", "8/1")
    monkeypatch.setenv("MIN_FPS", "2/1")
    monkeypatch.setattr(worker, "build_post_savant_evidence_bundle", fake_builder)
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 6.0)
    monkeypatch.setattr(
        worker,
        "write_continuous_annotation_bundle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("C2 production flow must not use continuous annotation")
        ),
    )

    updated = worker._process_sink_output(
        pg_conn,
        str(sink_dir),
        set(),
        evidence_output_dir=str(evidence_root),
        p1_raw_clip_finalizer_enabled=False,
    )

    assert updated == 1
    assert len(calls) == 1
    assert calls[0]["input_dir"] == sink_dir / "job-output"
    assert calls[0]["output_dir"] == evidence_root / event_id
    assert calls[0]["copy_video"] is True
    assert calls[0]["trim_sidecar_to_video"] is True
    assert calls[0]["decoded_frame_count_reader"](Path("unused")) == 2
    assert calls[0]["max_fps"] == "8/1"
    assert calls[0]["min_fps"] == "2/1"


def test_c2_media_worker_bundle_metadata_has_standard_sidecar_policy(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    worker = _activate_media_worker()
    event_id = "22222222-2222-4222-8222-222222222222"
    sink_dir = _make_sink_output(tmp_path, event_id=event_id)
    evidence_root = tmp_path / "evidence"
    pg_conn = _FakeConnection([_event_row(event_id)])

    monkeypatch.setenv("EVIDENCE_TOPOLOGY", "post_savant_replay")
    monkeypatch.setattr(
        worker,
        "build_post_savant_evidence_bundle",
        lambda **kwargs: _fake_builder_result(kwargs["output_dir"]),
    )
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 6.0)

    bundle = worker._finalize_c2_post_savant_evidence_bundle(
        pg_conn,
        event_id=event_id,
        meta_dir=str(sink_dir / "job-output"),
        metadata_file=str(sink_dir / "job-output" / "metadata.json"),
        evidence_output_dir=str(evidence_root),
    )

    metadata = json.loads(Path(bundle["metadata"]).read_text(encoding="utf-8"))
    assert Path(bundle["raw_clip"]).name == "raw_clip.mov"
    assert Path(bundle["sink_metadata"]).name == "sink_metadata.json"
    assert Path(bundle["annotations_jsonl"]).name == "annotations.frame_cache.identity.jsonl"
    assert Path(bundle["summary"]).name == "summary.json"
    assert not (Path(bundle["evidence_dir"]) / "annotations.jsonl").exists()
    assert metadata["evidence_topology"] == "post_savant_replay"
    assert metadata["annotations"]["annotation_source"] == "post_savant_sink_metadata"
    assert metadata["annotations"]["annotation_source_kind"] == "production_sidecar"
    assert metadata["annotations"]["production_ready"] is True
    assert metadata["annotations"]["legacy_used_for_visual_binding"] is False
    assert metadata["annotations"]["known_face_count"] == 0
    assert metadata["annotations"]["face_count"] == 2
    assert metadata["annotations"]["person_count"] == 3
    assert metadata["status"]["clip_status"] == "ready"


def test_c2_finalizer_records_source_observation_limitation_without_known_face(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    worker = _activate_media_worker()
    event_id = "33333333-3333-4333-8333-333333333333"
    sink_dir = _make_sink_output(tmp_path, event_id=event_id)
    pg_conn = _FakeConnection([_event_row(event_id)])

    monkeypatch.setattr(
        worker,
        "build_post_savant_evidence_bundle",
        lambda **kwargs: _fake_builder_result(kwargs["output_dir"]),
    )

    bundle = worker._finalize_c2_post_savant_evidence_bundle(
        pg_conn,
        event_id=event_id,
        meta_dir=str(sink_dir / "job-output"),
        metadata_file=str(sink_dir / "job-output" / "metadata.json"),
        evidence_output_dir=str(tmp_path / "evidence"),
    )

    metadata = json.loads(Path(bundle["metadata"]).read_text(encoding="utf-8"))
    assert bundle["known_face_count"] == 0
    assert metadata["annotations"]["known_face_count"] == 0
    assert "source_observation_id_missing_in_native_metadata" in metadata["limitations"]
    assert (
        "watchlist_trigger_identity_binding_not_verified_in_c2_2r"
        in metadata["limitations"]
    )


def test_c1_media_worker_path_is_default_when_c2_topology_not_set(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    worker = _activate_media_worker()
    event_id = "44444444-4444-4444-8444-444444444444"
    sink_dir = _make_sink_output(tmp_path, event_id=event_id)
    pg_conn = _FakeConnection([_already_not_ready_row()])
    p1_calls: list[dict[str, Any]] = []

    def fake_p1_finalizer(*args: Any, **kwargs: Any) -> dict[str, Any]:
        p1_calls.append(kwargs)
        output_dir = Path(kwargs["evidence_output_dir"]) / kwargs["event_id"]
        output_dir.mkdir(parents=True)
        return _p1_bundle(output_dir)

    monkeypatch.delenv("EVIDENCE_TOPOLOGY", raising=False)
    monkeypatch.setattr(
        worker,
        "_finalize_c2_post_savant_evidence_bundle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("C2 finalizer should be opt-in only")
        ),
    )
    monkeypatch.setattr(worker, "_finalize_p1_evidence_bundle", fake_p1_finalizer)
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 6.0)

    updated = worker._process_sink_output(
        pg_conn,
        str(sink_dir),
        set(),
        evidence_output_dir=str(tmp_path / "evidence"),
        p1_raw_clip_finalizer_enabled=True,
    )

    assert updated == 1
    assert len(p1_calls) == 1
    assert p1_calls[0]["event_id"] == event_id


def test_c2_media_worker_waits_for_stable_sink_output(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    worker = _activate_media_worker()
    event_id = "55555555-5555-4555-8555-555555555555"
    sink_dir = _make_sink_output(tmp_path, event_id=event_id)
    finalizer_calls: list[str] = []

    monkeypatch.setenv("EVIDENCE_TOPOLOGY", "post_savant_replay")
    monkeypatch.setattr(
        worker,
        "_is_already_ready",
        lambda _pg_conn, _event_id: False,
    )
    monkeypatch.setattr(
        worker,
        "_finalize_c2_post_savant_evidence_bundle",
        lambda _pg_conn, **kwargs: finalizer_calls.append(kwargs["event_id"])
        or _p1_bundle(tmp_path / "evidence" / kwargs["event_id"]),
    )
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 6.0)

    candidate_dirs: dict[str, tuple[int, int]] = {}
    processed_dirs: set[str] = set()
    first = worker._process_sink_output(
        _FakeConnection([]),
        str(sink_dir),
        processed_dirs,
        evidence_output_dir=str(tmp_path / "evidence"),
        candidate_dirs=candidate_dirs,
        p1_sink_stability_checks=1,
    )
    second = worker._process_sink_output(
        _FakeConnection([]),
        str(sink_dir),
        processed_dirs,
        evidence_output_dir=str(tmp_path / "evidence"),
        candidate_dirs=candidate_dirs,
        p1_sink_stability_checks=1,
    )

    assert first == 0
    assert second == 1
    assert finalizer_calls == [event_id]


def test_c2_media_worker_waits_for_decodable_sink_video(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    worker = _activate_media_worker()
    event_id = "66666666-6666-4666-8666-666666666666"
    sink_dir = _make_sink_output(tmp_path, event_id=event_id)
    finalizer_calls: list[str] = []

    monkeypatch.setenv("EVIDENCE_TOPOLOGY", "post_savant_replay")
    monkeypatch.setattr(
        worker,
        "_is_already_ready",
        lambda _pg_conn, _event_id: False,
    )
    monkeypatch.setattr(
        worker,
        "_finalize_c2_post_savant_evidence_bundle",
        lambda _pg_conn, **kwargs: finalizer_calls.append(kwargs["event_id"])
        or _p1_bundle(tmp_path / "evidence" / kwargs["event_id"]),
    )
    durations = iter([None, 6.0])
    monkeypatch.setattr(
        worker,
        "_probe_video_duration_seconds",
        lambda _path: next(durations),
    )

    candidate_dirs = {str(sink_dir / "job-output"): (10, 1)}
    processed_dirs: set[str] = set()
    first = worker._process_sink_output(
        _FakeConnection([]),
        str(sink_dir),
        processed_dirs,
        evidence_output_dir=str(tmp_path / "evidence"),
        candidate_dirs=candidate_dirs,
        p1_sink_stability_checks=1,
    )
    second = worker._process_sink_output(
        _FakeConnection([]),
        str(sink_dir),
        processed_dirs,
        evidence_output_dir=str(tmp_path / "evidence"),
        candidate_dirs=candidate_dirs,
        p1_sink_stability_checks=1,
    )

    assert first == 0
    assert second == 1
    assert finalizer_calls == [event_id]


def test_clip_worker_still_uses_record_request_source_id_as_replay_stream() -> None:
    clip_worker = _activate_clip_worker()
    request = {
        "source_event_id": "evt-1",
        "strategy": "savant_replay",
        "source_id": "c2_post_savant_fps_probe",
    }

    assert clip_worker._request_identity(request) == "evt-1:savant_replay"
    assert request["source_id"] == "c2_post_savant_fps_probe"


def _activate_media_worker() -> Any:
    return _activate_service_module(MEDIA_WORKER_ROOT, "app.worker")


def _activate_clip_worker() -> Any:
    return _activate_service_module(ROOT / "services" / "clip-worker", "app.worker")


def _activate_service_module(service_root: Path, module_name: str) -> Any:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    service_root_text = str(service_root)
    if service_root_text in sys.path:
        sys.path.remove(service_root_text)
    sys.path.insert(0, service_root_text)
    return importlib.import_module(module_name)


def _make_sink_output(tmp_path: Path, *, event_id: str) -> Path:
    sink_root = tmp_path / "sink"
    output_dir = sink_root / "job-output"
    output_dir.mkdir(parents=True)
    (output_dir / "video.mov").write_bytes(b"fake video")
    (output_dir / "metadata.json").write_text(
        "".join(
            json.dumps(_frame(event_id=event_id, index=index), sort_keys=True) + "\n"
            for index in range(2)
        )
        + json.dumps(
            {
                "schema": "EndOfStream",
                "source_id": f"replay-event-{event_id}",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return sink_root


def _frame(*, event_id: str, index: int) -> dict[str, Any]:
    return {
        "type": "VideoFrame",
        "source_id": f"replay-event-{event_id}",
        "job_id": "job-c2",
        "labels": {"event_id": event_id},
        "uuid": f"frame-{index}",
        "pts": 1_000_000 + index * 41_666_667,
        "width": 1920,
        "height": 1080,
        "objects": [
            {
                "namespace": "yolo26_pose",
                "label": "person",
                "id": 100 + index,
                "confidence": 0.82,
                "track_id": index,
                "track_box": {
                    "xc": 100.0,
                    "yc": 160.0,
                    "width": 40.0,
                    "height": 100.0,
                    "angle": 0.0,
                },
            }
        ],
    }


def _fake_builder_result(output_dir: Path) -> SimpleNamespace:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_clip = output_dir / "raw_clip.mov"
    sink_metadata = output_dir / "sink_metadata.json"
    sidecar = output_dir / "annotations.frame_cache.identity.jsonl"
    summary = output_dir / "summary.json"
    sidecar_summary = output_dir / "summary.frame_cache.identity.json"
    raw_clip.write_bytes(b"fake c2 video")
    sink_metadata.write_text("{}\n", encoding="utf-8")
    sidecar.write_text(json.dumps({"frame_index": 0, "objects": []}) + "\n", encoding="utf-8")
    payload = _c2_summary()
    summary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    sidecar_summary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return SimpleNamespace(
        input_dir=Path("unused"),
        output_dir=output_dir,
        raw_clip_path=raw_clip,
        sink_metadata_path=sink_metadata,
        production_sidecar_path=sidecar,
        sidecar_summary_path=sidecar_summary,
        summary_path=summary,
        summary=payload,
        report={"production_ready": True},
    )


def _c2_summary() -> dict[str, Any]:
    return {
        "schema_version": "2.0-c2",
        "evidence_topology": "post_savant_replay",
        "annotation_status": "complete",
        "annotation_source": "post_savant_sink_metadata",
        "production_ready": True,
        "visual_evidence_status": "verified_same_stream_metadata",
        "legacy_fallback_allowed": False,
        "legacy_used_for_visual_binding": False,
        "frame_count": 2,
        "sidecar_frame_count": 2,
        "original_metadata_frame_count": 2,
        "decoded_video_frame_count": 2,
        "timeline_reconciliation_status": "frame_counts_match",
        "object_counts": {"person": 3, "face": 2, "known_face": 0},
        "limitations": [
            "source_observation_id_missing_in_native_metadata",
            "watchlist_trigger_identity_binding_not_verified_in_c2_2r",
        ],
    }


def _p1_bundle(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_clip = output_dir / "raw_clip.mov"
    metadata = output_dir / "metadata.json"
    sink_metadata = output_dir / "sink_metadata.json"
    event_annotation = output_dir / "event_annotation.json"
    annotations = output_dir / "annotations.jsonl"
    summary = output_dir / "summary.json"
    for path in (raw_clip, metadata, sink_metadata, event_annotation, annotations, summary):
        path.write_text("{}\n", encoding="utf-8")
    return {
        "evidence_dir": str(output_dir),
        "raw_clip": str(raw_clip),
        "metadata": str(metadata),
        "sink_metadata": str(sink_metadata),
        "event_annotation": str(event_annotation),
        "annotations_jsonl": str(annotations),
        "summary": str(summary),
        "clip_status": "generated_unverified",
        "annotation_status": "complete",
        "annotation_lines": 1,
    }


def _already_not_ready_row() -> tuple[str, str, str]:
    return ("pending", "", "")


def _event_row(event_id: str) -> tuple[Any, ...]:
    return (
        "watchlist_hit",
        "cam-c2",
        "c2_post_savant_fps_probe",
        "41",
        1_000_000_000,
        "frame-1",
        {
            "media": {
                "replay_job_id": "job-c2",
                "replay_job_request": {
                    "anchor_keyframe": "kf-1",
                    "offset": {"seconds": 5},
                    "stop_condition": {"frame_count": 240},
                    "configuration": {
                        "stored_stream_id": "c2_post_savant_fps_probe",
                        "resulting_stream_id": f"replay-event-{event_id}",
                    },
                },
            }
        },
        0.91,
        "source-event-c2",
        "kf-1",
        "high",
        {},
        None,
    )


class _FakeConnection:
    def __init__(self, rows: list[Any]) -> None:
        self.cursor_obj = _FakeCursor(rows)

    def cursor(self) -> "_FakeCursor":
        return self.cursor_obj


class _FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = list(rows)
        self.rowcount = 1
        self.executed: list[tuple[str, dict[str, Any] | tuple[Any, ...] | None]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(
        self,
        sql: str,
        params: dict[str, Any] | tuple[Any, ...] | None = None,
    ) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if not self.rows:
            return None
        return self.rows.pop(0)
