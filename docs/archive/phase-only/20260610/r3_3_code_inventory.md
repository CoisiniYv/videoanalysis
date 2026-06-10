# R3.3 Code Inventory

Status: planning inventory only.

## Current Available Code

Event ingestion:

- `modules/savant_security/custom/models/events.py` defines
  `SecurityEvent.frame_uuid` and `SecurityEvent.keyframe_uuid`.
- `modules/savant_security/custom/pyfuncs/behavior_rules.py` emits intrusion
  events and explicitly sets `frame_uuid=None` and `keyframe_uuid=None`.
- `services/event-worker/app/repository.py` stores `frame_uuid` and
  `keyframe_uuid` into the `events` table.
- `services/event-worker/app/record_request.py` includes `event_ts_ms`,
  `frame_uuid`, and `keyframe_uuid` in record requests.

Face observation and face match:

- `modules/savant_security/custom/pyfuncs/face_observation_exporter.py` exports
  `timestamp_ms` from `frame_meta.pts` via `normalize_pts_to_ms()` and stores
  `frame_num`.
- `services/face-worker/app/repository.py` persists `timestamp_ms` and
  `frame_num` in `face_observations`.
- `services/face-worker/app/face_match_event_service.py` builds
  `watchlist_hit` events from `face_observations`, but sets `frame_uuid=None`
  and `keyframe_uuid=None`.

Evidence workers:

- `services/clip-worker/app/evidence_media_service.py` writes R3.2A/B lifecycle
  outputs under event directories.
- `services/clip-worker/app/evidence_raw_clip_writer.py` can write
  `raw_clip.mp4` only from an explicit raw MP4 fallback. It does not provide RTSP
  timeline mapping.
- `services/clip-worker/app/replay_client.py` has `find_keyframe()` and
  `create_job()` methods for Replay, but `find_keyframe()` intentionally drops
  `from_ns` and `to_ns` because epoch to Replay pipeline timestamp mapping is
  missing.
- `services/clip-worker/app/worker.py` can consume record requests and call
  ReplayClient, but recording is disabled in the current compose.

Debug-only local video:

- `services/clip-worker/app/local_video_debug_evidence.py` supports exact local
  MP4 debug evidence, not production RTSP evidence.

## Current Missing Fields

`events`:

- Has `event_ts_ms`, `frame_uuid`, and `keyframe_uuid`.
- Does not have `pipeline_ts_ns`, `source_pts_ns`, `segment_id`, or
  `timeline_mapping`.
- Runtime DB inspection showed `frame_uuid_count=0` and
  `keyframe_uuid_count=0` across existing rows.

`face_observations`:

- Has `timestamp_ms` and `frame_num`.
- Does not have `frame_uuid`, `keyframe_uuid`, `pipeline_ts_ns`, or
  `nvr_reference` in the current migration.
- Spec documents `nvr_reference` as a future target, but current migration does
  not create it.

`evidence_tasks`:

- Has event time, pre/post seconds, paths, and status.
- Does not store segment id, offset, PTS, frame_uuid, or keyframe_uuid.

## Timestamp Sources

Behavior events:

- `person_pose_adapter._get_timestamp_ms()` checks `frame_meta.ntp_timestamp`,
  then `frame_meta.buf_pts // 1_000_000`, then wall-clock fallback.
- `behavior_rules.py` uses `track.last_seen_ms` as `event_ts_ms`.

Face observations:

- `face_observation_exporter.py` uses `frame_meta.pts` normalized by
  `normalize_pts_to_ms()`.
- Local video adapter produced small offsets such as `17726`.
- RTSP runs can produce another time domain depending on adapter metadata.

SecurityEvent:

- Carries `frame_id`, `frame_uuid`, and `keyframe_uuid`.
- Current producers do not read or populate frame UUID/keyframe UUID from
  Savant metadata.

## ReplayClient Capability

`ReplayClient.find_keyframe(source_id, ts_ms, window_s)` can call:

```text
POST /api/v1/keyframes/find
```

However, the current implementation logs epoch-derived `from_ns`/`to_ns` and
then sets them back to `None`. This is correct for the current system because
Replay uses pipeline-relative timestamps and the project has no reliable
epoch-to-pipeline mapping.

`ReplayClient.create_job()` can create a Replay job from a known
`keyframe_uuid`. R3.3 needs a proven way to get that keyframe for the event
timeline.

## Sink Output

`metadata-sink`:

- Writes NDJSON under `/data/video-analytics/media/c1-official-metadata/`.
- Sample rows include `source_id`, `framerate`, `width`, `height`, `pts`, `dts`,
  `duration`, `keyframe`, `frame_num`, `tags`, and object metadata.
- It can help inspect frame-level metadata, but it is currently a debug sink.
- It does not by itself provide a bounded production segment index or retention.

`video-file-sink`:

- Writes under `/data/video-analytics/media/c1-official-savant-output/<source_id>%/.../`.
- Produces `video.mov` and a NDJSON-like `metadata.json`.
- It uses `CHUNK_SIZE=0`, so it is not a controlled 10s/30s segment writer.
- It is configured with `restart: unless-stopped` and can grow disk without
  retention.

## DB Schema Gaps

Potential future additions:

- `recording_segments` with `source_id`, `camera_id`, `segment_path`,
  `start_epoch_ms`, `end_epoch_ms`, `start_pts`, `duration_ms`, `fps`, `width`,
  and `height`.
- `events.timeline_mapping` or payload media fields for `segment_id`,
  `offset_ms`, `pipeline_ts_ns`, and exactness flags.
- `face_observations.frame_uuid`, `face_observations.keyframe_uuid`, and/or
  `face_observations.nvr_reference` if face match evidence needs exact replay.

## Files Future Work May Modify

Likely:

- `modules/savant_security/custom/pyfuncs/behavior_rules.py`
- `modules/savant_security/custom/pyfuncs/face_observation_exporter.py`
- `services/event-worker/app/repository.py`
- `services/face-worker/app/repository.py`
- `services/clip-worker/app/replay_client.py`
- `services/clip-worker/app/evidence_media_service.py`
- `services/clip-worker/app/evidence_metadata_writer.py`
- `services/clip-worker/app/evidence_raw_clip_writer.py`
- DB migrations for segment/timeline fields.

Should not change in R3.3 planning:

- YOLO26-pose, YOLOv8-Face, AdaFace model assets.
- Detector/tracker algorithm behavior.
- Savant pipeline topology.
- Performance tuning.
