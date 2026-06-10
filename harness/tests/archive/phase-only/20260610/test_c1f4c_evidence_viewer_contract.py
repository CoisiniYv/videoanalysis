"""C1F.4c file-based evidence viewer contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = REPO_ROOT / "services" / "evidence-viewer"
APP_ROOT = SERVICE_ROOT / "app"
STATIC_ROOT = APP_ROOT / "static"
COMPOSE = REPO_ROOT / "infra" / "docker-compose.midterm.yml"
SMOKE = REPO_ROOT / "scripts" / "smoke" / "check_c2_1_viewer_runtime_sidecar.sh"
DOC = REPO_ROOT / "docs" / "c1f4c_unified_file_based_evidence_viewer.md"

if str(SERVICE_ROOT) in sys.path:
    sys.path.remove(str(SERVICE_ROOT))
sys.path.insert(0, str(SERVICE_ROOT))

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.evidence_index import (  # noqa: E402
    EvidencePathError,
    annotation_time_seconds,
    bundle_manifest,
    discover_raw_clip,
    first_sink_frame_pts,
    media_type_for_path,
    normalize_bbox,
    object_label,
    object_style,
    parse_json_or_jsonl_records,
    parse_jsonl_records,
    safe_bundle_dir,
    scan_bundles,
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _make_bundle(root: Path, event_id: str = "event-1", raw_name: str = "raw_clip.mp4") -> Path:
    bundle = root / event_id
    bundle.mkdir(parents=True)
    (bundle / raw_name).write_bytes(b"video")
    (bundle / "metadata.json").write_text(
        json.dumps(
            {
                "event": {
                    "event_id": event_id,
                    "event_type": "watchlist_hit",
                    "source_id": "c1e_rtsp_replay",
                    "camera_id": "cam",
                },
                "media": {
                    "raw_clip_path": f"/evidence/{event_id}/{raw_name}",
                    "clip_validation": {"decode_error_count": 2},
                },
                "annotations": {
                    "frontend_overlay_required": True,
                    "annotation_mode": "continuous_jsonl",
                },
                "status": {"clip_status": "generated_corrupt"},
            }
        ),
        encoding="utf-8",
    )
    (bundle / "summary.json").write_text(
        json.dumps(
            {
                "annotation_lines": 1,
                "face_objects": 1,
                "matched_objects": 0,
                "unknown_objects": 1,
                "colors_used": ["#D50000"],
                "event_type": "watchlist_hit",
                "source_id": "c1e_rtsp_replay",
            }
        ),
        encoding="utf-8",
    )
    (bundle / "annotations.jsonl").write_text(
        json.dumps(
            {
                "frame_pts": 1100000000,
                "timestamp_ms": 100,
                "objects": [
                    {
                        "object_type": "face",
                        "track_id": "7",
                        "bbox": {"format": "cxcywh", "values": [50, 60, 20, 30]},
                        "identity": {"status": "unknown", "similarity": None},
                        "style": {"bbox_color": "#D50000", "reason": "alert_hit"},
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle / "sink_metadata.json").write_text(
        '{"pts": 1000000000, "width": 100, "height": 100, "framerate": "30/1"}\n',
        encoding="utf-8",
    )
    return bundle


def test_service_file_structure_exists() -> None:
    for path in (
        SERVICE_ROOT / "Dockerfile",
        SERVICE_ROOT / "requirements.txt",
        APP_ROOT / "__init__.py",
        APP_ROOT / "main.py",
        APP_ROOT / "config.py",
        APP_ROOT / "evidence_index.py",
        STATIC_ROOT / "index.html",
        STATIC_ROOT / "app.js",
        STATIC_ROOT / "operator.js",
        STATIC_ROOT / "evidence.js",
        STATIC_ROOT / "maintenance.js",
        STATIC_ROOT / "style.css",
        SMOKE,
        DOC,
    ):
        assert path.exists(), path


def test_requirements_are_file_viewer_only() -> None:
    requirements = _text(SERVICE_ROOT / "requirements.txt")
    assert "fastapi" in requirements
    assert "uvicorn" in requirements
    assert "psycopg" not in requirements
    assert "redis" not in requirements


def test_main_defines_required_api_endpoints() -> None:
    content = _text(APP_ROOT / "main.py")
    for route in (
        '@app.get("/")',
        '@app.get("/health")',
        '"/api/v1/{path:path}"',
        '"/media/{path:path}"',
        '@app.get("/api/bundles")',
        '@app.get("/api/bundles/{event_id}/annotations")',
        '@app.get("/api/bundles/{event_id}/sink-metadata")',
        '@app.get("/api/bundles/{event_id}/media/raw_clip")',
    ):
        assert route in content
    assert "FileResponse" in content
    assert "DATABASE_URL" not in content
    assert "REDIS_URL" not in content


def test_main_keeps_operator_proxy_separate_from_evidence_api() -> None:
    content = _text(APP_ROOT / "main.py")
    assert "operator_api_proxy" in content
    assert "operator_media_proxy" in content
    assert "OPERATOR_PROXY_ALLOWED_PREFIXES" in content
    assert '"maintenance"' in content
    assert "settings.operator_api_base_url" in content
    assert 'return f"{settings.operator_api_base_url}/api/v1/{clean_path}"' in content
    assert 'return f"{settings.operator_api_base_url}/media/{path.strip(\'/\')}"' in content
    assert '"/api/v1/{path:path}"' in content
    assert '"/media/{path:path}"' in content
    assert '"/api/bundles"' in content
    assert "api_bundles(" in content


def test_midterm_compose_serves_operator_portal_on_8090_with_internal_api() -> None:
    content = _text(COMPOSE)
    assert "evidence-viewer:" in content
    assert "container_name: video-analytics-midterm-evidence-viewer" in content
    assert "../services/evidence-viewer" in content
    assert "/data/video-analytics/media/evidence:/evidence:ro" in content
    assert "8090:8090" in content
    assert "OPERATOR_API_BASE_URL: http://api:8000" in content
    assert '"8000:8000"' not in content
    viewer_section = content.split("  evidence-viewer:", 1)[1]
    assert "DATABASE_URL" not in viewer_section
    assert "REDIS_URL" not in viewer_section
    assert "nvidia" not in viewer_section.lower()
    assert "privileged" not in viewer_section.lower()
    assert "api: { condition: service_started }" in viewer_section
    api_section = content.split("  api:", 1)[1].split("\n  postgres:", 1)[0]
    assert "../services/api:/app:rw" in api_section
    assert "/data/video-analytics/media:/data/video-analytics/media:rw" in api_section
    assert "expose:" in api_section
    assert '"8000"' in api_section
    assert '"8000:8000"' not in api_section


def test_safe_bundle_dir_rejects_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    bundle = _make_bundle(root, "event-1")
    assert safe_bundle_dir(root, "event-1") == bundle.resolve(strict=False)
    for bad in ("../etc/passwd", "..%2Fetc%2Fpasswd", "/tmp/event", "event/child", ".."):
        try:
            safe_bundle_dir(root, bad)
        except EvidencePathError:
            pass
        else:
            raise AssertionError(f"accepted unsafe event_id: {bad}")


def test_raw_clip_discovery_supports_mp4_mov_webm_mkv(tmp_path: Path) -> None:
    for name in ("raw_clip.mp4", "raw_clip.mov", "raw_clip.webm", "raw_clip.mkv"):
        root = tmp_path / name.replace(".", "_")
        bundle = _make_bundle(root, raw_name=name)
        raw_clip = discover_raw_clip(bundle)
        assert raw_clip is not None
        assert raw_clip.name == name
        assert media_type_for_path(raw_clip).startswith("video/")


def test_annotations_jsonl_tolerates_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "annotations.jsonl"
    path.write_text('{"a": 1}\n\nnot-json\n[]\n{"b": 2}\n', encoding="utf-8")
    records, warnings = parse_jsonl_records(path)
    assert records == [{"a": 1}, {"b": 2}]
    assert any("invalid_jsonl" in warning for warning in warnings)
    assert any("non_object_jsonl" in warning for warning in warnings)


def test_sink_metadata_parses_json_array_and_jsonl(tmp_path: Path) -> None:
    array_path = tmp_path / "array.json"
    array_path.write_text('[{"pts": 10}, {"pts": 20}]', encoding="utf-8")
    array_records, array_warnings = parse_json_or_jsonl_records(array_path)
    assert [row["pts"] for row in array_records] == [10, 20]
    assert not array_warnings

    jsonl_path = tmp_path / "sink_metadata.json"
    jsonl_path.write_text('{"pts": 30}\n{"pts": 40}\n', encoding="utf-8")
    jsonl_records, jsonl_warnings = parse_json_or_jsonl_records(jsonl_path)
    assert [row["pts"] for row in jsonl_records] == [30, 40]
    assert not jsonl_warnings
    assert first_sink_frame_pts(jsonl_records) == 30


def test_frame_pts_alignment_prefers_first_video_frame_pts() -> None:
    annotation = {"frame_pts": 1100000000, "time_offset_ms": 9000}
    assert annotation_time_seconds(annotation, 1000000000) == (0.1, "frame_pts")
    assert annotation_time_seconds({"time_offset_ms": 250}, None) == (
        0.25,
        "time_offset_ms_fallback",
    )
    assert annotation_time_seconds({}, None) == (None, "missing_time")


def test_bbox_normalization_supports_required_formats() -> None:
    assert normalize_bbox(
        {"format": "cxcywh", "values": [50, 50, 20, 10]}, width=100, height=100
    ) == {"x1": 40.0, "y1": 45.0, "x2": 60.0, "y2": 55.0}
    assert normalize_bbox(
        {"format": "xyxy", "values": [10, 20, 30, 40]}, width=100, height=100
    ) == {"x1": 10.0, "y1": 20.0, "x2": 30.0, "y2": 40.0}
    assert normalize_bbox(
        {"format": "xywh", "values": [10, 20, 30, 40]}, width=100, height=100
    ) == {"x1": 10.0, "y1": 20.0, "x2": 40.0, "y2": 60.0}
    assert normalize_bbox(
        {"format": "xyxy", "values": [-5, -5, 200, 200]}, width=100, height=100
    ) == {"x1": 0.0, "y1": 0.0, "x2": 100.0, "y2": 100.0}


def test_unknown_label_does_not_show_zero_similarity() -> None:
    obj = {"track_id": "7", "identity": {"status": "unknown", "similarity": None}}
    label = object_label(obj, {"timestamp_ms": 123})
    assert "Unknown face" in label
    assert "sim 0.00" not in label
    assert "0.sim" not in label
    assert "score 0.00" not in label


def test_matched_label_includes_name_and_similarity() -> None:
    obj = {
        "track_id": "7",
        "identity": {
            "status": "matched",
            "display_name": "Reese",
            "similarity": 0.500197,
        },
    }
    assert "Reese 0.500" in object_label(obj, {"timestamp_ms": 123})


def test_unknown_style_is_neutral_not_alert_red() -> None:
    obj = {
        "identity": {"status": "unknown"},
        "style": {"bbox_color": "#D50000", "reason": "alert_hit", "priority": 100},
    }
    style, warnings = object_style(obj)
    assert style["bbox_color"] != "#D50000"
    assert style["reason"] == "unknown_face"
    assert "unknown_style_overridden_from_event_alert" in warnings


def test_style_resolution_preserves_matched_and_low_similarity() -> None:
    matched_style, _ = object_style(
        {
            "identity": {"status": "matched", "external_person_id": "test:archive:reese"},
            "style": {"bbox_color": "#00C853", "priority": 50},
        }
    )
    assert matched_style["bbox_color"] == "#00C853"
    assert matched_style["reason"] == "identity_match"
    low_style, _ = object_style(
        {
            "identity": {"status": "low_similarity_candidate", "match_status": "below_threshold"},
            "style": {"bbox_color": "#D50000"},
        }
    )
    assert low_style["bbox_color"] != "#D50000"
    assert low_style["reason"] == "low_similarity_candidate"


def test_scan_and_manifest_return_bundle_contract(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    _make_bundle(root, "event-1")
    listing = scan_bundles(root, limit=10, offset=0)
    assert listing["total"] == 1
    bundle = listing["bundles"][0]
    assert bundle["event_id"] == "event-1"
    assert bundle["raw_clip_available"] is True
    assert bundle["raw_clip_name"] == "raw_clip.mp4"
    assert bundle["annotations_available"] is True
    assert bundle["frontend_overlay_required"] is True

    manifest = bundle_manifest(root, "event-1")
    assert manifest["raw_clip_url"] == "/api/bundles/event-1/media/raw_clip"
    assert manifest["annotations_url"] == "/api/bundles/event-1/annotations"
    assert manifest["sink_metadata_url"] == "/api/bundles/event-1/sink-metadata"


def test_viewer_static_uses_c1f4b_overlay_concepts() -> None:
    app_js = _text(STATIC_ROOT / "evidence.js")
    assert "canvas" in _text(STATIC_ROOT / "index.html")
    assert "annotationTimeSeconds" in app_js
    assert "frame_pts" in app_js
    assert "firstVideoFramePts" in app_js
    assert "time_offset_ms_fallback" in app_js
    assert "normalizeBbox" in app_js
    assert "cxcywh" in app_js
    assert "xyxy" in app_js
    assert "xywh" in app_js
    assert "drawLandmarks" in app_js
    assert "showLandmarks" in app_js
    assert "showLabels" in app_js
    assert "showMatched" in app_js
    assert "showUnknown" in app_js


def test_viewer_static_defaults_to_chinese_customer_view() -> None:
    index_html = _text(STATIC_ROOT / "index.html")
    evidence_js = _text(STATIC_ROOT / "evidence.js")
    css = _text(STATIC_ROOT / "style.css")
    assert 'lang="zh-CN"' in index_html
    assert "视频分析操作台" in index_html
    assert "摄像头管理" in index_html
    assert "人员与人脸" in index_html
    assert "告警证据" in index_html
    assert "告警分类" in index_html
    assert "证据列表" in index_html
    assert "画面标注" in index_html
    assert "事件信息" in index_html
    assert "识别摘要" in index_html
    assert "internal-debug" in index_html
    assert ".internal-debug" in css
    for token in (
        "app-shell",
        "brand",
        "top-tabs",
        "workspace-header",
        "evidence-workspace",
        "pane-title",
    ):
        assert token in index_html
    for token in (
        ".app-shell",
        ".sidebar",
        ".brand-mark",
        ".top-tab.active",
        ".workspace",
        ".status-pill",
        ".evidence-workspace",
    ):
        assert token in css
    assert "服务正常" in evidence_js
    assert "名单命中" in evidence_js
    assert "录像需复核" in evidence_js
    assert 'class="internal-debug" hidden>event_id</dt>' in index_html
    assert "Evidence Viewer" not in index_html
    assert "Overlay Controls" not in index_html
    assert ".topbar" not in css
    assert ".layout" not in css
    assert "annotation file</dt>" in index_html


def test_viewer_uses_object_level_hold_keys_for_sparse_faces() -> None:
    app_js = _text(STATIC_ROOT / "evidence.js")
    assert "objectTrackKey" in app_js
    assert "per_object_hold" in app_js
    assert "source_observation_id" in app_js
    assert "validTrackId(obj.track_id ?? line.track_id)" in app_js
    assert "trackPart === \"no_track\"" in app_js
    assert "const byObject = new Map()" in app_js
    assert "lineTrackKey" not in app_js
    assert "mode=per_track_hold" not in app_js


def test_viewer_dedupes_overlapping_face_hold_candidates() -> None:
    app_js = _text(STATIC_ROOT / "evidence.js")
    assert "OVERLAY_DEDUP_IOU_THRESHOLD" in app_js
    assert "function bboxIou" in app_js
    assert "function dedupeOverlayItems" in app_js
    assert 'role !== "matched_face" && role !== "unknown_face"' in app_js
    assert "bboxIou(candidate._dedupeBbox, existing._dedupeBbox)" in app_js
    assert "dedupeOverlayItems(visibleItems, sourceWidth, sourceHeight, currentTime)" in app_js
    assert "function betterOverlayItem" in app_js
    assert "candidateMatched !== existingMatched" in app_js


def test_viewer_static_does_not_depend_on_forbidden_runtime_paths() -> None:
    joined = "\n".join(
        _text(path)
        for path in (
            STATIC_ROOT / "index.html",
            STATIC_ROOT / "operator.js",
            STATIC_ROOT / "evidence.js",
            STATIC_ROOT / "style.css",
        )
    )
    assert "rtsp://" not in joined.lower()
    assert "ffmpeg" not in joined.lower()
    assert "annotated_clip.mp4" not in joined.lower()
    assert "https://" not in joined.lower()
    assert "cdn" not in joined.lower()


def test_smoke_checks_viewer_runtime_contract() -> None:
    content = _text(SMOKE)
    assert "docker compose" in content
    assert "up -d evidence-viewer" in content
    assert "/health" in content
    assert "/annotations" in content
    assert "annotations.frame_cache.identity.jsonl" in content
    assert "summary.frame_cache.identity.json" in content
    assert "production_sidecar" in content
    assert "fallback_used" in content
    assert "legacy_used_for_visual_binding" in content


def test_doc_declares_file_based_viewer_boundary() -> None:
    content = _text(DOC)
    assert "file-based" in content
    assert "does not depend on PostgreSQL" in content
    assert "does not depend on Redis" in content
    assert "read-only" in content
    assert "raw_clip.*" in content
    assert "annotations.jsonl" in content
    assert "event_annotation.json" in content
    assert "frame_pts" in content
    assert "(annotation.frame_pts - first_video_frame_pts) / 1e9" in content
    assert "Unknown face" in content
    assert "generated_corrupt" in content
    assert "not a production frontend" in content
