#!/usr/bin/env bash
set -euo pipefail

C2_10_OUTPUT_DIR="${C2_10_OUTPUT_DIR:-/data/video-analytics/media/evidence/c2_10_runtime_viewer_20260607T230426}"
C2_6R_BUNDLE="${C2_6R_BUNDLE:-/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035}"
C2_6R_AUDIT="${C2_6R_AUDIT:-/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035}"

python - "$C2_10_OUTPUT_DIR" "$C2_6R_BUNDLE" "$C2_6R_AUDIT" <<'PY'
import json
import re
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
bundle_dir = Path(sys.argv[2])
audit_dir = Path(sys.argv[3])

paths = {
    "c2_10_summary": output_dir / "c2_10_runtime_viewer_summary.json",
    "live_api_response_by_event_id": output_dir / "live_api_response_by_event_id.json",
    "live_api_response_by_source_event_id": output_dir / "live_api_response_by_source_event_id.json",
    "operator_report": output_dir / "operator_watchlist_evidence.html",
    "unsafe_scan": output_dir / "unsafe_payload_scan.json",
    "viewer_manifest": output_dir / "evidence_viewer_bundle_manifest.json",
    "viewer_annotations_summary": output_dir / "evidence_viewer_annotations_summary.json",
    "bundle_summary": bundle_dir / "summary.json",
    "bundle_c2_6r_summary": bundle_dir / "c2_6r_redis_watchlist_summary.json",
    "bundle_sidecar": bundle_dir / "annotations.frame_cache.identity.jsonl",
    "bundle_raw_clip": bundle_dir / "raw_clip.mov",
    "bundle_watchlist_event": bundle_dir / "redis_watchlist_event.json",
    "audit_index": audit_dir / "index.html",
    "audit_contact_sheet": audit_dir / "contact_sheet.jpg",
}

errors = []
for name, path in paths.items():
    if not path.exists():
        errors.append(f"missing:{name}:{path}")
    elif path.is_file() and path.stat().st_size <= 0 and name not in {"bundle_raw_clip"}:
        errors.append(f"empty:{name}:{path}")

def load_json(name):
    path = paths[name]
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"invalid_json:{name}:{exc}")
        return {}

summary = load_json("c2_10_summary")
unsafe = load_json("unsafe_scan")
bundle_summary = load_json("bundle_summary")
c2_6r_summary = load_json("bundle_c2_6r_summary")
event = load_json("bundle_watchlist_event")
api_by_event_id = load_json("live_api_response_by_event_id")
api_by_source_event_id = load_json("live_api_response_by_source_event_id")
viewer_manifest = load_json("viewer_manifest")
viewer_annotations = load_json("viewer_annotations_summary")

expected = {
    "result_marker": "PASS_C2_10_RUNTIME_API_VIEWER_READY",
    "event_type": "watchlist_hit",
    "source_observation_id": "face:c2_post_savant_fps_probe:4:17854:1",
    "person_id": 4,
    "external_person_id": "test:c2_4:person",
    "watchlist_rule_id": "c2_6r_test_watchlist_rule",
    "known_face_count": 1,
    "watchlist_hit_count": 1,
    "production_ready": True,
    "video_integrity_status": "pass",
    "evidence_bundle_path": str(bundle_dir),
    "evidence_capture_mode": "stable_post_savant_sink_time_crop",
    "workaround_used": True,
    "event_style_replay_job_passed": False,
    "payload_has_embedding": False,
    "payload_has_image_bytes": False,
    "live_api_http_verified": True,
    "evidence_viewer_verified": True,
    "containers_restarted": False,
}

for key, value in expected.items():
    if summary.get(key) != value:
        errors.append(f"summary:{key}:expected={value!r}:actual={summary.get(key)!r}")

if float(summary.get("similarity") or -1) != 1.0:
    errors.append("summary:similarity")
if float(summary.get("threshold") or -1) != 0.99:
    errors.append("summary:threshold")
if summary.get("event_style_replay_claimed_passed") is not False:
    errors.append("summary:event_style_replay_claimed_passed")
if unsafe.get("payload_has_embedding") is not False:
    errors.append("unsafe_scan:payload_has_embedding")
if unsafe.get("payload_has_image_bytes") is not False:
    errors.append("unsafe_scan:payload_has_image_bytes")
if unsafe.get("forbidden_key_paths") != []:
    errors.append("unsafe_scan:forbidden_key_paths")

for name, response in (
    ("api_by_event_id", api_by_event_id),
    ("api_by_source_event_id", api_by_source_event_id),
):
    if response.get("status_code") != 200:
        errors.append(f"{name}:status_code")
    data = ((response.get("json") or {}).get("data") or {})
    event_data = data.get("event") or {}
    detail = data.get("evidence_detail") or {}
    if event_data.get("event_type") != "watchlist_hit" and detail.get("event_type") != "watchlist_hit":
        errors.append(f"{name}:event_type")
    if detail and detail.get("source_observation_id") != expected["source_observation_id"]:
        errors.append(f"{name}:source_observation_id")

