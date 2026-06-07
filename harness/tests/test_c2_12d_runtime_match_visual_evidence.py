"""C2.12D runtime Finch visual evidence join contract tests."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "scripts" / "tools" / "build_c2_12d_runtime_match_visual_evidence.py"


def test_pass_requires_real_match_above_threshold() -> None:
    tool = _load_tool()
    below = _match(similarity=0.64)
    decision = tool.decide_result(
        match=below,
        geometry=tool.extract_match_geometry(below),
        sink_inventory=[_covering_sink()],
        join_attempts={"join_succeeded": True},
    )

    assert decision["result_marker"] == tool.RESULT_FAIL
    assert decision["reason"] == "match_below_threshold"


def test_pass_requires_sink_coverage_of_matched_timestamp() -> None:
    tool = _load_tool()
    match = _match()
    decision = tool.decide_result(
        match=match,
        geometry=tool.extract_match_geometry(match),
        sink_inventory=[_non_covering_sink()],
        join_attempts={"join_succeeded": True},
    )

    assert decision["result_marker"] == tool.RESULT_NO_SINK_COVERAGE


def test_pass_requires_sidecar_metadata_join() -> None:
    tool = _load_tool()
    match = _match()
    decision = tool.decide_result(
        match=match,
        geometry=tool.extract_match_geometry(match),
        sink_inventory=[_covering_sink()],
        join_attempts={"join_succeeded": False, "reason": "no_bbox_join"},
    )

    assert decision["result_marker"] == tool.RESULT_SIDECAR_JOIN_GAP


def test_no_sink_coverage_returns_partial_not_fake_evidence() -> None:
    tool = _load_tool()
    match = _match()
    join = tool.attempt_join(
        match=match,
        geometry=tool.extract_match_geometry(match),
        sink_inventory=[_non_covering_sink()],
    )
    decision = tool.decide_result(
        match=match,
        geometry=tool.extract_match_geometry(match),
        sink_inventory=[_non_covering_sink()],
        join_attempts=join,
    )

    assert decision["result_marker"] == tool.RESULT_NO_SINK_COVERAGE
    assert join["join_succeeded"] is False


def test_sidecar_join_gap_returns_partial() -> None:
    tool = _load_tool()
    match = _match()
    join = tool.attempt_join(
        match=match,
        geometry=tool.extract_match_geometry(match),
        sink_inventory=[_covering_sink(track_hits=1, sid_hits=0, bbox_iou=0.1)],
    )
    decision = tool.decide_result(
        match=match,
        geometry=tool.extract_match_geometry(match),
        sink_inventory=[_covering_sink(track_hits=1, sid_hits=0, bbox_iou=0.1)],
        join_attempts=join,
    )

    assert join["track_id_alone_used"] is False
    assert decision["result_marker"] == tool.RESULT_SIDECAR_JOIN_GAP


def test_geometry_is_not_modified_by_identity_patch() -> None:
    tool = _load_tool()
    match = _match()
    geometry = tool.extract_match_geometry(match)
    before_bbox = copy.deepcopy(geometry["face_bbox"])
    before_landmarks = copy.deepcopy(geometry["landmarks"])
    event = tool.build_watchlist_event(
        match,
        threshold=0.65,
        watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
        output_dir=Path("/tmp/c2_12d_test"),
    )

    rows = tool.build_identity_sidecar_rows(match, geometry, event)

    assert rows[0]["objects"][0]["bbox"] == before_bbox
    assert rows[0]["objects"][0]["landmarks"] == before_landmarks
    assert rows[0]["objects"][0]["identity"]["external_person_id"] == "demo:f4_3:finch"


def test_event_payload_has_no_embedding(tmp_path: Path) -> None:
    tool = _load_tool()
    event = tool.build_watchlist_event(
        _match(),
        threshold=0.65,
        watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
        output_dir=tmp_path,
    )
    scan = tool.scan_for_unsafe_payload(event)

    assert scan["payload_has_embedding"] is False
    assert scan["forbidden_key_paths"] == []


def test_event_and_report_have_no_image_base64_or_crop_bytes(tmp_path: Path) -> None:
    tool = _load_tool()
    event = tool.build_watchlist_event(
        _match(),
        threshold=0.65,
        watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
        output_dir=tmp_path,
    )
    report = tool.render_operator_report({"result_marker": tool.RESULT_PASS, "matched_finch": _match()})
    scan = tool.scan_for_unsafe_payload({"event": event, "report": report})

    assert scan["payload_has_image_bytes"] is False
    assert scan["forbidden_key_paths"] == []


def test_track_id_alone_is_not_sufficient_join_key() -> None:
    tool = _load_tool()
    match = _match()
    join = tool.attempt_join(
        match=match,
        geometry=tool.extract_match_geometry(match),
        sink_inventory=[_covering_sink(track_hits=8, bbox_iou=None, sid_hits=0)],
    )

    assert join["join_succeeded"] is False
    assert join["track_id_alone_used"] is False


def test_event_style_replay_false_preserved(tmp_path: Path) -> None:
    tool = _load_tool()
    event = tool.build_watchlist_event(
        _match(),
        threshold=0.65,
        watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
        output_dir=tmp_path,
    )

    assert event["evidence"]["event_style_replay_job_passed"] is False
    assert event["payload"]["event_style_replay_job_passed"] is False


def test_stable_sink_workaround_flag_preserved(tmp_path: Path) -> None:
    tool = _load_tool()
    event = tool.build_watchlist_event(
        _match(),
        threshold=0.65,
        watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
        output_dir=tmp_path,
    )

    assert event["evidence"]["capture_mode"] == "stable_post_savant_sink_time_crop"
    assert event["evidence"]["workaround_used"] is True


def test_geometry_missing_returns_partial() -> None:
    tool = _load_tool()
    match = _match()
    match["face_bbox"] = None
    decision = tool.decide_result(
        match=match,
        geometry=tool.extract_match_geometry(match),
        sink_inventory=[_covering_sink()],
        join_attempts={"join_succeeded": False, "reason": "match_geometry_missing"},
    )

    assert decision["result_marker"] == tool.RESULT_GEOMETRY_MISSING


def test_unsafe_scan_rejects_embedding_vector() -> None:
    tool = _load_tool()
    scan = tool.scan_for_unsafe_payload({"payload": {"embedding": [0.1] * 512}})

    assert scan["passed"] is False
    assert scan["payload_has_embedding"] is True


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("build_c2_12d_runtime_match_visual_evidence", TOOL_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _match(*, similarity: float = 0.690014918944816) -> dict[str, Any]:
    return {
        "query_external_person_id": "demo:f4_3:finch",
        "query_person_id": 6,
        "query_gallery_embedding_id": 5,
        "similarity": similarity,
        "source_observation_id": (
            "face:c2_12c_runtime:run:c2_post_savant_fps_probe:"
            "2196:36708508288888:6_1780849127595-0"
        ),
        "original_source_observation_id": "face:c2_post_savant_fps_probe:2196:36708508",
        "source_id": "c2_post_savant_fps_probe",
        "camera_id": "c2_post_savant_fps_probe",
        "track_id": "2196",
        "timestamp_ms": 36708508,
        "frame_num": 296551,
        "face_bbox": {
            "format": "cxcywh",
            "values": [647.3, 350.5, 348.4, 440.9],
            "coordinate_space": "pixel",
        },
        "landmarks": [594.5, 314.7, 772.2, 304.5],
        "fake_match_used": False,
        "gallery_self_match_used": False,
        "payload": {
            "media": {"frame_pts": 36708508288888},
            "redis_stream_id": "1780849127595-0",
        },
    }


def _covering_sink(
    *,
    track_hits: int = 1,
    sid_hits: int = 1,
    bbox_iou: float | None = 0.8,
) -> dict[str, Any]:
    return {
        "path": "/tmp/c2_sink",
        "video_path": "/tmp/c2_sink/video.mov",
        "metadata_path": "/tmp/c2_sink/metadata.json",
        "pts_start": 36700000000000,
        "pts_end": 36710000000000,
        "timestamp_start": 36700000,
        "timestamp_end": 36710000,
        "frame_num_start": 296500,
        "frame_num_end": 296600,
        "track_id_string_hits": track_hits,
        "source_observation_id_string_hits": sid_hits,
        "best_bbox_iou": bbox_iou,
        "covers_match": True,
        "reason": "target pts/frame within range",
    }


def _non_covering_sink() -> dict[str, Any]:
    sink = _covering_sink()
    sink.update(
        {
            "pts_start": 21523963911111,
            "pts_end": 21533973911111,
            "timestamp_start": 21523963,
            "timestamp_end": 21533973,
            "frame_num_start": None,
            "frame_num_end": None,
            "covers_match": False,
            "reason": "target pts/frame outside range",
        }
    )
    return sink
