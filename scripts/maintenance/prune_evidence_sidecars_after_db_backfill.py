#!/usr/bin/env python3
"""Prune DB-backed evidence sidecars after successful backfill.

Only successful bundles with raw_clip.mov and DB overlay/timeline rows are
eligible. Video files are never deleted.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


SIDECARS_TO_DELETE = (
    "annotations.frame_cache.identity.jsonl",
    "sink_metadata.json",
    "summary.json",
    "summary.frame_cache.identity.json",
    "metadata.json",
    "video_crop_ffmpeg.log",
)


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
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    evidence_root = Path(args.evidence_root).resolve(strict=False)
    if not evidence_root.is_dir():
        raise SystemExit(f"evidence root not found: {evidence_root}")

    selected = _eligible_bundles(
        database_url=args.database_url,
        source_id=args.source_id,
        limit=args.limit,
    )
    deleted_files = 0
    skipped_missing_raw_clip = 0
    skipped_unsafe_path = 0
    bundles_pruned = 0
    errors: list[str] = []

    for row in selected:
        event_id = str(row["event_id"])
        bundle_dir = (evidence_root / event_id).resolve(strict=False)
        try:
            bundle_dir.relative_to(evidence_root)
        except ValueError:
            skipped_unsafe_path += 1
            continue
        raw_clip = bundle_dir / "raw_clip.mov"
        if not raw_clip.is_file() or raw_clip.stat().st_size <= 0:
            skipped_missing_raw_clip += 1
            continue
        bundle_deleted = 0
        for name in SIDECARS_TO_DELETE:
            path = bundle_dir / name
            if not path.is_file():
                continue
            if args.dry_run:
                bundle_deleted += 1
                continue
            try:
                path.unlink()
                bundle_deleted += 1
            except OSError as exc:
                errors.append(f"{event_id}/{name}:{exc}")
        if bundle_deleted:
            bundles_pruned += 1
            deleted_files += bundle_deleted

    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "evidence_root": str(evidence_root),
                "source_id": args.source_id,
                "eligible": len(selected),
                "bundles_pruned": bundles_pruned,
                "deleted_files": deleted_files,
                "skipped_missing_raw_clip": skipped_missing_raw_clip,
                "skipped_unsafe_path": skipped_unsafe_path,
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if errors else 0


def _eligible_bundles(
    *,
    database_url: str,
    source_id: str,
    limit: int,
) -> list[dict]:
    where = [
        "COALESCE(eb.media_status, '') = 'materialized'",
        "COALESCE(eb.raw_clip_uri, '') <> ''",
        "EXISTS (SELECT 1 FROM evidence_overlay_segments eos WHERE eos.event_id = eb.event_id)",
        "EXISTS (SELECT 1 FROM evidence_frame_timeline eft WHERE eft.event_id = eb.event_id)",
    ]
    params: dict[str, object] = {}
    if source_id:
        where.append("eb.source_id = %(source_id)s")
        params["source_id"] = source_id
    limit_sql = ""
    if limit:
        limit_sql = "LIMIT %(limit)s"
        params["limit"] = limit
    query = f"""
        SELECT eb.event_id
        FROM evidence_bundles eb
        WHERE {" AND ".join(f"({clause})" for clause in where)}
        ORDER BY eb.event_created_at DESC NULLS LAST, eb.event_id
        {limit_sql}
    """
    with psycopg.connect(database_url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return list(cur.fetchall())


if __name__ == "__main__":
    raise SystemExit(main())
