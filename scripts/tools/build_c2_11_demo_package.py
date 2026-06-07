#!/usr/bin/env python3
"""Build a static C2.11 operator demo package for watchlist evidence."""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_C2_10_OUTPUT_DIR = Path(
    "/data/video-analytics/media/evidence/c2_10_runtime_viewer_20260607T230426"
)
DEFAULT_C2_6R_BUNDLE = Path(
    "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035"
)
DEFAULT_SOURCE_COMMIT = "06f8b4be753e4470306e5408c3263d742fe0c43c"
DEFAULT_REBASELINE_DOC = ROOT / "docs" / "c2_post_savant_watchlist_evidence_rebaseline_2026_06_07.md"
DEFAULT_CHECKLIST_DOC = ROOT / "docs" / "c2_demo_checklist_2026_06_07.md"
DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")

RESULT_PASS = "PASS_C2_11_DEMO_PACKAGE_READY"
RESULT_FAIL = "FAIL_C2_11_DEMO_PACKAGE_BLOCKED"

INDEX_FILE = "index.html"
README_FILE = "README.md"
MANIFEST_FILE = "demo_manifest.json"
CHECKLIST_FILE = "demo_checklist.md"
API_BY_EVENT_ID_FILE = "api_response_by_event_id.json"
API_BY_SOURCE_EVENT_ID_FILE = "api_response_by_source_event_id.json"
OPERATOR_REPORT_FILE = "operator_watchlist_evidence.html"
EVIDENCE_MANIFEST_FILE = "evidence_manifest.json"
SIDECAR_SAMPLE_FILE = "sidecar_annotation_sample.json"
UNSAFE_SCAN_FILE = "unsafe_payload_scan.json"
LIMITATIONS_FILE = "limitations.json"

REQUIRED_LIMITATIONS = [
    "event_style_replay_not_passed",
    "stable_post_savant_sink_time_crop_workaround",
    "deterministic_fixed_sample",
    "no_broad_accuracy_proof",
    "no_long_running_soak",
]


@dataclass(frozen=True)
class DemoBuildResult:
    result_marker: str
    output_dir: Path
    index_html: Path
    manifest_path: Path
    unsafe_scan_path: Path
    manifest: dict[str, Any]
    unsafe_scan: dict[str, Any]


