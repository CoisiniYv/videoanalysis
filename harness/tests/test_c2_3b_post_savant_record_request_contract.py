"""C2.3B post-Savant record_request and Replay stream mapping tests."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVENT_WORKER_ROOT = ROOT / "services" / "event-worker"
CLIP_WORKER_ROOT = ROOT / "services" / "clip-worker"
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"


def test_c2_record_request_carries_post_savant_policy_fields() -> None:
    record_request = _activate_service_module(EVENT_WORKER_ROOT, "app.record_request")
    event = _c2_event()

    record = record_request.build_record_request(
        event,
        "11111111-1111-4111-8111-111111111111",
        request_id="c2_3b:req:test",
    )

    assert record is not None
    assert record["request_id"] == "c2_3b:req:test"
    assert record["source_event_id"] == "c2_3b:synthetic:test"
    assert record["source_id"] == "c2_post_savant_fps_probe"
    assert record["replay_source_kind"] == "post_savant"
    assert record["evidence_topology"] == "post_savant"
    assert record["annotation_source_policy"] == "post_savant_sink_metadata_only"
    assert record["allow_db_annotation_fallback"] is False
    assert record["allow_legacy_annotation_fallback"] is False
    assert record["frame_pts"] == 9_721_166_666
    assert record["frame_num"] == 5
    assert record["requested_start_pts"] == 4_721_166_666
    assert record["requested_end_pts"] == 14_721_166_666
    assert record["event_frame_pts"] == 9_721_166_666
    assert record["replay_stop_strategy"] == "event_anchor_pre_seconds_rewind"
    assert record["pre_seconds"] == 3
    assert record["post_seconds"] == 3


def test_c1_record_request_default_path_does_not_get_c2_policy_fields() -> None:
    record_request = _activate_service_module(EVENT_WORKER_ROOT, "app.record_request")
    event = _c2_event()
    event.pop("evidence_policy")
    event["payload"]["media"].pop("evidence_topology")
    event["payload"]["media"].pop("replay_source_kind")

    record = record_request.build_record_request(
        event,
        "22222222-2222-4222-8222-222222222222",
        request_id="c1:req:test",
    )

    assert record is not None
    assert "replay_source_kind" not in record
    assert "annotation_source_policy" not in record
    assert "allow_db_annotation_fallback" not in record
    assert "allow_legacy_annotation_fallback" not in record


def test_clip_worker_replay_labels_preserve_c2_stream_mapping() -> None:
    clip_worker = _activate_service_module(CLIP_WORKER_ROOT, "app.worker")
    req = _c2_record_request()

    labels = clip_worker._replay_job_labels(req["event_id"], req)

    assert labels["event_id"] == req["event_id"]
    assert labels["request_id"] == req["request_id"]
    assert labels["source_event_id"] == req["source_event_id"]
    assert labels["replay_source_kind"] == "post_savant"
    assert labels["evidence_topology"] == "post_savant"
    assert labels["annotation_source_policy"] == "post_savant_sink_metadata_only"
    assert labels["allow_db_annotation_fallback"] == "false"
    assert labels["allow_legacy_annotation_fallback"] == "false"
    assert labels["frame_pts"] == "9721166666"
    assert labels["requested_start_pts"] == "4721166666"
    assert labels["requested_end_pts"] == "14721166666"
    assert labels["event_frame_pts"] == "9721166666"
    assert labels["replay_stop_strategy"] == "event_anchor_pre_seconds_rewind"


def test_replay_job_payload_uses_record_request_source_as_stored_stream() -> None:
    replay_client = _activate_service_module(CLIP_WORKER_ROOT, "app.replay_client")
    req = _c2_record_request()
    labels = _activate_service_module(CLIP_WORKER_ROOT, "app.worker")._replay_job_labels(
        req["event_id"],
        req,
    )

    payload = replay_client.build_job_payload(
        source_id=req["source_id"],
        keyframe_uuid="019ea0ab-8938-7020-9c98-f1558328185c",
        pre_seconds=req["pre_seconds"],
        post_seconds=req["post_seconds"],
        sink_endpoint="dealer+connect:tcp://video-file-sink:6666",
        labels=labels,
        stop_condition_mode="ts_delta_sec",
        fps=24,
        force_constant_cadence=True,
    )

    assert payload["configuration"]["stored_stream_id"] == "c2_post_savant_fps_probe"
    assert payload["configuration"]["send_metadata_only"] is False
    assert payload["configuration"]["labels"]["replay_source_kind"] == "post_savant"
    assert payload["configuration"]["labels"]["annotation_source_policy"] == (
        "post_savant_sink_metadata_only"
    )
    assert payload["sink"]["url"] == "dealer+connect:tcp://video-file-sink:6666"
    assert payload["offset"]["seconds"] == 3.0
    assert payload["stop_condition"] == {"ts_delta_sec": {"max_delta_sec": 6.0}}
    assert "frame_count" not in payload["stop_condition"]


def test_media_worker_c2_summary_records_c2_3b_mapping_fields(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    worker = _activate_service_module(MEDIA_WORKER_ROOT, "app.worker")
    event_id = "33333333-3333-4333-8333-333333333333"
    sink_dir = _make_sink_output(tmp_path, event_id=event_id)
    pg_conn = _FakeConnection([_event_row(event_id)])

    monkeypatch.setenv("EVIDENCE_PHASE", "C2.3B")
    monkeypatch.setattr(worker, "_probe_video_duration_seconds", lambda _path: 6.0)
    monkeypatch.setattr(
        worker,
        "build_post_savant_evidence_bundle",
        lambda **kwargs: _fake_builder_result(kwargs["output_dir"], kwargs["event_metadata"]),
    )
    monkeypatch.setattr(worker, "read_decoded_video_frame_count", lambda _path: 2)

    bundle = worker._finalize_c2_post_savant_evidence_bundle(
        pg_conn,
        event_id=event_id,
        meta_dir=str(sink_dir),
        metadata_file=str(sink_dir / "metadata.json"),
        evidence_output_dir=str(tmp_path / "evidence"),
    )

    metadata = json.loads(Path(bundle["metadata"]).read_text(encoding="utf-8"))
    summary = json.loads(Path(bundle["summary"]).read_text(encoding="utf-8"))
    assert metadata["phase"] == "C2.3B"
    assert metadata["replay"]["replay_source_kind"] == "post_savant"
    assert summary["replay_source_kind"] == "post_savant"
    assert summary["c2_3b_record_request_id"] == "c2_3b:req:test"
    assert summary["c2_3b_source_event_id"] == "c2_3b:synthetic:test"
    assert summary["c2_3b_source_id"] == "c2_post_savant_fps_probe"
    assert summary["replay_stored_stream_id"] == "c2_post_savant_fps_probe"
    assert summary["allow_db_annotation_fallback"] is False
    assert summary["allow_legacy_annotation_fallback"] is False
    assert summary["requested_start_pts"] == "4721166666"
    assert summary["requested_end_pts"] == "14721166666"
    assert summary["replay_stop_strategy"] == "event_anchor_pre_seconds_rewind"


def _activate_service_module(service_root: Path, module_name: str) -> Any:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    service_root_text = str(service_root)
    if service_root_text in sys.path:
        sys.path.remove(service_root_text)
    sys.path.insert(0, service_root_text)
    return importlib.import_module(module_name)


def _c2_event() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source_event_id": "c2_3b:synthetic:test",
        "event_type": "c2_3b_synthetic_event",
        "camera_id": "cam-c2",
        "source_id": "c2_post_savant_fps_probe",
        "event_ts_ms": 1_780_000_000_000,
        "frame_pts": 9_721_166_666,
        "frame_num": 5,
        "payload": {
            "media": {
                "source_id": "c2_post_savant_fps_probe",
                "replay_source_kind": "post_savant",
                "evidence_topology": "post_savant",
                "annotation_source_policy": "post_savant_sink_metadata_only",
                "allow_db_annotation_fallback": False,
                "allow_legacy_annotation_fallback": False,
                "frame_pts": 9_721_166_666,
                "frame_num": 5,
                "requested_start_pts": 4_721_166_666,
                "requested_end_pts": 14_721_166_666,
                "event_frame_pts": 9_721_166_666,
                "replay_stop_strategy": "legacy_anchor_start_offset_zero",
            }
        },
        "evidence_policy": {
            "pre_seconds": 3,
            "post_seconds": 3,
            "replay_source_kind": "post_savant",
            "evidence_topology": "post_savant",
            "annotation_source_policy": "post_savant_sink_metadata_only",
            "allow_db_annotation_fallback": False,
            "allow_legacy_annotation_fallback": False,
            "requested_start_pts": 4_721_166_666,
            "requested_end_pts": 14_721_166_666,
            "event_frame_pts": 9_721_166_666,
            "replay_stop_strategy": "legacy_anchor_start_offset_zero",
        },
    }


def _c2_record_request() -> dict[str, Any]:
    return {
        "request_id": "c2_3b:req:test",
        "event_id": "33333333-3333-4333-8333-333333333333",
        "source_event_id": "c2_3b:synthetic:test",
        "camera_id": "cam-c2",
        "source_id": "c2_post_savant_fps_probe",
        "event_ts_ms": 1_780_000_000_000,
        "frame_pts": 9_721_166_666,
        "frame_num": 5,
        "pre_seconds": 3,
        "post_seconds": 3,
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "allow_db_annotation_fallback": False,
        "allow_legacy_annotation_fallback": False,
        "requested_start_pts": 4_721_166_666,
        "requested_end_pts": 14_721_166_666,
        "event_frame_pts": 9_721_166_666,
        "replay_stop_strategy": "event_anchor_pre_seconds_rewind",
        "strategy": "savant_replay",
        "status": "pending",
    }


def _make_sink_output(tmp_path: Path, *, event_id: str) -> Path:
    sink_dir = tmp_path / "sink"
    sink_dir.mkdir()
    (sink_dir / "video.mov").write_bytes(b"fake video")
    (sink_dir / "metadata.json").write_text(
        json.dumps(
            {
                "type": "VideoFrame",
                "source_id": f"replay-event-{event_id}",
                "job_id": "job-c2-3b",
                "labels": {"event_id": event_id},
                "objects": [
                    {
                        "namespace": "yolo26_pose",
                        "label": "person",
                        "id": 1,
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return sink_dir


def _event_row(event_id: str) -> tuple[Any, ...]:
    return (
        "c2_3b_synthetic_event",
        "cam-c2",
        "c2_post_savant_fps_probe",
        "41",
        1_780_000_000_000,
        "frame-1",
        {
            "media": {
                "replay_job_id": "job-c2-3b",
                "replay_job_request": {
                    "anchor_keyframe": "019ea0ab-8938-7020-9c98-f1558328185c",
                    "offset": {"seconds": 3},
                    "stop_condition": {"ts_delta_sec": {"max_delta_sec": 6.0}},
                    "configuration": {
                        "stored_stream_id": "c2_post_savant_fps_probe",
                        "resulting_stream_id": f"replay-event-{event_id}",
                        "labels": {
                            "event_id": event_id,
                            "request_id": "c2_3b:req:test",
                            "source_event_id": "c2_3b:synthetic:test",
                            "replay_source_kind": "post_savant",
                            "evidence_topology": "post_savant",
                            "annotation_source_policy": (
                                "post_savant_sink_metadata_only"
                            ),
                            "allow_db_annotation_fallback": "false",
                            "allow_legacy_annotation_fallback": "false",
                            "frame_pts": "9721166666",
                            "frame_num": "5",
                            "requested_start_pts": "4721166666",
                            "requested_end_pts": "14721166666",
                            "event_frame_pts": "9721166666",
                            "replay_stop_strategy": "event_anchor_pre_seconds_rewind",
                        },
                    },
                },
            }
        },
        0.91,
        "c2_3b:synthetic:test",
        "019ea0ab-8938-7020-9c98-f1558328185c",
        "medium",
        {},
        None,
    )


def _fake_builder_result(output_dir: Path, event_metadata: dict[str, Any]) -> SimpleNamespace:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_clip = output_dir / "raw_clip.mov"
    sink_metadata = output_dir / "sink_metadata.json"
    sidecar = output_dir / "annotations.frame_cache.identity.jsonl"
    summary = output_dir / "summary.json"
    sidecar_summary = output_dir / "summary.frame_cache.identity.json"
    raw_clip.write_bytes(b"fake c2 video")
    sink_metadata.write_text("{}\n", encoding="utf-8")
    sidecar.write_text(json.dumps({"frame_index": 0, "objects": []}) + "\n", encoding="utf-8")
    payload = {
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
        "limitations": ["source_observation_id_missing_in_native_metadata"],
        **event_metadata,
    }
    summary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    sidecar_summary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return SimpleNamespace(
        output_dir=output_dir,
        raw_clip_path=raw_clip,
        sink_metadata_path=sink_metadata,
        production_sidecar_path=sidecar,
        sidecar_summary_path=sidecar_summary,
        summary_path=summary,
        summary=payload,
    )


class _FakeConnection:
    def __init__(self, rows: list[Any]) -> None:
        self.cursor_obj = _FakeCursor(rows)

    def cursor(self) -> "_FakeCursor":
        return self.cursor_obj


class _FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = list(rows)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, _sql: str, _params: Any = None) -> None:
        return None

    def fetchone(self) -> Any:
        if not self.rows:
            return None
        return self.rows.pop(0)
