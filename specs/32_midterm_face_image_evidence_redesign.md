# Midterm Face Location And Image Evidence Redesign

## Decision

Face recognition should no longer default to video evidence.

For the midterm operator product, `face.watchlist` and one-click person search
are person-location workflows: answer "where was this person most recently and
credibly seen?" The primary output is the latest suitable camera hit, timestamp,
face crop, full-frame image, similarity, quality, and matched person metadata.

Video evidence remains the default for behavior incidents such as intrusion,
loitering, running, chasing, crowd gathering, fall, and future wall-climb
detection. Face evidence may request a video clip only as an explicit manual or
rule-level override.

## Current-State Audit

The current code already has most of the data foundation. As of this
implementation pass, the default face evidence semantics have been changed from
video-first to image-first; the notes below preserve the audit trail and call
out the specific paths that were corrected.

### Face Observation Path

Savant exports face observations to Redis stream `security.face_observations`.
The face-worker consumes that stream and inserts rows into `face_observations`.
The table already stores:

- `source_observation_id`
- `camera_id`
- `source_id`
- `track_id`
- `timestamp_ms`
- face/person bbox and landmarks
- embedding
- `snapshot_path`
- `crop_path`
- JSON payload

This is the right base for face appearance history. No video is required for the
core query.

### Watchlist Path

`WatchlistMatchEmitter` runs inside face-worker after an observation is inserted.
It resolves per-camera `face.watchlist` rules when available, otherwise can fall
back to environment-configured targets. It searches the gallery and publishes a
`watchlist_hit` event to `security.events`.

Previously, `build_watchlist_hit_event()` emitted face hits with:

```yaml
snapshot_required: true
clip_required: true
payload.media.clip_required: true
```

That made every face hit compete for the rolling-cache video materializer. The
implemented default is now:

```yaml
snapshot_required: true
clip_required: false
payload.media.evidence_mode: image_only
payload.media.playback_kind: image
payload.media.recording_strategy: image_only
```

### Event/Evidence Task Path

event-worker still materializes behavior events through the rolling-cache video
path, but image-only face hits now create `task_type = image_only` pending
tasks. They do not claim rolling-cache video slots and do not publish
`record_request` messages by default. media-worker has a separate lightweight
image materializer: once the rolling-cache segment containing the event frame is
ready, it extracts `full_frame.jpg`, crops `face_crop.jpg`, writes optional
`annotated_frame.jpg`, and only then promotes the bundle to
`media_status = image_ready`.

### DB-Backed Evidence Index

`evidence_bundles` and `evidence_artifacts` are already flexible enough to store
image artifacts:

- `evidence_bundles.summary` can carry face/person metadata.
- `evidence_artifacts.artifact_type` can represent `face_crop`,
  `full_frame`, or `annotated_frame`.

The evidence listing path now accepts either a playable raw clip or an
image-ready bundle/artifact. The 8090 evidence detail view switches to an image
viewer for `playback_kind = image` instead of trying to load a video player.

### People And Trajectory Path

The people API now exposes:

```http
GET /api/v1/people/{person_id}/latest-location
GET /api/v1/people/{person_id}/trajectory
```

8090 has a "查找此人" control on the person detail pane that calls the latest
location endpoint and displays the camera, time, similarity, and image.

## Target Product Semantics

| Workflow | Trigger | Primary answer | Default artifacts | Video |
| --- | --- | --- | --- | --- |
| Intrusion / behavior alert | behavior rule event | what happened around this incident | video clip + overlay + snapshot | yes |
| Watchlist hit | configured person appears | this person appeared at this camera/time | face crop + full frame + match metadata | no |
| One-click person search | operator selects/searches person | latest suitable camera hit | face crop + full frame + ranked observations | no |
| Face observation history | face seen over time | historical camera trajectory | metadata; sampled/hit images only | no |

`live_search_hit` should not become a separate production algorithm. It is a
compatibility name for ad-hoc face matching and should route through the same
face-match result model as `face.watchlist`.

## Data Model Changes

### Add Explicit Evidence Mode

Use explicit modes instead of inferring everything from `clip_required`:

```text
video_clip
image_only
metadata_only
```

