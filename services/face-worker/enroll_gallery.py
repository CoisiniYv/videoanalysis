#!/usr/bin/env python3
"""Enroll a gallery embedding from an existing face_observation.

Usage:
    # Create new person by display name
    python enroll_gallery.py --observation-id face:cam1:42:1000 --person-name "John Doe"

    # Add to existing person by id
    python enroll_gallery.py --observation-id face:cam1:42:1000 --person-id 5 --set-primary

    # Enroll by external_person_id (reuses existing or creates new)
    python enroll_gallery.py --observation-id face:cam1:42:1000 --external-person-id "badge-1234"

Exactly one of --person-name, --person-id, or --external-person-id is required.
Requires DATABASE_URL environment variable.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from app.gallery_repository import GalleryRepository
from app.person_repository import PersonRepository


def _fetch_observation_embedding(
    conn: psycopg.Connection, source_observation_id: str,
) -> dict | None:
    """Fetch a face_observation's embedding and metadata by source_observation_id."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, source_observation_id, camera_id, track_id,
                   timestamp_ms, face_bbox, landmarks,
                   face_confidence, quality, embedding
            FROM face_observations
            WHERE source_observation_id = %(sid)s
            """,
            {"sid": source_observation_id},
        )
        return cur.fetchone()


def enroll(
    *,
    observation_id: str,
    person_name: str | None,
    person_id: int | None,
    external_person_id: str | None,
    set_primary: bool,
    source_type: str,
    created_by: str | None,
) -> None:
    """Main enrollment logic."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=True)
    register_vector(conn)

    try:
        # 1. Fetch the observation
        obs = _fetch_observation_embedding(conn, observation_id)
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

        # Convert pgvector to plain list of Python floats
        if hasattr(embedding, "tolist"):
            embedding_list = [float(x) for x in embedding.tolist()]
        else:
            embedding_list = [float(x) for x in embedding]

        print(f"Observation: {observation_id}")
        print(f"  camera_id={obs['camera_id']} track_id={obs['track_id']}")
        print(f"  quality={obs['quality']:.3f} confidence={obs['face_confidence']:.3f}")

        # 2. Resolve or create person — exactly one selector is guaranteed by argparse
        person_repo = PersonRepository(conn)

        if person_id is not None:
            # --person-id: enroll into existing person
            person = person_repo.get_by_id(person_id)
            if person is None:
                print(f"ERROR: person_id={person_id} not found", file=sys.stderr)
                sys.exit(1)
            print(f"Person: id={person['id']} name={person['name']}")
        elif external_person_id is not None:
            # --external-person-id: reuse existing or create new
            person = person_repo.get_by_external_person_id(external_person_id)
            if person is not None:
                person_id = int(person["id"])
                print(f"Reusing existing person: id={person_id} name={person['name']}")
            else:
                person_id = person_repo.create_person(
                    name=external_person_id,
                    external_person_id=external_person_id,
                    created_by=created_by,
                    updated_by=created_by,
                )
                print(f"Created person: id={person_id} external_person_id={external_person_id}")
        else:
            # --person-name: create a new registered person
            person_id = person_repo.create_person(
                name=person_name,
                created_by=created_by,
                updated_by=created_by,
            )
            print(f"Created person: id={person_id} name={person_name}")

        # 3. Enroll gallery embedding
        gallery_repo = GalleryRepository(conn)
        gallery_id = gallery_repo.add_embedding(
            person_id=person_id,
            embedding=embedding_list,
            source_type=source_type,
            source_observation_id=observation_id,
            face_bbox=obs["face_bbox"],
            landmarks=obs["landmarks"],
            quality=obs["quality"],
            is_primary=set_primary,
        )

        print(f"Enrolled gallery embedding: id={gallery_id}")
        print(f"  person_id={person_id} is_primary={set_primary}")
        print(f"  source_observation_id={observation_id}")

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enroll a gallery embedding from a face_observation.",
    )
    parser.add_argument(
        "--observation-id",
        required=True,
        help="source_observation_id from face_observations table",
    )

    # Exactly one person selector is required.
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--person-name",
        help="Display name for a new registered person",
    )
    selector.add_argument(
        "--person-id",
        type=int,
        help="Existing person_id to enroll into",
    )
    selector.add_argument(
        "--external-person-id",
        help=(
            "External person identifier. Reuses existing person if found; "
            "otherwise creates a new person using this value as the display name."
        ),
    )

    parser.add_argument(
        "--set-primary",
        action="store_true",
        default=False,
        help="Mark this gallery embedding as the primary for the person",
    )
    parser.add_argument(
        "--source-type",
        default="snapshot_extract",
        help="source_type for the gallery embedding (default: snapshot_extract)",
    )
    parser.add_argument(
        "--created-by",
        default=None,
        help="created_by / updated_by value",
    )
    args = parser.parse_args()

    enroll(
        observation_id=args.observation_id,
        person_name=args.person_name,
        person_id=args.person_id,
        external_person_id=args.external_person_id,
        set_primary=args.set_primary,
        source_type=args.source_type,
        created_by=args.created_by,
    )


if __name__ == "__main__":
    main()
