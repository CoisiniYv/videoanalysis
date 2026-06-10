# R3.2E Local Video Debug Evidence Mode

Status: debug/test mode only.

R3.2E restores the old F4.3 local MP4 inspection workflow for algorithm
development. It is not production RTSP evidence and does not replace R3.3
timeline mapping.

R3.2E does not change the Savant pipeline, does not run performance tests, does
not write canonical production `clip_path`, and does not expose local-video
debug files as production evidence API media.

## Purpose

The accepted F4.3 behavior worked because inference and evidence export used
the same local MP4 file:

```text
source_mp4_path=/home/user/video-analytics/testVideo/1080movie.mp4
source_timeline=local_mp4
timestamp_ms is local video offset in milliseconds
```

`face_observations.timestamp_ms` is interpreted as the local video offset. The
exporter seeks the same MP4 with `cv2.CAP_PROP_POS_MSEC`, reads the aligned
frame, and draws the face bbox from the same observation timeline.

This mode is intentionally different from RTSP production evidence, where event
timestamps are wall-clock/epoch milliseconds and R3.3 timeline mapping is still
required for production RTSP.

## Required Inputs

The CLI requires an explicit source MP4:

```text
--source-mp4 /home/user/video-analytics/testVideo/1080movie.mp4
--source-id <local_video_source_id>
```

`source_id` should come from a local video adapter run using the same MP4. If
`source_id` appears to be RTSP/live, the exporter keeps an
`alignment_warning`:

```text
source_id is not verified as local-video source
```

## Outputs

Files are written under a debug-only directory:

```text
/data/video-analytics/media/debug/local_video_evidence/<source_id>/<run_ts>/
  debug_overlay_snapshot.jpg
  debug_annotated_clip.mp4
  debug_visual_metadata.json
  hits.csv
```

These files are debug-only. They are not canonical production `snapshot.jpg`,
`raw_clip.mp4`, or `annotated_clip.mp4`.

## Snapshot

The exporter reads `face_observations` for the requested `source_id`, matches
them against gallery embeddings, and selects hits above threshold.

For the first selected hit:

```text
cv2.VideoCapture(source_mp4_path)
cap.set(cv2.CAP_PROP_POS_MSEC, timestamp_ms)
read frame
```

The stored bbox format is `[xc, yc, w, h]` in full-frame pixels. The exporter
converts it to `[x1, y1, x2, y2]`, draws the face bbox, landmarks, person label,
similarity, observation id, and a visible warning:

```text
LOCAL VIDEO DEBUG - NOT PRODUCTION RTSP EVIDENCE
```

The output file is `debug_overlay_snapshot.jpg`.

## Annotated Clip

The exporter creates `debug_annotated_clip.mp4` by reading the same local MP4.
This video may be reencoded because it is debug mode, not production evidence.

Clip window:

```text
start_ms = max(first_hit_timestamp_ms - 5000, 0)
end_ms = first_hit_timestamp_ms + 5000
```

For each decoded frame, the exporter checks observations where:

```text
abs(row.timestamp_ms - frame_pos_ms) <= 600
```

Matching observations are drawn on that frame. If no observation is near the
frame timestamp, the exporter writes a nearest-hit label instead of pretending
there is a frame-level detection.

## Metadata

`debug_visual_metadata.json` records:

```json
{
  "debug_only": true,
  "evidence_mode": "local_video_debug",
  "source_mp4_path": "...",
  "source_id": "...",
  "source_timeline": "local_mp4",
  "timeline_assumption": "timestamp_ms is local video offset in milliseconds",
  "not_production_rtsp_evidence": true,
  "snapshot_path": ".../debug_overlay_snapshot.jpg",
  "annotated_clip_path": ".../debug_annotated_clip.mp4",
  "exact_event_frame": true,
  "exact_event_clip": true,
  "alignment_reason": "same local mp4 used for inference and evidence export"
}
```

`exact_event_frame=true` and `exact_event_clip=true` are valid only for this
local-video debug mode, and only because the same local MP4 is used for
inference and evidence export. They must not be copied to RTSP evidence.

## Data Source

R3.2E first supports face debug evidence:

- `face_observations` filtered by `source_id`
- `person_gallery_embeddings` and `persons`
- similarity computed with `1 - (fo.embedding <=> pge.embedding)`
- default external ids:
  - `demo:f4_3:finch`
  - `demo:f4_3:reese`

Events are not required for the first version.

## CLI

```bash
python services/clip-worker/export_local_video_debug_evidence.py \
  --source-mp4 /home/user/video-analytics/testVideo/1080movie.mp4 \
  --source-id r3_2e_local_video_123 \
  --external-person-id demo:f4_3:reese \
  --threshold 0.35 \
  --output-root /data/video-analytics/media/debug/local_video_evidence \
  --limit 50 \
  --pre-ms 5000 \
  --post-ms 5000 \
  --window-ms 600 \
  --output-json
```

If no match is above threshold, the command prints top candidates and returns
`NO_HIT_ABOVE_THRESHOLD`. It does not fabricate hits.

## Boundary

R3.2E is debug/test mode:

- not production RTSP evidence
- no canonical production `clip_path`
- no canonical production `raw_clip_path`
- no production evidence API update
- no Savant pipeline change
- no performance test
- R3.3 timeline mapping still required for production RTSP visual evidence
