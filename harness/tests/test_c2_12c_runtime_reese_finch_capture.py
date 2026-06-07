"""C2.12C runtime Reese / Finch capture contract tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "tools" / "probe_c2_12c_runtime_reese_finch_capture.py"


def test_pass_requires_captured_runtime_observation_not_gallery_self_match() -> None:
    tool = _load_tool()
    match = _match(similarity=0.91)
    match["gallery_self_match_used"] = True

    assert tool.is_valid_pass_match(match, threshold=0.65) is False


def test_pass_requires_reese_or_finch_external_person_id() -> None:
    tool = _load_tool()
    match = _match(similarity=0.91)
    match["query_external_person_id"] = "test:c2_4:person"

    assert tool.is_valid_pass_match(match, threshold=0.65) is False


def test_threshold_recorded_and_enforced() -> None:
    tool = _load_tool()
    below = _match(similarity=0.64)
    above = _match(similarity=0.65)

    assert tool.is_valid_pass_match(below, threshold=0.65) is False
    assert tool.is_valid_pass_match(above, threshold=0.65) is True


def test_no_match_in_window_returns_partial_not_fake_pass() -> None:
    tool = _load_tool()
    decision = tool.decide_result(
        captured_rows=[{"source_observation_id": "face:c2_12c_runtime:x"}],
        matches_by_identity={"reese": [_match(similarity=0.50)], "finch": [_match(similarity=0.40, identity_key="finch")]},
        threshold=0.65,
        runtime_before=_runtime(stream_len=10),
        runtime_after=_runtime(stream_len=10),
    )

    assert decision["result_marker"] == tool.RESULT_NO_MATCH


def test_no_new_observations_returns_partial_not_fake_pass() -> None:
    tool = _load_tool()
    decision = tool.decide_result(
        captured_rows=[],
        matches_by_identity={"reese": [], "finch": []},
        threshold=0.65,
        runtime_before=_runtime(stream_len=10),
        runtime_after=_runtime(stream_len=10, face_worker_running=False),
    )

    assert decision["result_marker"] == tool.RESULT_NOT_CONSUMING


def test_event_payload_has_no_embedding(tmp_path: Path) -> None:
    tool = _load_tool()
    event = tool.build_watchlist_event(
        match=_match(similarity=0.91),
        threshold=0.65,
        watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
        output_dir=tmp_path,
    )

    scan = tool.scan_for_unsafe_payload(event)
    assert scan["payload_has_embedding"] is False
    assert scan["forbidden_key_paths"] == []


def test_report_has_no_image_base64_or_crop_bytes() -> None:
    tool = _load_tool()
    scan = tool.scan_for_unsafe_payload(
        {
            "report": {
                "source_observation_id": "face:c2_12c_runtime:x",
                "face_bbox": {"values": [1, 2, 3, 4]},
            }
        }
    )

    assert scan["payload_has_image_bytes"] is False
    assert scan["forbidden_key_paths"] == []


def test_source_observation_id_required() -> None:
    tool = _load_tool()
    match = _match(similarity=0.91)
    match["source_observation_id"] = ""

    assert tool.is_valid_pass_match(match, threshold=0.65) is False


def test_event_style_replay_false_preserved(tmp_path: Path) -> None:
    tool = _load_tool()
    event = tool.build_watchlist_event(
        match=_match(similarity=0.91),
        threshold=0.65,
        watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
        output_dir=tmp_path,
    )

    assert event["evidence"]["event_style_replay_job_passed"] is False
    assert event["payload"]["event_style_replay_job_passed"] is False


def test_track_id_alone_not_used_as_identity_join_key() -> None:
    tool = _load_tool()
    match = _match(similarity=0.91)
    match["identity_join_key"] = "track_id"
    match["track_id_only_identity_join_used"] = True

    assert tool.is_valid_pass_match(match, threshold=0.65) is False


def test_top_match_reports_do_not_include_embedding_vectors() -> None:
    tool = _load_tool()
    matches = {"finch": [_match(similarity=0.70)], "reese": [_match(similarity=0.61)]}
    scan = tool.scan_for_unsafe_payload(matches)

    assert scan["payload_has_embedding"] is False
    assert scan["forbidden_key_paths"] == []


def test_unsafe_scan_rejects_embedding_vector() -> None:
    tool = _load_tool()
    scan = tool.scan_for_unsafe_payload({"payload": {"embedding": [0.1] * 512}})

    assert scan["passed"] is False
    assert scan["payload_has_embedding"] is True


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("probe_c2_12c_runtime_reese_finch_capture", TOOL)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _match(*, similarity: float, identity_key: str = "finch") -> dict[str, Any]:
    external_id = "demo:f4_3:finch" if identity_key == "finch" else "demo:f4_3:reese"
    return {
        "captured_runtime_observation": True,
        "fake_match_used": False,
        "gallery_self_match_used": False,
        "query_external_person_id": external_id,
        "query_person_id": 6 if identity_key == "finch" else 5,
        "query_gallery_embedding_id": 5 if identity_key == "finch" else 4,
        "source_observation_id": "face:c2_12c_runtime:run:c2_post_savant_fps_probe:2196:36708508288888:0",
        "original_source_observation_id": "face:c2_post_savant_fps_probe:2196:36708508",
        "identity_join_key": "source_observation_id",
        "track_id_only_identity_join_used": False,
        "similarity": similarity,
        "threshold": 0.65,
        "source_id": "c2_post_savant_fps_probe",
        "camera_id": "c2_post_savant_fps_probe",
        "track_id": "2196",
        "timestamp_ms": 36708508,
        "frame_num": 296551,
        "sidecar_join": {"joinable": False},
    }


def _runtime(*, stream_len: int, face_worker_running: bool = True) -> dict[str, Any]:
    return {
        "face_worker_container_running": face_worker_running,
        "redis": {
            "streams": {
                "security.face_observations": {
                    "xlen": stream_len,
                    "last_id": "1-0" if stream_len else None,
                }
            }
        },
    }