def build_c2_11_demo_package(
    *,
    output_dir: Path,
    c2_10_output_dir: Path,
    c2_6r_bundle: Path,
    rebaseline_doc: Path,
    checklist_doc: Path,
    overwrite: bool = False,
) -> DemoBuildResult:
    output_dir = output_dir.resolve(strict=False)
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        _clear_dir(output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    inputs = _collect_input_paths(c2_10_output_dir, c2_6r_bundle, rebaseline_doc, checklist_doc)
    _validate_input_paths(inputs)

    c2_10_summary = _read_json(inputs["c2_10_summary"])
    bundle_summary = _read_json(inputs["bundle_summary"])
    c2_6r_summary = _read_json(inputs["bundle_c2_6r_summary"])
    watchlist_event = _read_json(inputs["bundle_watchlist_event"])

    copied_files = _copy_package_files(output_dir, inputs)
    known_face_sample = extract_known_face_sample(
        inputs["bundle_sidecar"],
        source_observation_id=str(c2_10_summary.get("source_observation_id") or ""),
    )
    _write_json(output_dir / SIDECAR_SAMPLE_FILE, known_face_sample)

    limitations = build_limitations()
    _write_json(output_dir / LIMITATIONS_FILE, limitations)

    manifest = build_demo_manifest(
        source_branch=_git_value(["rev-parse", "--abbrev-ref", "HEAD"]),
        source_commit=DEFAULT_SOURCE_COMMIT,
        package_builder_commit=_git_value(["rev-parse", "HEAD"]),
        c2_10_summary=c2_10_summary,
        bundle_summary=bundle_summary,
        c2_6r_summary=c2_6r_summary,
        watchlist_event=watchlist_event,
        input_paths=inputs,
        copied_files=copied_files,
        output_dir=output_dir,
    )
    validate_demo_manifest(manifest)

    index_html = render_index_html(manifest)
    readme = render_readme(manifest)
    (output_dir / INDEX_FILE).write_text(index_html, encoding="utf-8")
    (output_dir / README_FILE).write_text(readme, encoding="utf-8")
    _write_json(output_dir / MANIFEST_FILE, manifest)

    unsafe_scan = scan_paths(
        [
            output_dir / API_BY_EVENT_ID_FILE,
            output_dir / API_BY_SOURCE_EVENT_ID_FILE,
            output_dir / OPERATOR_REPORT_FILE,
            output_dir / MANIFEST_FILE,
            output_dir / SIDECAR_SAMPLE_FILE,
        ]
    )
    _write_json(output_dir / UNSAFE_SCAN_FILE, unsafe_scan)
    validate_unsafe_scan(unsafe_scan)

    return DemoBuildResult(
        result_marker=RESULT_PASS,
        output_dir=output_dir,
        index_html=output_dir / INDEX_FILE,
        manifest_path=output_dir / MANIFEST_FILE,
        unsafe_scan_path=output_dir / UNSAFE_SCAN_FILE,
        manifest=manifest,
        unsafe_scan=unsafe_scan,
    )


def build_demo_manifest(
    *,
    source_branch: str,
    source_commit: str,
    package_builder_commit: str,
    c2_10_summary: dict[str, Any],
    bundle_summary: dict[str, Any],
    c2_6r_summary: dict[str, Any],
    watchlist_event: dict[str, Any],
    input_paths: dict[str, Path],
    copied_files: dict[str, Path],
    output_dir: Path,
) -> dict[str, Any]:
    event = {
        "event_type": c2_10_summary.get("event_type"),
        "event_id": c2_10_summary.get("db_event_id"),
        "source_event_id": c2_10_summary.get("source_event_id"),
        "person_id": c2_10_summary.get("person_id"),
        "external_person_id": c2_10_summary.get("external_person_id"),
        "source_observation_id": c2_10_summary.get("source_observation_id"),
        "similarity": c2_10_summary.get("similarity"),
        "threshold": c2_10_summary.get("threshold"),
        "watchlist_rule_id": c2_10_summary.get("watchlist_rule_id"),
        "gallery_embedding_id": c2_10_summary.get("gallery_embedding_id"),
        "match_result_id": c2_10_summary.get("match_result_id"),
        "producer": watchlist_event.get("producer"),
        "track_id": c2_10_summary.get("track_id"),
        "primary_identity_join_key": c2_6r_summary.get("primary_identity_join_key"),
        "track_id_join_warning": c2_6r_summary.get("track_id_join_warning"),
        "db_observation_track_id": c2_6r_summary.get("db_observation_track_id"),
        "evidence_sidecar_track_id": c2_6r_summary.get("evidence_sidecar_track_id"),
    }
    evidence = {
        "bundle_path": c2_10_summary.get("evidence_bundle_path"),
        "operator_report": str(copied_files["operator_report"]),
        "source_operator_report": str(input_paths["operator_report"]),
        "api_response_by_event_id": str(copied_files["api_response_by_event_id"]),
        "api_response_by_source_event_id": str(copied_files["api_response_by_source_event_id"]),
        "evidence_manifest": str(copied_files.get("evidence_manifest") or ""),
        "sidecar_annotation_sample": str(output_dir / SIDECAR_SAMPLE_FILE),
        "unsafe_payload_scan": str(output_dir / UNSAFE_SCAN_FILE),
        "raw_clip_path": c2_10_summary.get("raw_clip_path"),
        "sidecar_path": c2_10_summary.get("sidecar_path"),
        "summary_path": c2_10_summary.get("summary_path"),
        "watchlist_event_path": c2_10_summary.get("watchlist_event_path"),
        "audit_index_path": c2_10_summary.get("audit_index_path"),
        "contact_sheet_path": c2_10_summary.get("contact_sheet_path"),
        "evidence_viewer_manifest_url": c2_10_summary.get("viewer_manifest_url"),
        "evidence_viewer_annotations_url": c2_10_summary.get("viewer_annotations_url"),
        "evidence_viewer_raw_clip_url": c2_10_summary.get("viewer_raw_clip_url"),
        "known_face_count": c2_10_summary.get("known_face_count"),
        "watchlist_hit_count": c2_10_summary.get("watchlist_hit_count"),
        "production_ready": c2_10_summary.get("production_ready"),
        "video_integrity_status": c2_10_summary.get("video_integrity_status"),
        "capture_mode": c2_10_summary.get("evidence_capture_mode"),
        "workaround_used": c2_10_summary.get("workaround_used"),
        "event_style_replay_job_passed": c2_10_summary.get("event_style_replay_job_passed"),
    }
    safety = {
        "embedding_present": False,
        "image_base64_crop_present": False,
        "fallback_used": bundle_summary.get("fallback_used"),
        "legacy_used_for_visual_binding": bundle_summary.get("legacy_used_for_visual_binding"),
        "allow_db_annotation_fallback": bundle_summary.get("allow_db_annotation_fallback"),
        "allow_legacy_annotation_fallback": bundle_summary.get("allow_legacy_annotation_fallback"),
        "geometry_source": "post_savant_sidecar",
        "identity_source": "match_results_gallery_person",
        "raw_video_copied": False,
    }
    return {
        "schema_version": "1.0",
        "demo_name": "c2_watchlist_evidence_demo",
        "result_marker": "PASS_C2_REBASELINE_DEMO_READY",
        "c2_10_result_marker": c2_10_summary.get("result_marker"),
        "package_result_marker": RESULT_PASS,
        "source_branch": source_branch,
        "source_commit": source_commit,
        "package_builder_commit": package_builder_commit,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input_artifacts": {
            "rebaseline_doc": str(input_paths["rebaseline_doc"]),
            "demo_checklist": str(input_paths["checklist_doc"]),
            "c2_10_output_dir": str(input_paths["c2_10_output_dir"]),
            "c2_6r_bundle": str(input_paths["c2_6r_bundle"]),
            "c2_10_summary": str(input_paths["c2_10_summary"]),
            "api_response_by_event_id": str(input_paths["api_response_by_event_id"]),
            "api_response_by_source_event_id": str(input_paths["api_response_by_source_event_id"]),
            "operator_report": str(input_paths["operator_report"]),
            "bundle_summary": str(input_paths["bundle_summary"]),
            "watchlist_event": str(input_paths["bundle_watchlist_event"]),
        },
        "event": event,
        "evidence": evidence,
        "safety": safety,
        "limitations": list(REQUIRED_LIMITATIONS),
        "demo_steps": [
            "open_operator_report",
            "open_api_response_json",
            "open_evidence_viewer_manifest",
            "open_sidecar_annotations_endpoint",
            "verify_known_face_watchlist_fields",
            "verify_workaround_flags_visible",
        ],
        "runtime_semantics_changed": False,
        "notes": [
            "C2.11 is static packaging only.",
            "Raw video is referenced by path and viewer URL; it is not copied into the package.",
            "No Replay, worker loop, DB write, or Redis write is performed by this package.",
        ],
    }


