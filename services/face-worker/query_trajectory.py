#!/usr/bin/env python3
"""Query trajectory (appearance history) for a registered person.

Usage:
    # Query by person_id
    python query_trajectory.py --person-id 42

    # Query by external_person_id
    python query_trajectory.py --external-person-id EMP-00123

    # With time range (ISO 8601, timezone-aware)
    python query_trajectory.py --person-id 42 \
        --time-from 2026-05-01T00:00:00+08:00 \
        --time-to 2026-05-28T23:59:59+08:00

    # Filter by camera and similarity
    python query_trajectory.py --person-id 42 \
        --camera-id cam-lobby --min-similarity 0.7

    # JSON output
    python query_trajectory.py --person-id 42 --json

Requires DATABASE_URL environment variable.

Semantics (midterm):
    - Read-only: queries match_results with
      search_mode = 'registered_person_history'.
    - Does NOT write to any table.
    - Joins persons and face_observations for enriched output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import psycopg
from pgvector.psycopg import register_vector

from app.person_repository import PersonRepository
from app.trajectory_repository import TrajectoryRepository


def _parse_iso8601(value: str, label: str) -> int:
    """Parse an ISO 8601 timezone-aware datetime string to epoch milliseconds.

    Raises SystemExit on naive datetimes or parse errors.
    """
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        print(
            f"ERROR: {label} is not valid ISO 8601: {value}",
            file=sys.stderr,
        )
        sys.exit(1)

    if dt.tzinfo is None:
        print(
            f"ERROR: {label} must be timezone-aware (e.g. +08:00): {value}",
            file=sys.stderr,
        )
        sys.exit(1)

    return int(dt.timestamp() * 1000)


def _format_table(rows: list[dict]) -> str:
    """Format trajectory rows as a human-readable table."""
    if not rows:
        return "(no results)"

    headers = [
        "rank",
        "timestamp",
        "camera_id",
        "track_id",
        "similarity",
        "person_name",
        "external_id",
        "snapshot_path",
    ]

    # Pre-compute column widths
    col_widths = {h: len(h) for h in headers}
    formatted_rows = []
    for row in rows:
        ts_ms = row.get("matched_timestamp_ms")
        if ts_ms is not None:
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S %Z")
        else:
            ts_str = "?"

        values = {
            "rank": str(row.get("rank", "?")),
            "timestamp": ts_str,
            "camera_id": str(row.get("matched_camera_id", "?")),
            "track_id": str(row.get("matched_track_id", "?")),
            "similarity": (
                f"{row['similarity']:.4f}" if row.get("similarity") is not None else "?"
            ),
            "person_name": str(row.get("person_name", "?")),
            "external_id": str(row.get("external_person_id", "") or ""),
            "snapshot_path": str(row.get("snapshot_path", "") or ""),
        }
        for h in headers:
            col_widths[h] = max(col_widths[h], len(values[h]))
        formatted_rows.append(values)

    # Build table
    sep = "+-" + "-+-".join("-" * col_widths[h] for h in headers) + "-+"
    header_line = "| " + " | ".join(h.ljust(col_widths[h]) for h in headers) + " |"

    lines = [sep, header_line, sep]
    for values in formatted_rows:
        line = "| " + " | ".join(values[h].ljust(col_widths[h]) for h in headers) + " |"
        lines.append(line)
    lines.append(sep)
    lines.append(f"({len(formatted_rows)} rows)")

    return "\n".join(lines)


def query(
    *,
    person_id: int | None,
    external_person_id: str | None,
    time_from_ms: int | None,
    time_to_ms: int | None,
    camera_id: str | None,
    min_similarity: float | None,
    limit: int,
    output_json: bool,
) -> list[dict]:
    """Main trajectory query logic.

    Returns the list of trajectory rows.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=True)
    register_vector(conn)

    try:
        # Resolve person_id from external_person_id if needed
        resolved_person_id = person_id
        if external_person_id is not None:
            person_repo = PersonRepository(conn)
            person = person_repo.get_by_external_person_id(external_person_id)
            if person is None:
                print(
                    f"ERROR: person not found with external_person_id={external_person_id}",
                    file=sys.stderr,
                )
                sys.exit(1)
            resolved_person_id = int(person["id"])
            if not output_json:
                print(
                    f"Resolved external_person_id={external_person_id} "
                    f"-> person_id={resolved_person_id}"
                )

        assert resolved_person_id is not None

        repo = TrajectoryRepository(conn)
        rows = repo.get_person_trajectory(
            resolved_person_id,
            time_from_ms=time_from_ms,
            time_to_ms=time_to_ms,
            camera_id=camera_id,
            min_similarity=min_similarity,
            limit=limit,
        )

        if output_json:
            # Convert non-serializable types
            serializable = []
            for row in rows:
                out = {}
                for k, v in row.items():
                    if hasattr(v, "hex") and hasattr(v, "version"):
                        # UUID objects
                        out[k] = str(v)
                    elif hasattr(v, "isoformat"):
                        out[k] = v.isoformat()
                    elif hasattr(v, "tolist"):
                        out[k] = v.tolist()
                    else:
                        out[k] = v
                serializable.append(out)
            print(json.dumps(serializable, indent=2, ensure_ascii=False))
        else:
            print(f"Trajectory for person_id={resolved_person_id}")
            if camera_id:
                print(f"  camera_id={camera_id}")
            if min_similarity is not None:
                print(f"  min_similarity={min_similarity}")
            print()
            print(_format_table(rows))

        return rows

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query trajectory (appearance history) for a registered person.",
    )

    # Person identification — mutually exclusive
    person_group = parser.add_mutually_exclusive_group(required=True)
    person_group.add_argument(
        "--person-id",
        type=int,
        default=None,
        help="Numeric person id from the persons table",
    )
    person_group.add_argument(
        "--external-person-id",
        default=None,
        help="External person id (resolved via persons.external_person_id)",
    )

    # Time range
    parser.add_argument(
        "--time-from",
        default=None,
        help="Start of time range (ISO 8601, timezone-aware, e.g. 2026-05-01T00:00:00+08:00)",
    )
    parser.add_argument(
        "--time-to",
        default=None,
        help="End of time range (ISO 8601, timezone-aware)",
    )

    # Filters
    parser.add_argument(
        "--camera-id",
        default=None,
        help="Filter by camera_id",
    )
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=None,
        help="Minimum similarity threshold [0.0, 1.0]",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum results (default: 100, clamped to [1, 1000])",
    )

    # Output format
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        dest="output_json",
        help="Output results as JSON",
    )

    args = parser.parse_args()

    # Validate min_similarity range at CLI layer
    if args.min_similarity is not None:
        if not (0.0 <= args.min_similarity <= 1.0):
            parser.error(
                f"--min-similarity must be in [0.0, 1.0], got {args.min_similarity}"
            )

    time_from_ms = None
    if args.time_from:
        time_from_ms = _parse_iso8601(args.time_from, "--time-from")

    time_to_ms = None
    if args.time_to:
        time_to_ms = _parse_iso8601(args.time_to, "--time-to")

    # Validate time range ordering
    if time_from_ms is not None and time_to_ms is not None:
        if time_from_ms > time_to_ms:
            print(
                f"ERROR: --time-from ({args.time_from}) is after --time-to ({args.time_to})",
                file=sys.stderr,
            )
            sys.exit(1)

    query(
        person_id=args.person_id,
        external_person_id=args.external_person_id,
        time_from_ms=time_from_ms,
        time_to_ms=time_to_ms,
        camera_id=args.camera_id,
        min_similarity=args.min_similarity,
        limit=args.limit,
        output_json=args.output_json,
    )


if __name__ == "__main__":
    main()
