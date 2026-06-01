"""FastAPI service for browsing file-based evidence bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import load_settings
from app.evidence_index import (
    EvidencePathError,
    bundle_manifest,
    discover_raw_clip,
    ensure_bundle_dir,
    media_type_for_path,
    parse_json_or_jsonl_records,
    parse_jsonl_records,
    scan_bundles,
)


settings = load_settings()
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Evidence Viewer", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, EvidencePathError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


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
    source_id: str | None = None,
    camera_id: str | None = None,
    event_id: str | None = None,
    person: str | None = None,
    clip_status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    capped_limit = min(limit, settings.max_bundles)
    return scan_bundles(
        settings.evidence_root,
        filters={
            "event_type": event_type,
            "source_id": source_id,
            "camera_id": camera_id,
            "event_id": event_id,
            "person": person,
            "clip_status": clip_status,
        },
        limit=capped_limit,
        offset=offset,
    )


@app.get("/api/bundles/{event_id}/annotations")
def api_annotations(
    event_id: str,
    format: Literal["json", "jsonl"] = "json",
):
    try:
        bundle_dir = ensure_bundle_dir(settings.evidence_root, event_id)
    except Exception as exc:
        raise _http_error(exc) from exc

    path = bundle_dir / "annotations.jsonl"
    if format == "jsonl":
        if not path.is_file():
            raise HTTPException(status_code=404, detail="annotations.jsonl not found")
        return PlainTextResponse(
            path.read_text(encoding="utf-8"), media_type="application/x-ndjson"
        )

    records, warnings = parse_jsonl_records(path)
    return JSONResponse(
        {
            "event_id": event_id,
            "count": len(records),
            "records": records,
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
        return JSONResponse(bundle_manifest(settings.evidence_root, event_id))
    except Exception as exc:
        raise _http_error(exc) from exc
