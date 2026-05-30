# R3.3A1 Unified Frame Anchor Propagation

Status: metadata propagation only. No Replay clip. No exact snapshot. No Video
File Sink change. No media-worker logic. No production exact visual evidence yet.
No performance test.

## Architecture Boundary

One RTSP frame enters the Savant module and carries one runtime frame anchor.
The frame is shared by two full-frame base perception branches:

- YOLO26-pose full-frame -> person bbox, keypoints, nvtracker track_id ->
  behavior rules -> `SecurityEvent` -> Redis `security.events`.
- YOLOv8-Face full-frame primary -> face bbox, 5 landmarks, face-person
  association with YOLO26-pose tracks, face quality, AdaFace ->
  `FaceObservation` -> Redis `security.face_observations`.

YOLOv8-Face is not secondary ROI inference. R3.3A1 does not add a second RTSP
pull and does not change the Savant pipeline topology.

## Frame Anchor

R3.3A0 proved the current runtime exposes a nested `VideoFrame` object and that
`VideoFrame.uuid` is readable. Frame UUID is available from runtime VideoFrame.
R3.3A1 extracts optional frame-level metadata:

- `frame_uuid`
- `keyframe_uuid`
- `previous_keyframe_uuid`
- `frame_pts`
- `frame_dts`
- `duration`
- `frame_num`
- `ntp_timestamp`
- `time_base`
- `source_id`
- `metadata_source`

All fields are optional. Missing fields must not fail behavior event or face
observation creation. `previous_keyframe_uuid` and `keyframe_uuid` may still be
null.

## Propagation Rules

Behavior events carry the frame anchor when available:

- top-level `SecurityEvent.frame_uuid`
- top-level `SecurityEvent.keyframe_uuid`
- `payload.media.frame_uuid`
- `payload.media.keyframe_uuid`
- `payload.media.previous_keyframe_uuid`
- `payload.media.frame_pts`
- `payload.media.frame_dts`
- `payload.media.duration`
- `payload.media.frame_num`
- `payload.media.ntp_timestamp`
- `payload.media.time_base`
- `payload.media.metadata_source`

Face observations carry the same frame anchor under `payload.media`. The
face-worker persists this metadata in `face_observations.payload` without adding
new columns.

Face match events inherit frame anchor metadata from the source
`face_observations.payload.media` when available. Missing values remain null.

## Non-Goals

R3.3A1 only propagates frame anchor metadata. It does not implement Replay clip
generation, exact snapshot extraction, segment recording, Video File Sink
changes, media-worker logic, annotated video, or performance tests.

Replay UUID anchor validation remains the next verification step before
claiming production exact visual evidence.
