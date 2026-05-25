"""Phase E1 — Alert Evidence MVP Worker.

Processes intrusion events from PostgreSQL, generates evidence
(snapshot, annotated snapshot, clip) from Phase 3H.2 aligned video+metadata.

One-shot: processes pending events and exits. Run again to process new events.
"""

from __future__ import annotations

import logging
import os
import sys

import psycopg

from app.config import load_config
from app.repository import find_events_needing_evidence, update_event_evidence
from app.evidence import (
    find_metadata_file,
    find_event_frame,
    extract_snapshot,
    extract_clip,
)
from app.bbox_draw import draw_bboxes_on_frame

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("evidence-worker")


def process_event(event: dict, cfg) -> dict | None:
    """Generate evidence for one event. Returns result dict or None on failure."""
    event_id = str(event["id"])
    event_type = str(event.get("event_type", "intrusion"))
    track_id = str(event.get("track_id", ""))
    source_id = str(event.get("source_id", ""))

    if not track_id:
        logger.warning("event %s has no track_id, skipping", event_id)
        return None

    logger.info(
        "processing event_id=%s type=%s track_id=%s source_id=%s",
        event_id, event_type, track_id, source_id,
    )

    # 1. Find metadata.json
    metadata_path = find_metadata_file(cfg.video_dir, source_id)
    if not metadata_path:
        logger.error("metadata.json not found under %s for source_id=%s",
                     cfg.video_dir, source_id)
        return None

    video_path = os.path.join(os.path.dirname(metadata_path), "video.mov")
    if not os.path.isfile(video_path):
        logger.error("video.mov not found at %s", video_path)
        return None

    # 2. Find frame matching the event's track_id
    frame_result = find_event_frame(metadata_path, track_id)
    if frame_result is None:
        logger.warning("track_id=%s not found in metadata, skipping", track_id)
        return None

    frame_num, objects = frame_result

    # 3. Extract snapshot
    snapshot_path = os.path.join(cfg.snapshot_output_dir, f"{event_id}.jpg")
    if not extract_snapshot(video_path, frame_num, snapshot_path):
        return None

    # 4. Draw annotated snapshot
    annotated_path = os.path.join(cfg.annotated_output_dir, f"{event_id}.jpg")
    if not draw_bboxes_on_frame(
        snapshot_path, objects, annotated_path,
        event_id=event_id, event_type=event_type,
        track_id=track_id, frame_num=frame_num,
    ):
        return None

    # 5. Extract clip
    clip_path = os.path.join(cfg.clip_output_dir, f"{event_id}.mp4")
    if not extract_clip(
        video_path, frame_num, cfg.pre_seconds, cfg.post_seconds,
        cfg.fps, clip_path,
    ):
        # Clip failure is non-fatal; continue with snapshot evidence
        logger.warning("clip extraction failed, continuing without clip")
        clip_path = ""

    # 6. Write back to PostgreSQL
    update_event_evidence(
        conn, event_id, snapshot_path, annotated_path,
        clip_path, frame_num, track_id,
    )

    return {
        "event_id": event_id,
        "track_id": track_id,
        "frame_num": frame_num,
        "snapshot_path": snapshot_path,
        "annotated_snapshot_path": annotated_path,
        "clip_path": clip_path,
    }


def main() -> None:
    cfg = load_config()
    logger.info(
        "evidence-worker starting: video_dir=%s max_events=%d",
        cfg.video_dir, cfg.evidence_max_events,
    )

    dsn = cfg.database_url
    with psycopg.connect(dsn) as conn:
        events = find_events_needing_evidence(conn, limit=cfg.evidence_max_events)
        logger.info("found %d intrusion events needing evidence", len(events))

        results = []
        for event in events:
            try:
                result = process_event(event, cfg)
                if result:
                    results.append(result)
            except Exception:
                logger.exception("failed to process event %s", event.get("id"))

    # Print summary
    print()
    print("=" * 60)
    print("Phase E1 Evidence Generation Summary")
    print("=" * 60)
    for r in results:
        print(f"  event_id:            {r['event_id']}")
        print(f"  track_id:            {r['track_id']}")
        print(f"  selected frame_num:  {r['frame_num']}")
        print(f"  snapshot_path:       {r['snapshot_path']}")
        print(f"  annotated_snapshot:  {r['annotated_snapshot_path']}")
        print(f"  clip_path:           {r['clip_path']}")
        print("-" * 60)
    print(f"Processed: {len(results)}/{len(events)} events")
    print("=" * 60)

    if not results:
        logger.warning("No events were processed successfully")
        sys.exit(1)


if __name__ == "__main__":
    main()
