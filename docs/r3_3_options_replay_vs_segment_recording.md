# R3.3 Options: Replay Vs Segment Recording

Status: planning comparison only.

## Option A — Savant Replay / Keyframe Mapping

Use Savant Replay/keyframe features. Events carry or resolve
`frame_uuid`/`keyframe_uuid`/`pipeline_ts_ns`, then clip-worker creates replay
jobs anchored to the keyframe.

Pros:

- Same inference pipeline and media timeline.
- Best semantic fit for production evidence if frame identity can be exposed.
- Avoids a separate recorder if Replay is already deployed and retained.

Risks:

- Current `frame_uuid` and `keyframe_uuid` are always null.
- `ReplayClient.find_keyframe()` cannot safely use event epoch time because
  epoch to pipeline timestamp mapping is missing.
- Need to inspect Replay API, keyframe database, and pipeline timestamp domain.

Dual T4 / 60 streams:

- Attractive if Replay is already designed for this scale.
- Requires strict Replay retention and queue controls.
- Avoid running pgvector or clipping inside the GPU inference path.

Storage growth:

- Depends on Replay retention. Unbounded Replay storage would be high risk.

## Option B — Controlled Segment Recording

Create a bounded recording segment index. Each RTSP stream is recorded into
short segments, for example 10s or 30s, with an index:

```text
recording_segments:
  id
  camera_id
  source_id
  segment_path
  start_epoch_ms
  end_epoch_ms
  start_pts
  duration_ms
  fps
  width
  height
```

Event handling:

```text
event_ts_ms -> segment index -> offset_ms -> exact snapshot/raw_clip
```

Pros:

- Clear production model.
- Does not depend on undocumented Replay timestamp behavior.
- Similar to NVR segment indexing and easy to reason about.

Risks:

- Requires a recorder service and retention cleanup.
- 60 streams can create heavy disk IO and storage growth.
- Need robust segment close/index durability.

Dual T4 / 60 streams:

- Feasible if recording is isolated from Savant GPU inference and uses bounded
  worker concurrency, copy/remux, and retention.
- Requires capacity planning before enabling all streams.

Storage growth:

- High risk without retention. Must enforce `retention_days`, `max_total_gb`,
  `max_camera_gb`, and delete-oldest-first.

## Option C — External NVR / MediaMTX Recording

Delegate RTSP recording to MediaMTX or an external NVR. The application stores
or queries an NVR reference and uses its API for playback/export.

Pros:

- Recording responsibility moves out of the inference stack.
- Closer to production security deployments.
- Can reuse NVR retention, disk layout, and playback APIs.

Risks:

- Integration-specific API behavior.
- Segment index synchronization can fail.
- Need vendor-neutral `nvr_reference` and clear error states.

Dual T4 / 60 streams:

- Often best operationally if the NVR is sized separately from inference.
- Reduces pressure on the inference host.

Storage growth:

- Shifted to NVR/MediaMTX retention. Still needs monitoring and media status
  updates when media expires.

## Option D — Hybrid

Use Replay/keyframe when available and fall back to controlled segment/NVR
index. Store a normalized `timeline_mapping` so evidence workers do not care
which provider supplied the media.

Pros:

- Incremental.
- Allows local lab and production deployments to differ.

Risks:

- More states and tests.
- Requires a strict provider contract.

## Recommended MVP

Recommended R3.3 MVP:

1. R3.3A inspect and record actual frame metadata fields: `pts`, `dts`,
   `duration`, `frame_num`, `keyframe`, possible NTP, and possible frame UUID.
2. R3.3B choose schema for `timeline_mapping` and `recording_segments`.
3. Implement Option B controlled segment recording MVP first if Replay timestamp
   mapping cannot be proven quickly.
4. Keep Option A as preferred long-term path if Savant Replay can provide exact
   keyframe lookup from pipeline timestamps.
5. Treat Option C as production deployment integration once an external NVR is
   selected.

Reasoning:

- Current sink metadata already exposes `pts`, `frame_num`, and `keyframe`, but
  not segment start epoch.
- Segment recording makes `event_ts_ms -> offset_ms` explicit.
- It can be validated with deterministic smoke tests before scaling.

## Deferred Until Performance Baseline

- Annotated production video.
- 60-stream full recording enablement.
- Long retention at high bitrate.
- Multiple fallback providers enabled simultaneously.
- Heavy re-encoding.

No production exact visual evidence before timeline mapping. No Savant pipeline
change in planning. No performance test in planning.