Store the mode in event payload and task/index metadata:

```json
{
  "evidence_mode": "image_only",
  "playback_kind": "image",
  "result_mode": "latest_camera_hit"
}
```

### Evidence Bundle Compatibility

Keep `evidence_bundles.raw_clip_uri` for video evidence, but allow image-only
bundles where `raw_clip_uri` is empty and `evidence_artifacts` contains:

- `face_crop`
- `full_frame`
- `annotated_frame`

Recommended bundle fields:

```json
{
  "media_status": "image_ready",
  "evidence_state": "image_ready",
  "frontend_overlay_required": false,
  "summary": {
    "evidence_mode": "image_only",
    "playback_kind": "image",
    "person": {...},
    "match": {...},
    "observation": {...},
    "latest_camera_hit": {...}
  }
}
```

### Face Location Query

Add an API query model based on person and observation/match rows:

```http
GET /api/v1/people/{person_id}/latest-location
GET /api/v1/people/{person_id}/trajectory
```

Ranking for latest-location:

1. newest `timestamp_ms` / captured time;
2. similarity above threshold;
3. quality above threshold;
4. camera/source still known/enabled when possible;
5. prefer rows with usable image artifact paths.

The response should include:

- person id/name/external id;
- camera id/name/source id;
- timestamp;
- similarity and threshold;
- quality and face confidence;
- `source_observation_id`;
- `face_crop_url`;
- `full_frame_url`;
- optional linked `event_id` when the result came from a watchlist alert.

## Policy Changes

Default algorithm policies should become:

```yaml
behavior.intrusion:
  evidence_mode: video_clip
  snapshot_required: true
  clip_required: true
  pre_seconds: 5
  post_seconds: 5

face.watchlist:
  evidence_mode: image_only
  result_mode: latest_camera_hit
  snapshot_required: true
  clip_required: false
  save_face_crop: true
  save_full_frame: true

face.live_search:
  alias_of: face.watchlist
  production_route: face.watchlist
  evidence_mode: image_only
  result_mode: latest_camera_hit
  clip_required: false

face.observation:
  evidence_mode: metadata_only
  snapshot_required: false
  clip_required: false
  save_hit_images_only: true
```

8090 should make this visible: behavior rules produce video; face rules produce
latest-location/image evidence.

## Runtime Flow

### Face Watchlist Hit

1. Savant exports face observation to Redis.
2. face-worker inserts `face_observations`.
3. face-worker resolves watchlist targets and searches gallery/Qdrant/pgvector.
4. On match, face-worker emits `watchlist_hit` with:
   - `clip_required=false`;
   - `evidence_mode=image_only`;
   - `source_observation_id`;
   - matched person/gallery metadata;
   - frame identity (`frame_uuid` / `frame_pts`) and face bbox.
5. event-worker inserts the event and creates an `image_only` evidence task with
   `materialization_pending` / `image_pending`.
6. media-worker claims the image task, looks up the matching rolling-cache
   segment, extracts one full-frame image, crops the face bbox, and upserts
   `evidence_bundles` / `evidence_artifacts`.
7. 8090 shows an image result card and latest camera hit. If no image artifact
   was generated, the bundle is explicitly `image_missing` rather than falsely
   reported as `image_ready`.

### One-Click Person Search

1. Operator selects a person in 8090.
2. API queries recent match/observation rows for that person.
3. API returns the latest suitable camera hit plus ranked history.
4. UI shows location, timestamp, face crop, full frame, confidence, and a camera
   jump action.
5. No event or video materialization is created just because the operator
   searched.

### Behavior Video Evidence

Behavior events continue through the rolling-cache materialization path. This
keeps video generation capacity focused on events that actually need temporal
context.

## Implementation Plan

### Phase 1: Stop Face Hits From Requesting Video

Scope:

- Change `DEFAULT_EVIDENCE_POLICY` for face match event construction to
  `clip_required=false`.
- Change `payload.media.clip_required=false`.
- Add `evidence_mode=image_only`, `playback_kind=image`, and
  `result_mode=latest_camera_hit` to face-hit payloads.
- Preserve an explicit override path if a camera rule really sets
  `clip_required=true`.

