# Phase 3F0 — Real Savant Bbox Media Evidence

Date: 2026-05-25

## Design Goal

Verify that real Savant detection events produce trusted bbox annotated snapshots through the full media pipeline. Establish `bbox_source` as the canonical trust marker for bbox drawing decisions.

## Why Replay metadata.json Cannot Be the Trust Source

The Replay/video-file-sink path bypasses Savant:

```
source-adapter → (ZMQ) → replay-service → (ZMQ) → video-file-sink
```

- `metadata.json` from video-file-sink captures metadata embedded in GStreamer frames delivered by Replay.
- Replay receives raw frames from source-adapter (testVideo/test.mp4). No detection metadata exists.
- Savant reads from RTSP (`rtsp://rtsp-server:8554/phase3b`) independently and exports events to Redis.
- Savant does NOT sit between source-adapter and video-file-sink.

Therefore `metadata.objects` is always `[]` in the Replay/video-file-sink path. This is **expected** and **not a bug**.

## Bbox Trust Model (Phase 3F0.1)

The trust signal comes from the event payload itself, set by the Savant event export probe:

| `payload.bbox_source` | Trust | `bbox_overlay_status` | Bbox drawn? |
|---|---|---|---|
| `"savant_detection"` | Trusted | `ready` | Yes |
| `"smoke_injected"` | Untrusted | `skipped_untrusted_bbox` | No |
| Absent (smoke/legacy) | Untrusted | `skipped_untrusted_bbox` | No |
| No bbox in payload | N/A | `skipped_missing_bbox` | No |

## Media Trust Chain

```
Savant (YOLO detection)
  → behavior_event_export_probe
    → payload.bbox_source = "savant_detection"
    → payload.bbox = {x, y, width, height} from track.current_bbox
  → RedisStreamEventExporter
  → Redis security.events

event-worker
  → inserts event (with bbox_source in payload)
  → if clip_required=true: publishes record_request

clip-worker
  → calls Replay API
  → video-file-sink writes clip

media-worker
  → reads bbox_source from events.payload
  → bbox_trusted = (bbox_source == "savant_detection")
  → generates annotated snapshot with trusted bbox
  → bbox_overlay_status = "ready"
```

## Debug Media Flag

For MVP Phase 3F0 verification, Savant events need `clip_required=true` and
`snapshot_required=true`. These are controlled by the env var:

```bash
SAVANT_EVENT_MEDIA_REQUIRED=true
```

When set, the behavior_event_export_probe overrides:
- `event.snapshot_required = True`
- `event.clip_required = True`
- `payload.media.snapshot_required = True`
- `payload.media.clip_required = True`
- `payload.media.recording_strategy = "savant_replay"`

This is **MVP/debug behavior only**. Production event media policy requires per-rule configuration (not implemented).

## Modified Files

| File | Change |
|---|---|
| `modules/savant_phase2c/custom/pyfuncs/behavior_event_export_probe.py` | Added `bbox_source: "savant_detection"` to payload; added `SAVANT_EVENT_MEDIA_REQUIRED` env flag |
| `services/media-worker/app/worker.py` | Replaced `_metadata_has_detections(clip_path)` with `payload.get("bbox_source") == "savant_detection"` |
| `harness/tests/test_phase3e_annotated_snapshot.py` | 31 tests (was 27): added 3 bbox_source tests, 1 integration test |
| `scripts/smoke/check_phase3f0_real_savant_bbox.sh` | New: 13-check manual GPU verification script |
| `docs/phase3f0_real_savant_bbox_summary.md` | New: this document |

## Commands

```bash
# Start GPU profile
docker compose -f infra/docker-compose.phase3b.yml --profile gpu up -d savant

# Enable debug media for real Savant events
# (add SAVANT_EVENT_MEDIA_REQUIRED=true to savant env in compose)

# Run GPU verification
bash scripts/smoke/check_phase3f0_real_savant_bbox.sh
```

## Remaining Technical Debt

1. **Replay timestamp-domain mismatch** — snapshot frame may differ from detection frame (inherited from Phase 3C).
2. **Replay metadata not carrying Savant objects** — metadata.json is always empty in the Replay path. This is architectural: Savant reads from RTSP, not from the Replay stream.
3. **Debug event media requirement** — `SAVANT_EVENT_MEDIA_REQUIRED=true` is MVP/debug behavior. Production events need per-rule `snapshot_required`/`clip_required` configuration via cameras.yml or a management API.
4. **No annotated video/clip** — annotation is on single JPEG frame only.
5. **Font fallback** — uses DejaVuSans if available, else PIL default.
6. **Zone polygon must be in event payload** — currently Savant events only include `zone_id`, not polygon coordinates. Zone overlay shows `skipped_missing_polygon`.
