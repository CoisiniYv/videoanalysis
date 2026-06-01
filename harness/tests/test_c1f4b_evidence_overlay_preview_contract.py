"""C1F.4b evidence overlay preview contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "tools" / "generate_evidence_overlay_preview.py"
SMOKE = REPO_ROOT / "scripts" / "smoke" / "check_c1f4b_evidence_overlay_preview.sh"
DOC = REPO_ROOT / "docs" / "c1f4b_evidence_overlay_preview.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _make_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "evidence" / "event-1"
    bundle.mkdir(parents=True)
    (bundle / "raw_clip.mov").write_bytes(b"video")
    (bundle / "metadata.json").write_text(
        json.dumps(
            {
                "event": {"event_id": "event-1", "source_id": "c1e_rtsp_replay"},
                "media": {
                    "raw_clip_duration": 15.06,
                    "clip_validation": {
                        "decode_error_count": 2,
                        "decode_error_sample": ["h264 missing reference"],
                    },
                },
                "status": {"clip_status": "generated_corrupt"},
            }
        ),
        encoding="utf-8",
    )
    (bundle / "sink_metadata.json").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "pts": 902184066666,
                        "width": 1920,
                        "height": 1080,
                        "framerate": "24000/1001",
                        "duration": 41708333,
                    }
                ),
                json.dumps(
                    {
                        "pts": 902226066666,
                        "width": 1920,
                        "height": 1080,
                        "framerate": "24000/1001",
                        "duration": 41708333,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle / "annotations.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source_id": "c1e_rtsp_replay",
                "camera_id": "cam",
                "timestamp_ms": 903649,
                "time_offset_ms": 0,
                "frame_pts": 902184066666,
                "objects": [
                    {
                        "object_type": "face",
                        "track_id": "1006",
                        "bbox": {
                            "format": "cxcywh",
                            "values": [960, 540, 100, 120],
                            "confidence": 0.91,
                        },
                        "landmarks": {
                            "format": "5_point",
                            "points": [[900, 500], [980, 500]],
                        },
                        "identity": {
                            "status": "matched",
                            "similarity": 0.51,
                            "display_name": "Reese",
                        },
                        "style": {
                            "bbox_color": "#D50000",
                            "label_color": "#D50000",
                            "label": "Reese 0.51",
                            "line_width": 3,
                        },
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle / "summary.json").write_text(
        json.dumps(
            {
                "annotation_lines": 1,
                "face_objects": 1,
                "matched_objects": 1,
                "unknown_objects": 0,
                "colors_used": ["#D50000"],
                "embedding_leaked": False,
                "image_bytes_leaked": False,
            }
        ),
        encoding="utf-8",
    )
    return bundle


def _generated_preview(tmp_path: Path) -> str:
    bundle = _make_bundle(tmp_path)
    subprocess.run(
        [sys.executable, str(GENERATOR), "--bundle-dir", str(bundle)],
        check=True,
        capture_output=True,
        text=True,
    )
    return (bundle / "preview.html").read_text(encoding="utf-8")


def test_generator_exists() -> None:
    assert GENERATOR.exists()


def test_smoke_script_exists() -> None:
    assert SMOKE.exists()


def test_generator_reads_metadata_json() -> None:
    assert "metadata.json" in _text(GENERATOR)
    assert "_load_json(bundle_dir / \"metadata.json\")" in _text(GENERATOR)


def test_generator_reads_sink_metadata_json() -> None:
    content = _text(GENERATOR)
    assert "sink_metadata.json" in content
    assert "_first_sink_frame" in content
    assert "pts" in content


def test_generator_reads_annotations_jsonl() -> None:
    content = _text(GENERATOR)
    assert "annotations.jsonl" in content
    assert "_load_jsonl(bundle_dir / \"annotations.jsonl\")" in content


def test_generator_writes_preview_html(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "<!doctype html>" in preview
    assert "Evidence Overlay Preview" in preview


def test_preview_uses_raw_clip_mov(tmp_path: Path) -> None:
    assert './raw_clip.mov' in _generated_preview(tmp_path)


def test_preview_uses_video_tag(tmp_path: Path) -> None:
    assert "<video" in _generated_preview(tmp_path)


def test_preview_uses_canvas_overlay(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "<canvas" in preview
    assert "getContext(\"2d\")" in preview


def test_preview_supports_cxcywh_bbox_conversion(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "bboxToRect" in preview
    assert "cxcywh" in preview
    assert "cx - w / 2" in preview


def test_preview_supports_style_bbox_color(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "bbox_color" in preview
    assert "ctx.strokeStyle = color" in preview


def test_preview_supports_landmarks_toggle(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "showLandmarks" in preview
    assert "drawLandmarks" in preview


def test_preview_supports_labels_toggle(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "showLabels" in preview
    assert "labelForObject" in preview


def test_preview_supports_unknown_and_matched_filters(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "showUnknown" in preview
    assert "showMatched" in preview
    assert "objectVisible" in preview


def test_preview_uses_frame_pts_alignment_against_first_sink_pts(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "firstVideoFramePts" in preview
    assert "targetPts = state.firstVideoFramePts + currentTime * NS_PER_SECOND" in preview
    assert "line._framePts" in preview


def test_preview_does_not_rely_only_on_time_offset_ms(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "time_offset_ms_fallback" in preview
    assert "line.frame_pts" in preview
    assert "time_offset_ms / 1000" not in preview


def test_preview_does_not_reference_annotated_clip_as_required_media(tmp_path: Path) -> None:
    assert "annotated_clip" not in _generated_preview(tmp_path).lower()


def test_preview_does_not_use_ffmpeg(tmp_path: Path) -> None:
    assert "ffmpeg" not in _generated_preview(tmp_path).lower()


def test_preview_does_not_use_rtsp_url(tmp_path: Path) -> None:
    assert "rtsp://" not in _generated_preview(tmp_path).lower()


def test_preview_does_not_use_external_cdn(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path).lower()
    assert "http://" not in preview
    assert "https://" not in preview
    assert "cdn" not in preview


def test_preview_displays_clip_validation_warnings(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "decodeWarnings" in preview
    assert "warningBadge" in preview
    assert "decode_error_count" in preview


def test_preview_displays_active_object_count(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "activeObjects" in preview
    assert "active objects" in preview


def test_preview_handles_generated_corrupt_warning_as_non_fatal(tmp_path: Path) -> None:
    preview = _generated_preview(tmp_path)
    assert "generated_corrupt" in preview
    assert "warning" in preview.lower()
    assert "throw new Error" not in preview.split("generated_corrupt", 1)[-1][:200]


def test_docs_preview_only_not_production_frontend() -> None:
    content = _text(DOC).lower()
    assert "preview only" in content
    assert "not the production frontend" in content
    assert "not an api" in content