if viewer_manifest.get("status_code") not in (None, 200):
    errors.append("viewer_manifest:status_code")
manifest_json = viewer_manifest.get("json") if isinstance(viewer_manifest.get("json"), dict) else viewer_manifest
if manifest_json.get("event_status") != "watchlist_hit":
    errors.append("viewer_manifest:event_status")
if manifest_json.get("production_sidecar_ready") is not True:
    errors.append("viewer_manifest:production_sidecar_ready")
if manifest_json.get("legacy_used_for_visual_binding") is not False:
    errors.append("viewer_manifest:legacy_used_for_visual_binding")

if viewer_annotations.get("status_code") not in (None, 200):
    errors.append("viewer_annotations:status_code")

for name, data in (
    ("bundle_summary", bundle_summary),
    ("c2_6r_summary", c2_6r_summary),
    ("event", event),
):
    if data.get("event_type") != "watchlist_hit":
        errors.append(f"{name}:event_type")
    if data.get("source_observation_id") != expected["source_observation_id"]:
        errors.append(f"{name}:source_observation_id")
    if data.get("external_person_id") != expected["external_person_id"]:
        errors.append(f"{name}:external_person_id")
    if data.get("watchlist_rule_id") != expected["watchlist_rule_id"]:
        errors.append(f"{name}:watchlist_rule_id")

if c2_6r_summary.get("redis_consumer_loop_verified") is not True:
    errors.append("c2_6r_summary:redis_consumer_loop_verified")
if c2_6r_summary.get("track_id_join_warning") is not True:
    errors.append("c2_6r_summary:track_id_join_warning")
if str(c2_6r_summary.get("db_observation_track_id")) != "4":
    errors.append("c2_6r_summary:db_observation_track_id")
if str(c2_6r_summary.get("evidence_sidecar_track_id")) != "1":
    errors.append("c2_6r_summary:evidence_sidecar_track_id")
if c2_6r_summary.get("primary_identity_join_key") != "source_observation_id":
    errors.append("c2_6r_summary:primary_identity_join_key")

report_text = paths["operator_report"].read_text(encoding="utf-8") if paths["operator_report"].exists() else ""
for token in (
    "Watchlist Hit",
    "known_face",
    "test:c2_4:person",
    "face:c2_post_savant_fps_probe:4:17854:1",
    "stable_post_savant_sink_time_crop",
    "Event-style Replay is not passed",
):
    if token not in report_text:
        errors.append(f"operator_report_missing:{token}")

unsafe_patterns = [
    r'"embedding"\s*:\s*\[',
    r'"embedding_vector"\s*:\s*\[',
    r'"image_bytes"\s*:\s*"[^"]+',
    r'"crop_bytes"\s*:\s*"[^"]+',
    r'"base64"\s*:\s*"[^"]+',
    r"data:image/",
]
scan_files = [
    paths["operator_report"],
    paths["live_api_response_by_event_id"],
    paths["live_api_response_by_source_event_id"],
    paths["bundle_watchlist_event"],
]
for path in scan_files:
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8")
    for pattern in unsafe_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            errors.append(f"unsafe_pattern:{path}:{pattern}")

result = {
    "result_marker": "PASS_C2_REBASELINE_DEMO_READY" if not errors else "FAIL_C2_REBASELINE_BLOCKED",
    "errors": errors,
    "c2_10_output_dir": str(output_dir),
    "c2_6r_bundle": str(bundle_dir),
    "operator_report": str(paths["operator_report"]),
    "live_api_response_by_event_id": str(paths["live_api_response_by_event_id"]),
    "live_api_response_by_source_event_id": str(paths["live_api_response_by_source_event_id"]),
    "viewer_manifest_url": summary.get("viewer_manifest_url"),
    "viewer_annotations_url": summary.get("viewer_annotations_url"),
    "event_type": summary.get("event_type"),
    "source_observation_id": summary.get("source_observation_id"),
    "external_person_id": summary.get("external_person_id"),
    "known_face_count": summary.get("known_face_count"),
    "watchlist_hit_count": summary.get("watchlist_hit_count"),
    "evidence_capture_mode": summary.get("evidence_capture_mode"),
    "workaround_used": summary.get("workaround_used"),
    "event_style_replay_job_passed": summary.get("event_style_replay_job_passed"),
    "payload_has_embedding": summary.get("payload_has_embedding"),
    "payload_has_image_bytes": summary.get("payload_has_image_bytes"),
}
print(json.dumps(result, indent=2, sort_keys=True))
if errors:
    raise SystemExit(2)
PY
