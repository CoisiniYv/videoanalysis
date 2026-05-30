#!/usr/bin/env python3
"""Export R3.2E local-video debug evidence from face_observations."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.local_video_debug_evidence import DEFAULT_OUTPUT_ROOT  # noqa: E402
from app.local_video_debug_evidence import export_local_video_debug_evidence  # noqa: E402


def _parse_external_ids(raw: str) -> list[str]:
    values = [part.strip() for part in raw.split(",") if part.strip()]
    if not values:
        raise argparse.ArgumentTypeError("at least one external person id is required")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-mp4", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument(
        "--external-person-id",
        action="append",
        default=[],
        help="Target gallery external_person_id; may be repeated.",
    )
    parser.add_argument(
        "--external-person-ids",
        type=_parse_external_ids,
        default=None,
        help="Comma-separated target gallery external_person_id values.",
    )
    parser.add_argument("--threshold", type=float, default=0.35)
    parser.add_argument("--output-root", default=os.getenv("R3_2E_OUTPUT_ROOT", DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--pre-ms", type=int, default=5000)
    parser.add_argument("--post-ms", type=int, default=5000)
    parser.add_argument("--window-ms", type=int, default=600)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-json", action="store_true")
    parser.add_argument(
        "--database-url",
        default=os.getenv(
            "DATABASE_URL",
            "postgresql://video:video@localhost:5438/video_analytics",
        ),
    )
    args = parser.parse_args()

    external_ids = list(args.external_person_id)
    if args.external_person_ids:
        external_ids.extend(args.external_person_ids)
    external_ids = list(dict.fromkeys(external_ids))

    with psycopg.connect(args.database_url, autocommit=True) as conn:
        result = export_local_video_debug_evidence(
            conn,
            source_mp4_path=args.source_mp4,
            source_id=args.source_id,
            external_person_ids=external_ids or None,
            threshold=args.threshold,
            output_root=args.output_root,
            limit=args.limit,
            pre_ms=args.pre_ms,
            post_ms=args.post_ms,
            window_ms=args.window_ms,
            dry_run=args.dry_run,
        )

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result.get("hit_count", 0) <= 0:
        print("NO_HIT_ABOVE_THRESHOLD", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
