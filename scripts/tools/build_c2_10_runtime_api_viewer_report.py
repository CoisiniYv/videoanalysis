#!/usr/bin/env python3
"""Build C2.10 runtime API/viewer proof artifacts for watchlist evidence."""

from __future__ import annotations

import argparse
import html
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "services" / "api"
DEFAULT_DATABASE_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_EVENT_ID = "b4cf4b6b-9d90-4282-9b7f-5e0c56e81a32"
DEFAULT_SOURCE_EVENT_ID = (
    "c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:"
    "c2_7_event_worker_persistence_20260607T223424"
)
DEFAULT_C2_8_RESPONSE = Path(
    "/data/video-analytics/media/evidence/"
    "c2_8_api_evidence_detail_20260607T225039/api_evidence_detail_response.json"
)
DEFAULT_C2_6R_BUNDLE = Path(
    "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035"
)
DEFAULT_C2_6R_AUDIT = Path(
    "/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035"
)
DEFAULT_VIEWER_BASE_URL = "http://127.0.0.1:8090"

RESULT_PASS = "PASS_C2_10_RUNTIME_API_VIEWER_READY"
RESULT_LIVE_API_STATIC = "PARTIAL_C2_10_LIVE_API_READY_VIEWER_STATIC_READY"
RESULT_STATIC_ONLY = "PARTIAL_C2_10_STATIC_REPORT_ONLY_READY"
RESULT_FAIL = "FAIL_C2_10_RUNTIME_VIEWER_BLOCKED"

LIVE_API_BY_EVENT_ID_FILE = "live_api_response_by_event_id.json"
LIVE_API_BY_SOURCE_EVENT_ID_FILE = "live_api_response_by_source_event_id.json"
VIEWER_HEALTH_FILE = "evidence_viewer_health.json"
VIEWER_MANIFEST_FILE = "evidence_viewer_bundle_manifest.json"
VIEWER_ANNOTATIONS_SUMMARY_FILE = "evidence_viewer_annotations_summary.json"
REPORT_FILE = "operator_watchlist_evidence.html"
SUMMARY_FILE = "c2_10_runtime_viewer_summary.json"
UNSAFE_SCAN_FILE = "unsafe_payload_scan.json"
TEMP_API_LOG_FILE = "temporary_api_http.log"


@dataclass
class TempApiProcess:
    process: subprocess.Popen[str]
    base_url: str
    log_handle: Any

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log_handle.close()


@dataclass(frozen=True)
class BuildResult:
    result_marker: str
    output_dir: Path
    summary_path: Path
    report_path: Path
    unsafe_scan_path: Path
    summary: dict[str, Any]


