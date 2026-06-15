"""FastAPI service for browsing file-based evidence bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import load_settings
from app.evidence_index import (
    EvidencePathError,
    bundle_manifest,
    discover_raw_clip,
    ensure_bundle_dir,
    load_camera_name_lookup,
    load_json_object,
    media_type_for_path,
    parse_json_or_jsonl_records,
    parse_jsonl_records,
    scan_bundles,
)


settings = load_settings()
STATIC_DIR = Path(__file__).resolve().parent / "static"
OPERATOR_PROXY_TIMEOUT_SECONDS = 120.0
OPERATOR_PROXY_ALLOWED_PREFIXES = (
    "cameras",
    "algorithms",
    "events",
    "people",
    "maintenance",
    "runtime",
    "ws",
)
LEGACY_ANNOTATIONS_FILE = "annotations.jsonl"
SIDECAR_ANNOTATIONS_FILE = "annotations.frame_cache.identity.jsonl"
SIDECAR_PREVIEW_ANNOTATIONS_FILE = "annotations.frame_cache.identity.rebased.preview.jsonl"
LEGACY_SUMMARY_FILE = "summary.json"
SIDECAR_SUMMARY_FILE = "summary.frame_cache.identity.json"
SIDECAR_PREVIEW_SUMMARY_FILE = "summary.frame_cache.identity.rebased.preview.json"
AnnotationSource = Literal["auto", "sidecar", "sidecar_preview", "legacy"]
ANNOTATION_SOURCE_UNAVAILABLE = "unavailable"
AnnotationSourceKind = Literal["production_sidecar", "legacy_debug", "preview_debug", "unavailable"]
WATCHLIST_EVENT_TYPE = "watchlist_hit"
PRODUCTION_TIMELINE_DOMAIN = "final_canonical_clip"

app = FastAPI(title="Evidence Viewer", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _camera_name_lookup() -> dict[str, str]:
    return load_camera_name_lookup(
        camera_config_path=settings.camera_config_path,
        sources_config_path=settings.sources_config_path,
    )


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, EvidencePathError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _proxy_headers(request: Request) -> dict[str, str]:
    excluded = {
        "host",
        "connection",
        "content-length",
        "transfer-encoding",
        "upgrade",
    }
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in excluded
    }


def _response_headers(raw_headers) -> dict[str, str]:
    excluded = {
        "connection",
        "content-encoding",
        "content-length",
        "transfer-encoding",
    }
    return {
        key: value
        for key, value in raw_headers.items()
        if key.lower() not in excluded
    }


def _operator_proxy_url(path: str) -> str:
    clean_path = path.strip("/")
    first_segment = clean_path.split("/", 1)[0]
    if first_segment not in OPERATOR_PROXY_ALLOWED_PREFIXES:
        raise HTTPException(status_code=404, detail="operator api route not proxied")
    return f"{settings.operator_api_base_url}/api/v1/{clean_path}"


def _media_proxy_url(path: str) -> str:
    return f"{settings.operator_api_base_url}/media/{path.strip('/')}"


def _query_suffix(request: Request) -> str:
    query = urlencode(list(request.query_params.multi_items()))
    return f"?{query}" if query else ""


def _proxy_request(method: str, target: str, request: Request, body: bytes) -> Response:
    url = target + _query_suffix(request)
    url_request = UrlRequest(
        url,
        data=body if method not in {"GET", "HEAD"} else None,
        headers=_proxy_headers(request),
        method=method,
    )
    try:
        with urlopen(url_request, timeout=OPERATOR_PROXY_TIMEOUT_SECONDS) as proxied:
            content = proxied.read()
            headers = _response_headers(proxied.headers)
            return Response(
                content=content,
                status_code=proxied.status,
                headers=headers,
                media_type=proxied.headers.get("content-type"),
            )
    except HTTPError as exc:
        content = exc.read()
        return Response(
            content=content,
            status_code=exc.code,
            headers=_response_headers(exc.headers),
            media_type=exc.headers.get("content-type"),
        )
    except URLError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "data": None,
                "error": {
                    "message": f"operator api unavailable: {exc.reason}",
                    "code": 502,
                },
                "request_id": None,
            },
        )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.api_route(
    "/api/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def operator_api_proxy(path: str, request: Request) -> Response:
    target = _operator_proxy_url(path)
    body = await request.body()
    return _proxy_request(request.method, target, request, body)


@app.api_route("/media/{path:path}", methods=["GET", "HEAD", "OPTIONS"])
async def operator_media_proxy(path: str, request: Request) -> Response:
    return _proxy_request(request.method, _media_proxy_url(path), request, b"")


@app.get("/health")
def health() -> JSONResponse:
    root_exists = settings.evidence_root.is_dir()
    status_code = 200 if root_exists else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if root_exists else "degraded",
            "evidence_root": str(settings.evidence_root),
            "read_only": True,
        },
    )


@app.get("/api/bundles")
def api_bundles(
    event_type: str | None = None,
    event_category: str | None = None,
    source_id: str | None = None,
    camera_id: str | None = None,
    event_id: str | None = None,
    person: str | None = None,
    clip_status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    capped_limit = min(limit, settings.max_bundles)
    camera_name_lookup = _camera_name_lookup()
    result = scan_bundles(
        settings.evidence_root,
        filters={
            "event_type": event_type,
            "event_category": event_category,
            "source_id": source_id,
            "camera_id": camera_id,
            "event_id": event_id,
            "person": None,
            "clip_status": clip_status,
        },
        limit=settings.max_bundles if person else capped_limit,
        offset=0 if person else offset,
        camera_name_lookup=camera_name_lookup,
        materialize_all_matches=bool(person),
    )
    if person:
        matched = [
            bundle
            for bundle in result.get("bundles", [])
            if _bundle_contains_person(
                settings.evidence_root / str(bundle.get("event_id") or ""),
                person,
                source="auto",
            )
        ]
        result["total"] = len(matched)
        result["offset"] = offset
        result["limit"] = capped_limit
        result["bundles"] = matched[offset : offset + capped_limit]
    for bundle in result.get("bundles", []):
        event = bundle.get("event_id")
        if event:
            bundle.update(annotation_source_summary(settings.evidence_root / str(event)))
    return result


@app.get("/api/bundles/{event_id}/annotations")
def api_annotations(
    event_id: str,
    format: Literal["json", "jsonl"] = "json",
    source: AnnotationSource = "auto",
):
    try:
        bundle_dir = ensure_bundle_dir(settings.evidence_root, event_id)
    except Exception as exc:
        raise _http_error(exc) from exc

    try:
        selected = select_annotation_file(bundle_dir, source)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if selected["annotation_source"] == ANNOTATION_SOURCE_UNAVAILABLE:
        visual_summary = visual_evidence_summary(bundle_dir)
        return JSONResponse(
            {
                "event_id": event_id,
                "count": 0,
                "records": [],
                "annotations": [],
                "annotation_source": ANNOTATION_SOURCE_UNAVAILABLE,
                "annotation_source_kind": _annotation_source_kind(ANNOTATION_SOURCE_UNAVAILABLE),
                "requested_source": source,
                "annotation_file": None,
                "fallback_used": False,
                "fallback_reason": None,
                "reason": selected["reason"],
                "legacy_available": selected["legacy_available"],
                "sidecar_available": selected["sidecar_available"],
                "sidecar_summary_available": selected["sidecar_summary_available"],
                "production_ready": False,
                **visual_summary,
                "explicit_legacy_url": f"/api/bundles/{event_id}/annotations?source=legacy"
                if selected["legacy_available"]
                else None,
                "source_hint": "source=legacy" if selected["legacy_available"] else None,
                "preview": False,
                "production_file_overwritten": False,
                "legacy_warning": None,
                "preview_warning": None,
                "warnings": [selected["reason"], "legacy_annotations_debug_only"],
            }
        )

    path = selected["path"]
    if format == "jsonl":
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"{selected['annotation_file']} not found",
            )
        return PlainTextResponse(
            path.read_text(encoding="utf-8"), media_type="application/x-ndjson"
        )

    records, warnings = parse_jsonl_records(path)
    raw_count = len(records)
    records, legacy_known_face_blocked = _filter_displayable_records(records, selected)
    if selected["annotation_source"] == "legacy":
        warnings = warnings + ["legacy_annotations_may_contain_sql_window_track_propagation_artifacts"]
        if legacy_known_face_blocked:
            warnings = warnings + ["legacy_known_face_visual_confirmation_blocked"]
    if selected["annotation_source"] == "sidecar_preview":
        warnings = warnings + ["preview_sidecar_source_not_production_evidence"]
    visual_summary = visual_evidence_summary(bundle_dir)
    return JSONResponse(
        {
            "event_id": event_id,
            "count": len(records),
            "raw_count": raw_count,
            "records": records,
            "annotations": records,
            "annotation_source": selected["annotation_source"],
            "annotation_source_kind": selected["annotation_source_kind"],
            "requested_source": source,
            "annotation_file": selected["annotation_file"],
            "fallback_used": selected["fallback_used"],
            "fallback_reason": selected["fallback_reason"],
            "preview": bool(selected["preview"]),
            "production_ready": selected.get("production_ready"),
            **visual_summary,
            "timeline_domain": selected.get("timeline_domain"),
            "sidecar_type": selected.get("sidecar_type"),
            "canonical_clip": selected.get("canonical_clip"),
            "legacy_fallback_allowed": selected.get("legacy_fallback_allowed"),
            "production_file_overwritten": False,
            "legacy_known_face_blocked": legacy_known_face_blocked,
            "legacy_warning": (
                "Legacy annotations may contain SQL/window/track-propagation artifacts."
                if selected["annotation_source"] == "legacy"
                else None
            ),
            "preview_warning": (
                "Preview sidecar source - not production evidence."
                if selected["annotation_source"] == "sidecar_preview"
                else None
            ),
            "warnings": warnings,
        }
    )


@app.get("/api/bundles/{event_id}/sink-metadata")
def api_sink_metadata(event_id: str) -> JSONResponse:
    try:
        bundle_dir = ensure_bundle_dir(settings.evidence_root, event_id)
    except Exception as exc:
        raise _http_error(exc) from exc
    records, warnings = parse_json_or_jsonl_records(bundle_dir / "sink_metadata.json")
    return JSONResponse(
        {
            "event_id": event_id,
            "count": len(records),
            "records": records,
            "warnings": warnings,
        }
    )


@app.get("/api/bundles/{event_id}/media/raw_clip")
def api_raw_clip(event_id: str) -> FileResponse:
    try:
        bundle_dir = ensure_bundle_dir(settings.evidence_root, event_id)
        manifest = bundle_manifest(settings.evidence_root, event_id)
        raw_clip = discover_raw_clip(bundle_dir, manifest.get("metadata", {}))
    except Exception as exc:
        raise _http_error(exc) from exc
    if raw_clip is None:
        raise HTTPException(status_code=404, detail="raw_clip.* not found")
    return FileResponse(
        raw_clip,
        media_type=media_type_for_path(raw_clip),
        filename=raw_clip.name,
    )


@app.get("/api/bundles/{event_id:path}")
def api_bundle_manifest(event_id: str) -> JSONResponse:
    try:
        manifest = bundle_manifest(
            settings.evidence_root,
            event_id,
            camera_name_lookup=_camera_name_lookup(),
        )
        manifest.update(annotation_source_summary(settings.evidence_root / event_id))
        return JSONResponse(manifest)
    except Exception as exc:
        raise _http_error(exc) from exc


def annotation_source_summary(bundle_dir: Path) -> dict:
    sidecar_path = bundle_dir / SIDECAR_ANNOTATIONS_FILE
    sidecar_preview_path = bundle_dir / SIDECAR_PREVIEW_ANNOTATIONS_FILE
    legacy_path = bundle_dir / LEGACY_ANNOTATIONS_FILE
    sidecar_summary = bundle_dir / SIDECAR_SUMMARY_FILE
    sidecar_preview_summary = bundle_dir / SIDECAR_PREVIEW_SUMMARY_FILE
    legacy_summary = bundle_dir / LEGACY_SUMMARY_FILE
    sidecar_available = sidecar_path.is_file()
    sidecar_preview_available = sidecar_preview_path.is_file()
    legacy_available = legacy_path.is_file()
    watchlist_event = _is_watchlist_bundle(bundle_dir)
    sidecar_ready, sidecar_not_ready_reason = _production_sidecar_ready(bundle_dir)
    default_source = "sidecar" if sidecar_ready else ANNOTATION_SOURCE_UNAVAILABLE
    visual_summary = visual_evidence_summary(bundle_dir)
    return {
        "sidecar_available": sidecar_available,
        "production_sidecar_ready": sidecar_ready,
        "production_sidecar_not_ready_reason": None if sidecar_ready else sidecar_not_ready_reason,
        "sidecar_preview_available": sidecar_preview_available,
        "legacy_available": legacy_available,
        "legacy_debug_only": legacy_available,
        "preview_debug_only": sidecar_preview_available,
        "auto_requires_production_sidecar": True,
        "watchlist_auto_requires_production_sidecar": watchlist_event,
        "default_annotation_source": default_source,
        "default_annotation_source_kind": (
            _annotation_source_kind(default_source) if default_source else None
        ),
        "default_annotation_file": (
            SIDECAR_ANNOTATIONS_FILE
            if default_source == "sidecar"
            else LEGACY_ANNOTATIONS_FILE
            if default_source == "legacy"
            else None
        ),
        "sidecar_summary_path": SIDECAR_SUMMARY_FILE if sidecar_summary.is_file() else None,
        "sidecar_preview_summary_path": SIDECAR_PREVIEW_SUMMARY_FILE if sidecar_preview_summary.is_file() else None,
        "legacy_summary_path": LEGACY_SUMMARY_FILE if legacy_summary.is_file() else None,
        **visual_summary,
    }


def select_annotation_file(bundle_dir: Path, source: AnnotationSource = "auto") -> dict:
    sidecar_path = bundle_dir / SIDECAR_ANNOTATIONS_FILE
    sidecar_preview_path = bundle_dir / SIDECAR_PREVIEW_ANNOTATIONS_FILE
    legacy_path = bundle_dir / LEGACY_ANNOTATIONS_FILE
    sidecar_ready, not_ready_reason = _production_sidecar_ready(bundle_dir)
    if source == "sidecar":
        if not sidecar_path.is_file():
            raise FileNotFoundError(f"{SIDECAR_ANNOTATIONS_FILE} not found")
        return _annotation_selection("sidecar", sidecar_path, fallback_used=False, fallback_reason=None, bundle_dir=bundle_dir)
    if source == "sidecar_preview":
        if not sidecar_preview_path.is_file():
            raise FileNotFoundError(f"{SIDECAR_PREVIEW_ANNOTATIONS_FILE} not found")
        return _annotation_selection("sidecar_preview", sidecar_preview_path, fallback_used=False, fallback_reason=None, preview=True)
    if source == "legacy":
        if not legacy_path.is_file():
            raise FileNotFoundError(f"{LEGACY_ANNOTATIONS_FILE} not found")
        return _annotation_selection("legacy", legacy_path, fallback_used=False, fallback_reason=None, bundle_dir=bundle_dir)
    if sidecar_ready and sidecar_path.is_file():
        return _annotation_selection("sidecar", sidecar_path, fallback_used=False, fallback_reason=None, bundle_dir=bundle_dir)
    return {
        "annotation_source": ANNOTATION_SOURCE_UNAVAILABLE,
        "annotation_source_kind": _annotation_source_kind(ANNOTATION_SOURCE_UNAVAILABLE),
        "annotation_file": None,
        "path": None,
        "fallback_used": False,
        "fallback_reason": None,
        "preview": False,
        "reason": not_ready_reason or "production_sidecar_not_ready",
        "legacy_available": legacy_path.is_file(),
        "sidecar_available": sidecar_path.is_file(),
        "sidecar_summary_available": (bundle_dir / SIDECAR_SUMMARY_FILE).is_file(),
        "sidecar_not_ready_reason": not_ready_reason,
    }


def _production_sidecar_ready(bundle_dir: Path) -> tuple[bool, str]:
    sidecar_path = bundle_dir / SIDECAR_ANNOTATIONS_FILE
    summary_path = bundle_dir / SIDECAR_SUMMARY_FILE
    if not sidecar_path.is_file():
        return False, "production_sidecar_annotations_missing"
    summary, warnings = load_json_object(summary_path)
    if warnings:
        return False, "production_sidecar_summary_missing_or_invalid"
    if summary.get("production_ready") is not True:
        return False, "production_sidecar_not_ready"
    if summary.get("timeline_domain") != PRODUCTION_TIMELINE_DOMAIN:
        return False, "production_sidecar_wrong_timeline_domain"
    return True, ""


def _is_watchlist_bundle(bundle_dir: Path) -> bool:
    metadata, _metadata_warnings = load_json_object(bundle_dir / "metadata.json")
    summary, _summary_warnings = load_json_object(bundle_dir / "summary.json")
    sidecar_summary, _sidecar_warnings = load_json_object(bundle_dir / SIDECAR_SUMMARY_FILE)
    event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
    for value in (
        event.get("event_type"),
        metadata.get("event_type"),
        summary.get("event_type"),
        sidecar_summary.get("event_type"),
    ):
        if str(value or "") == WATCHLIST_EVENT_TYPE:
            return True
    return False


def visual_evidence_summary(bundle_dir: Path) -> dict:
    metadata, _metadata_warnings = load_json_object(bundle_dir / "metadata.json")
    legacy_summary, _legacy_warnings = load_json_object(bundle_dir / LEGACY_SUMMARY_FILE)
    sidecar_summary, _sidecar_warnings = load_json_object(bundle_dir / SIDECAR_SUMMARY_FILE)
    event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
    event_status = _first_text(
        event.get("event_type"),
        metadata.get("event_type"),
        sidecar_summary.get("event_type"),
        legacy_summary.get("event_type"),
    )
    sidecar_file_available = (bundle_dir / SIDECAR_ANNOTATIONS_FILE).is_file()
    source_observation_id = _first_text(
        sidecar_summary.get("source_observation_id"),
        _dict(sidecar_summary.get("event_anchor")).get("source_observation_id"),
        event.get("source_observation_id"),
    )
    sidecar_ready = sidecar_summary.get("production_ready") is True
    sidecar_status = str(sidecar_summary.get("annotation_status") or "")
    visual_status = str(sidecar_summary.get("visual_binding_status") or "")
    if not visual_status:
        visual_status = "verified" if sidecar_ready else "unverified"
    reason = str(sidecar_summary.get("visual_binding_reason") or "")
    if not reason and sidecar_status == "cache_stale_or_epoch_mismatch":
        reason = "cache_stale_or_epoch_mismatch"
    if not reason and not sidecar_file_available:
        reason = "production_sidecar_annotations_missing"
    if not reason and not sidecar_ready:
        reason = "production_sidecar_not_ready"
    if not reason:
        reason = "production_sidecar_trigger_bound"
    frame_identity_method = _first_text(
        sidecar_summary.get("frame_identity_method"),
        "frame_uuid" if sidecar_summary.get("rows_matched_by_frame_uuid") else None,
        "frame_pts_fallback" if sidecar_summary.get("rows_matched_by_pts_fallback") else None,
    )
    frame_identity_confidence = _first_text(
        sidecar_summary.get("frame_identity_confidence"),
        "high" if visual_status == "verified" and frame_identity_method == "frame_uuid" else None,
        "medium" if visual_status == "verified" and frame_identity_method else None,
        "none",
    )
    return {
        "event_status": event_status,
        "visual_evidence_status": "verified" if visual_status == "verified" else "unverified",
        "reason": reason,
        "visual_binding_status": visual_status,
        "visual_binding_reason": reason,
        "source_observation_id": source_observation_id,
        "frame_identity_method": frame_identity_method,
        "frame_identity_confidence": frame_identity_confidence,
        "trigger_face_row_exists": bool(sidecar_summary.get("trigger_face_row_exists")),
        "trigger_face_row_passed_freshness_guard": bool(
            sidecar_summary.get("trigger_face_row_passed_freshness_guard")
        ),
        "legacy_used_for_visual_binding": False,
    }


def _filter_displayable_records(records: list[dict[str, Any]], selected: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    if selected.get("annotation_source") == "legacy" and selected.get("event_status") == WATCHLIST_EVENT_TYPE:
        filtered = [_strip_legacy_known_face_visual_confirmation(record) for record in records]
        blocked = sum(1 for record in filtered if record.get("legacy_known_face_blocked") is True)
        return filtered, blocked
    if selected.get("annotation_source") not in {"sidecar", "sidecar_preview"}:
        return records, 0
    return [record for record in records if record.get("displayable") is not False], 0


def _bundle_contains_person(
    bundle_dir: Path,
    person: str,
    *,
    source: AnnotationSource = "auto",
) -> bool:
    needle = str(person or "").strip().lower()
    if not needle:
        return True
    try:
        metadata, _warnings = parse_json_or_jsonl_records(bundle_dir / "metadata.json")
    except Exception:
        metadata = []
    if metadata and needle in str(metadata).lower():
        return True
    try:
        selected = select_annotation_file(bundle_dir, source)
    except FileNotFoundError:
        return False
    if selected.get("annotation_source") == ANNOTATION_SOURCE_UNAVAILABLE:
        return False
    try:
        if selected.get("annotation_source") in {"sidecar", "sidecar_preview"}:
            records, _warnings = parse_jsonl_records(selected["path"])
            records, _blocked = _filter_displayable_records(records, selected)
            haystack = str(records).lower()
        else:
            haystack = selected["path"].read_text(encoding="utf-8").lower()
        if needle in haystack:
            return True
    except OSError:
        return False
    return False


def _annotation_selection(
    annotation_source: Literal["sidecar", "sidecar_preview", "legacy"],
    path: Path,
    *,
    fallback_used: bool,
    fallback_reason: str | None,
    preview: bool = False,
    bundle_dir: Path | None = None,
) -> dict:
    summary = _selection_sidecar_summary(bundle_dir, annotation_source)
    return {
        "annotation_source": annotation_source,
        "annotation_source_kind": _annotation_source_kind(annotation_source),
        "annotation_file": path.name,
        "path": path,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "preview": preview,
        **summary,
    }


def _strip_legacy_known_face_visual_confirmation(record: dict[str, Any]) -> dict[str, Any]:
    output = {**record}
    blocked = False
    if _is_known_face_annotation(output):
        _mark_legacy_face_unverified(output)
        blocked = True
    objects = output.get("objects")
    if isinstance(objects, list):
        clean_objects = []
        for obj in objects:
            if isinstance(obj, dict) and _is_known_face_annotation(obj):
                clean = {**obj}
                _mark_legacy_face_unverified(clean)
                clean_objects.append(clean)
                blocked = True
            else:
                clean_objects.append(obj)
        output["objects"] = clean_objects
    if blocked:
        output["legacy_known_face_blocked"] = True
        output["visual_binding_status"] = "unverified"
        output["visual_binding_reason"] = "legacy_debug_not_allowed_for_watchlist_trigger"
    return output


def _is_known_face_annotation(item: dict[str, Any]) -> bool:
    if item.get("object_type") != "face":
        return False
    if item.get("annotation_role") == "watchlist_trigger_face":
        return True
    label = item.get("label") if isinstance(item.get("label"), dict) else {}
    identity = item.get("identity") if isinstance(item.get("identity"), dict) else {}
    return bool(
        label.get("kind") == "known_face"
        or identity.get("status") == "matched"
        or identity.get("match_status") == "above_threshold"
        or label.get("display_name")
        or identity.get("display_name")
        or label.get("external_person_id")
        or identity.get("external_person_id")
    )


def _mark_legacy_face_unverified(item: dict[str, Any]) -> None:
    item.pop("annotation_role", None)
    label = item.get("label") if isinstance(item.get("label"), dict) else {}
    item["label"] = {"kind": "unknown_face"}
    identity = item.get("identity") if isinstance(item.get("identity"), dict) else {}
    threshold = identity.get("threshold") or label.get("threshold")
    item["identity"] = {
        "status": "unknown",
        "match_status": "not_searched",
        "person_id": None,
        "external_person_id": None,
        "display_name": "",
        "similarity": None,
        "rank": None,
        "threshold": threshold,
        "identity_skip_reason": "legacy_debug_not_allowed_for_watchlist_trigger",
    }
    item["identity_skip_reason"] = "legacy_debug_not_allowed_for_watchlist_trigger"
    style = item.get("style") if isinstance(item.get("style"), dict) else {}
    item["style"] = {
        **style,
        "bbox_color": "#9E9E9E",
        "label_color": "#9E9E9E",
        "reason": "legacy_debug_unverified_face",
        "label": "Unverified face",
        "priority": 10,
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value in (None, ""):
            continue
        text = str(value)
        if text.strip():
            return text
    return None


def _annotation_source_kind(annotation_source: str) -> AnnotationSourceKind:
    if annotation_source == "sidecar":
        return "production_sidecar"
    if annotation_source == "legacy":
        return "legacy_debug"
    if annotation_source == "sidecar_preview":
        return "preview_debug"
    return "unavailable"


def _selection_sidecar_summary(bundle_dir: Path | None, annotation_source: str) -> dict:
    if bundle_dir is None:
        return {
            "production_ready": None,
            "timeline_domain": None,
            "sidecar_type": None,
            "canonical_clip": None,
            "legacy_fallback_allowed": None,
            "event_status": None,
        }
    visual = visual_evidence_summary(bundle_dir)
    if annotation_source != "sidecar":
        return {
            "production_ready": None,
            "timeline_domain": None,
            "sidecar_type": None,
            "canonical_clip": None,
            "legacy_fallback_allowed": None,
            "event_status": visual.get("event_status"),
        }
    summary, _warnings = load_json_object(bundle_dir / SIDECAR_SUMMARY_FILE)
    return {
        "production_ready": summary.get("production_ready"),
        "timeline_domain": summary.get("timeline_domain"),
        "sidecar_type": summary.get("sidecar_type"),
        "canonical_clip": summary.get("canonical_clip"),
        "legacy_fallback_allowed": summary.get("legacy_fallback_allowed"),
        "event_status": visual.get("event_status"),
    }