Acceptance:

- New `watchlist_hit` rows have `clip_required=false`.
- New face events do not publish record requests for video clips by default.
- Intrusion behavior remains unchanged.

### Phase 2: Image-Only Evidence Index

Scope:

- Add event-worker/media-worker path for image-only face evidence.
- Resolve `source_observation_id` to `face_observations`.
- Use existing `crop_path` / `snapshot_path` if present.
- If full-frame is missing but the rolling cache still has the frame, extract a
  single JPEG only; do not remux a video clip.
- Upsert `evidence_bundles` with `media_status=image_ready`.
- Insert `evidence_artifacts` rows for `face_crop`, `full_frame`, and optional
  `annotated_frame`.

Acceptance:

- 8090 evidence API can retrieve the image bundle by event id.
- The bundle has no `raw_clip_uri`, but has image artifact URLs.
- The row is not counted as video materialization backlog.

### Phase 3: Evidence API And 8090 Image UI

Scope:

- Remove the evidence list requirement that `raw_clip_uri` must be non-empty.
  Replace it with "has either raw clip or image artifacts".
- Extend bundle manifest with `playback_kind`.
- In 8090 evidence view:
  - render video player for `playback_kind=video`;
  - render image card for `playback_kind=image`;
  - do not show "cannot play video" for image-only face hits.

Acceptance:

- Image-only face hits are visible in the evidence list.
- Opening one does not load the video player path.
- The UI displays person, camera, time, similarity, crop, and full frame.

### Phase 4: People Latest-Location API

Scope:

- Promote trajectory/latest-location repository into API service.
- Add:
  - `GET /api/v1/people/{person_id}/latest-location`
  - `GET /api/v1/people/{person_id}/trajectory`
- Join cameras for readable camera names.
- Return media URLs for image paths.

Acceptance:

- From 8090 person detail, the operator can click "查找此人" and see the latest
  suitable camera.
- The query does not generate an event, record request, or video evidence task.

### Phase 5: Algorithm Registry And UI Semantics

Scope:

- Mark `face.live_search` as alias/deferred compatibility, not a separate
  production algorithm.
- In 8090 algorithm controls, show `face.watchlist` as "名单命中/找人".
- Show evidence type: video vs image.
- Keep face observation as history/trajectory, not as an alarm toggle.

Acceptance:

- Operator cannot mistake face search for video evidence generation.
- Support matrix says production-ready face path is `face.watchlist` image
  evidence + latest-location.

### Phase 6: Reporting

Scope:

- Split pressure/run reports:
  - `behavior_video_events`
  - `face_image_hits`
  - `video_evidence_materialized`
  - `image_evidence_ready`
  - `video_materialization_backlog`
- Exclude image-only face hits from video deadline calculations.

Acceptance:

- 60-source pressure report shows whether behavior video evidence meets SLA
  independently from face image hit volume.

## Migration And Compatibility

- Existing video-based watchlist evidence remains readable as old evidence.
- New face hits default to image-only.
- `live_search_hit` remains accepted as input but is normalized to the same
  face-match image evidence semantics.
- If old clients expect `raw_clip_url`, they should see `playback_kind=image`
  and image artifact URLs instead of an unavailable-video error.

## Risks

- Some face observations may not have `snapshot_path`/`crop_path` today. The
  MVP should tolerate this by showing metadata-only latest-location first, then
  add single-frame extraction for missing images.
- If 8090 evidence list still filters on `raw_clip_uri`, image evidence will be
  invisible. This must be fixed before enabling image-only watchlist by default.
- Per-camera watchlist rules and env fallback currently coexist. The UI should
  clearly show which target source produced a hit.
- Face history should have retention limits; otherwise `face_observations` can
  grow without bound.

## Final Acceptance Criteria

- Default `watchlist_hit` creates zero video materialization work.
- Default `watchlist_hit` is visible in 8090 as image/location evidence.
- One-click person search returns latest suitable camera hit without generating
  evidence video.
- Intrusion still creates playable video evidence.
- 60-source reports separate face image throughput from behavior video SLA.
- The operator can answer "where is this person now/recently?" from 8090 without
  opening a video clip.
