#!/usr/bin/env python3
"""Backfill evidence bundle metadata into PostgreSQL index tables.

This script never stores video bytes in PostgreSQL and never deletes bundle
files. It reads existing bundle sidecars and writes searchable DB summaries.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_DIR = ROOT / "services" / "media-worker"
if str(MEDIA_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(MEDIA_WORKER_DIR))

from app.evidence_db_index import upsert_evidence_bundle_index  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", "postgresql://video:video@localhost:5432/video_analytics"),
    )
    parser.add_argument(
        "--evidence-root",
        default=os.getenv("EVIDENCE_ROOT", "/data/video-analytics/media/evidence"),
    )
    parser.add_argument("--source-id", default="")
    parser.add_argument("--camera-id", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--compute-sha256", action="store_true")
    parser.add_argument("--include-timeline", action="store_true", default=True)
    parser.add_argument("--include-overlays", action="store_true", default=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    evidence_root = Path(args.evidence_root)
    if not evidence_root.is_dir():
        raise SystemExit(f"evidence root not found: {evidence_root}")

    conn = psycopg.connect(args.database_url, autocommit=True)
    selected: list[Path] = []
    skipped = 0
    for bundle in sorted(evidence_root.iterdir(), key=lambda path: path.name):
        if not bundle.is_dir() or bundle.name == "events":
            continue
        metadata = _load_json(bundle / "metadata.json")
        event = metadata.get("event") if isinstance(metadata.get("event"), dict) else {}
        source_id = str(metadata.get("source_id") or event.get("source_id") or "")
        camera_id = str(metadata.get("camera_id") or event.get("camera_id") or "")
        if args.source_id and source_id != args.source_id:
            skipped += 1
            continue
        if args.camera_id and camera_id != args.camera_id:
            skipped += 1
            continue
        selected.append(bundle)
        if args.limit and len(selected) >= args.limit:
            break

    results: list[dict] = []
    if not args.dry_run:
        for bundle in selected:
            results.append(
                upsert_evidence_bundle_index(
                    conn,
                    event_id=bundle.name,
                    bundle_dir=bundle,
                    compute_sha256=args.compute_sha256,
                    include_timeline=args.include_timeline,
                    include_overlays=args.include_overlays,
                )
            )

    summary = {
        "dry_run": args.dry_run,
        "evidence_root": str(evidence_root),
        "source_id": args.source_id,
        "camera_id": args.camera_id,
        "selected": len(selected),
        "skipped_by_filter": skipped,
        "indexed": len(results),
        "artifacts": sum(int(row.get("artifacts") or 0) for row in results),
        "timeline_rows": sum(int(row.get("timeline_rows") or 0) for row in results),
        "overlay_rows": sum(int(row.get("overlay_rows") or 0) for row in results),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
