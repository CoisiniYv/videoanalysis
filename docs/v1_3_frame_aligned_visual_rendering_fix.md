# V1.3 Frame-Aligned Visual Rendering Fix

Current status:

- V1 renderer plumbing: DONE.
- V1.1 annotation correctness: NOT ACCEPTED.
- V1.2 diagnosis: frame/bbox/source alignment is not proven.

V1.3 changes the renderer contract: boxes are drawn only when the raw image is
explicitly aligned to the record `frame_uuid`. If a frame-aligned image cannot
be resolved, the renderer writes `diagnosis.json` and `report.md` but does not
write a misleading annotated snapshot.

## Diagnosis of V1.1 Output

Behavior intrusion sample:

- `raw_snapshot.jpg` came from A2a `identity_summary.json`
  `inspection_material_path`.
- `annotated_snapshot.jpg` was drawn from that raw image.
- The record frame UUID was
  `019e78d0-bd1d-7de1-b83b-694fd63a0de6`.
- The behavior bbox raw value was project event `payload.bbox`
  top-left pixel `xywh`:
  `{x: 1154.25, y: 36.28125, width: 370.5, height: 637.8750610351562}`.
- Converted `xyxy` was `[1154, 36, 1525, 674]` on a `1920x1080` image.
- ROI polygon was `[[96,54],[1824,54],[1824,1026],[96,1026]]` from
  `modules/savant_security/config/cameras.generated.yml`.
- Manual inspection showed the person bbox over a blank curtain region, so V1.1
  field-presence checks were not enough to prove visual correctness.

Face observation sample:

- The face sample was a plain `face_observation`, not gallery recognition.
- Its frame UUID was `019e78d0-bd46-7f40-8be9-1bb1c81e4090`.
- The raw face bbox was center pixel `cxcywh`:
  `[790.4658203125, 279.23974609375, 100.4879150390625, 96.55111694335938]`.
- Converted `xyxy` was `[740,231,841,328]` on a `1920x1080` image.
- Manual inspection showed the face bbox and landmarks over blank curtain, so
  the image/frame alignment was not proven.
- The face sample must not be presented as a gallery/watchlist recognition
  result.

## Frame-Aligned Resolver

The renderer now resolves an image for a record with this precedence:

1. Explicit `--source-frame` plus matching `--source-frame-uuid`.
2. A2a `identity_summary.json` whose `event.frame_uuid` matches the record and
   whose `inspection_material_path` is the source image.
3. Runtime frame dump:
   `/data/video-analytics/media/debug/runtime_frame_dump/{source_id}/{frame_uuid}.jpg`.
4. Frame UUID trace plus source MP4 extraction:
   `frame_uuid -> trace frame_pts/source_frame_index -> source_mp4 frame`.

If none of these prove `image_frame_uuid == record_frame_uuid`, the result is
blocked:

```json
{
  "frame_alignment_status": "blocked",
  "blocking_reason": "no_frame_uuid_aligned_image"
}
```

## Diagnosis Fields

Each output directory contains `diagnosis.json` with:

- `result_type`
- `record_frame_uuid`
- `image_frame_uuid`
- `frame_alignment_status`
- `image_size`
- raw bbox value
- declared/detected bbox format
- converted `xyxy`
- normalized/clamped flags in `metadata.json`
- ROI source and points
- `visual_correctness_status`
- recognition semantics status

## Recognition Semantics

`face_observation != gallery recognition`.

If no frame-anchored `watchlist_hit`, `live_search_hit`, or gallery-match result
is available, V1.3 writes:

```json
{
  "gallery_recognition_visual_sample": {
    "available": false,
    "reason": "no_frame_anchored_watchlist_hit_or_gallery_match"
  }
}
```

## Manual Inspection Output

The smoke rebuilds:

- `manual-inspection/v1_visual_result_latest/index.html`
- `manual-inspection/v1_visual_result_latest/README.md`
- `behavior_intrusion/diagnosis.json`
- `face_observation/diagnosis.json`
- `gallery_recognition/diagnosis.json`

`index.html` reports behavior frame alignment, face frame alignment, and gallery
recognition availability. Manual review is still required before accepting
visual correctness.

## Boundaries

V1.3 does not implement production clip-worker, production media-worker,
Replay/cache/sink deployment, production Video File Sink deployment, DB
migration, API/UI integration, or performance testing. Generated JPG/MP4 media
and manual inspection artifacts are not committed.