def validate_demo_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != "1.0":
        raise RuntimeError("manifest_schema_version_invalid")
    if manifest.get("demo_name") != "c2_watchlist_evidence_demo":
        raise RuntimeError("manifest_demo_name_invalid")
    if manifest.get("result_marker") != "PASS_C2_REBASELINE_DEMO_READY":
        raise RuntimeError("manifest_result_marker_invalid")
    if manifest.get("runtime_semantics_changed") is not False:
        raise RuntimeError("runtime_semantics_changed")

    event = _dict(manifest.get("event"))
    evidence = _dict(manifest.get("evidence"))
    safety = _dict(manifest.get("safety"))
    limitations = manifest.get("limitations") or []

    if event.get("event_type") != "watchlist_hit":
        raise RuntimeError("manifest_event_type_not_watchlist_hit")
    for key in (
        "person_id",
        "external_person_id",
        "source_observation_id",
        "similarity",
        "threshold",
        "watchlist_rule_id",
    ):
        if event.get(key) in (None, ""):
            raise RuntimeError(f"manifest_event_missing_{key}")
    if event.get("external_person_id") != "test:c2_4:person":
        raise RuntimeError("manifest_external_person_id_invalid")
    if event.get("source_observation_id") != "face:c2_post_savant_fps_probe:4:17854:1":
        raise RuntimeError("manifest_source_observation_id_invalid")
    if float(event.get("similarity") or -1) != 1.0:
        raise RuntimeError("manifest_similarity_invalid")
    if float(event.get("threshold") or -1) != 0.99:
        raise RuntimeError("manifest_threshold_invalid")

    if not evidence.get("bundle_path") or not Path(str(evidence["bundle_path"])).is_dir():
        raise RuntimeError("manifest_evidence_bundle_path_missing")
    for key in (
        "operator_report",
        "api_response_by_event_id",
        "api_response_by_source_event_id",
        "sidecar_annotation_sample",
    ):
        value = evidence.get(key)
        if not value or not Path(str(value)).is_file():
            raise RuntimeError(f"manifest_evidence_file_missing_{key}")
    if evidence.get("capture_mode") != "stable_post_savant_sink_time_crop":
        raise RuntimeError("manifest_capture_mode_invalid")
    if evidence.get("workaround_used") is not True:
        raise RuntimeError("manifest_workaround_not_true")
    if evidence.get("event_style_replay_job_passed") is not False:
        raise RuntimeError("manifest_event_style_replay_not_false")
    if int(evidence.get("known_face_count") or 0) < 1:
        raise RuntimeError("manifest_known_face_count_invalid")
    if int(evidence.get("watchlist_hit_count") or 0) != 1:
        raise RuntimeError("manifest_watchlist_hit_count_invalid")

    if safety.get("embedding_present") is not False:
        raise RuntimeError("manifest_embedding_present")
    if safety.get("image_base64_crop_present") is not False:
        raise RuntimeError("manifest_image_base64_crop_present")
    if safety.get("fallback_used") is not False:
        raise RuntimeError("manifest_fallback_used")
    if safety.get("legacy_used_for_visual_binding") is not False:
        raise RuntimeError("manifest_legacy_used_for_visual_binding")

    missing_limitations = [item for item in REQUIRED_LIMITATIONS if item not in limitations]
    if missing_limitations:
        raise RuntimeError(f"manifest_limitations_missing:{','.join(missing_limitations)}")


