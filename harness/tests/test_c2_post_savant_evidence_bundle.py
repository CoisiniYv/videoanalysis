"""C2 post-Savant production evidence bundle tests."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"


def test_metadata_rows_trim_to_decoded_video_frame_count(tmp_path: Path) -> None:
    bundle = _activate_bundle_builder()
    input_dir = _make_sink_output(tmp_path, frame_count=120)
    output_dir = tmp_path / "evidence" / "event-c2-2"

    result = bundle.build_post_savant_evidence_bundle(
        input_dir=input_dir,
        output_dir=output_dir,
        copy_video=True,
        trim_sidecar_to_video=True,
        decoded_frame_count_reader=lambda _path: 106,
    )

    sidecar_rows = _read_jsonl(output_dir / "annotations.frame_cache.identity.jsonl")
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    sidecar_summary = json.loads((output_dir / "summary.frame_cache.identity.json").read_text(encoding="utf-8"))

    assert len(sidecar_rows) == 106
    assert summary == sidecar_summary
    assert result.report["original_metadata_frame_count"] == 120
    assert result.report["decoded_video_frame_count"] == 106
    assert result.report["sidecar_frame_count"] == 106
    assert result.report["trim_occurred"] is True
    assert result.report["timeline_reconciliation_status"] == "needs_visual_or_time_mapping_verification"
    assert summary["original_metadata_frame_count"] == 120
    assert summary["decoded_video_frame_count"] == 106
    assert summary["sidecar_frame_count"] == 106
    assert summary["frame_count"] == 106
    assert summary["sidecar_trimmed"] is True
    assert summary["trim_occurred"] is True
    assert summary["timeline_reconciliation_status"] == "needs_visual_or_time_mapping_verification"
    assert summary["production_ready"] is False
    assert summary["annotation_status"] == "timeline_reconciliation_unverified"
    assert summary["visual_evidence_status"] == "unverified"
    assert "metadata_frame_count_exceeds_decoded_video_frames" in summary["limitations"]
    assert "sidecar_trimmed_to_playable_frame_count" in summary["limitations"]


def test_metadata_rows_matching_decoded_video_frames_are_verified(tmp_path: Path) -> None:
    bundle = _activate_bundle_builder()
    input_dir = _make_sink_output(tmp_path, frame_count=240)
    output_dir = tmp_path / "evidence" / "event-c2-2r"

    result = bundle.build_post_savant_evidence_bundle(
        input_dir=input_dir,
        output_dir=output_dir,
        copy_video=True,
        trim_sidecar_to_video=True,
        max_fps="8/1",
        min_fps="2/1",
        fps_gating_applied=True,
        source_input_fps_estimate=23.976,
        decoded_frame_count_reader=lambda _path: 240,
    )

    summary = result.summary
    assert summary["original_metadata_frame_count"] == 240
    assert summary["decoded_video_frame_count"] == 240
    assert summary["sidecar_frame_count"] == 240
    assert summary["frame_count"] == 240
    assert summary["trim_occurred"] is False
    assert summary["timeline_reconciliation_status"] == "frame_counts_match"
    assert summary["production_ready"] is True
    assert summary["visual_evidence_status"] == "verified_same_stream_metadata"
    assert summary["fps"]["max_fps"] == "8/1"
    assert summary["fps"]["min_fps"] == "2/1"
    assert summary["fps"]["fps_gating_applied"] is True
    assert summary["fps"]["source_input_fps_estimate"] == 23.976


def test_required_standard_bundle_files_are_created(tmp_path: Path) -> None:
    bundle = _activate_bundle_builder()
    input_dir = _make_sink_output(tmp_path, frame_count=2)
    output_dir = tmp_path / "evidence" / "event-c2-2"

    bundle.build_post_savant_evidence_bundle(
        input_dir=input_dir,
        output_dir=output_dir,
        copy_video=True,
        trim_sidecar_to_video=True,
        decoded_frame_count_reader=lambda _path: 2,
    )

    assert (output_dir / "raw_clip.mov").is_file()
    assert (output_dir / "sink_metadata.json").is_file()
    assert (output_dir / "annotations.frame_cache.identity.jsonl").is_file()
    assert (output_dir / "summary.json").is_file()
    assert (output_dir / "summary.frame_cache.identity.json").is_file()


def test_bundle_builder_does_not_use_db_or_redis_access(tmp_path: Path, monkeypatch: Any) -> None:
    bundle = _activate_bundle_builder()
    input_dir = _make_sink_output(tmp_path, frame_count=2)
    output_dir = tmp_path / "evidence" / "event-c2-2"

    monkeypatch.setitem(sys.modules, "redis", _FailingModule())
    monkeypatch.setitem(sys.modules, "psycopg", _FailingModule())
    monkeypatch.setitem(sys.modules, "psycopg2", _FailingModule())

    result = bundle.build_post_savant_evidence_bundle(
        input_dir=input_dir,
        output_dir=output_dir,
        copy_video=True,
        trim_sidecar_to_video=True,
        decoded_frame_count_reader=lambda _path: 2,
    )

    assert result.summary["annotation_source"] == "post_savant_sink_metadata"
    assert result.summary["production_ready"] is True


def test_no_source_observation_id_keeps_known_face_count_zero(tmp_path: Path) -> None:
    bundle = _activate_bundle_builder()
    input_dir = _make_sink_output(tmp_path, frame_count=3)
    output_dir = tmp_path / "evidence" / "event-c2-2"

    result = bundle.build_post_savant_evidence_bundle(
        input_dir=input_dir,
        output_dir=output_dir,
        copy_video=True,
        trim_sidecar_to_video=True,
        decoded_frame_count_reader=lambda _path: 3,
    )

    assert result.summary["object_counts"]["known_face"] == 0
    assert result.summary["known_face_objects_count"] == 0
    assert result.summary["trigger_face_row_exists"] is False
    assert "source_observation_id_missing_in_native_metadata" in result.summary["limitations"]
    assert "watchlist_trigger_identity_binding_not_verified_in_c2_2r" in result.summary["limitations"]


def test_bundle_builder_fails_when_no_person_or_face_objects_found(tmp_path: Path) -> None:
    bundle = _activate_bundle_builder()
    input_dir = _make_sink_output(tmp_path, frame_count=3, objects_per_frame=False)
    output_dir = tmp_path / "evidence" / "event-c2-2"

    try:
        bundle.build_post_savant_evidence_bundle(
            input_dir=input_dir,
            output_dir=output_dir,
            copy_video=True,
            trim_sidecar_to_video=True,
            decoded_frame_count_reader=lambda _path: 3,
        )
    except ValueError as exc:
        assert "no_post_savant_person_or_face_objects_found" in str(exc)
    else:
        raise AssertionError("expected no-object bundle build to fail")


def test_no_trim_limitations_when_metadata_fits_video(tmp_path: Path) -> None:
    bundle = _activate_bundle_builder()
    input_dir = _make_sink_output(tmp_path, frame_count=5)
    output_dir = tmp_path / "evidence" / "event-c2-2"

    result = bundle.build_post_savant_evidence_bundle(
        input_dir=input_dir,
        output_dir=output_dir,
        copy_video=True,
        trim_sidecar_to_video=True,
        decoded_frame_count_reader=lambda _path: 8,
    )

    assert result.summary["original_metadata_frame_count"] == 5
    assert result.summary["decoded_video_frame_count"] == 8
    assert result.summary["sidecar_frame_count"] == 5
    assert result.summary["sidecar_trimmed"] is False
    assert result.summary["timeline_reconciliation_status"] == "needs_visual_or_time_mapping_verification"
    assert result.summary["production_ready"] is False
    assert "metadata_frame_count_less_than_decoded_video_frames" in result.summary["limitations"]
    assert "metadata_frame_count_exceeds_decoded_video_frames" not in result.summary["limitations"]
    assert "sidecar_trimmed_to_playable_frame_count" not in result.summary["limitations"]


def test_output_directory_must_not_exist_without_overwrite(tmp_path: Path) -> None:
    bundle = _activate_bundle_builder()
    input_dir = _make_sink_output(tmp_path, frame_count=2)
    output_dir = tmp_path / "evidence" / "event-c2-2"
    output_dir.mkdir(parents=True)

    try:
        bundle.build_post_savant_evidence_bundle(
            input_dir=input_dir,
            output_dir=output_dir,
            copy_video=True,
            trim_sidecar_to_video=True,
            decoded_frame_count_reader=lambda _path: 2,
        )
    except FileExistsError as exc:
        assert str(output_dir) in str(exc)
    else:
        raise AssertionError("expected existing output directory to fail without overwrite")


def _activate_bundle_builder() -> Any:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if str(MEDIA_WORKER_ROOT) in sys.path:
        sys.path.remove(str(MEDIA_WORKER_ROOT))
    sys.path.insert(0, str(MEDIA_WORKER_ROOT))
    return importlib.import_module("app.post_savant_evidence_bundle")


def _make_sink_output(
    tmp_path: Path,
    *,
    frame_count: int,
    objects_per_frame: bool = True,
) -> Path:
    input_dir = tmp_path / "sink"
    input_dir.mkdir()
    (input_dir / "video.mov").write_bytes(b"fake video")
    rows = [
        _frame(index=index, objects=_objects(index) if objects_per_frame else [])
        for index in range(frame_count)
    ]
    (input_dir / "metadata.json").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return input_dir


def _frame(index: int, objects: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "VideoFrame",
        "source_id": "c2-poc",
        "uuid": f"frame-{index}",
        "pts": 1_000_000 + index * 41_666_667,
        "width": 1920,
        "height": 1080,
        "objects": objects,
    }


def _objects(index: int) -> list[dict[str, Any]]:
    return [
        {
            "namespace": "yolo26_pose",
            "label": "person",
            "id": 100 + index,
            "confidence": 0.82,
            "track_id": index,
            "track_box": _box(100.0, 160.0, 40.0, 100.0),
            "attributes": [
                {
                    "namespace": "yolo26_pose",
                    "name": "keypoints",
                    "values": [{"confidence": 1.0, "value": {"FloatVector": _keypoints()}}],
                }
            ],
        },
        {
            "namespace": "yolov8_face",
            "label": "face",
            "id": 200 + index,
            "confidence": 0.91,
            "detection_box": _box(100.0, 110.0, 30.0, 30.0),
            "attributes": [
                {
                    "namespace": "yolov8_face",
                    "name": "landmarks",
                    "values": [
                        {
                            "confidence": 1.0,
                            "value": {
                                "FloatVector": [
                                    90.0,
                                    100.0,
                                    110.0,
                                    100.0,
                                    100.0,
                                    110.0,
                                    92.0,
                                    120.0,
                                    108.0,
                                    120.0,
                                ]
                            },
                        }
                    ],
                }
            ],
        },
    ]


def _box(xc: float, yc: float, width: float, height: float) -> dict[str, float]:
    return {"xc": xc, "yc": yc, "width": width, "height": height, "angle": 0.0}


def _keypoints() -> list[float]:
    values = []
    for index in range(17):
        values.extend([float(10 + index), float(20 + index), 0.5])
    return values


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _FailingModule:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected DB/Redis access: {name}")
