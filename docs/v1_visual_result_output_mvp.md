# V1 Visual Result Output MVP

V1 generates debug/MVP visual output for human review of algorithm results. It
is not production evidence, not Replay evidence, and not output from a
production clip-worker or media-worker.

## Scope

The V1 renderer reads existing structured event or face-observation metadata
plus a source frame and optional source-aligned clip. It writes a standalone
debug result directory containing:

- `metadata.json`
- `report.md`
- `raw_snapshot.jpg`
- `annotated_snapshot.jpg`
- `raw_clip.mp4`
- `annotated_clip.mp4`

Outputs default to `/data/video-analytics/media/debug/visual_results/{run_id}`.
If that location cannot be written, the renderer falls back to
`tmp/visual_results/{run_id}` and records `fallback_output_root=true` with the
reason in `metadata.json`.

## Inputs

Supported inputs:

- `--a2a-summary-json`: an R3.3A2a `identity_summary.json`.
- `--event-json`: a behavior event or face observation JSON object.
- `--event-id`: best-effort PostgreSQL lookup of an existing row in `events`.

The source frame is supplied with `--source-frame`, usually an A2a
`matched_frame.jpg`. The optional source clip is supplied with `--source-clip`,
usually an A2a `source_aligned_clip.mp4`.

## Annotations

The renderer draws available metadata only:

- person bbox from behavior-event `payload.bbox` or `person_bbox`.
- face bbox from `face_bbox`.
- ROI polygon from `roi_polygon`, `polygon`, or compatible payload fields.
- labels for event type, camera id, source id, track id, timestamp, frame UUID,
  confidence, and quality.

Missing fields are not fabricated. Missing bbox, face bbox, or ROI polygon are
recorded in `metadata.json` limitations and the corresponding annotation status
is `unavailable`.

## Clip Behavior

`annotated_clip.mp4` uses a static overlay across the whole source clip. V1 does
not reconstruct per-frame dynamic boxes. If MP4 writing is unavailable, the
renderer may write `annotated_frames/` and records
`clip_annotation_status=frames_only`.

## Metadata Contract

`metadata.json` contains:

- `schema_version=1.0`
- `phase=V1`
- `visual_result_type=debug_mvp`
- `debug_only=true`
- `not_production_evidence=true`
- normalized source fields including `event_id`, `source_event_id`,
  `event_type`, `camera_id`, `source_id`, `track_id`, `event_ts_ms`,
  `frame_uuid`, `keyframe_uuid`, and `previous_keyframe_uuid`
- media output paths and annotation statuses
- limitations including `source extraction only; not Replay evidence` and
  `not production evidence; not production clip-worker output`

## Boundaries

V1 does not implement production clip-worker logic, production media-worker
logic, Replay/cache/sink deployment, Video File Sink deployment, production
recording, DB migration, API/UI integration, or performance testing. Debug media
outputs must not be committed.
