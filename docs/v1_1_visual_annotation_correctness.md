# V1.1 Visual Annotation Correctness

V1 renderer plumbing is done, but V1 normal visual output was not accepted until
annotation semantics are correct. V1.1 requires real behavior and face visual
samples with required annotations, not just non-empty media files.

## Inspection Summary

Recent intrusion event rows show:

- `events.track_id` is populated in PostgreSQL, for example `track_id='6'`.
- A2a `identity_summary.json` did not carry top-level `track_id`; it only
  carried `source_event_id` like
  `savant_security:cam_r2_5_rtsp:6:intrusion:1780143406204`.
- V1.1 therefore uses `events.track_id` when present and falls back to parsing
  `source_event_id` as `producer:camera_id:track_id:event_type:ts`. When this
  fallback is used, `metadata.json` records
  `source.track_id_source=source_event_id_fallback`.
- Behavior event `payload.bbox` is project-schema `xywh` in full-frame pixel
  coordinates: `{x, y, width, height}`.
- Behavior event payload carries `zone_id` but does not carry ROI points.
- The ROI polygon for the latest A2a source is available from
  `modules/savant_security/config/cameras.generated.yml` and PostgreSQL
  `camera_zones.points`, for example `r2_5_full_frame` has
  `[[96.0, 54.0], [1824.0, 54.0], [1824.0, 1026.0], [96.0, 1026.0]]`.

Recent face observation rows show:

- `face_observations.track_id` is populated.
- `face_observations.face_bbox` uses center-based `[xc, yc, w, h]` pixel
  coordinates.
- `person_bbox` may be null.
- `landmarks` contains five landmark points as ten numeric values when
  available.
- `quality` is populated.
- `payload.media.frame_uuid` is populated from R3.3A1 frame anchor propagation.

## Correctness Rules

For `result_type=behavior_intrusion`, V1.1 requires:

- ROI polygon: required.
- person bbox: required.
- event label: required.
- track_id: required when present in the event or source_event_id.
- frame_uuid: required when the event has it.

If ROI or person bbox is missing, `metadata.json` records
`required_annotation_status=fail` and the smoke fails. Missing ROI is not
treated as a pass and is not fabricated.

For `result_type=face_observation`, V1.1 requires:

- face bbox: required.
- label: required.
- track_id, camera_id, frame_uuid when present in the stored observation.
- five landmarks are drawn when available; missing landmarks are allowed but
  recorded.

## BBox Parser

V1.1 uses explicit bbox parsing:

- `xywh`: `[x, y, w, h]` or `{x, y, width, height}`.
- `xyxy`: `[x1, y1, x2, y2]` or `{x1, y1, x2, y2}` /
  `{left, top, right, bottom}`.
- `cxcywh` / `center_xywh`: `[xc, yc, w, h]` or `{xc, yc, width, height}`.

If all bbox values are between `0.0` and `1.0`, they are treated as normalized
coordinates and scaled by image width/height. Out-of-bounds boxes are clamped to
the image and `bbox.*_clamped=true` is recorded in metadata.

## Manual Inspection Outputs

The V1.1 smoke writes:

- `manual-inspection/v1_visual_result_latest/behavior_intrusion/`
- `manual-inspection/v1_visual_result_latest/face_observation/`
- `manual-inspection/v1_visual_result_latest/index.html`
- `manual-inspection/v1_visual_result_latest/README.md`

The manual inspection directory is a debug artifact and must not be committed.

## Boundaries

V1.1 does not implement production clip-worker, production media-worker,
Replay/cache/sink deployment, production Video File Sink deployment, DB
migration, API/UI integration, or performance testing. Generated JPG/MP4 media
and manual inspection artifacts are not committed.
