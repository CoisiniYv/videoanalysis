"""P1 single-stream Replay raw clip evidence contract checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "p1_single_stream_replay_clip_output.md"
COMPOSE = ROOT / "infra" / "docker-compose.p1-replay-clip-poc.yml"
SMOKE = ROOT / "scripts" / "smoke" / "check_p1_single_stream_replay_clip_output.sh"

EW_DIR = ROOT / "services" / "event-worker"
CW_DIR = ROOT / "services" / "clip-worker"
MW_DIR = ROOT / "services" / "media-worker"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _activate_service_path(path: Path) -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    service_path = str(path)
    if service_path in sys.path:
        sys.path.remove(service_path)
    sys.path.insert(0, service_path)


def test_p1_files_exist() -> None:
    assert DOC.exists()
    assert COMPOSE.exists()
    assert SMOKE.exists()


def test_record_request_schema_includes_replay_anchor_fields() -> None:
    _activate_service_path(EW_DIR)
    from app.record_request import RecordRequestPublisher

    fake = fakeredis.FakeRedis(decode_responses=False)
    pub = RecordRequestPublisher(fake, "security.record_requests")
    pub.publish(
        {
            "source_event_id": "p1-source-event",
            "event_type": "intrusion",
            "camera_id": "cam-p1",
            "source_id": "p1_single_stream",
            "event_ts_ms": 123456,
            "frame_uuid": "frame-uuid",
            "keyframe_uuid": "keyframe-uuid",
            "payload": {
                "media": {
                    "previous_keyframe_uuid": "prev-keyframe-uuid",
                    "source_id": "p1_single_stream",
                }
            },
        },
        event_id="11111111-1111-4111-8111-111111111111",
    )

    _, fields = fake.xrange("security.record_requests", "-", "+")[0]
    record = json.loads(fields[b"data"])
    for key in (
        "event_id",
        "source_event_id",
        "source_id",
        "event_ts_ms",
        "frame_uuid",
        "keyframe_uuid",
        "previous_keyframe_uuid",
        "pre_seconds",
        "post_seconds",
        "strategy",
    ):
        assert key in record
    assert record["strategy"] == "savant_replay"
    assert record["previous_keyframe_uuid"] == "prev-keyframe-uuid"


def test_clip_worker_prefers_previous_keyframe_uuid() -> None:
    _activate_service_path(CW_DIR)
    from app.replay_client import build_job_payload

    payload = build_job_payload(
        source_id="p1_single_stream",
        keyframe_uuid="prev-keyframe-uuid",
        pre_seconds=5,
        post_seconds=10,
        sink_endpoint="pub+connect:tcp://video-file-sink:6666",
        labels={"event_id": "11111111-1111-4111-8111-111111111111"},
    )
    assert payload["anchor_keyframe"] == "prev-keyframe-uuid"
    assert payload["offset"]["seconds"] == 5
    assert payload["stop_condition"]["frame_count"] == 450
    assert payload["configuration"]["labels"]["event_id"].startswith("11111111")


def test_event_annotation_schema_and_no_annotated_clip(tmp_path: Path) -> None:
    _activate_service_path(MW_DIR)
    from app.worker import _finalize_p1_evidence_bundle

    event_id = "11111111-1111-4111-8111-111111111111"
    sink_dir = tmp_path / "sink"
    sink_dir.mkdir()
    video = sink_dir / "video.mov"
    metadata = sink_dir / "metadata.json"
    video.write_bytes(b"raw replay video")
    metadata.write_text('{"source_id":"p1_single_stream"}\n', encoding="utf-8")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (
        "intrusion",
        "cam-p1",
        "p1_single_stream",
        "track-7",
        123456,
        "frame-uuid",
        {
            "bbox": [10, 20, 100, 180],
            "zone_id": "zone-1",
            "roi_polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
        },
        0.91,
    )
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    bundle = _finalize_p1_evidence_bundle(
        mock_conn,
        event_id=event_id,
        meta_dir=str(sink_dir),
        video_file=str(video),
        metadata_file=str(metadata),
        evidence_output_dir=str(tmp_path / "evidence"),
    )
    evidence_dir = Path(bundle["evidence_dir"])
    assert Path(bundle["raw_clip"]).name == "raw_clip.mov"
    assert Path(bundle["metadata"]).name == "metadata.json"
    annotation = json.loads(Path(bundle["event_annotation"]).read_text())
    assert annotation["schema_version"] == "1.0"
    assert annotation["annotation_type"] == "event_frame"
    assert annotation["event"]["event_id"] == event_id
    assert annotation["overlays"][0]["type"] == "person_bbox"
    assert annotation["overlays"][1]["type"] == "roi_polygon"
    assert not (evidence_dir / "annotated_clip.mp4").exists()


def test_media_worker_generated_status_contract(tmp_path: Path) -> None:
    _activate_service_path(MW_DIR)
    from app.worker import _process_sink_output

    event_id = "11111111-1111-4111-8111-111111111111"
    sink_dir = tmp_path / "replay-sink-output" / f"replay-event-{event_id}-00000000"
    sink_dir.mkdir(parents=True)
    (sink_dir / "video.mov").write_bytes(b"raw replay video")
    (sink_dir / "metadata.json").write_text(
        json.dumps({"labels": {"event_id": event_id}, "job_id": "job-p1"}) + "\n",
        encoding="utf-8",
    )

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.side_effect = [
        ("replay_job_created",),
        (
            "intrusion",
            "cam-p1",
            "p1_single_stream",
            "track-7",
            123456,
            "frame-uuid",
            {},
            0.91,
        ),
    ]
    mock_cursor.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    updated = _process_sink_output(
        mock_conn,
        str(tmp_path / "replay-sink-output"),
        set(),
        evidence_output_dir=str(tmp_path / "evidence"),
        p1_raw_clip_finalizer_enabled=True,
    )
    assert updated == 1
    sql_calls = "\n".join(str(call.args[0]) for call in mock_cursor.execute.call_args_list)
    params = [
        call.args[1] for call in mock_cursor.execute.call_args_list if len(call.args) > 1
    ]
    assert "clip_status" in sql_calls
    assert any(p.get("clip_status") == '"generated"' for p in params if isinstance(p, dict))
    assert any("evidence_dir" in p for p in params if isinstance(p, dict))


def test_failed_and_blocked_statuses_are_allowed_contract_states() -> None:
    repo = _text(ROOT / "services" / "clip-worker" / "app" / "repository.py")
    smoke = _text(SMOKE)
    doc = _text(DOC)
    assert "failed" in repo
    assert "BLOCKED" in smoke
    assert "clip_status = blocked" in doc
    assert "clip_status = failed" in doc


def test_source_extraction_fallback_is_forbidden_for_p1() -> None:
    smoke = _text(SMOKE)
    compose = _text(COMPOSE)
    doc = _text(DOC)
    combined = "\n".join([smoke, compose, doc])
    assert "source extraction fallback" in combined
    assert "manual trigger fallback" in doc
    assert "generate_visual_result.py" not in smoke
    assert "export_local_video_debug_evidence.py" not in smoke
    assert "process_evidence_task.py" not in smoke


def test_p1_poc_compose_boundary() -> None:
    compose = _text(COMPOSE)
    assert "POC only" in compose
    assert "replay-service" in compose
    assert "video-file-sink" in compose
    assert "clip-worker" in compose
    assert "media-worker" in compose
    assert "savant-security" in compose
    assert "source-adapter" in compose
    assert "P1_RAW_CLIP_FINALIZER_ENABLED" in compose
    assert "RECORDING_ENABLED: \"true\"" in compose
    assert "out_stream:" not in compose