def render_index_html(manifest: dict[str, Any]) -> str:
    event = _dict(manifest.get("event"))
    evidence = _dict(manifest.get("evidence"))
    safety = _dict(manifest.get("safety"))
    limitations = manifest.get("limitations") or []
    rows = [
        ("Result marker", manifest.get("result_marker")),
        ("C2.10 status", manifest.get("c2_10_result_marker")),
        ("Event type", event.get("event_type")),
        ("Person id", event.get("person_id")),
        ("External person id", event.get("external_person_id")),
        ("Source observation id", event.get("source_observation_id")),
        ("Similarity", event.get("similarity")),
        ("Threshold", event.get("threshold")),
        ("Watchlist rule id", event.get("watchlist_rule_id")),
        ("Evidence bundle", evidence.get("bundle_path")),
        ("Operator report", evidence.get("operator_report")),
        ("API response by event id", evidence.get("api_response_by_event_id")),
        ("API response by source event id", evidence.get("api_response_by_source_event_id")),
        ("Evidence viewer manifest URL", evidence.get("evidence_viewer_manifest_url")),
        ("Evidence viewer sidecar annotations URL", evidence.get("evidence_viewer_annotations_url")),
        ("Embedding present", safety.get("embedding_present")),
        ("Image/base64/crop present", safety.get("image_base64_crop_present")),
        ("Fallback used", safety.get("fallback_used")),
        ("Legacy visual binding used", safety.get("legacy_used_for_visual_binding")),
        ("Capture mode", evidence.get("capture_mode")),
        ("Workaround used", evidence.get("workaround_used")),
        ("Event-style Replay passed", evidence.get("event_style_replay_job_passed")),
    ]
    row_html = "\n".join(
        f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>"
        for label, value in rows
    )
    viewer_links = [
        ("Open operator report", OPERATOR_REPORT_FILE),
        ("Open API response JSON", API_BY_EVENT_ID_FILE),
        ("Open source-event API response JSON", API_BY_SOURCE_EVENT_ID_FILE),
        ("Open evidence manifest JSON", EVIDENCE_MANIFEST_FILE),
        ("Open known_face sidecar sample", SIDECAR_SAMPLE_FILE),
    ]
    local_links_html = "\n".join(
        f'<li><a href="{_esc(href)}">{_esc(label)}</a></li>' for label, href in viewer_links
    )
    runtime_links = []
    if evidence.get("evidence_viewer_manifest_url"):
        runtime_links.append(("Evidence viewer manifest", evidence["evidence_viewer_manifest_url"]))
    if evidence.get("evidence_viewer_annotations_url"):
        runtime_links.append(("Evidence viewer sidecar annotations", evidence["evidence_viewer_annotations_url"]))
    runtime_links_html = "\n".join(
        f'<li><a href="{_esc(href)}">{_esc(label)}</a></li>' for label, href in runtime_links
    ) or "<li>Runtime viewer URL not available in manifest.</li>"
    limitations_html = "\n".join(f"<li>{_esc(item)}</li>" for item in limitations)
    steps_html = "\n".join(f"<li>{_esc(step)}</li>" for step in manifest.get("demo_steps") or [])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>C2 Watchlist Evidence Demo</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #17212b; }}
    h1 {{ margin-bottom: 6px; }}
    h2 {{ margin-top: 24px; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 1180px; }}
    th, td {{ border: 1px solid #d8dee4; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ width: 280px; background: #f6f8fa; }}
    .status {{ border-left: 4px solid #1f883d; padding: 10px 12px; background: #f0fff4; max-width: 1180px; }}
    .warning {{ border-left: 4px solid #cf222e; padding: 10px 12px; background: #fff8f8; max-width: 1180px; }}
  </style>
</head>
<body>
  <h1>C2 Watchlist Evidence Demo</h1>
  <p class="status">PASS_C2_REBASELINE_DEMO_READY and PASS_C2_10_RUNTIME_API_VIEWER_READY.</p>
  <div class="warning">Event-style Replay is not passed. This demo uses the stable_post_savant_sink_time_crop workaround and a deterministic fixed sample.</div>
  <h2>Main Event</h2>
  <table>
    {row_html}
  </table>
  <h2>Open Package Files</h2>
  <ul>{local_links_html}</ul>
  <h2>Runtime Viewer Links</h2>
  <ul>{runtime_links_html}</ul>
  <h2>Suggested Demo Steps</h2>
  <ol>{steps_html}</ol>
  <h2>Caveats</h2>
  <ul>{limitations_html}</ul>
</body>
</html>
"""


def render_readme(manifest: dict[str, Any]) -> str:
    event = _dict(manifest.get("event"))
    evidence = _dict(manifest.get("evidence"))
    return f"""# C2 Watchlist Evidence Demo

Result marker: `{manifest.get("result_marker")}`

Open `index.html` first. It links to the packaged API responses, operator
report, evidence manifest, and known-face sidecar sample.

## Main Event

- `event_type={event.get("event_type")}`
- `person_id={event.get("person_id")}`
- `external_person_id={event.get("external_person_id")}`
- `source_observation_id={event.get("source_observation_id")}`
- `similarity={event.get("similarity")}`
- `threshold={event.get("threshold")}`
- `watchlist_rule_id={event.get("watchlist_rule_id")}`

## Evidence

- bundle path: `{evidence.get("bundle_path")}`
- operator report: `{OPERATOR_REPORT_FILE}`
- API response by event id: `{API_BY_EVENT_ID_FILE}`
- API response by source event id: `{API_BY_SOURCE_EVENT_ID_FILE}`
- viewer manifest URL: `{evidence.get("evidence_viewer_manifest_url")}`
- viewer annotations URL: `{evidence.get("evidence_viewer_annotations_url")}`

## Caveats

- event-style Replay not passed
- stable sink workaround active
- deterministic fixed sample
- no broad recognition accuracy proof
- no long-running soak yet

No raw video is copied into this package; the manifest links to the existing raw
clip path and viewer raw clip URL.
"""


def build_limitations() -> dict[str, Any]:
    return {
        "limitations": list(REQUIRED_LIMITATIONS),
        "details": {
            "event_style_replay_not_passed": "C2.3B event-style Replay remains backlog.",
            "stable_post_savant_sink_time_crop_workaround": "Evidence capture still uses the stable sink crop workaround.",
            "deterministic_fixed_sample": "The demo uses the fixed C2 deterministic sample and test person.",
            "no_broad_accuracy_proof": "This proves pipeline semantics, not broad face-recognition accuracy.",
            "no_long_running_soak": "C2.6R/C2.7 were one-message proofs, not long-running worker soak.",
        },
    }


def extract_known_face_sample(sidecar_path: Path, *, source_observation_id: str) -> dict[str, Any]:
    with sidecar_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            for obj in record.get("objects") or []:
                identity = _dict(obj.get("identity"))
                label = _dict(obj.get("label"))
                if (
                    obj.get("object_type") == "known_face"
                    or label.get("kind") == "known_face"
                    or identity.get("source_observation_id") == source_observation_id
                ):
                    return {
                        "schema_version": "1.0",
                        "source_file": str(sidecar_path),
                        "line_number": line_number,
                        "frame_index": record.get("frame_index"),
                        "clip_frame_index": record.get("clip_frame_index"),
                        "frame_pts": record.get("frame_pts"),
                        "frame_uuid": record.get("frame_uuid"),
                        "source_id": record.get("source_id"),
                        "object": obj,
                    }
    raise RuntimeError("known_face_sample_not_found")


def scan_paths(paths: list[Path]) -> dict[str, Any]:
    scanned_files: list[str] = []
    forbidden: list[str] = []
    embedding_hits: list[str] = []
    image_hits: list[str] = []
    vector_hits: list[str] = []
    for path in paths:
        scanned_files.append(str(path))
        if not path.is_file():
            forbidden.append(f"{path}:missing")
            continue
        if path.suffix.lower() == ".json":
            value = _read_json(path)
        else:
            value = path.read_text(encoding="utf-8")
        scan = scan_for_unsafe_payload(value)
        embedding_hits.extend(f"{path}:{hit}" for hit in scan["embedding_key_paths"])
        image_hits.extend(f"{path}:{hit}" for hit in scan["image_byte_key_paths"])
        vector_hits.extend(f"{path}:{hit}" for hit in scan["suspicious_vector_paths"])
        forbidden.extend(f"{path}:{hit}" for hit in scan["forbidden_key_paths"])
    forbidden_sorted = sorted(set(forbidden))
    embedding_sorted = sorted(set(embedding_hits))
    image_sorted = sorted(set(image_hits))
    vector_sorted = sorted(set(vector_hits))
    return {
        "passed": not forbidden_sorted and not embedding_sorted and not image_sorted and not vector_sorted,
        "payload_has_embedding": bool(embedding_sorted),
        "payload_has_image_bytes": bool(image_sorted),
        "has_suspicious_numeric_vectors": bool(vector_sorted),
        "embedding_key_paths": embedding_sorted,
        "image_byte_key_paths": image_sorted,
        "suspicious_vector_paths": vector_sorted,
        "forbidden_key_paths": forbidden_sorted,
        "scanned_files": scanned_files,
    }


def scan_for_unsafe_payload(value: Any) -> dict[str, Any]:
    state = {
        "embedding_key_paths": [],
        "image_byte_key_paths": [],
        "suspicious_vector_paths": [],
        "forbidden_key_paths": [],
    }
    _scan_value(value, "", state)
    for key in state:
        state[key] = sorted(set(state[key]))
    return {
        "passed": not any(state.values()),
        "payload_has_embedding": bool(state["embedding_key_paths"]),
        "payload_has_image_bytes": bool(state["image_byte_key_paths"]),
        "has_suspicious_numeric_vectors": bool(state["suspicious_vector_paths"]),
        **state,
    }


def validate_unsafe_scan(scan: dict[str, Any]) -> None:
    if scan.get("passed") is not True:
        raise RuntimeError(f"unsafe_payload_scan_failed:{scan}")
    if scan.get("payload_has_embedding") is not False:
        raise RuntimeError("unsafe_embedding_present")
    if scan.get("payload_has_image_bytes") is not False:
        raise RuntimeError("unsafe_image_bytes_present")
    if scan.get("has_suspicious_numeric_vectors") is not False:
        raise RuntimeError("unsafe_suspicious_vector_present")
    if scan.get("forbidden_key_paths"):
        raise RuntimeError("unsafe_forbidden_key_paths_present")


def _scan_value(value: Any, path: str, state: dict[str, list[str]]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            next_path = f"{path}.{key_text}" if path else key_text
            if lowered in _embedding_keys() and _non_empty_payload(nested):
                state["embedding_key_paths"].append(next_path)
            if lowered in _image_byte_keys() and _non_empty_payload(nested):
                state["image_byte_key_paths"].append(next_path)
            if (
                any(token in lowered for token in ("base64", "image_bytes", "crop_bytes", "face_crop"))
                and lowered not in _safe_status_keys()
                and _non_empty_payload(nested)
            ):
                state["image_byte_key_paths"].append(next_path)
            _scan_value(nested, next_path, state)
    elif isinstance(value, list):
        if _is_large_numeric_vector(value) and not _path_allows_geometry(path):
            state["suspicious_vector_paths"].append(path or "value")
        for index, nested in enumerate(value):
            _scan_value(nested, f"{path}.{index}" if path else str(index), state)
    elif isinstance(value, str):
        lowered = value.lower()
        if "data:image" in lowered or ";base64," in lowered:
            state["image_byte_key_paths"].append(path or "value")
        elif _looks_like_large_base64(value) and _path_mentions_image_or_crop(path):
            state["image_byte_key_paths"].append(path or "value")


def _embedding_keys() -> set[str]:
    return {
        "embedding",
        "embeddings",
        "embedding_vector",
        "embedding_values",
        "embedding_list",
        "face_embedding",
        "query_embedding",
        "gallery_embedding",
    }


def _image_byte_keys() -> set[str]:
    return {
        "image_bytes",
        "crop_bytes",
        "face_crop_bytes",
        "frame_bytes",
        "raw_image_bytes",
        "raw_frame",
        "base64",
        "base64_image",
        "image_base64",
        "crop_base64",
        "face_crop_base64",
    }


def _safe_status_keys() -> set[str]:
    return {
        "embedding_included",
        "embedding_present",
        "payload_has_embedding",
        "embedding_vectors_in_output",
        "image_bytes_included",
        "crop_bytes_included",
        "image_base64_crop_present",
        "payload_has_image_bytes",
    }


def _path_allows_geometry(path: str) -> bool:
    lowered = path.lower()
    return any(
        token in lowered
        for token in (
            "bbox",
            "xyxy",
            "cxcywh",
            "landmark",
            "keypoint",
            "points",
            "pose",
            "polygon",
        )
    )


def _path_mentions_image_or_crop(path: str) -> bool:
    lowered = path.lower()
    return any(token in lowered for token in ("image", "crop", "frame", "base64"))


def _is_large_numeric_vector(value: list[Any]) -> bool:
    return len(value) >= 32 and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)


def _looks_like_large_base64(value: str) -> bool:
    if len(value) < 128:
        return False
    try:
        base64.b64decode(value, validate=True)
    except Exception:
        return False
    return True


def _non_empty_payload(value: Any) -> bool:
    return value not in (None, "", False, 0, [], {})


def _collect_input_paths(
    c2_10_output_dir: Path,
    c2_6r_bundle: Path,
    rebaseline_doc: Path,
    checklist_doc: Path,
) -> dict[str, Path]:
    return {
        "c2_10_output_dir": c2_10_output_dir,
        "c2_6r_bundle": c2_6r_bundle,
        "rebaseline_doc": rebaseline_doc,
        "checklist_doc": checklist_doc,
        "c2_10_summary": c2_10_output_dir / "c2_10_runtime_viewer_summary.json",
        "api_response_by_event_id": c2_10_output_dir / "live_api_response_by_event_id.json",
        "api_response_by_source_event_id": c2_10_output_dir / "live_api_response_by_source_event_id.json",
        "operator_report": c2_10_output_dir / "operator_watchlist_evidence.html",
        "c2_10_unsafe_scan": c2_10_output_dir / "unsafe_payload_scan.json",
        "evidence_manifest": c2_10_output_dir / "evidence_viewer_bundle_manifest.json",
        "bundle_summary": c2_6r_bundle / "summary.json",
        "bundle_c2_6r_summary": c2_6r_bundle / "c2_6r_redis_watchlist_summary.json",
        "bundle_sidecar": c2_6r_bundle / "annotations.frame_cache.identity.jsonl",
        "bundle_watchlist_event": c2_6r_bundle / "redis_watchlist_event.json",
    }


def _validate_input_paths(paths: dict[str, Path]) -> None:
    dirs = ("c2_10_output_dir", "c2_6r_bundle")
    for key, path in paths.items():
        if key in dirs:
            if not path.is_dir():
                raise FileNotFoundError(f"{key} missing: {path}")
        elif key == "evidence_manifest":
            if not path.is_file():
                continue
        elif not path.is_file():
            raise FileNotFoundError(f"{key} missing: {path}")


def _copy_package_files(output_dir: Path, inputs: dict[str, Path]) -> dict[str, Path]:
    copied = {
        "demo_checklist": output_dir / CHECKLIST_FILE,
        "api_response_by_event_id": output_dir / API_BY_EVENT_ID_FILE,
        "api_response_by_source_event_id": output_dir / API_BY_SOURCE_EVENT_ID_FILE,
        "operator_report": output_dir / OPERATOR_REPORT_FILE,
    }
    shutil.copy2(inputs["checklist_doc"], copied["demo_checklist"])
    shutil.copy2(inputs["api_response_by_event_id"], copied["api_response_by_event_id"])
    shutil.copy2(inputs["api_response_by_source_event_id"], copied["api_response_by_source_event_id"])
    shutil.copy2(inputs["operator_report"], copied["operator_report"])
    if inputs["evidence_manifest"].is_file():
        copied["evidence_manifest"] = output_dir / EVIDENCE_MANIFEST_FILE
        shutil.copy2(inputs["evidence_manifest"], copied["evidence_manifest"])
    return copied


def _clear_dir(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _git_value(args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    except Exception:
        return ""


def _esc(value: Any) -> str:
    if isinstance(value, bool):
        value = str(value).lower()
    elif value is None:
        value = ""
    return html.escape(str(value), quote=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--run-id", default=f"c2_11_demo_package_{datetime.now().strftime('%Y%m%dT%H%M%S')}")
    parser.add_argument("--c2-10-output-dir", type=Path, default=DEFAULT_C2_10_OUTPUT_DIR)
    parser.add_argument("--c2-6r-bundle", type=Path, default=DEFAULT_C2_6R_BUNDLE)
    parser.add_argument("--rebaseline-doc", type=Path, default=DEFAULT_REBASELINE_DOC)
    parser.add_argument("--checklist-doc", type=Path, default=DEFAULT_CHECKLIST_DOC)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or (args.evidence_root / args.run_id)
    result = build_c2_11_demo_package(
        output_dir=output_dir,
        c2_10_output_dir=args.c2_10_output_dir,
        c2_6r_bundle=args.c2_6r_bundle,
        rebaseline_doc=args.rebaseline_doc,
        checklist_doc=args.checklist_doc,
        overwrite=args.overwrite,
    )
    print(
        json.dumps(
            {
                "result_marker": result.result_marker,
                "output_dir": str(result.output_dir),
                "index_html": str(result.index_html),
                "demo_manifest": str(result.manifest_path),
                "unsafe_payload_scan": str(result.unsafe_scan_path),
                "unsafe_payload_scan_passed": result.unsafe_scan.get("passed"),
                "payload_has_embedding": result.unsafe_scan.get("payload_has_embedding"),
                "payload_has_image_bytes": result.unsafe_scan.get("payload_has_image_bytes"),
                "has_suspicious_numeric_vectors": result.unsafe_scan.get("has_suspicious_numeric_vectors"),
                "errors": [],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
