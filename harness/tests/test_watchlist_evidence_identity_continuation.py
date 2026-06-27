"""Watchlist evidence identity continuation tests."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
MEDIA_WORKER_ROOT = str(REPO_ROOT / "services" / "media-worker")


def _activate(module_name: str):
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    service_roots = {str(path) for path in (REPO_ROOT / "services").glob("*")}
    sys.path[:] = [path for path in sys.path if path not in service_roots]
    sys.path.insert(0, MEDIA_WORKER_ROOT)
    return importlib.import_module(module_name)


def test_watchlist_identity_marks_trigger_and_threshold_continuations() -> None:
    writer = _activate("app.frame_cache_sidecar_writer")
    patch = {
        "source_observation_id": "face:cam:7:1000",
        "person_id": 5,
        "external_person_id": "demo:reese",
        "display_name": "Reese",
        "gallery_embedding_id": 4,
        "similarity": 0.82,
        "threshold": 0.6,
    }
    continuations = {
        "face:cam:7:1500": {
            "person_id": 5,
            "external_person_id": "demo:reese",
            "display_name": "Reese",
            "gallery_embedding_id": 4,
            "similarity": 0.71,
            "threshold": 0.6,
        }
    }

    rows = writer._apply_event_payload_identity(
        [
            _face("face:cam:7:1000"),
            _face("face:cam:7:1500"),
            _face("face:cam:7:2200"),
        ],
        patch,
        trigger_source_observation_id="face:cam:7:1000",
        continuation_identities=continuations,
    )

    assert rows[0]["label"]["kind"] == "known_face"
    assert rows[0]["annotation_role"] == "watchlist_trigger_face"
    assert rows[0]["label"]["display_name"] == "Reese"
    assert rows[1]["label"]["kind"] == "known_face"
    assert rows[1]["annotation_role"] == "watchlist_threshold_continuation_face"
    assert rows[1]["label"]["similarity"] == 0.71
    assert rows[2]["label"]["kind"] == "unknown_face"
    assert rows[2].get("annotation_role") is None


def test_contract_accepts_threshold_continuations_without_scope_leak() -> None:
    writer = _activate("app.frame_cache_sidecar_writer")
    rows = [
        _frame_row(
            "frame-trigger",
            100_000_000_000,
            [
                _known_face(
                    "face:cam:7:1000",
                    annotation_role="watchlist_trigger_face",
                    similarity=0.82,
                ),
                _person(),
            ],
            t_ms=5_000,
        ),
        _frame_row(
            "frame-continuation",
            101_000_000_000,
            [
                _known_face(
                    "face:cam:7:1500",
                    annotation_role="watchlist_threshold_continuation_face",
                    similarity=0.71,
                ),
                _person(),
            ],
            t_ms=6_000,
        ),
    ]
    counts = writer._count_identity_annotations(rows)

    summary = writer._production_sidecar_contract_summary(
        event={
            "event_type": "watchlist_hit",
            "source_id": "cam",
            "camera_id": "camera-1",
            "payload": {
                "match": {
                    "source_observation_id": "face:cam:7:1000",
                    "similarity": 0.82,
                    "threshold": 0.6,
                },
                "matched_person": {"person_id": 5, "name": "Reese"},
            },
        },
        annotations=rows,
        identity_counts=counts,
        clip_timeline_summary={
            "enabled": True,
            "status": "aligned",
            "rows_total_input": 2,
            "annotations_unmatched": 0,
            "rows_displayable": 2,
            "rows_matched_by_frame_uuid": 2,
            "first_pts": 95_000_000_000,
            "last_pts": 105_000_000_000,
        },
        raw_clip_path="/evidence/raw_clip.mov",
        metadata_path="/evidence/sink_metadata.json",
        final_clip_context={
            "raw_clip_path": "/evidence/raw_clip.mov",
            "sink_metadata_path": "/evidence/sink_metadata.json",
            "raw_clip_duration": 10.0,
            "expected_duration_seconds": 10.0,
            "event_projected_t_s": 5.0,
            "event_pts_inside_clip": True,
            "event_position_ratio": 0.5,
        },
        config={},
    )

    assert counts["known_face_count"] == 2
    assert counts["known_face_threshold_continuation_count"] == 1
    assert summary["identity_scope_status"] == "trigger_plus_threshold_continuations"
    assert summary["known_face_trigger_only"] is False
    assert summary["known_face_threshold_continuation_count"] == 1
    assert "known_face_scope_invalid" not in summary["production_ready_failures"]
    assert summary["production_ready"] is True


def _face(source_observation_id: str) -> dict[str, Any]:
    return {
        "object_type": "face",
        "source_observation_id": source_observation_id,
        "track_id": "7",
        "label": {"kind": "unknown_face"},
    }


def _known_face(
    source_observation_id: str,
    *,
    annotation_role: str,
    similarity: float,
) -> dict[str, Any]:
    return {
        "object_type": "face",
        "source_observation_id": source_observation_id,
        "track_id": "7",
        "annotation_role": annotation_role,
        "label": {
            "kind": "known_face",
            "display_name": "Reese",
            "person_id": 5,
            "similarity": similarity,
            "threshold": 0.6,
        },
    }


def _person() -> dict[str, Any]:
    return {
        "object_type": "person",
        "annotation_role": "person_context",
        "track_id": "7",
        "label": {"kind": "person"},
    }


def _frame_row(
    frame_uuid: str,
    frame_pts: int,
    objects: list[dict[str, Any]],
    *,
    t_ms: int,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source": "frame_annotation_cache",
        "source_id": "cam",
        "camera_id": "camera-1",
        "frame_uuid": frame_uuid,
        "frame_pts": frame_pts,
        "clip_frame_index": t_ms // 1000,
        "clip_timeline_match": "metadata_frame_uuid",
        "displayable": True,
        "t_ms": t_ms,
        "objects": objects,
    }