def build_c2_10_runtime_api_viewer_report(
    *,
    output_dir: Path,
    event_id: str,
    source_event_id: str,
    c2_8_response_path: Path,
    c2_6r_bundle: Path,
    c2_6r_audit: Path,
    database_url: str,
    api_base_url: str | None,
    viewer_base_url: str | None,
    start_temp_api: bool,
    overwrite: bool = False,
) -> BuildResult:
    output_dir = output_dir.resolve(strict=False)
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        _clear_dir(output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    if not c2_8_response_path.is_file():
        raise FileNotFoundError(f"C2.8 API response missing: {c2_8_response_path}")
    if not c2_6r_bundle.is_dir():
        raise FileNotFoundError(f"C2.6R bundle missing: {c2_6r_bundle}")
    if not c2_6r_audit.is_dir():
        raise FileNotFoundError(f"C2.6R audit missing: {c2_6r_audit}")

    c2_8_response = _read_json(c2_8_response_path)
    c2_8_detail = _extract_evidence_detail(c2_8_response)
    if not c2_8_detail:
        raise RuntimeError("c2_8_evidence_detail_missing")

    temp_api: TempApiProcess | None = None
    api_runtime_mode = "not_verified"
    api_probe_base_url = api_base_url
    if not api_probe_base_url and start_temp_api:
        temp_api = _start_temp_api(output_dir, database_url)
        api_probe_base_url = temp_api.base_url
        api_runtime_mode = "temporary_local_uvicorn"
    elif api_probe_base_url:
        api_runtime_mode = "configured_http"

    try:
        api_by_event_id = _query_api_evidence(
            api_probe_base_url,
            event_id,
            output_dir / LIVE_API_BY_EVENT_ID_FILE,
        )
        api_by_source_event_id = _query_api_evidence(
            api_probe_base_url,
            source_event_id,
            output_dir / LIVE_API_BY_SOURCE_EVENT_ID_FILE,
        )
    finally:
        if temp_api is not None:
            temp_api.stop()

    viewer_health = _query_viewer_json(
        viewer_base_url,
        "/health",
        output_dir / VIEWER_HEALTH_FILE,
    )
    bundle_id = c2_6r_bundle.name
    viewer_manifest = _query_viewer_json(
        viewer_base_url,
        f"/api/bundles/{urllib.parse.quote(bundle_id, safe='')}",
        output_dir / VIEWER_MANIFEST_FILE,
    )
    viewer_annotations = _query_viewer_annotations_summary(
        viewer_base_url,
        bundle_id,
        output_dir / VIEWER_ANNOTATIONS_SUMMARY_FILE,
    )

    live_detail = _extract_evidence_detail(api_by_source_event_id.get("json") or {})
    detail = live_detail or c2_8_detail
    known_face = _load_known_face_from_sidecar(
        c2_6r_bundle / "annotations.frame_cache.identity.jsonl",
        source_observation_id=str(detail.get("source_observation_id") or ""),
    )
    context = _build_report_context(
        detail=detail,
        known_face=known_face,
        c2_8_response_path=c2_8_response_path,
        c2_6r_bundle=c2_6r_bundle,
        c2_6r_audit=c2_6r_audit,
        live_api_by_event_id=api_by_event_id,
        live_api_by_source_event_id=api_by_source_event_id,
        viewer_health=viewer_health,
        viewer_manifest=viewer_manifest,
        viewer_annotations=viewer_annotations,
        api_runtime_mode=api_runtime_mode,
        api_base_url=api_probe_base_url or "",
        viewer_base_url=viewer_base_url or "",
        temp_api_started=temp_api is not None,
    )
    report_html = render_operator_report(context)
    report_path = output_dir / REPORT_FILE
    report_path.write_text(report_html, encoding="utf-8")

    unsafe_scan = scan_for_unsafe_payload(
        {
            "evidence_detail": detail,
            "known_face": known_face,
            "operator_report_html": report_html,
        }
    )
    summary = build_summary(context, unsafe_scan, output_dir)
    validate_summary(summary)
    result_marker = determine_result_marker(summary)
    summary["result_marker"] = result_marker

    summary_path = output_dir / SUMMARY_FILE
    unsafe_scan_path = output_dir / UNSAFE_SCAN_FILE
    _write_json(summary_path, summary)
    _write_json(unsafe_scan_path, unsafe_scan)
    return BuildResult(
        result_marker=result_marker,
        output_dir=output_dir,
        summary_path=summary_path,
        report_path=report_path,
        unsafe_scan_path=unsafe_scan_path,
        summary=summary,
    )


def render_operator_report(context: dict[str, Any]) -> str:
    detail = _dict(context.get("detail"))
    person = _dict(detail.get("person"))
    watchlist = _dict(detail.get("watchlist"))
    evidence = _dict(detail.get("evidence"))
    rows = [
        ("Event type", detail.get("event_type")),
        ("Event id", detail.get("event_id")),
        ("Source event id", detail.get("source_event_id")),
        ("Source observation id", detail.get("source_observation_id")),
        ("Object label", "known_face"),
        ("Person id", person.get("person_id")),
        ("External person id", person.get("external_person_id")),
        ("Watchlist rule", watchlist.get("watchlist_rule_id")),
        ("Similarity", watchlist.get("similarity")),
        ("Threshold", watchlist.get("threshold")),
        ("Gallery embedding id", watchlist.get("gallery_embedding_id")),
        ("Match result id", watchlist.get("match_result_id")),
        ("Evidence bundle", evidence.get("bundle_path")),
        ("Raw clip", evidence.get("raw_clip_path")),
        ("Sidecar", evidence.get("sidecar_path")),
        ("Watchlist event", evidence.get("watchlist_event_path")),
        ("Summary", evidence.get("summary_path")),
        ("Audit HTML", context.get("audit_index_path")),
        ("Contact sheet", context.get("contact_sheet_path")),
        ("Capture mode", evidence.get("capture_mode")),
        ("Workaround used", evidence.get("workaround_used")),
        ("Event-style Replay passed", evidence.get("event_style_replay_job_passed")),
        ("Video integrity", evidence.get("video_integrity_status")),
        ("Production ready", evidence.get("production_ready")),
        ("Live API status", context.get("live_api_status")),
        ("Evidence viewer status", context.get("viewer_status")),
    ]
    row_html = "\n".join(
        f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>"
        for label, value in rows
    )
    viewer_links = ""
    if context.get("viewer_manifest_url"):
        viewer_links += f'<li><a href="{_esc(context["viewer_manifest_url"])}">Viewer bundle manifest</a></li>'
    if context.get("viewer_annotations_url"):
        viewer_links += f'<li><a href="{_esc(context["viewer_annotations_url"])}">Viewer sidecar annotations</a></li>'
    if context.get("viewer_raw_clip_url"):
        viewer_links += f'<li><a href="{_esc(context["viewer_raw_clip_url"])}">Viewer raw clip</a></li>'
    if not viewer_links:
        viewer_links = "<li>Viewer runtime URL not verified.</li>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>C2.10 Watchlist Evidence Detail</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #1f2933; }}
    h1 {{ font-size: 24px; margin-bottom: 4px; }}
    h2 {{ font-size: 18px; margin-top: 24px; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 1180px; }}
    th, td {{ border: 1px solid #d9e2ec; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ width: 260px; background: #f0f4f8; }}
    .warning {{ border-left: 4px solid #d64545; padding: 10px 12px; background: #fff5f5; max-width: 1180px; }}
    .ok {{ border-left: 4px solid #2f855a; padding: 10px 12px; background: #f0fff4; max-width: 1180px; }}
  </style>
</head>
<body>
  <h1>Watchlist Hit</h1>
  <p class="ok">Runtime evidence detail recovered for a known_face watchlist_hit.</p>
  <div class="warning">Event-style Replay is not passed. This evidence uses the stable_post_savant_sink_time_crop workaround.</div>
  <h2>Operator Evidence</h2>
  <table>
    {row_html}
  </table>
  <h2>Runtime Links</h2>
  <ul>{viewer_links}</ul>
  <h2>Limitations</h2>
  <ul>
    <li>stable sink workaround</li>
    <li>event-style Replay not passed</li>
    <li>not a long-running worker soak</li>
    <li>not a broad accuracy test</li>
  </ul>
</body>
</html>
"""


def build_summary(
    context: dict[str, Any],
    unsafe_scan: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    detail = _dict(context.get("detail"))
    person = _dict(detail.get("person"))
    watchlist = _dict(detail.get("watchlist"))
    evidence = _dict(detail.get("evidence"))
    live_api_verified = bool(context.get("live_api_by_event_id_verified")) and bool(
        context.get("live_api_by_source_event_id_verified")
    )
    viewer_verified = bool(context.get("viewer_health_verified")) and bool(
        context.get("viewer_manifest_verified")
    ) and bool(context.get("viewer_annotations_verified"))
    report_path = output_dir / REPORT_FILE
    summary = {
        "result_marker": RESULT_FAIL,
        "output_dir": str(output_dir),
        "operator_report_path": str(report_path),
        "db_event_id": detail.get("event_id"),
        "source_event_id": detail.get("source_event_id"),
        "event_type": detail.get("event_type"),
        "status": detail.get("status"),
        "camera_id": detail.get("camera_id"),
        "source_id": detail.get("source_id"),
        "track_id": detail.get("track_id"),
        "source_observation_id": detail.get("source_observation_id"),
        "person_id": person.get("person_id"),
        "external_person_id": person.get("external_person_id"),
        "watchlist_rule_id": watchlist.get("watchlist_rule_id"),
        "similarity": watchlist.get("similarity"),
        "threshold": watchlist.get("threshold"),
        "gallery_embedding_id": watchlist.get("gallery_embedding_id"),
        "match_result_id": watchlist.get("match_result_id"),
        "evidence_bundle_path": evidence.get("bundle_path"),
        "raw_clip_path": evidence.get("raw_clip_path"),
        "sidecar_path": evidence.get("sidecar_path"),
        "watchlist_event_path": evidence.get("watchlist_event_path"),
        "summary_path": evidence.get("summary_path"),
        "audit_index_path": context.get("audit_index_path"),
        "contact_sheet_path": context.get("contact_sheet_path"),
        "production_ready": evidence.get("production_ready"),
        "video_integrity_status": evidence.get("video_integrity_status"),
        "known_face_count": evidence.get("known_face_count"),
        "watchlist_hit_count": evidence.get("watchlist_hit_count"),
        "evidence_capture_mode": evidence.get("capture_mode"),
        "workaround_used": evidence.get("workaround_used"),
        "event_style_replay_job_passed": evidence.get("event_style_replay_job_passed"),
        "live_api_http_verified": live_api_verified,
        "live_api_by_event_id_verified": context.get("live_api_by_event_id_verified"),
        "live_api_by_source_event_id_verified": context.get("live_api_by_source_event_id_verified"),
        "live_api_status_code_by_event_id": context.get("live_api_status_code_by_event_id"),
        "live_api_status_code_by_source_event_id": context.get("live_api_status_code_by_source_event_id"),
        "live_api_base_url": context.get("api_base_url"),
        "api_runtime_mode": context.get("api_runtime_mode"),
        "temporary_api_started": context.get("temp_api_started"),
        "temporary_api_stopped": context.get("temp_api_started"),
        "live_api_response_by_event_id_json": str(output_dir / LIVE_API_BY_EVENT_ID_FILE),
        "live_api_response_by_source_event_id_json": str(output_dir / LIVE_API_BY_SOURCE_EVENT_ID_FILE),
        "evidence_viewer_verified": viewer_verified,
        "viewer_health_verified": context.get("viewer_health_verified"),
        "viewer_manifest_verified": context.get("viewer_manifest_verified"),
        "viewer_annotations_verified": context.get("viewer_annotations_verified"),
        "viewer_health_status_code": context.get("viewer_health_status_code"),
        "viewer_manifest_status_code": context.get("viewer_manifest_status_code"),
        "viewer_annotations_status_code": context.get("viewer_annotations_status_code"),
        "viewer_base_url": context.get("viewer_base_url"),
        "viewer_manifest_url": context.get("viewer_manifest_url"),
        "viewer_annotations_url": context.get("viewer_annotations_url"),
        "viewer_raw_clip_url": context.get("viewer_raw_clip_url"),
        "viewer_health_json": str(output_dir / VIEWER_HEALTH_FILE),
        "viewer_manifest_json": str(output_dir / VIEWER_MANIFEST_FILE),
        "viewer_annotations_summary_json": str(output_dir / VIEWER_ANNOTATIONS_SUMMARY_FILE),
        "operator_report_generated": report_path.is_file(),
        "payload_has_embedding": unsafe_scan.get("payload_has_embedding"),
        "payload_has_image_bytes": unsafe_scan.get("payload_has_image_bytes"),
        "forbidden_key_paths": unsafe_scan.get("forbidden_key_paths") or [],
        "unsafe_payload_scan_json": str(output_dir / UNSAFE_SCAN_FILE),
        "containers_restarted": False,
        "event_style_replay_claimed_passed": False,
    }
    return summary


def determine_result_marker(summary: dict[str, Any]) -> str:
    if summary.get("live_api_http_verified") is True and summary.get("evidence_viewer_verified") is True:
        return RESULT_PASS
    if summary.get("live_api_http_verified") is True and summary.get("operator_report_generated") is True:
        return RESULT_LIVE_API_STATIC
    if summary.get("operator_report_generated") is True:
        return RESULT_STATIC_ONLY
    return RESULT_FAIL


def validate_summary(summary: dict[str, Any]) -> None:
    if summary.get("operator_report_generated") is not True:
        raise RuntimeError("operator_report_missing")
    if summary.get("event_type") != "watchlist_hit":
        raise RuntimeError("event_type_not_watchlist_hit")
    for key in (
        "source_observation_id",
        "person_id",
        "external_person_id",
        "watchlist_rule_id",
        "similarity",
        "threshold",
        "evidence_bundle_path",
        "raw_clip_path",
        "sidecar_path",
        "watchlist_event_path",
    ):
        if summary.get(key) in (None, ""):
            raise RuntimeError(f"summary_missing_{key}")
    if not Path(str(summary["evidence_bundle_path"])).is_dir():
        raise RuntimeError("evidence_bundle_missing")
    for key in ("raw_clip_path", "sidecar_path", "watchlist_event_path", "summary_path"):
        value = summary.get(key)
        if value and not Path(str(value)).is_file():
            raise RuntimeError(f"evidence_file_missing_{key}")
    if summary.get("video_integrity_status") != "pass":
        raise RuntimeError("video_integrity_not_pass")
    if summary.get("production_ready") is not True:
        raise RuntimeError("production_ready_not_true")
    if int(summary.get("known_face_count") or 0) <= 0:
        raise RuntimeError("known_face_count_missing")
    if int(summary.get("watchlist_hit_count") or 0) != 1:
        raise RuntimeError("watchlist_hit_count_not_1")
    if summary.get("evidence_capture_mode") != "stable_post_savant_sink_time_crop":
        raise RuntimeError("capture_mode_invalid")
    if summary.get("workaround_used") is not True:
        raise RuntimeError("workaround_used_not_true")
    if summary.get("event_style_replay_job_passed") is not False:
        raise RuntimeError("event_style_replay_job_passed_not_false")
    if summary.get("payload_has_embedding") is not False:
        raise RuntimeError("unsafe_embedding_present")
    if summary.get("payload_has_image_bytes") is not False:
        raise RuntimeError("unsafe_image_bytes_present")
    if summary.get("forbidden_key_paths"):
        raise RuntimeError("unsafe_forbidden_key_paths_present")


def scan_for_unsafe_payload(value: Any) -> dict[str, Any]:
    hits = sorted(set(_find_unsafe(value)))
    return {
        "payload_has_embedding": any("embedding" in hit.lower() for hit in hits),
        "payload_has_image_bytes": any(
            token in hit.lower()
            for hit in hits
            for token in ("image_bytes", "crop_bytes", "base64", "frame_bytes", "raw_frame")
        ),
        "forbidden_key_paths": hits,
    }


def _find_unsafe(value: Any, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            next_path = f"{path}.{key_text}" if path else key_text
            if lowered in {
                "embedding",
                "embeddings",
                "embedding_vector",
                "embedding_values",
                "embedding_list",
            }:
                hits.append(next_path)
            if lowered in {"image_bytes", "crop_bytes", "frame_bytes", "raw_frame", "base64", "base64_image", "image_base64"}:
                if nested not in (None, "", False, 0, [], {}):
                    hits.append(next_path)
            hits.extend(_find_unsafe(nested, next_path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            hits.extend(_find_unsafe(nested, f"{path}.{index}" if path else str(index)))
    elif isinstance(value, str):
        lowered = value.lower()
        if ";base64," in lowered or lowered.startswith("data:image"):
            hits.append(path or "value")
    return hits


def _build_report_context(
    *,
    detail: dict[str, Any],
    known_face: dict[str, Any],
    c2_8_response_path: Path,
    c2_6r_bundle: Path,
    c2_6r_audit: Path,
    live_api_by_event_id: dict[str, Any],
    live_api_by_source_event_id: dict[str, Any],
    viewer_health: dict[str, Any],
    viewer_manifest: dict[str, Any],
    viewer_annotations: dict[str, Any],
    api_runtime_mode: str,
    api_base_url: str,
    viewer_base_url: str,
    temp_api_started: bool,
) -> dict[str, Any]:
    bundle_id = c2_6r_bundle.name
    viewer_manifest_url = (
        f"{viewer_base_url.rstrip('/')}/api/bundles/{urllib.parse.quote(bundle_id, safe='')}"
        if viewer_base_url
        else ""
    )
    viewer_annotations_url = (
        f"{viewer_manifest_url}/annotations?source=sidecar" if viewer_manifest_url else ""
    )
    viewer_raw_clip_url = (
        f"{viewer_manifest_url}/media/raw_clip" if viewer_manifest_url else ""
    )
    return {
        "detail": detail,
        "known_face": known_face,
        "c2_8_response_path": str(c2_8_response_path),
        "c2_6r_bundle": str(c2_6r_bundle),
        "c2_6r_audit": str(c2_6r_audit),
        "audit_index_path": str(c2_6r_audit / "index.html"),
        "contact_sheet_path": str(c2_6r_audit / "contact_sheet.jpg"),
        "live_api_by_event_id_verified": live_api_by_event_id.get("verified") is True,
        "live_api_by_source_event_id_verified": live_api_by_source_event_id.get("verified") is True,
        "live_api_status_code_by_event_id": live_api_by_event_id.get("status_code"),
        "live_api_status_code_by_source_event_id": live_api_by_source_event_id.get("status_code"),
        "live_api_status": _status_text(
            live_api_by_event_id.get("verified") is True
            and live_api_by_source_event_id.get("verified") is True,
            live_api_by_source_event_id.get("status_code"),
        ),
        "api_runtime_mode": api_runtime_mode,
        "api_base_url": api_base_url,
        "temp_api_started": temp_api_started,
        "viewer_health_verified": viewer_health.get("verified") is True,
        "viewer_manifest_verified": viewer_manifest.get("verified") is True
        and _dict(viewer_manifest.get("json")).get("event_id") == bundle_id,
        "viewer_annotations_verified": viewer_annotations.get("verified") is True
        and viewer_annotations.get("known_face_found") is True,
        "viewer_health_status_code": viewer_health.get("status_code"),
        "viewer_manifest_status_code": viewer_manifest.get("status_code"),
        "viewer_annotations_status_code": viewer_annotations.get("status_code"),
        "viewer_status": _status_text(
            viewer_health.get("verified") is True
            and viewer_manifest.get("verified") is True
            and viewer_annotations.get("verified") is True,
            viewer_annotations.get("status_code") or viewer_manifest.get("status_code"),
        ),
        "viewer_base_url": viewer_base_url,
        "viewer_manifest_url": viewer_manifest_url,
        "viewer_annotations_url": viewer_annotations_url,
        "viewer_raw_clip_url": viewer_raw_clip_url,
    }


def _query_api_evidence(
    base_url: str | None,
    event_id: str,
    output_path: Path,
) -> dict[str, Any]:
    if not base_url:
        payload = {"verified": False, "status_code": None, "error": "api_base_url_unavailable"}
        _write_json(output_path, payload)
        return payload
    url = f"{base_url.rstrip('/')}/api/v1/events/{urllib.parse.quote(event_id, safe=':')}/evidence"
    response = _http_json(url)
    body = _dict(response.get("json"))
    detail = _extract_evidence_detail(body)
    verified = (
        response.get("status_code") == 200
        and detail.get("event_type") == "watchlist_hit"
        and detail.get("source_observation_id") == "face:c2_post_savant_fps_probe:4:17854:1"
        and _dict(detail.get("person")).get("person_id") == 4
        and _dict(detail.get("evidence")).get("capture_mode") == "stable_post_savant_sink_time_crop"
        and _dict(detail.get("evidence")).get("event_style_replay_job_passed") is False
    )
    payload = {"verified": bool(verified), "url": url, **response}
    _write_json(output_path, payload)
    return payload


def _query_viewer_json(
    base_url: str | None,
    path: str,
    output_path: Path,
) -> dict[str, Any]:
    if not base_url:
        payload = {"verified": False, "status_code": None, "error": "viewer_base_url_unavailable"}
        _write_json(output_path, payload)
        return payload
    url = f"{base_url.rstrip('/')}{path}"
    response = _http_json(url)
    payload = {
        "verified": response.get("status_code") == 200 and isinstance(response.get("json"), dict),
        "url": url,
        **response,
    }
    _write_json(output_path, payload)
    return payload


def _query_viewer_annotations_summary(
    base_url: str | None,
    bundle_id: str,
    output_path: Path,
) -> dict[str, Any]:
    if not base_url:
        payload = {"verified": False, "status_code": None, "error": "viewer_base_url_unavailable"}
        _write_json(output_path, payload)
        return payload
    url = (
        f"{base_url.rstrip('/')}/api/bundles/{urllib.parse.quote(bundle_id, safe='')}"
        "/annotations?source=sidecar"
    )
    response = _http_json(url)
    body = _dict(response.get("json"))
    known_face = _find_known_face_in_records(body.get("records"))
    payload = {
        "verified": response.get("status_code") == 200 and isinstance(body.get("records"), list),
        "url": url,
        "status_code": response.get("status_code"),
        "event_id": body.get("event_id"),
        "count": body.get("count"),
        "annotation_source": body.get("annotation_source"),
        "annotation_source_kind": body.get("annotation_source_kind"),
        "fallback_used": body.get("fallback_used"),
        "legacy_used_for_visual_binding": body.get("legacy_used_for_visual_binding"),
        "production_ready": body.get("production_ready"),
        "known_face_found": bool(known_face),
        "known_face": known_face,
        "error": response.get("error"),
    }
    _write_json(output_path, payload)
    return payload


def _http_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url)
    try:
        with opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = {"text": text[:2000]}
            return {"status_code": response.status, "json": body}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {"text": text[:2000]}
        return {"status_code": exc.code, "json": body, "error": str(exc)}
    except Exception as exc:
        return {"status_code": None, "json": {}, "error": str(exc)}


def _start_temp_api(output_dir: Path, database_url: str) -> TempApiProcess:
    port = _free_port()
    log_path = output_dir / TEMP_API_LOG_FILE
    log_handle = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["MEDIA_ROOT"] = "/data/video-analytics/media"
    env["PYTHONPATH"] = f"{API_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(API_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        if proc.poll() is not None:
            log_handle.flush()
            raise RuntimeError(f"temporary_api_exited:{proc.returncode}")
        health = _http_json(f"{base_url}/health", timeout=1.0)
        if health.get("status_code") == 200:
            return TempApiProcess(process=proc, base_url=base_url, log_handle=log_handle)
        time.sleep(0.25)
    proc.terminate()
    proc.wait(timeout=5)
    log_handle.close()
    raise RuntimeError("temporary_api_health_timeout")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _load_known_face_from_sidecar(path: Path, *, source_observation_id: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        known = _find_known_face_in_records([row], source_observation_id=source_observation_id)
        if known:
            return known
    return {}


def _find_known_face_in_records(
    records: Any,
    *,
    source_observation_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(records, list):
        return {}
    for row in records:
        if not isinstance(row, dict):
            continue
        for obj in row.get("objects") or []:
            if not isinstance(obj, dict):
                continue
            identity = _dict(obj.get("identity"))
            label = _dict(obj.get("label"))
            if source_observation_id and identity.get("source_observation_id") != source_observation_id:
                continue
            if obj.get("object_type") == "known_face" or label.get("kind") == "known_face":
                return {
                    "frame_index": row.get("frame_index"),
                    "frame_pts": row.get("frame_pts"),
                    "object_type": obj.get("object_type"),
                    "track_id": obj.get("track_id"),
                    "label": {
                        "kind": label.get("kind"),
                        "person_id": label.get("person_id"),
                        "external_person_id": label.get("external_person_id"),
                        "similarity": label.get("similarity"),
                        "threshold": label.get("threshold"),
                        "watchlist_rule_id": label.get("watchlist_rule_id"),
                    },
                    "identity": {
                        "event_type": identity.get("event_type"),
                        "person_id": identity.get("person_id"),
                        "external_person_id": identity.get("external_person_id"),
                        "similarity": identity.get("similarity"),
                        "threshold": identity.get("threshold"),
                        "source_observation_id": identity.get("source_observation_id"),
                        "watchlist_rule_id": identity.get("watchlist_rule_id"),
                        "match_result_id": identity.get("match_result_id"),
                        "gallery_embedding_id": identity.get("gallery_embedding_id"),
                        "primary_identity_join_key": identity.get("primary_identity_join_key"),
                        "track_id_join_warning": identity.get("track_id_join_warning"),
                    },
                }
    return {}


def _extract_evidence_detail(response: dict[str, Any]) -> dict[str, Any]:
    if isinstance(response.get("evidence_detail"), dict):
        return response["evidence_detail"]
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    if isinstance(data.get("evidence_detail"), dict):
        return data["evidence_detail"]
    json_payload = response.get("json") if isinstance(response.get("json"), dict) else {}
    data = json_payload.get("data") if isinstance(json_payload.get("data"), dict) else {}
    if isinstance(data.get("evidence_detail"), dict):
        return data["evidence_detail"]
    return {}


def _status_text(verified: bool, status_code: Any) -> str:
    if verified:
        return f"verified:{status_code}"
    if status_code:
        return f"not_verified:{status_code}"
    return "not_verified"


def _esc(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"json_not_object:{path}")
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clear_dir(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            import shutil

            shutil.rmtree(child)
        else:
            child.unlink()


def _result_payload(result: BuildResult) -> dict[str, Any]:
    return {
        "result_marker": result.result_marker,
        "output_dir": str(result.output_dir),
        "operator_report_path": str(result.report_path),
        "c2_10_runtime_viewer_summary_json": str(result.summary_path),
        "unsafe_payload_scan_json": str(result.unsafe_scan_path),
        "live_api_http_verified": result.summary.get("live_api_http_verified"),
        "evidence_viewer_verified": result.summary.get("evidence_viewer_verified"),
        "live_api_base_url": result.summary.get("live_api_base_url"),
        "viewer_base_url": result.summary.get("viewer_base_url"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--event-id", default=DEFAULT_EVENT_ID)
    parser.add_argument("--source-event-id", default=DEFAULT_SOURCE_EVENT_ID)
    parser.add_argument("--c2-8-response", default=DEFAULT_C2_8_RESPONSE, type=Path)
    parser.add_argument("--c2-6r-bundle", default=DEFAULT_C2_6R_BUNDLE, type=Path)
    parser.add_argument("--c2-6r-audit", default=DEFAULT_C2_6R_AUDIT, type=Path)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--api-base-url", default="")
    parser.add_argument("--viewer-base-url", default=DEFAULT_VIEWER_BASE_URL)
    parser.add_argument("--start-temp-api", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--overwrite", action="store_true", default=False)
    args = parser.parse_args(argv)

    try:
        result = build_c2_10_runtime_api_viewer_report(
            output_dir=args.output_dir,
            event_id=args.event_id,
            source_event_id=args.source_event_id,
            c2_8_response_path=args.c2_8_response,
            c2_6r_bundle=args.c2_6r_bundle,
            c2_6r_audit=args.c2_6r_audit,
            database_url=args.database_url,
            api_base_url=args.api_base_url or None,
            viewer_base_url=args.viewer_base_url or None,
            start_temp_api=args.start_temp_api,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        payload = {
            "result_marker": RESULT_FAIL,
            "reason": f"{type(exc).__name__}:{exc}",
            "output_dir": str(args.output_dir),
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    print(json.dumps(_result_payload(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
