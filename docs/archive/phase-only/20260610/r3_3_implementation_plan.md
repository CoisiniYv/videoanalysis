# R3.3 Implementation Plan

Status: staged plan only.

## R3.3A — Timestamp / Frame Metadata Inspection

Goal:

- Inspect Savant frame metadata and sink outputs for `pts`, `dts`, `duration`,
  `frame_num`, `keyframe`, possible NTP timestamp, `frame_uuid`,
  `keyframe_uuid`, and `pipeline_ts_ns`.

Likely modified files:

- New inspection scripts under `scripts/smoke/` or `scripts/diagnostics/`.
- Documentation only unless an inspection helper is required.

DB migration:

- None.

Tests:

- Static contract for inspection outputs.
- Smoke that reads metadata-sink/video-file-sink samples.

Acceptance:

- Clear answer for which timestamp fields exist and which timestamp domain they
  use.
- Confirm whether frame UUID/keyframe UUID can be read without a Savant pipeline
  change.

Boundaries:

- No production clip.
- No annotated video.
- No Savant pipeline change in planning.
- No performance test.

## R3.3B — Choose Mapping Strategy And Schema

Goal:

- Choose Replay/keyframe, controlled segment recording, external NVR/MediaMTX,
  or hybrid.
- Define `timeline_mapping`, `recording_segments`, and event/evidence payload
  exactness fields.

Likely modified files:

- DB migration for segment index or timeline fields.
- `specs/05_database_schema.md`.
- Evidence metadata docs.

DB migration:

- Candidate `011_r3_3_recording_segments.sql`.

Tests:

- Schema contract.
- Repository contract.

Acceptance:

- `event_ts_ms -> segment/frame/clip` lookup can be represented without
  ambiguity.

Boundaries:

- No production media generation yet.
- No pipeline changes unless separately approved after planning.

## R3.3C — Exact Snapshot MVP

Goal:

- Generate `snapshot.jpg` from the exact event frame using verified
  `timeline_mapping`.
- Set `exact_event_frame=true` and `snapshot_alignment_status=exact` only when
  proven.

Likely modified files:

- `services/clip-worker/app/evidence_media_service.py`
- `services/clip-worker/app/evidence_snapshot_writer.py`
- `services/clip-worker/app/evidence_metadata_writer.py`
- Repository/schema files as needed.

DB migration:

- Only if R3.3B fields are not already enough.

Tests:

- Contract test for exact snapshot states.
- Runtime smoke with known mapped segment.

Acceptance:

- Bbox and frame are same source.
- Non-exact snapshot never draws production bbox overlay.

Boundaries:

- No annotated video.
- No raw clip MVP in this phase unless already mapped.

## R3.3D — Exact Raw Clip MVP

Goal:

- Generate canonical `raw_clip.mp4` for event pre/post window using verified
  segment/replay/NVR mapping.
- Set `exact_event_clip=true` and `clip_alignment_status=exact` only when
  proven.

Likely modified files:

- `services/clip-worker/app/evidence_raw_clip_writer.py`
- `services/clip-worker/app/replay_client.py`
- `services/clip-worker/app/evidence_media_service.py`
- API schema if new exactness fields are surfaced.

DB migration:

- Only if R3.3B fields are not already enough.

Tests:

- Contract test for no unmapped fallback as production evidence.
- Runtime smoke verifying event timestamp is inside clip window.

Acceptance:

- `raw_clip.mp4` contains the exact event frame.
- `clip_path`/`raw_clip_path` are set only for exact production clip.

Boundaries:

- No `annotated_clip.mp4` default.
- No default dual video output.

## R3.3E — Production Retention / Cleanup Integration

Goal:

- Integrate exact evidence media with R3.1C retention policy.
- Enforce `retention_days`, `max_total_gb`, `max_camera_gb`,
  `min_free_disk_percent`, and delete-oldest-first.

Likely modified files:

- New cleanup worker or scheduled job.
- Media status repository updates.
- Monitoring docs.

DB migration:

- Possible media expiry/deletion audit fields.

Tests:

- Cleanup contract.
- Dry-run cleanup smoke.

Acceptance:

- Cleanup deletes media files, never DB business rows.
- DB marks `media_expired` or `media_deleted`.
- Inference path remains unblocked under storage pressure.

Boundaries:

- No performance test until retention and exactness are stable.
- No unbounded video-file-sink/debug sink dependency.

## Overall Boundary

R3.3 planning does not implement replay/keyframe code, does not implement a
recording segment writer, does not generate annotated video, does not change
YOLO/AdaFace/tracker, does not change the Savant pipeline, and does not run a
performance test.
