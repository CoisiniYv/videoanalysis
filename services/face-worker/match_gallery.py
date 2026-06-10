#!/usr/bin/env python3
"""Match a face_observation against the person gallery.

Usage:
    # Basic: match an observation against all gallery embeddings
    python match_gallery.py --observation-id face:cam1:42:1000

    # With custom search parameters
    python match_gallery.py --observation-id face:cam1:42:1000 \
        --top-k 5 --min-similarity 0.6

    # Restrict to specific persons
    python match_gallery.py --observation-id face:cam1:42:1000 \
        --person-ids 1,2,3

    # Idempotent rerun with explicit search_request_id
    python match_gallery.py --observation-id face:cam1:42:1000 \
        --search-request-id <uuid>

Requires DATABASE_URL environment variable.

Semantics (midterm):
    - query side: face_observation identified by --observation-id
    - target side: person_gallery_embeddings (gallery entries)
    - match_results.query_observation_id = query observation UUID
    - match_results.query_source_observation_id = query source_observation_id
    - match_results.query_gallery_embedding_id = matched gallery embedding id
    - match_results.matched_observation_id = NULL (no historical observation target)
    - idempotency: UNIQUE(search_request_id, query_gallery_embedding_id)
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from app.match_repository import MatchResultRepository
from app.vector_store import FaceVectorStore


def _fetch_observation(
    conn: psycopg.Connection, source_observation_id: str,
) -> dict | None:
    """Fetch a face_observation by source_observation_id."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, source_observation_id, camera_id, source_id,
                   track_id, timestamp_ms, face_confidence, quality,
                   embedding
            FROM face_observations
            WHERE source_observation_id = %(sid)s
            """,
            {"sid": source_observation_id},
        )
        return cur.fetchone()


def match(
    *,
    observation_id: str,
    search_request_id: str | None,
    top_k: int,
    min_similarity: float | None,
    search_mode: str,
    person_ids: list[int] | None,
    expires_in_seconds: int,
    dry_run: bool,
) -> str:
    """Main match logic.

    Returns the search_request_id used.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=True)
    register_vector(conn)

    try:
        # 1. Fetch the query observation
        obs = _fetch_observation(conn, observation_id)
        if obs is None:
            print(
                f"ERROR: face_observation not found: {observation_id}",
                file=sys.stderr,
            )
            sys.exit(1)

        embedding = obs["embedding"]
        if embedding is None:
            print(
                f"ERROR: observation {observation_id} has NULL embedding",
                file=sys.stderr,
            )
            sys.exit(1)

        if hasattr(embedding, "tolist"):
            embedding_list = [float(x) for x in embedding.tolist()]
        else:
            embedding_list = [float(x) for x in embedding]

        print(f"Query observation: {observation_id}")
        print(f"  observation UUID: {obs['id']}")
        print(f"  camera_id={obs['camera_id']} track_id={obs['track_id']}")
        print(
            f"  quality={obs['quality']:.3f} "
            f"confidence={obs['face_confidence']:.3f}"
        )

        # 2. Search gallery
        store = FaceVectorStore(conn)
        results = store.search_gallery(
            embedding_list,
            top_k=top_k,
            min_similarity=min_similarity,
            person_ids=person_ids,
        )

        print(f"\nGallery search: {len(results)} results (top_k={top_k})")
        if min_similarity is not None:
            print(f"  min_similarity={min_similarity}")
        if person_ids:
            print(f"  person_ids={person_ids}")

        if not results:
            print("\nNo matches found.")
            return search_request_id or str(uuid.uuid4())

        # 3. Generate or use provided search_request_id
        req_id = search_request_id or str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in_seconds,
        )

        # 4. Write results to match_results
        repo = MatchResultRepository(conn)
        inserted = 0
        duplicates = 0

        for rank_idx, result in enumerate(results):
            row = {
                "search_request_id": req_id,
                "search_mode": search_mode,
                # Query side: the input face_observation
                "query_observation_id": obs["id"],
                "query_source_observation_id": observation_id,
                # Gallery target side
                "query_person_id": result.get("person_id"),
                "query_gallery_embedding_id": result["id"],
                "query_embedding_model": result.get(
                    "embedding_model", "adaface"
                ),
                "similarity_threshold": min_similarity,
                # matched_observation_id is NULL for gallery_match
                # (set internally by repository)
                "rank": rank_idx + 1,
                "similarity": result["similarity"],
                "face_confidence": obs["face_confidence"],
                "quality": obs["quality"],
                "expires_at": expires_at,
            }

            if dry_run:
                inserted += 1
                continue

            row_id = repo.insert_gallery_match_result(row)
            if row_id is not None:
                inserted += 1
            else:
                duplicates += 1

        # 5. Print summary
        prefix = "[DRY RUN] " if dry_run else ""
        print(f"\n{prefix}search_request_id: {req_id}")
        print(f"{prefix}Inserted: {inserted}, Duplicates: {duplicates}")
        print(f"Expires at: {expires_at.isoformat()}")

        print("\nMatch results:")
        for rank_idx, result in enumerate(results):
            print(
                f"  #{rank_idx + 1}  "
                f"person_id={result['person_id']}  "
                f"person_name={result.get('person_name', '?')}  "
                f"gallery_embedding_id={result['id']}  "
                f"similarity={result['similarity']:.4f}  "
                f"source_type={result.get('source_type', '?')}  "
                f"is_primary={result.get('is_primary', False)}"
            )

        return req_id

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Match a face_observation against the person gallery.",
    )
    parser.add_argument(
        "--observation-id",
        required=True,
        help="source_observation_id from face_observations table",
    )
    parser.add_argument(
        "--search-request-id",
        default=None,
        help="Explicit search_request_id (UUID). Auto-generated if omitted.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Maximum number of gallery matches to return (default: 10)",
    )
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=None,
        help="Minimum cosine similarity threshold [0.0, 1.0]",
    )
    parser.add_argument(
        "--search-mode",
        default="gallery_match",
        choices=["gallery_match"],
        help="search_mode label stored in match_results (default: gallery_match)",
    )
    parser.add_argument(
        "--person-ids",
        default=None,
        help="Comma-separated list of person_ids to restrict search",
    )
    parser.add_argument(
        "--expires-in-seconds",
        type=int,
        default=3600,
        help="TTL for match_results rows (default: 3600 = 1 hour)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Search but do not write to match_results",
    )
    args = parser.parse_args()

    person_ids = None
    if args.person_ids:
        person_ids = [int(x.strip()) for x in args.person_ids.split(",")]

    match(
        observation_id=args.observation_id,
        search_request_id=args.search_request_id,
        top_k=args.top_k,
        min_similarity=args.min_similarity,
        search_mode=args.search_mode,
        person_ids=person_ids,
        expires_in_seconds=args.expires_in_seconds,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
