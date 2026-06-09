"""C1J.11 frame-cache identity sidecar writer tests."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = str(ROOT / "services" / "media-worker")
for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        del sys.modules[name]
if MEDIA_WORKER_ROOT in sys.path:
    sys.path.remove(MEDIA_WORKER_ROOT)
if MEDIA_WORKER_ROOT not in sys.path:
    sys.path.insert(0, MEDIA_WORKER_ROOT)

from app.frame_cache_sidecar_writer import (  # noqa: E402
    _trigger_visual_binding_summary,
    write_frame_cache_identity_sidecar,
)
from app.production_sidecar_policy import (  # noqa: E402
    FrameCacheSidecarRunState,
    load_frame_cache_sidecar_config,
)


SOURCE_OBSERVATION_ID = "face:c1e_rtsp_replay:13:34646"
EVENT_CREATED_AT = "2026-06-06T08:01:27.655618Z"
EVENT_TS_MS = 1_780_732_887_656


class FakeRedis:
    def __init__(self, messages: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.messages = messages or []
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def xrevrange(self, name: str, max: str = "+", min: str = "-", count: int | None = None) -> list[Any]:
        self.calls.append({"name": name, "max": max, "min": min, "count": count})
        if self.fail:
            raise RuntimeError("redis unavailable")
        selected = list(reversed(self.messages))
        if count is not None:
            selected = selected[: int(count)]
        return [
            (f"{index}-0", {"data": json.dumps(message, separators=(",", ":"))})
            for index, message in enumerate(selected)
        ]


def test_disabled_sidecar_returns_skipped_without_writing(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path)

    summary, result = write_frame_cache_identity_sidecar(
        event=_event(),
        evidence_dir=str(evidence_dir),
        raw_clip_path=None,
        metadata_path=None,
        redis_client=FakeRedis([_frame_message()]),
        config=_config(enabled=False),
        state=FrameCacheSidecarRunState(),
    )

    assert result["written"] is False
    assert summary["annotation_status"] == "disabled"
    assert not (evidence_dir / "annotations.frame_cache.identity.jsonl").exists()
    assert not (evidence_dir / "summary.frame_cache.identity.json").exists()


def test_non_allowed_event_skipped(tmp_path: Path) -> None:
    event = _event()
    event["event_type"] = "intrusion"
    evidence_dir = _evidence_dir(tmp_path)
    state = FrameCacheSidecarRunState()

    summary, result = write_frame_cache_identity_sidecar(
        event=event,
        evidence_dir=str(evidence_dir),
        raw_clip_path=None,
        metadata_path=None,
        redis_client=FakeRedis([_frame_message()]),
        config=_config(enabled=True),
        state=state,
    )

    assert result["written"] is False
    assert summary["skip_reason"] == "event_type_not_allowed"
    assert state.sidecar_skipped_event_type == 1


def test_intrusion_event_writes_person_bbox_sidecar_without_known_face(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path)
    (evidence_dir / "sink_metadata.json").write_text(
        "\n".join(
            [
                json.dumps({"frame_num": 0, "pts": 15_000_000_000, "frame_uuid": "frame-start"}),
                json.dumps({"frame_num": 120, "pts": 20_000_000_000, "frame_uuid": "frame-intrusion"}),
                json.dumps({"frame_num": 240, "pts": 25_000_000_000, "frame_uuid": "frame-end"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary, result = _write(
        tmp_path,
        event=_intrusion_event(),
        messages=[_intrusion_frame_message()],
        evidence_dir=evidence_dir,
        config=_config(
            enabled=True,
            event_types={"intrusion", "watchlist_hit"},
            require_trigger_face=False,
        ),
    )
    rows = _read_jsonl(Path(result["annotations_path"]))

    assert result["written"] is True
    assert summary["annotation_status"] == "complete"
    assert summary["production_ready"] is True
    assert summary["identity_trigger_required"] is False
    assert summary["identity_scope_status"] == "missing_trigger_known_face"
    assert summary["person_context_rows"] >= 1
    assert summary["known_face_count"] == 0
    assert summary["embedding_vectors_in_output"] == 0
    assert summary["image_bytes_in_output"] == 0
    assert rows
    objects = [obj for row in rows for obj in row.get("objects", [])]
    assert all((obj.get("label") or {}).get("kind") != "known_face" for obj in objects)
    assert any(obj.get("object_type") == "person" for obj in objects)
    assert any(row.get("clip_timeline_match") == "metadata_frame_uuid" for row in rows)


def test_freshness_guard_env_thresholds_are_configurable() -> None:
    config = load_frame_cache_sidecar_config(
        {
            "FRAME_CACHE_SIDECAR_ENABLED": "true",
            "FRAME_CACHE_MAX_ROW_AGE_BEFORE_EVENT_SECONDS": "42.5",
            "FRAME_CACHE_MAX_ROW_AGE_AFTER_EVENT_SECONDS": "17",
        }
    )

    assert config["max_row_age_before_event_seconds"] == 42.5
    assert config["max_row_age_after_event_seconds"] == 17.0


def test_watchlist_hit_writes_sidecar_annotations(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path)

    summary, result = _write(tmp_path, evidence_dir=evidence_dir)

    assert result["written"] is True
    assert Path(result["annotations_path"]).is_file()
    assert Path(result["summary_path"]).is_file()
    assert summary["annotation_status"] == "complete"
    assert summary["annotations_written"] >= 1


def test_old_annotations_path_is_not_overwritten(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path)
    old_annotations = evidence_dir / "annotations.jsonl"
    original = '{"old":true}\n'
    old_annotations.write_text(original, encoding="utf-8")

    summary, _result = _write(tmp_path, evidence_dir=evidence_dir)

    assert old_annotations.read_text(encoding="utf-8") == original
    assert summary["old_annotations_preserved"] is True


def test_sidecar_output_contains_known_face_when_identity_patch_matches(tmp_path: Path) -> None:
    _summary, result = _write(tmp_path)

    rows = _read_jsonl(Path(result["annotations_path"]))
    objects = [obj for row in rows for obj in row.get("objects", [])]

    assert any((obj.get("label") or {}).get("kind") == "known_face" for obj in objects)


def test_trigger_face_gets_watchlist_trigger_role(tmp_path: Path) -> None:
    _summary, result = _write(tmp_path)

    rows = _read_jsonl(Path(result["annotations_path"]))
    objects = [obj for row in rows for obj in row.get("objects", [])]

    assert any(obj.get("annotation_role") == "watchlist_trigger_face" for obj in objects)


def test_missing_trigger_face_returns_partial_without_raise(tmp_path: Path) -> None:
    message = _frame_message(source_observation_id="face:other")

    summary, result = _write(tmp_path, messages=[message])

    assert result["written"] is True
    assert summary["annotation_status"] == "missing_trigger_face_annotation"
    assert summary["trigger_known_face_present"] is False


def test_redis_failure_fail_open_does_not_raise(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path)

    summary, result = write_frame_cache_identity_sidecar(
        event=_event(),
        evidence_dir=str(evidence_dir),
        raw_clip_path=None,
        metadata_path=None,
        redis_client=FakeRedis(fail=True),
        config=_config(enabled=True, fail_open=True),
        state=FrameCacheSidecarRunState(),
    )

    assert result["written"] is False
    assert Path(result["summary_path"]).is_file()
    assert summary["annotation_status"] == "partial"
    assert "RuntimeError" in summary["error"]


def test_output_contains_no_embedding_vector(tmp_path: Path) -> None:
    _summary, result = _write(tmp_path, messages=[_frame_message(include_forbidden=True)])

    rows = _read_jsonl(Path(result["annotations_path"]))
    serialized = json.dumps(rows)

    assert "embedding_vector" not in serialized
    assert "embedding" not in serialized
    assert _summary["embedding_vectors_in_output"] == 0


def test_output_contains_no_image_bytes(tmp_path: Path) -> None:
    summary, result = _write(tmp_path, messages=[_frame_message(include_forbidden=True)])

    rows = _read_jsonl(Path(result["annotations_path"]))
    serialized = json.dumps(rows)

    assert "image_bytes" not in serialized
    assert "crop_bytes" not in serialized
    assert summary["image_bytes_in_output"] == 0


def test_max_events_per_run_enforced(tmp_path: Path) -> None:
    state = FrameCacheSidecarRunState()
    config = _config(enabled=True, max_events_per_run=1)
    evidence_dir = _evidence_dir(tmp_path / "one")

    first_summary, first_result = _write(
        tmp_path / "one",
        evidence_dir=evidence_dir,
        config=config,
        state=state,
    )
    event = _event(event_id="event-2")
    second_summary, second_result = write_frame_cache_identity_sidecar(
        event=event,
        evidence_dir=str(_evidence_dir(tmp_path / "two")),
        raw_clip_path=None,
        metadata_path=None,
        redis_client=FakeRedis([_frame_message()]),
        config=config,
        state=state,
    )

    assert first_result["written"] is True
    assert first_summary["annotation_status"] == "complete"
    assert second_result["written"] is False
    assert second_summary["skip_reason"] == "max_events_per_run_reached"
    assert state.sidecar_attempted == 1
    assert state.sidecar_skipped_max_events == 1


def test_input_event_not_mutated(tmp_path: Path) -> None:
    event = _event()
    original = copy.deepcopy(event)

    _write(tmp_path, event=event)

    assert event == original


def test_production_replacement_false(tmp_path: Path) -> None:
    summary, _result = _write(tmp_path)

    assert summary["production_replacement"] is False


def test_sidecar_summary_includes_anchor_found_by(tmp_path: Path) -> None:
    summary, _result = _write(tmp_path)

    assert summary["anchor_found_by"] == "source_observation_id"


def test_sidecar_summary_includes_old_annotations_preserved_true(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path)

    summary, _result = _write(tmp_path, evidence_dir=evidence_dir)

    assert summary["old_annotations_preserved"] is True


def test_sidecar_timing_aligns_to_final_clip_metadata(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path)
    (evidence_dir / "sink_metadata.json").write_text(
        "\n".join(
            [
                json.dumps({"frame_num": 0, "pts": 15_000_000_000, "frame_uuid": "frame-start"}),
                json.dumps({"frame_num": 120, "pts": 20_000_000_000, "frame_uuid": "frame-34646"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary, result = _write(tmp_path, evidence_dir=evidence_dir)
    rows = _read_jsonl(Path(result["annotations_path"]))
    trigger_row = next(
        row
        for row in rows
        if any(
            obj.get("annotation_role") == "watchlist_trigger_face"
            for obj in row.get("objects", [])
        )
    )

    assert trigger_row["t_ms"] == 5000
    assert trigger_row["clip_frame_index"] == 120
    assert trigger_row["clip_timeline_match"] == "metadata_frame_uuid"
    assert summary["clip_timeline_alignment"]["status"] == "aligned"
    assert summary["visual_binding_status"] == "unverified"
    assert summary["evidence_visual_status"] == "unverified"
    assert summary["visual_binding_reason"] == "canonical_duration_not_verified"
    assert summary["source_observation_id"] == SOURCE_OBSERVATION_ID
    assert summary["frame_identity_method"] == "frame_uuid"
    assert summary["frame_identity_confidence"] == "none"
    assert summary["trigger_face_row_exists"] is True
    assert summary["trigger_face_row_passed_freshness_guard"] is True


def test_stale_pts_fallback_row_is_rejected_fail_closed(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path)
    (evidence_dir / "sink_metadata.json").write_text(
        json.dumps({"frame_num": 0, "pts": 34_646}) + "\n",
        encoding="utf-8",
    )
    event = _event()
    message = _frame_message(created_at="2026-06-06T07:28:44.614117Z")

    summary, result = _write(
        tmp_path,
        event=event,
        messages=[message],
        evidence_dir=evidence_dir,
    )
    rows = _read_jsonl(Path(result["annotations_path"]))

    assert rows == []
    assert summary["annotation_status"] == "missing_frame_metadata"
    assert summary["production_ready"] is False
    assert summary["clip_timeline_alignment"]["wall_clock_filter_enabled"] is True
    assert summary["clip_timeline_alignment"]["wall_clock_filter_rejected_messages"] == 1
    assert summary["visual_binding_status"] == "unverified"
    assert summary["evidence_visual_status"] == "unverified"
    assert summary["visual_binding_reason"] == "canonical_duration_not_verified"
    assert summary["frame_identity_confidence"] == "none"
    assert summary["trigger_face_row_exists"] is False
    assert summary["trigger_face_row_passed_freshness_guard"] is False


def test_fresh_pts_fallback_row_is_allowed(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path)
    (evidence_dir / "sink_metadata.json").write_text(
        json.dumps({"frame_num": 0, "pts": 34_646}) + "\n",
        encoding="utf-8",
    )
    event = _event()
    message = _frame_message(created_at="2026-06-06T08:01:26.171308Z")

    summary, result = _write(
        tmp_path,
        event=event,
        messages=[message],
        evidence_dir=evidence_dir,
    )
    rows = _read_jsonl(Path(result["annotations_path"]))

    assert len(rows) >= 1
    assert rows[0]["clip_timeline_match"] == "metadata_frame_pts_exact"
    assert rows[0]["displayable"] is True
    assert rows[0]["objects"][0]["frame_annotation_created_at"] == (
        "2026-06-06T08:01:26.171308Z"
    )
    assert summary["annotation_status"] == "complete"
    assert summary["rows_matched_by_pts_fallback"] >= 1
    assert summary["rows_rejected_stale_cache"] == 0
    assert summary["visual_binding_status"] == "unverified"
    assert summary["frame_identity_method"] == "frame_pts_fresh"
    assert summary["frame_identity_confidence"] == "none"


def test_sidecar_collapses_multiple_objects_to_one_row_per_metadata_frame(
    tmp_path: Path,
) -> None:
    evidence_dir = _evidence_dir(tmp_path)
    (evidence_dir / "sink_metadata.json").write_text(
        "\n".join(
            [
                json.dumps({"frame_num": 0, "pts": 34_646, "frame_uuid": "frame-34646"}),
                json.dumps({"frame_num": 1, "pts": 76_312, "frame_uuid": "frame-next"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    face = _frame_message()
    person = _intrusion_frame_message()["objects"][0]
    second_person = copy.deepcopy(person)
    second_person["object_id"] = "person-14"
    second_person["track_id"] = "14"
    second_person["source_observation_id"] = "person:c1e_rtsp_replay:14:34646"
    face["objects"] = [person, second_person]

    summary, result = _write(
        tmp_path,
        messages=[face],
        evidence_dir=evidence_dir,
        config=_config(enabled=True, freshness_guard_mode="metadata_pts"),
    )
    rows = _read_jsonl(Path(result["annotations_path"]))

    assert len(rows) == 1
    assert summary["rows_written"] == 1
    assert summary["annotations_written"] == 1
    assert rows[0]["frame_uuid"] == "frame-34646"
    assert rows[0]["clip_timeline_match"] == "metadata_frame_uuid"
    assert len(rows[0]["objects"]) == 2
    assert {obj["object_type"] for obj in rows[0]["objects"]} == {"person"}


def test_uuid_keyed_sink_metadata_prevents_looped_pts_misalignment(
    tmp_path: Path,
) -> None:
    evidence_dir = _evidence_dir(tmp_path)
    (evidence_dir / "sink_metadata.json").write_text(
        "\n".join(
            [
                json.dumps({"frame_num": 0, "pts": 34_646, "uuid": "loop-a-frame"}),
                json.dumps({"frame_num": 1, "pts": 34_646, "uuid": "loop-b-frame"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    event = _event()
    event["frame_uuid"] = "loop-b-frame"
    message = _frame_message(frame_pts=34_646, frame_uuid="loop-b-frame")

    summary, result = _write(
        tmp_path,
        event=event,
        messages=[message],
        evidence_dir=evidence_dir,
        config=_config(enabled=True, freshness_guard_mode="metadata_pts"),
    )
    rows = _read_jsonl(Path(result["annotations_path"]))

    assert len(rows) == 1
    assert rows[0]["frame_uuid"] == "loop-b-frame"
    assert rows[0]["clip_frame_index"] == 1
    assert rows[0]["clip_timeline_match"] == "metadata_frame_uuid"
    assert summary["rows_matched_by_frame_uuid"] == 1
    assert summary["rows_matched_by_pts_fallback"] == 0


def test_sidecar_groups_different_source_frames_by_final_metadata_pts(
    tmp_path: Path,
) -> None:
    evidence_dir = _evidence_dir(tmp_path)
    (evidence_dir / "sink_metadata.json").write_text(
        json.dumps({"frame_num": 0, "pts": 10_000_000_000, "frame_uuid": "final-frame"})
        + "\n",
        encoding="utf-8",
    )
    event = _intrusion_event()
    event["event_ts_ms"] = 1_780_918_000_000
    event["created_at"] = 1_780_918_000_000
    event["frame_pts"] = 10_000_000_000
    event["frame_uuid"] = "final-frame"
    first = _intrusion_frame_message()
    first["frame_pts"] = 9_970_000_000
    first["frame_uuid"] = "source-a"
    first["timestamp_ms"] = 1_780_918_000_000
    first["created_at"] = 1_780_918_000_000
    second = copy.deepcopy(first)
    second["frame_pts"] = 10_010_000_000
    second["frame_uuid"] = "source-b"
    second["objects"][0]["source_observation_id"] = "person:c1e_rtsp_replay:13:10010"
    second["objects"][0]["track_id"] = "13"
    second["objects"][0]["bbox"]["xyxy"] = [104, 122, 224, 362]

    summary, result = _write(
        tmp_path,
        event=event,
        messages=[first, second],
        evidence_dir=evidence_dir,
        config=_config(
            enabled=True,
            event_types={"intrusion", "watchlist_hit"},
            require_trigger_face=False,
            freshness_guard_mode="metadata_pts",
        ),
    )
    rows = _read_jsonl(Path(result["annotations_path"]))

    assert len(rows) == 1
    assert rows[0]["frame_uuid"] == "final-frame"
    assert rows[0]["frame_pts"] == 10_000_000_000
    assert rows[0]["matched_metadata_pts"] == 10_000_000_000
    assert len(rows[0]["objects"]) == 1
    assert rows[0]["objects"][0]["source_frame_uuid"] == "source-b"
    assert rows[0]["objects"][0]["source_frame_pts"] == 10_010_000_000
    assert rows[0]["objects"][0]["bbox"]["xyxy"] == [104.0, 122.0, 224.0, 362.0]
    assert summary["rows_written"] == 1
    assert summary["annotations_written"] == 1
    assert summary["collapse_input_objects"] == 2
    assert summary["collapse_output_objects"] == 1
    assert summary["collapse_identity_many_to_one_dropped"] == 1
    assert summary["collapse_identity_many_to_one_replaced"] == 1
    reader_summary = summary["frame_cache_reader_summary"]
    assert reader_summary["duplicate_frame_anchor_messages"] == 0


def test_sidecar_dedups_duplicate_source_frame_objects(
    tmp_path: Path,
) -> None:
    evidence_dir = _evidence_dir(tmp_path)
    (evidence_dir / "sink_metadata.json").write_text(
        json.dumps({"frame_num": 0, "pts": 34_646, "frame_uuid": "frame-34646"})
        + "\n",
        encoding="utf-8",
    )
    message = _frame_message()
    duplicate = copy.deepcopy(message)

    summary, result = _write(
        tmp_path,
        messages=[message, duplicate],
        evidence_dir=evidence_dir,
        config=_config(enabled=True, freshness_guard_mode="metadata_pts"),
    )
    rows = _read_jsonl(Path(result["annotations_path"]))

    assert len(rows) == 1
    assert len(rows[0]["objects"]) == 1
    assert rows[0]["objects"][0]["track_id"] == "13"
    assert summary["collapse_input_objects"] == 2
    assert summary["collapse_output_objects"] == 1
    assert summary["collapse_duplicate_fingerprint_dropped"] == 1
    reader_summary = summary["frame_cache_reader_summary"]
    assert reader_summary["duplicate_frame_uuid_messages"] == 1
    assert reader_summary["duplicate_frame_pts_messages"] == 1
    assert reader_summary["duplicate_frame_anchor_messages"] == 1
    assert reader_summary["max_messages_per_frame_anchor"] == 2


def test_metadata_pts_guard_rejects_old_wall_clock_loop_rows(
    tmp_path: Path,
) -> None:
    evidence_dir = _evidence_dir(tmp_path)
    (evidence_dir / "sink_metadata.json").write_text(
        json.dumps({"frame_num": 0, "pts": 34_646}) + "\n",
        encoding="utf-8",
    )
    event = _event()
    message = _frame_message(created_at="2026-06-06T07:28:44.614117Z")

    summary, result = _write(
        tmp_path,
        event=event,
        messages=[message],
        evidence_dir=evidence_dir,
        config=_config(enabled=True, freshness_guard_mode="metadata_pts"),
    )
    rows = _read_jsonl(Path(result["annotations_path"]))

    assert rows == []
    assert summary["freshness_guard_mode"] == "metadata_pts"
    assert summary["clip_timeline_alignment"]["freshness_guard_mode"] == "metadata_pts"
    assert summary["clip_timeline_alignment"]["wall_clock_filter_enabled"] is True
    assert summary["clip_timeline_alignment"]["wall_clock_filter_rejected_messages"] == 1
    assert summary["rows_rejected_stale_cache"] == 0
    assert summary["rows_rejected_epoch_mismatch"] == 0
    assert summary["rows_rejected_pts_non_unique"] == 0
    assert summary["annotation_status"] == "missing_frame_metadata"


def test_replay_first_can_be_ready_when_event_inside_but_not_centered(
    tmp_path: Path,
) -> None:
    evidence_dir = _evidence_dir(tmp_path)
    (evidence_dir / "sink_metadata.json").write_text(
        "\n".join(
            [
                json.dumps({"frame_num": 0, "pts": 10_000_000_000}),
                json.dumps({"frame_num": 1, "pts": 13_000_000_000}),
                json.dumps({"frame_num": 239, "pts": 19_968_000_000}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    event = _event()
    event["payload"]["observation"]["frame_pts"] = 13_000_000_000
    event["payload"]["media"]["frame_pts"] = 13_000_000_000
    message = _frame_message(frame_pts=13_000_000_000)

    summary, result = _write(
        tmp_path,
        event=event,
        messages=[message],
        evidence_dir=evidence_dir,
        config=_config(
            enabled=True,
            freshness_guard_mode="metadata_pts",
            require_event_centered=False,
        ),
    )
    rows = _read_jsonl(Path(result["annotations_path"]))

    assert rows
    assert summary["event_pts_inside_clip"] is True
    assert summary["event_center_required"] is False
    assert summary["event_centered_in_clip"] is False
    assert summary["canonical_clip"] is True
    assert summary["production_ready"] is False
    assert "person_context_missing" in summary["production_ready_failures"]
    assert "event_not_centered_in_clip" not in summary["production_ready_failures"]


def test_visual_binding_verified_when_production_ready_trigger_uuid_bound() -> None:
    summary = _trigger_visual_binding_summary(
        event=_event(),
        annotations=[
            {
                "object_type": "face",
                "annotation_role": "watchlist_trigger_face",
                "source_observation_id": SOURCE_OBSERVATION_ID,
                "displayable": True,
                "clip_timeline_match": "metadata_frame_uuid",
                "frame_uuid": "frame-34646",
                "frame_pts": 34_646,
                "label": {"kind": "known_face"},
            }
        ],
        production_ready=True,
        annotation_status="complete",
        fallback_reason=None,
    )

    assert summary["visual_binding_status"] == "verified"
    assert summary["visual_binding_reason"] == "production_sidecar_trigger_bound"
    assert summary["frame_identity_method"] == "frame_uuid"
    assert summary["frame_identity_confidence"] == "high"
    assert summary["trigger_face_row_exists"] is True
    assert summary["trigger_face_row_passed_freshness_guard"] is True


def test_visual_binding_verified_with_fresh_pts_fallback_medium_confidence() -> None:
    summary = _trigger_visual_binding_summary(
        event=_event(),
        annotations=[
            {
                "object_type": "face",
                "annotation_role": "watchlist_trigger_face",
                "source_observation_id": SOURCE_OBSERVATION_ID,
                "displayable": True,
                "clip_timeline_match": "metadata_frame_pts_exact",
                "frame_uuid": "frame-34646",
                "frame_pts": 34_646,
                "label": {"kind": "known_face"},
            }
        ],
        production_ready=True,
        annotation_status="complete",
        fallback_reason=None,
    )

    assert summary["visual_binding_status"] == "verified"
    assert summary["frame_identity_method"] == "frame_pts_fresh"
    assert summary["frame_identity_confidence"] == "medium"


def test_writer_uses_event_payload_bridge_without_identity_patches(tmp_path: Path) -> None:
    summary, result = _write(tmp_path)
    rows = _read_jsonl(Path(result["annotations_path"]))
    objects = [obj for row in rows for obj in row.get("objects", [])]

    assert summary["identity_source"] == "event_payload_bridge"
    assert any(obj.get("identity_source") == "event_payload_bridge" for obj in objects)


def _write(
    tmp_path: Path,
    *,
    event: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    evidence_dir: Path | None = None,
    config: dict[str, Any] | None = None,
    state: FrameCacheSidecarRunState | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    out_dir = evidence_dir or _evidence_dir(tmp_path)
    return write_frame_cache_identity_sidecar(
        event=event or _event(),
        evidence_dir=str(out_dir),
        raw_clip_path=str(out_dir / "raw_clip.mov"),
        metadata_path=str(out_dir / "sink_metadata.json"),
        redis_client=FakeRedis(messages or [_frame_message()]),
        config=config or _config(enabled=True),
        state=state or FrameCacheSidecarRunState(),
    )


def _config(**overrides: Any) -> dict[str, Any]:
    env = {
        "FRAME_CACHE_SIDECAR_ENABLED": "false",
        "FRAME_CACHE_SIDECAR_EVENT_TYPES": "watchlist_hit",
        "FRAME_CACHE_SIDECAR_REQUIRE_TRIGGER_FACE": "true",
        "FRAME_CACHE_SIDECAR_WRITE_MODE": "sidecar_only",
        "FRAME_CACHE_SIDECAR_FAIL_OPEN": "true",
        "FRAME_CACHE_SIDECAR_LOOKBACK_COUNT": "10000",
        "FRAME_CACHE_SIDECAR_MAX_SCAN": "20000",
        "FRAME_CACHE_SIDECAR_MAX_EVENTS_PER_RUN": "5",
        "FRAME_CACHE_SIDECAR_OUTPUT_ANNOTATIONS": "annotations.frame_cache.identity.jsonl",
        "FRAME_CACHE_SIDECAR_OUTPUT_SUMMARY": "summary.frame_cache.identity.json",
    }
    config = load_frame_cache_sidecar_config(env)
    config.update(overrides)
    return config


def _evidence_dir(tmp_path: Path) -> Path:
    evidence_dir = tmp_path / "evidence" / "event-1"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "annotations.jsonl").write_text('{"old":true}\n', encoding="utf-8")
    (evidence_dir / "summary.json").write_text('{"old_summary":true}\n', encoding="utf-8")
    (evidence_dir / "raw_clip.mov").write_bytes(b"video")
    (evidence_dir / "sink_metadata.json").write_text(
        json.dumps({"frame_num": 0, "pts": 34_646, "frame_uuid": "frame-34646"})
        + "\n",
        encoding="utf-8",
    )
    return evidence_dir


def _event(*, event_id: str = "event-1") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "created_at": EVENT_CREATED_AT,
        "event_type": "watchlist_hit",
        "source_event_id": "watchlist_hit:face:c1e_rtsp_replay:13:34646:2",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "frame_uuid": "frame-34646",
        "event_ts_ms": EVENT_TS_MS,
        "payload": {
            "match": {
                "source_observation_id": SOURCE_OBSERVATION_ID,
                "similarity": 0.91,
                "threshold": 0.5,
            },
            "matched_person": {
                "person_id": "person-1",
                "external_person_id": "test:archive:reese",
                "display_name": "Reese",
            },
            "observation": {
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam_c1e_rtsp_replay",
                "timestamp_ms": EVENT_TS_MS,
                "frame_pts": 34_646,
                "frame_uuid": "frame-34646",
                "face_bbox": {"xyxy": [10, 20, 30, 40], "confidence": 0.88},
            },
            "media": {
                "event_ts_ms": EVENT_TS_MS,
                "frame_pts": 34_646,
                "frame_uuid": "frame-34646",
            },
        },
    }


def _intrusion_event() -> dict[str, Any]:
    return {
        "event_id": "event-intrusion-1",
        "created_at": EVENT_CREATED_AT,
        "event_type": "intrusion",
        "source_event_id": "savant_security:c1e_rtsp_replay:13:intrusion:1780000000000",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "track_id": "13",
        "frame_uuid": "frame-intrusion",
        "frame_pts": 20_000_000_000,
        "event_ts_ms": EVENT_TS_MS,
    }


def _frame_message(
    *,
    source_observation_id: str = SOURCE_OBSERVATION_ID,
    frame_pts: int = 34_646,
    frame_uuid: str = "frame-34646",
    timestamp_ms: int = EVENT_TS_MS,
    include_forbidden: bool = False,
    created_at: str = "2026-06-06T08:01:26.171308Z",
) -> dict[str, Any]:
    face: dict[str, Any] = {
        "object_id": "face-1",
        "object_type": "face",
        "track_id": "13",
        "source_observation_id": source_observation_id,
        "bbox": {
            "format": "xyxy",
            "xyxy": [10, 20, 30, 40],
            "confidence": 0.88,
            "coordinate_space": "pixel",
        },
        "quality": {"detector_confidence": 0.88},
    }
    if include_forbidden:
        face["embedding_vector"] = [0.1, 0.2]
        face["image_bytes"] = "abc123"
        face["crop_bytes"] = "def456"
    return {
        "schema_version": "1.0",
        "message_type": "frame_annotation",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "frame_pts": frame_pts,
        "frame_uuid": frame_uuid,
        "frame_num": 1,
        "timestamp_ms": timestamp_ms,
        "created_at": created_at,
        "objects": [face],
    }


def _intrusion_frame_message() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "message_type": "frame_annotation",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "frame_pts": 20_000_000_000,
        "frame_uuid": "frame-intrusion",
        "frame_num": 120,
        "timestamp_ms": 20_000,
        "created_at": "2026-06-06T08:01:26.171308Z",
        "objects": [
            {
                "object_id": "person-13",
                "object_type": "person",
                "track_id": "13",
                "source_observation_id": "person:c1e_rtsp_replay:13:20000",
                "bbox": {
                    "format": "xyxy",
                    "xyxy": [100, 120, 220, 360],
                    "confidence": 0.91,
                    "coordinate_space": "pixel",
                },
                "quality": {"detector_confidence": 0.91},
            }
        ],
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
