#!/usr/bin/env python3
"""Build C2.8 API evidence-detail proof artifacts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "services" / "api"
DEFAULT_DATABASE_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_SOURCE_EVENT_ID = (
    "c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:"
    "c2_7_event_worker_persistence_20260607T223424"
)
RESULT_PASS = "PASS_C2_8_API_EVIDENCE_DETAIL_READY"
RESULT_REPOSITORY_ONLY = "PARTIAL_C2_8_REPOSITORY_ONLY_EVIDENCE_DETAIL_READY"
RESULT_FAIL = "FAIL_C2_8_API_EVIDENCE_DETAIL_BLOCKED"

API_RESPONSE_FILE = "api_evidence_detail_response.json"
REPOSITORY_RESPONSE_FILE = "repository_evidence_detail_response.json"
SUMMARY_FILE = "c2_8_api_evidence_detail_summary.json"
UNSAFE_SCAN_FILE = "unsafe_payload_scan.json"


@dataclass(frozen=True)
class BuildResult:
    result_marker: str
    output_dir: Path
    api_response_path: Path
    repository_response_path: Path
    summary_path: Path
    unsafe_scan_path: Path
    api_response: dict[str, Any]
    repository_response: dict[str, Any]
    summary: dict[str, Any]
    unsafe_scan: dict[str, Any]


def build_c2_8_api_evidence_detail(
    *,
    source_event_id: str,
    output_dir: Path,
    database_url: str,
    overwrite: bool = False,
) -> BuildResult:
    output_dir = output_dir.resolve(strict=False)
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        for child in output_dir.iterdir():
            if child.is_dir():
                import shutil

                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    repository_response = query_repository_detail(
        database_url=database_url,
        source_event_id=source_event_id,
    )
    api_response = query_testclient_detail(
        database_url=database_url,
        source_event_id=source_event_id,
    )
    repository_detail = _detail_from_repository_response(repository_response)
    api_detail = _detail_from_api_response(api_response)
    detail = api_detail or repository_detail
    if not detail:
        raise RuntimeError("evidence_detail_response_missing")

    unsafe_scan = scan_for_unsafe_payload(detail)
    summary = build_summary(
        source_event_id=source_event_id,
        output_dir=output_dir,
        api_response=api_response,
        repository_response=repository_response,
        detail=detail,
        unsafe_scan=unsafe_scan,
    )
    validate_summary(summary, detail, unsafe_scan)

    result_marker = RESULT_PASS if summary["api_route_verified"] else RESULT_REPOSITORY_ONLY
    summary["result_marker"] = result_marker

    api_response_path = output_dir / API_RESPONSE_FILE
    repository_response_path = output_dir / REPOSITORY_RESPONSE_FILE
    summary_path = output_dir / SUMMARY_FILE
    unsafe_scan_path = output_dir / UNSAFE_SCAN_FILE
    _write_json(api_response_path, api_response)
    _write_json(repository_response_path, repository_response)
    _write_json(summary_path, summary)
    _write_json(unsafe_scan_path, unsafe_scan)
    return BuildResult(
        result_marker=result_marker,
        output_dir=output_dir,
        api_response_path=api_response_path,
        repository_response_path=repository_response_path,
        summary_path=summary_path,
        unsafe_scan_path=unsafe_scan_path,
        api_response=api_response,
        repository_response=repository_response,
        summary=summary,
        unsafe_scan=unsafe_scan,
    )


def query_repository_detail(*, database_url: str, source_event_id: str) -> dict[str, Any]:
    code = r"""
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

api_root, database_url, source_event_id = sys.argv[1:4]
sys.path.insert(0, api_root)
os.environ["DATABASE_URL"] = database_url

import psycopg
from psycopg.rows import dict_row
from app.repositories.events import EventRepository
from app.schemas.events import EventResponse, EvidenceTaskResponse, EventEvidenceResponse
from app.services.evidence_detail_resolver import resolve_event_evidence_detail

