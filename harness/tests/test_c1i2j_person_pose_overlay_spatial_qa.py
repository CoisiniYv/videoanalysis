"""C1I.2j person/pose bbox overlay spatial QA diagnostics."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DIAGNOSE = ROOT / "scripts" / "tools" / "diagnose_overlay_alignment.py"
SMOKE = (
    ROOT
    / "scripts"
    / "smoke"
    / "current"
    / "check_c1i2j_person_pose_overlay_spatial_qa.sh"
)
CONTINUOUS_ANNOTATION = (
    ROOT / "services" / "media-worker" / "app" / "continuous_annotation.py"
)


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("diagnose_overlay_alignment", DIAGNOSE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _person_record(
    *,
    role: str = "person_context",
    source: str = "person_bbox_observations.person_bbox",
    bbox: dict[str, Any] | None = None,
    track_id: str = "7",
    offset: int = 1000,
) -> dict[str, Any]:
    return {
        "record_type": "object_annotation",
        "object_type": "person",
        "annotation_role": role,
        "track_id": track_id,
        "time_offset_ms": offset,
        "time_alignment_status": "estimated",
        "frame_pts": 123_000_000,
        "frame_num": 42,
        "bbox": bbox
        or {
            "format": "xyxy",
            "xyxy": [100, 120, 300, 720],
            "coordinate_space": "pixel",
            "source": source,
        },
        "label": {"kind": "behavior_event" if role == "behavior_event" else "object_detection"},
        "action": {"event_type": "intrusion" if role == "behavior_event" else None},
    }


def test_diagnose_overlay_alignment_supports_object_person() -> None:
    text = DIAGNOSE.read_text(encoding="utf-8")
    assert "--object" in text
    assert "choices=OBJECT_CHOICES" in text
    assert '"person"' in text


def test_diagnose_overlay_alignment_supports_object_all(tmp_path: Path) -> None:
    module = _load_module()
    ann = tmp_path / "annotations.jsonl"
    records = [
        _person_record(),
        {
            "record_type": "object_annotation",
            "object_type": "face",
            "time_offset_ms": 1000,
            "bbox": {"format": "xyxy", "xyxy": [10, 10, 40, 40]},
        },
    ]
    ann.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")

    items = list(module.iter_object_annotations(ann, "all"))

    assert [item["object_type"] for item in items] == ["person", "face"]


def test_person_bbox_xyxy_parses_correctly() -> None:
    module = _load_module()

    assert module.bbox_to_xyxy({"format": "xyxy", "xyxy": [1, 2, 3, 4]}) == (
        1.0,
        2.0,
        3.0,
        4.0,
    )
    assert module.bbox_to_xyxy({"format": "cxcywh", "values": [10, 20, 4, 6]}) == (
        8.0,
        17.0,
        12.0,
        23.0,
    )


def test_person_context_and_behavior_event_are_counted_separately() -> None:
    module = _load_module()
    audit = module.audit_person_annotations(
        [_person_record(), _person_record(role="behavior_event", source="payload.person_bbox")],
        width=1920,
        height=1080,
    )

    assert audit["person_annotation_count"] == 2
    assert audit["person_context_count"] == 1
    assert audit["behavior_event_count"] == 1
    assert audit["person_bbox_source_distribution"] == {
        "person_bbox_observations.person_bbox": 1,
        "payload.person_bbox": 1,
    }


def test_bbox_out_of_frame_is_detected() -> None:
    module = _load_module()
    audit = module.audit_person_annotations(
        [
            _person_record(
                bbox={
                    "format": "xyxy",
                    "xyxy": [-10, 0, 2000, 900],
                    "coordinate_space": "pixel",
                    "source": "person_bbox_observations.person_bbox",
                }
            )
        ],
        width=1920,
        height=1080,
    )

    assert audit["person_bbox_out_of_frame_count"] == 1
    assert audit["diagnosis"] == "PERSON_SPATIAL_MISALIGNMENT"


def test_person_debug_frame_output_path_is_stable(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()

    def fake_draw_frame(*args: Any, **kwargs: Any) -> bool:
        out = Path(args[6])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"png")
        return True

    monkeypatch.setattr(module, "draw_frame", fake_draw_frame)
    created = module.create_person_triptych_frames(
        ffmpeg="ffmpeg",
        clip="raw_clip.mov",
        bundle_id="bundle-1",
        persons=[_person_record(offset=1000)],
        width=1920,
        height=1080,
        out_dir=tmp_path,
        max_count=1,
    )

    assert len(created) == 3
    assert Path(created[0]).name == "person_00_t0600ms__a_before.png"
    assert Path(created[1]).name == "person_00_t1000ms__b_at.png"
    assert Path(created[2]).name == "person_00_t1400ms__c_after.png"


def test_summary_contains_diagnosis() -> None:
    module = _load_module()
    audit = module.audit_person_annotations([_person_record()], width=1920, height=1080)

    assert audit["diagnosis"] == "MODEL_BBOX_LOOSE_BUT_VALID"
    assert audit["person_time_anchor_status"] == "PERSON_TIME_ANCHOR_NOT_FRAME_BASED"


def test_c1i2j_does_not_modify_face_overlay_logic() -> None:
    diagnose_text = DIAGNOSE.read_text(encoding="utf-8")
    continuous_text = CONTINUOUS_ANNOTATION.read_text(encoding="utf-8")
    assert "from app.continuous_annotation" not in diagnose_text
    assert "sink_frame_offset_index" in continuous_text
    assert "exact_frame_pts" in continuous_text


def test_c1i2j_smoke_contract_exists() -> None:
    assert SMOKE.exists()
    text = SMOKE.read_text(encoding="utf-8")
    assert "person_pose_overlay_spatial_qa_summary.json" in text
    assert "person_pose_overlay_spatial_qa_report.md" in text
    assert "PASS_C1I2J_PERSON_POSE_OVERLAY_QA_MODEL_BBOX_LOOSE" in text
