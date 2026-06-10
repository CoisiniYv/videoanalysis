#!/usr/bin/env python3
"""Process one evidence task into metadata.json, snapshot.jpg, and optional raw clip."""

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

from app.evidence_media_service import process_event_evidence  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--rtsp-url", default=os.getenv("R3_2A_RTSP_URL", ""))
    parser.add_argument(
        "--media-root",
        default=os.getenv("MEDIA_ROOT_HOST", "/data/video-analytics/media"),
    )
    parser.add_argument(
        "--capture-backend",
        default=os.getenv("SNAPSHOT_CAPTURE_BACKEND", "opencv"),
        choices=("opencv", "gstreamer", "ffmpeg_fallback"),
    )
    parser.add_argument(
        "--raw-mp4",
        default=os.getenv("R3_2B_RAW_MP4", ""),
        help="Explicit debug/smoke raw MP4 fallback source; not production default.",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv(
            "DATABASE_URL",
            "postgresql://video:video@localhost:5438/video_analytics",
        ),
    )
    args = parser.parse_args()

    with psycopg.connect(args.database_url, autocommit=True) as conn:
        result = process_event_evidence(
            conn,
            event_id=args.event_id,
            rtsp_url=args.rtsp_url or None,
            media_root=args.media_root,
            capture_backend=args.capture_backend,
            raw_mp4_path=args.raw_mp4 or None,
        )
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