def encode(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(type(value).__name__)

with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
    repo = EventRepository(conn)
    row = repo.get_by_source_event_id(source_event_id)
    if row is None:
        payload = {"verified": False, "error": "event_not_found"}
    else:
        event = EventResponse.from_db_row(row)
        tasks = [EvidenceTaskResponse.from_db_row(t) for t in repo.list_evidence_tasks(source_event_id)]
        response = EventEvidenceResponse.from_event_and_tasks(
            event=event,
            evidence_tasks=tasks,
            evidence_detail=resolve_event_evidence_detail(event),
        )
        payload = {"verified": True, "data": response.model_dump()}
print(json.dumps(payload, default=encode, sort_keys=True))
"""
    return _run_json_subprocess(code, database_url, source_event_id)


def query_testclient_detail(*, database_url: str, source_event_id: str) -> dict[str, Any]:
    code = r"""
import json
import os
import sys
from urllib.parse import quote

api_root, database_url, source_event_id = sys.argv[1:4]
sys.path.insert(0, api_root)
os.environ["DATABASE_URL"] = database_url

from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    response = client.get("/api/v1/events/" + quote(source_event_id, safe=":") + "/evidence")
body = response.json()
payload = {
    "verified": response.status_code == 200
        and isinstance(body, dict)
        and isinstance(body.get("data"), dict)
        and body["data"].get("source_event_id") == source_event_id
        and isinstance(body["data"].get("evidence_detail"), dict),
    "status_code": response.status_code,
    "json": body,
}
print(json.dumps(payload, sort_keys=True))
"""
    return _run_json_subprocess(code, database_url, source_event_id)


def _run_json_subprocess(code: str, database_url: str, source_event_id: str) -> dict[str, Any]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    proc = subprocess.run(
        [sys.executable, "-c", code, str(API_ROOT), database_url, source_event_id],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "verified": False,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "verified": False,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "error": f"json_decode:{exc}",
        }
    return payload if isinstance(payload, dict) else {"verified": False, "payload": payload}


def build_summary(
    *,
    source_event_id: str,
    output_dir: Path,
    api_response: dict[str, Any],
    repository_response: dict[str, Any],
    detail: dict[str, Any],
    unsafe_scan: dict[str, Any],
) -> dict[str, Any]:
    evidence = _dict(detail.get("evidence"))
    person = _dict(detail.get("person"))
    watchlist = _dict(detail.get("watchlist"))
    return {
        "result_marker": RESULT_PASS if api_response.get("verified") is True else RESULT_REPOSITORY_ONLY,
        "db_event_id": detail.get("event_id"),
        "source_event_id": source_event_id,
        "api_route_verified": api_response.get("verified") is True,
        "testclient_verified": api_response.get("verified") is True,
        "repository_verified": repository_response.get("verified") is True,
        "live_http_verified": False,
        "event_type": detail.get("event_type"),
        "source_observation_id": detail.get("source_observation_id"),
        "person_id": person.get("person_id"),
        "external_person_id": person.get("external_person_id"),
        "watchlist_rule_id": watchlist.get("watchlist_rule_id"),
        "similarity": watchlist.get("similarity"),
        "threshold": watchlist.get("threshold"),
        "match_result_id": watchlist.get("match_result_id"),
        "gallery_embedding_id": watchlist.get("gallery_embedding_id"),
        "evidence_bundle_path": evidence.get("bundle_path"),
        "raw_clip_path": evidence.get("raw_clip_path"),
        "summary_path": evidence.get("summary_path"),
        "sidecar_path": evidence.get("sidecar_path"),
        "watchlist_event_path": evidence.get("watchlist_event_path"),
        "audit_path": evidence.get("audit_path"),
        "report_path": evidence.get("report_path"),
        "production_ready": evidence.get("production_ready"),
        "video_integrity_status": evidence.get("video_integrity_status"),
        "known_face_count": evidence.get("known_face_count"),
        "watchlist_hit_count": evidence.get("watchlist_hit_count"),
        "payload_has_embedding": unsafe_scan.get("payload_has_embedding"),
        "payload_has_image_bytes": unsafe_scan.get("payload_has_image_bytes"),
        "forbidden_key_paths": unsafe_scan.get("forbidden_key_paths") or [],
        "evidence_capture_mode": evidence.get("capture_mode"),
        "workaround_used": evidence.get("workaround_used"),
        "event_style_replay_job_passed": evidence.get("event_style_replay_job_passed"),
        "api_evidence_detail_response_json": str(output_dir / API_RESPONSE_FILE),
        "repository_evidence_detail_response_json": str(output_dir / REPOSITORY_RESPONSE_FILE),
        "unsafe_payload_scan_json": str(output_dir / UNSAFE_SCAN_FILE),
        "c2_8_api_evidence_detail_summary_json": str(output_dir / SUMMARY_FILE),
    }


def validate_summary(
    summary: dict[str, Any],
    detail: dict[str, Any],
    unsafe_scan: dict[str, Any],
) -> None:
    evidence = _dict(detail.get("evidence"))
    if summary.get("api_route_verified") is not True and summary.get("repository_verified") is not True:
        raise RuntimeError("query_path_unavailable")
    for key in ("source_observation_id", "event_type"):
        if not detail.get(key):
            raise RuntimeError(f"detail_missing_{key}")
    if detail.get("event_type") != "watchlist_hit":
        raise RuntimeError("detail_event_type_not_watchlist_hit")
    for key in ("person_id", "external_person_id"):
        if _dict(detail.get("person")).get(key) in (None, ""):
            raise RuntimeError(f"detail_person_missing_{key}")
    for key in ("watchlist_rule_id", "similarity", "threshold", "match_result_id", "gallery_embedding_id"):
        if _dict(detail.get("watchlist")).get(key) in (None, ""):
            raise RuntimeError(f"detail_watchlist_missing_{key}")
    for key in ("bundle_path", "raw_clip_path", "summary_path", "sidecar_path", "watchlist_event_path"):
        value = evidence.get(key)
        if not value or not Path(str(value)).is_file() and key != "bundle_path":
            raise RuntimeError(f"evidence_file_missing_{key}")
    if evidence.get("bundle_path") and not Path(str(evidence["bundle_path"])).is_dir():
        raise RuntimeError("evidence_bundle_missing")
    if evidence.get("video_integrity_status") != "pass":
        raise RuntimeError("video_integrity_status_not_pass")
    if evidence.get("production_ready") is not True:
        raise RuntimeError("production_ready_not_true")
    if int(evidence.get("known_face_count") or 0) <= 0:
        raise RuntimeError("known_face_count_missing")
    if int(evidence.get("watchlist_hit_count") or 0) != 1:
        raise RuntimeError("watchlist_hit_count_not_1")
    if evidence.get("capture_mode") != "stable_post_savant_sink_time_crop":
        raise RuntimeError("capture_mode_invalid")
    if evidence.get("workaround_used") is not True:
        raise RuntimeError("workaround_not_true")
    if evidence.get("event_style_replay_job_passed") is not False:
        raise RuntimeError("event_style_replay_job_passed_not_false")
    if unsafe_scan.get("payload_has_embedding") is not False:
        raise RuntimeError("unsafe_embedding_present")
    if unsafe_scan.get("payload_has_image_bytes") is not False:
        raise RuntimeError("unsafe_image_bytes_present")
    if unsafe_scan.get("forbidden_key_paths"):
        raise RuntimeError("unsafe_forbidden_key_paths_present")


FORBIDDEN_KEYS = {
    "embedding",
    "embeddings",
    "embedding_vector",
    "embedding_values",
    "embedding_list",
    "image_bytes",
    "crop_bytes",
    "frame_bytes",
    "raw_frame",
    "base64",
    "base64_image",
    "image_base64",
    "crop",
    "crop_image",
    "image",
}


def scan_for_unsafe_payload(value: Any) -> dict[str, Any]:
    hits = sorted(set(_find_forbidden(value)))
    return {
        "payload_has_embedding": any("embedding" in hit.lower() for hit in hits),
        "payload_has_image_bytes": any(
            token in hit.lower()
            for hit in hits
            for token in ("image", "base64", "crop", "frame_bytes", "raw_frame")
        ),
        "forbidden_key_paths": hits,
    }


def _find_forbidden(value: Any, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_KEYS:
                hits.append(next_path)
            hits.extend(_find_forbidden(nested, next_path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            hits.extend(_find_forbidden(nested, f"{path}.{index}" if path else str(index)))
    elif isinstance(value, str):
        lowered = value.lower()
        if ";base64," in lowered or lowered.startswith("data:image"):
            hits.append(path or "value")
    return hits


def _detail_from_api_response(response: dict[str, Any]) -> dict[str, Any]:
    return _dict(_dict(_dict(response.get("json")).get("data")).get("evidence_detail"))


def _detail_from_repository_response(response: dict[str, Any]) -> dict[str, Any]:
    return _dict(_dict(response.get("data")).get("evidence_detail"))


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _result_payload(result: BuildResult) -> dict[str, Any]:
    return {
        "result_marker": result.result_marker,
        "output_dir": str(result.output_dir),
        "api_evidence_detail_response_json": str(result.api_response_path),
        "repository_evidence_detail_response_json": str(result.repository_response_path),
        "unsafe_payload_scan_json": str(result.unsafe_scan_path),
        "c2_8_api_evidence_detail_summary_json": str(result.summary_path),
        "source_event_id": result.summary.get("source_event_id"),
        "db_event_id": result.summary.get("db_event_id"),
        "event_type": result.summary.get("event_type"),
        "api_route_verified": result.summary.get("api_route_verified"),
        "repository_verified": result.summary.get("repository_verified"),
        "payload_has_embedding": result.summary.get("payload_has_embedding"),
        "payload_has_image_bytes": result.summary.get("payload_has_image_bytes"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-event-id", default=DEFAULT_SOURCE_EVENT_ID)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--overwrite", action="store_true", default=False)
    args = parser.parse_args(argv)

    try:
        result = build_c2_8_api_evidence_detail(
            source_event_id=args.source_event_id,
            output_dir=args.output_dir,
            database_url=args.database_url,
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
