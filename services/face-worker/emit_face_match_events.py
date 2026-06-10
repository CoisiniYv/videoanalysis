#!/usr/bin/env python3
"""Emit midterm face match SecurityEvents from stored face_observations."""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from redis import Redis

from app.face_match_event_service import (
    DEFAULT_FACE_MATCH_THRESHOLD,
    EVENT_STREAM_DEFAULT,
    emit_watchlist_hits_for_source,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Search stored face_observations against active gallery embeddings "
            "and emit watchlist_hit SecurityEvents."
        )
    )
    parser.add_argument("--source-id", required=True)
    parser.add_argument(
        "--event-type",
        default="watchlist_hit",
        choices=["watchlist_hit"],
        help="Midterm emits watchlist_hit; live_search_hit is contract-only.",
    )
    parser.add_argument(
        "--external-person-id",
        action="append",
        default=None,
        help="Restrict matches to one external person id. Repeatable.",
    )
    parser.add_argument(
        "--all-active-gallery",
        action="store_true",
        default=False,
        help="Search all active gallery embeddings.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=float(os.getenv("FACE_MATCH_THRESHOLD", DEFAULT_FACE_MATCH_THRESHOLD)),
        help="Similarity threshold. Default FACE_MATCH_THRESHOLD or 0.50.",
    )
    parser.add_argument("--top-k", type=int, default=1)
    parser.add_argument("--observation-limit", type=int, default=100)
    parser.add_argument("--event-stream", default=os.getenv("EVENT_STREAM", EVENT_STREAM_DEFAULT))
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://redis:6379/0"))
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--output-json", action="store_true", default=False)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.database_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        sys.exit(2)
    if not args.all_active_gallery and not args.external_person_id:
        print(
            "ERROR: provide --all-active-gallery or --external-person-id",
            file=sys.stderr,
        )
        sys.exit(2)

    conn = psycopg.connect(args.database_url, autocommit=True)
    redis_client = None if args.dry_run else Redis.from_url(args.redis_url)
    try:
        result = emit_watchlist_hits_for_source(
            conn=conn,
            redis_client=redis_client,
            source_id=args.source_id,
            threshold=args.threshold,
            top_k=args.top_k,
            observation_limit=args.observation_limit,
            external_person_ids=None if args.all_active_gallery else args.external_person_id,
            dry_run=args.dry_run,
            event_stream=args.event_stream,
        )
    finally:
        conn.close()
        if redis_client is not None:
            redis_client.close()

    if args.output_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print("=== Midterm Face Match Event Emitter ===")
    print(f"source_id: {result['source_id']}")
    print(f"event_type: {result['event_type']}")
    print(f"algorithm_type: {result['algorithm_type']}")
    print(f"threshold: {result['threshold']}")
    print(f"dry_run: {result['dry_run']}")
    print(f"observations_checked: {result['observations_checked']}")
    print(f"events_emitted: {result['events_emitted']}")
    print("top_candidates:")
    for item in result["top_candidates"][:10]:
        print(
            "  "
            f"source_observation_id={item['source_observation_id']} "
            f"person_id={item['person_id']} "
            f"external_person_id={item.get('external_person_id')} "
            f"name={item['person_name']} "
            f"similarity={item['similarity']:.6f} "
            f"event_created={item['event_created']}"
        )


if __name__ == "__main__":
    main()
