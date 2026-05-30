from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "r3_3a2b_replay_keyframe_visual_clip_report.md"
SMOKE = ROOT / "scripts" / "smoke" / "check_r3_3a2b_replay_keyframe_visual_clip.sh"


def test_report_exists_and_records_poc_boundary():
    text = REPORT.read_text()
    assert "R3.3A2b Replay Keyframe Visual Clip Report" in text
    assert "POC-only" in text
    assert "no production clip-worker" in text
    assert "no production media-worker" in text
    assert "no DB migration" in text
    assert "no production Video File Sink" in text
    assert "no performance" in text


def test_report_status_is_unverified_or_blocked_not_pass():
    text = REPORT.read_text()
    lowered = text.lower()
    assert "status: unverified / blocked" in lowered
    assert "same-stream Replay/cache POC not deployed" in text
    assert "pending same-stream Replay/cache POC" in text
    assert "Status: PASS" not in text


def test_report_records_event_and_anchor_fields():
    text = REPORT.read_text()
    assert "event_id" in text
    assert "source_event_id" in text
    assert "event_ts_ms" in text
    assert "frame_uuid" in text
    assert "keyframe_uuid" in text
    assert "previous_keyframe_uuid" in text
    assert "Replay anchor used" in text


def test_report_distinguishes_frame_uuid_from_replay_anchor():
    text = REPORT.read_text()
    assert "event.frame_uuid" in text
    assert "alarm frame identity" in text
    assert "was not sent as a Replay keyframe anchor" in text
    assert "previous_keyframe_uuid" in text


def test_report_does_not_treat_missing_clip_as_replay_failure():
    text = REPORT.read_text()
    assert "Clip path: none" in text
    assert "Playable: unverified" in text
    assert "Visual alignment result: UNVERIFIED" in text
    assert "Replay clip: not generated; pending same-stream Replay/cache POC" in text
    assert "Replay visual alignment failed" not in text
    assert "Replay scheme is not viable" not in text


def test_smoke_is_poc_only_and_uses_a2a_summary():
    text = SMOKE.read_text()
    assert "R3_3A2B_A2A_SUMMARY_PATH" in text
    assert "previous_keyframe_uuid" in text
    assert "keyframe_uuid" in text
    assert "no_production_clip_worker=YES" in text
    assert "no_production_media_worker=YES" in text
    assert "no_db_migration=YES" in text
    assert "no_production_video_file_sink=YES" in text
    assert "no_performance_test=YES" in text
    assert "replay_api_calls=NO" in text


def test_smoke_does_not_use_frame_uuid_as_replay_anchor():
    text = SMOKE.read_text()
    assert "anchor_uuid = previous_keyframe_uuid or keyframe_uuid" in text
    assert "current frame_uuid was not used as Replay anchor" in text
    assert "urllib.request" not in text
    assert "/api/v1/job" not in text
