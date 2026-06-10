"""C2.12B external Reese / Finch watchlist evidence contract tests."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "scripts" / "tools" / "build_c2_12b_external_watchlist_evidence.py"


def test_gallery_self_match_is_not_accepted_as_pass() -> None:
    tool = _load_tool()
    match = _match(similarity=0.99)
    match["self_match_used_for_pass"] = True

    assert tool.is_valid_video_match(match, threshold=0.65) is False


def test_fake_match_is_rejected() -> None:
    tool = _load_tool()
    match = _match(similarity=0.99)
    match["fake_match"] = True

    assert tool.is_valid_video_match(match, threshold=0.65) is False


def test_event_requires_reese_or_finch_external_person_id(tmp_path: Path) -> None:
    tool = _load_tool()
    match = _match(similarity=0.99)
    match["query_external_person_id"] = "test:c2_4:person"

    try:
        tool.build_watchlist_event(
            match=match,
            bundle_path=tmp_path,
            threshold=0.65,
            watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
        )
    except ValueError as exc:
        assert "external_person_id_must_be_reese_or_finch" in str(exc)
    else:
        raise AssertionError("non Reese/Finch external_person_id should be rejected")


def test_event_requires_real_source_observation_id(tmp_path: Path) -> None:
    tool = _load_tool()
    match = _match(similarity=0.99)
    match["source_observation_id"] = ""

    try:
        tool.build_watchlist_event(
            match=match,
            bundle_path=tmp_path,
            threshold=0.65,
            watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
        )
    except ValueError as exc:
        assert "real_source_observation_id_required" in str(exc)
    else:
        raise AssertionError("event without source_observation_id should be rejected")


def test_event_payload_does_not_include_embedding(tmp_path: Path) -> None:
    tool = _load_tool()
    event = tool.build_watchlist_event(
        match=_match(similarity=0.99),
        bundle_path=tmp_path,
        threshold=0.65,
        watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
    )
    scan = tool.scan_for_unsafe_payload(event)

    assert scan["payload_has_embedding"] is False
    assert scan["forbidden_key_paths"] == []


def test_event_payload_does_not_include_image_base64_or_crop_bytes(tmp_path: Path) -> None:
    tool = _load_tool()
    event = tool.build_watchlist_event(
        match=_match(similarity=0.99),
        bundle_path=tmp_path,
        threshold=0.65,
        watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
    )
    scan = tool.scan_for_unsafe_payload(event)

    assert scan["payload_has_image_bytes"] is False
    assert scan["forbidden_key_paths"] == []


def test_evidence_geometry_unchanged_by_identity_patch() -> None:
    tool = _load_tool()
    rows = [_sidecar_row()]
    before = copy.deepcopy(rows[0]["objects"][0]["bbox"])

    patched, check = tool.patch_sidecar_for_external_match(rows, _match(similarity=0.99))

    assert check["patched_object_count"] == 1
    assert check["geometry_unchanged"] is True
    assert patched[0]["objects"][0]["bbox"] == before
    assert patched[0]["objects"][0]["identity"]["external_person_id"] == "demo:f4_3:reese"


def test_no_video_match_produces_partial_not_fail() -> None:
    tool = _load_tool()
    decision = tool.decide_result(
        gallery=_gallery_ready(),
        inventory={"face_observations_with_embedding": 1},
        matches_by_identity={
            "reese": [_match(similarity=0.40)],
            "finch": [_match(similarity=-0.10, identity_key="finch", external_id="demo:f4_3:finch")],
        },
        threshold=0.65,
    )

    assert decision["result_marker"] == tool.RESULT_NO_MATCH


def test_top_match_reports_do_not_include_embedding_vectors() -> None:
    tool = _load_tool()
    matches = {"reese": [_match(similarity=0.57)], "finch": []}
    scan = tool.scan_for_unsafe_payload(matches)

    assert scan["payload_has_embedding"] is False
    assert scan["forbidden_key_paths"] == []


def test_unsafe_scan_rejects_embedding_vectors() -> None:
    tool = _load_tool()
    scan = tool.scan_for_unsafe_payload({"payload": {"embedding": [0.1] * 512}})

    assert scan["passed"] is False
    assert scan["payload_has_embedding"] is True


def test_unsafe_scan_rejects_image_base64_and_crop_bytes() -> None:
    tool = _load_tool()
    scan = tool.scan_for_unsafe_payload(
        {"payload": {"image_base64": "data:image/jpeg;base64,AAAA", "crop_bytes": "bytes"}}
    )

    assert scan["passed"] is False
    assert scan["payload_has_image_bytes"] is True


def test_stable_sink_workaround_flag_preserved_if_bundle_generated(tmp_path: Path) -> None:
    tool = _load_tool()
    event = tool.build_watchlist_event(
        match=_match(similarity=0.99),
        bundle_path=tmp_path,
        threshold=0.65,
        watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
    )

    assert event["evidence"]["capture_mode"] == "stable_post_savant_sink_time_crop"
    assert event["evidence"]["workaround_used"] is True


def test_event_style_replay_not_claimed() -> None:
    tool = _load_tool()
    summary = tool.build_summary(
        output_dir=Path("/tmp/c2_12b"),
        stable_bundle=Path("/tmp/stable"),
        gallery=_gallery_ready(),
        inventory={"face_observations_with_embedding": 1, "total_face_observations": 1},
        matches_by_identity={"reese": [_match(similarity=0.40)], "finch": []},
        decision={"result_marker": tool.RESULT_NO_MATCH, "reason": "no_match", "best_match": None},
        unsafe_scan={"passed": True, "payload_has_embedding": False, "payload_has_image_bytes": False},
        threshold=0.65,
        watchlist_rule_id=tool.DEFAULT_WATCHLIST_RULE_ID,
        pass_summary=None,
    )

    assert summary["event_style_replay_job_passed"] is False
    assert summary["event_style_replay_claimed"] is False


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("build_c2_12b_external_watchlist_evidence", TOOL_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _match(
    *,
    similarity: float,
    identity_key: str = "reese",
    external_id: str = "demo:f4_3:reese",
) -> dict[str, Any]:
    return {
        "identity_key": identity_key,
        "query_external_person_id": external_id,
        "query_gallery_embedding_id": 4 if identity_key == "reese" else 5,
        "query_person_id": 5 if identity_key == "reese" else 6,
        "match_source": "video_face_observation",
        "fake_match": False,
        "self_match_used_for_pass": False,
        "threshold": 0.65,
        "threshold_passed": similarity >= 0.65,
        "real_face_observation": True,
        "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
        "camera_id": "c2_post_savant_fps_probe",
        "source_id": "c2_post_savant_fps_probe",
        "track_id": "4",
        "frame_num": 200,
        "timestamp_ms": 17854,
        "similarity": similarity,
        "distance": 1.0 - similarity,
        "c2_stable_sidecar_join": {
            "appears_in_sidecar": True,
            "track_id_join_warning": True,
        },
    }


def _gallery_ready() -> dict[str, Any]:
    return {
        "targets": {
            "reese": {"active_gallery_embedding_ids": [4]},
            "finch": {"active_gallery_embedding_ids": [5]},
        },
        "rows": [],
    }


def _sidecar_row() -> dict[str, Any]:
    return {
        "frame_index": 194,
        "frame_pts": 17854288888,
        "objects": [
            {
                "object_type": "known_face",
                "object_id": "889031753",
                "track_id": "1",
                "bbox": {
                    "format": "xyxy",
                    "xyxy": [705.0, 478.0, 826.0, 638.0],
                    "coordinate_space": "pixel",
                },
                "pose": {"keypoints": [{"x": 1.0, "y": 2.0}]},
                "identity": {
                    "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
                    "external_person_id": "test:c2_4:person",
                    "track_id_join_warning": True,
                },
            }
        ],
    }
