from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELPER = (
    ROOT
    / "modules"
    / "savant_security"
    / "custom"
    / "services"
    / "frame_anchor_metadata.py"
)
BEHAVIOR_RULES = (
    ROOT
    / "modules"
    / "savant_security"
    / "custom"
    / "pyfuncs"
    / "behavior_rules.py"
)
COMPOSE = ROOT / "infra" / "docker-compose.c1-official-adapter.yml"
SMOKE = ROOT / "scripts" / "smoke" / "check_r3_3a2a_frame_uuid_source_frame_identity.sh"
REPORT = ROOT / "docs" / "r3_3a2a_frame_uuid_source_frame_identity_report.md"


def load_helper():
    spec = importlib.util.spec_from_file_location("frame_anchor_metadata", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_trace_helper_exists_and_is_env_gated():
    text = HELPER.read_text()
    assert "class FrameAnchorTraceWriter" in text
    assert "R3_3A2A_FRAME_ANCHOR_TRACE_ENABLED" in text
    assert "R3_3A2A_TRACE_OUTPUT_ROOT" in text
    assert "r3_3a2a_frame_anchor_trace" in text
    assert "def find_unique_frame_anchor_trace_match" in text


def test_behavior_rules_writes_trace_without_changing_event_logic():
    text = BEHAVIOR_RULES.read_text()
    assert "FrameAnchorTraceWriter" in text
    assert "self._frame_anchor_trace.write" in text
    assert "extract_frame_anchor_metadata(frame_meta)" in text


def test_compose_keeps_trace_disabled_by_default():
    text = COMPOSE.read_text()
    assert "R3_3A2A_FRAME_ANCHOR_TRACE_ENABLED" in text
    assert "${R3_3A2A_FRAME_ANCHOR_TRACE_ENABLED:-false}" in text
    assert "R3_3A2A_TRACE_OUTPUT_ROOT" in text


def test_unique_frame_uuid_match_logic():
    helper = load_helper()
    records = [
        {"frame_uuid": "frame-a", "frame_num": 1},
        {"frame_uuid": "frame-b", "frame_num": 2},
    ]
    match = helper.find_unique_frame_anchor_trace_match(records, "frame-b")
    assert match["frame_num"] == 2


def test_missing_frame_uuid_reports_not_found():
    helper = load_helper()
    try:
        helper.find_unique_frame_anchor_trace_match([{"frame_uuid": "frame-a"}], "x")
    except ValueError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("missing frame_uuid should raise")


def test_duplicate_frame_uuid_reports_error():
    helper = load_helper()
    records = [{"frame_uuid": "dup"}, {"frame_uuid": "dup"}]
    try:
        helper.find_unique_frame_anchor_trace_match(records, "dup")
    except ValueError as exc:
        assert "multiple trace records" in str(exc)
    else:
        raise AssertionError("duplicate frame_uuid should raise")


def test_trace_record_schema_is_compact_and_jsonable():
    helper = load_helper()

    class VideoFrame:
        uuid = "frame-1"
        previous_keyframe_uuid = "key-0"
        pts = 123456789
        dts = 123000000
        duration = 41708333
        time_base = (1, 1000000000)
        source_id = "source-a"

    class FrameMeta:
        video_frame = VideoFrame()
        frame_num = 7
        ntp_timestamp = 1780000000000000000

    record = helper.build_frame_anchor_trace_record(
        FrameMeta(),
        timestamp_ms_used_by_event=123,
        notes="test",
    )
    assert record["source_id"] == "source-a"
    assert record["frame_uuid"] == "frame-1"
    assert record["previous_keyframe_uuid"] == "key-0"
    assert record["frame_pts"] == 123456789
    assert record["frame_num"] == 7
    assert record["time_base"] == "1/1000000000"
    json.dumps(record)


def test_smoke_and_report_document_boundaries():
    smoke = SMOKE.read_text()
    assert "R3_3A2A_FRAME_ANCHOR_TRACE_ENABLED=true" in smoke
    assert "no_replay_service=YES" in smoke
    assert "no_video_file_sink_production=YES" in smoke
    assert "no_db_migration=YES" in smoke
    assert "frame_uuid" in smoke
    assert "trace.jsonl" in smoke

    report = REPORT.read_text()
    assert "event.frame_uuid" in report
    assert "unique trace match" in report
    assert "source frame identity result" in report
    assert "source aligned clip path" in report
    assert "clip window" in report
    assert "event frame position in clip" in report
    assert "source clip visual alignment: PASS" in report
    assert "source-video extraction, NOT Replay extraction" in report
    assert "No Replay Service" in report
    assert "No Video File Sink production deployment" in report
    assert "No DB migration" in report
