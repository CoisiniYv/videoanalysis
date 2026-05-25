"""Phase E1 — Alert Evidence MVP Worker.

Processes intrusion events from PostgreSQL, generates evidence
(snapshot, annotated snapshot, clip) from Phase 3H.2 aligned video+metadata.

One-shot: processes pending events and exits. Run again to process new events.

Phase E1.1a — output directory lockdown:
    /media/evidence/events/{event_id}/
        snapshot.jpg
        annotated_snapshot.jpg
        clip_raw.mp4
        evidence_metadata.json
"""

from __future__ import annotations

import json
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
from app.clip_annotator import extract_annotated_clip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("evidence-worker")


def evidence_dir(events_root: str, event_id: str) -> str:
    """Return the per-event evidence directory path."""
    return os.path.join(events_root, event_id)


def process_event(event: dict, cfg, conn: psycopg.Connection) -> dict | None:
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
    metadata_path = find_metadata_file(cfg.video_input_dir, source_id)
    if not metadata_path:
        logger.error("metadata.json not found under %s for source_id=%s",
                     cfg.video_input_dir, source_id)
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

    # Create per-event directory
    event_dir = evidence_dir(cfg.evidence_events_dir, event_id)
    os.makedirs(event_dir, exist_ok=True)

    snapshot_path = os.path.join(event_dir, "snapshot.jpg")
    annotated_path = os.path.join(event_dir, "annotated_snapshot.jpg")
    clip_path = os.path.join(event_dir, "clip_raw.mp4")
    annotated_clip_path = os.path.join(event_dir, "clip_annotated.mp4")
    metadata_out = os.path.join(event_dir, "evidence_metadata.json")

    # 3. Extract snapshot
    if not extract_snapshot(video_path, frame_num, snapshot_path):
        return None

    # 4. Draw annotated snapshot
    if not draw_bboxes_on_frame(
        snapshot_path, objects, annotated_path,
        event_id=event_id, event_type=event_type,
        track_id=track_id, frame_num=frame_num,
    ):
        return None

    # 5. Extract raw clip
    if not extract_clip(
        video_path, frame_num, cfg.pre_seconds, cfg.post_seconds,
        cfg.fps, clip_path,
    ):
        logger.warning("clip extraction failed, continuing without clip")
        clip_path = ""

    # 5b. Generate annotated clip (per-frame bbox overlay)
    if not extract_annotated_clip(
        video_path, metadata_path, frame_num,
        cfg.pre_seconds, cfg.post_seconds, cfg.fps,
        annotated_clip_path,
        event_id=event_id, event_type=event_type, track_id=track_id,
        crf=cfg.annotated_clip_crf,
        preset=cfg.annotated_clip_preset,
        bbox_width=cfg.annotated_clip_bbox_width,
    ):
        logger.warning("annotated clip generation failed, continuing without it")
        annotated_clip_path = ""

    # 6. Write evidence metadata
    try:
        meta = {
            "event_id": event_id,
            "track_id": track_id,
            "frame_num": frame_num,
            "source_id": source_id,
            "event_type": event_type,
            "objects_count": len(objects),
            "phase": "E1.1a",
        }
        with open(metadata_out, "w") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        logger.warning("failed to write evidence_metadata.json")

    # 7. Write back to PostgreSQL
    update_event_evidence(
        conn, event_id, snapshot_path, annotated_path,
        clip_path, frame_num, track_id, annotated_clip_path,
    )

    return {
        "event_id": event_id,
        "track_id": track_id,
        "frame_num": frame_num,
        "snapshot_path": snapshot_path,
        "annotated_snapshot_path": annotated_path,
        "clip_path": clip_path,
        "annotated_clip_path": annotated_clip_path,
        "event_dir": event_dir,
    }


def main() -> None:
    cfg = load_config()
    logger.info(
        "evidence-worker starting: video_input_dir=%s evidence_events_dir=%s max_events=%d",
        cfg.video_input_dir, cfg.evidence_events_dir, cfg.evidence_max_events,
    )

    dsn = cfg.database_url
    with psycopg.connect(dsn) as conn:
        events = find_events_needing_evidence(conn, limit=cfg.evidence_max_events)
        logger.info("found %d intrusion events needing evidence", len(events))

        results = []
        for event in events:
            try:
                result = process_event(event, cfg, conn)
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
        raw_size = os.path.getsize(r['clip_path']) if r.get('clip_path') and os.path.isfile(r['clip_path']) else 0
        ann_size = os.path.getsize(r['annotated_clip_path']) if r.get('annotated_clip_path') and os.path.isfile(r['annotated_clip_path']) else 0
        print(f"  event_id:            {r['event_id']}")
        print(f"  track_id:            {r['track_id']}")
        print(f"  selected frame_num:  {r['frame_num']}")
        print(f"  event_dir:           {r['event_dir']}")
        print(f"    snapshot.jpg       {r['snapshot_path']}")
        print(f"    annotated_snapshot.jpg {r['annotated_snapshot_path']}")
        print(f"    clip_raw.mp4       {r['clip_path']} ({raw_size/1024:.0f} KB)")
        print(f"    clip_annotated.mp4 {r['annotated_clip_path']} ({ann_size/1024:.0f} KB)")
        print("-" * 60)
    print(f"Processed: {len(results)}/{len(events)} events")
    print("=" * 60)

    if results:
        logger.info(
            "=== Please manually inspect clip_annotated.mp4 for visual artifacts ===\n"
            "  crf=%d preset=%s bbox_width=%d",
            cfg.annotated_clip_crf, cfg.annotated_clip_preset,
            cfg.annotated_clip_bbox_width,
        )

    if not results:
        logger.warning("No events were processed successfully")
        sys.exit(1)


if __name__ == "__main__":
    main()
