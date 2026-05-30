# R3.3A0 Savant Frame UUID / Replay Anchor Runtime Probe

Status: runtime diagnostic only. No Replay clip implementation. No exact
snapshot implementation. No controlled segment recording. No production exact
visual evidence yet. No performance test.

## Purpose

The `events.frame_uuid` and `events.keyframe_uuid` columns already exist, but
current producers store `NULL`. That is a current producer behavior, not proof
that Savant runtime objects cannot expose a frame UUID, `keyframe_uuid`, or
`previous_keyframe_uuid`.

R3.3A0 verifies the actual `VideoFrame` / pyfunc frame object visible at the
event decision point. It must not turn "currently NULL in DB" into a fixed
expectation that `frame_uuid` is always null.

## Why Runtime Probe Comes First

R3.3 needs a reliable anchor:

```text
event_ts_ms or frame anchor
  -> frame_uuid / previous_keyframe_uuid / keyframe_uuid / pipeline timestamp
  -> Replay UUID anchor or timeline mapping
  -> exact frame and exact raw clip
```

If runtime exposes a Replay UUID anchor, R3.3 should prioritize Replay UUID
anchor validation before building a separate recording subsystem. If UUIDs are
not readable at runtime, R3.3 can then evaluate controlled segment recording or
external NVR integration with clear evidence.

Option B segment recording cannot skip drift root cause. Segment recording
still needs a proven mapping between event time, frame timestamp, segment
start/end, and video PTS. It is a fallback strategy, not a way to ignore
timestamp-domain mismatch.

## Probe Scope

The probe is enabled only by:

```text
R3_3A0_FRAME_UUID_PROBE_ENABLED=true
```

It writes the first `R3_3A0_PROBE_MAX_FRAMES` samples per `source_id` to:

```text
/data/video-analytics/media/debug/r3_3a0_frame_uuid_probe/<source_id>/*.json
```

The probe inspects the actual pyfunc frame object and nested objects such as
`video_frame`, `_video_frame`, `frame_meta`, and `metadata`. It records:

- `type(obj)`
- `dir(obj)`
- `repr(obj)`
- `uuid`
- `frame_uuid`
- `previous_keyframe_uuid`
- `keyframe_uuid`
- `pts`
- `dts`
- `duration`
- `frame_num`
- `source_id`
- `time_base`
- `metadata`
- `buf_pts`
- `ntp_timestamp`

This avoids relying only on `NvDsFrameMeta` surface fields or only on a static
`VideoFrame` constructor signature.

It does not change event logic and does not populate production event UUID
fields.

## Expected Output Shape

Each probe JSON contains:

```json
{
  "source_id": "camera-source",
  "frame_object_type": "runtime.class.Name",
  "available_attrs": [],
  "uuid": null,
  "previous_keyframe_uuid": null,
  "keyframe_uuid": null,
  "video_frame_object_type": null,
  "nested_objects": {},
  "pts": null,
  "dts": null,
  "duration": null,
  "frame_num": null,
  "timestamp_ms_used_by_event": null,
  "notes": "BehaviorRulesPyFunc event-decision frame_meta runtime object"
}
```

`null` is allowed for any field. The diagnostic question is what the current
runtime exposes, not what the contract demands.

## Metadata Sink Check

The metadata-sink NDJSON should also be inspected for `uuid`,
`previous_keyframe_uuid`, and `keyframe_uuid`. Existing samples show `pts`,
`dts`, `duration`, `frame_num`, `keyframe`, and `source_id`, but not UUID
fields. Metadata-sink absence does not prove the runtime pyfunc object lacks
UUID fields.

## Decision Rules

If runtime exposes `frame_uuid` or `previous_keyframe_uuid` / `keyframe_uuid`:

- prefer Replay UUID anchor for R3.3;
- preserve the UUID through `SecurityEvent`, `events`, and evidence tasks;
- validate Replay can resolve exact frame/clip from that anchor.

If runtime does not expose a UUID anchor:

- keep `frame_uuid` / `keyframe_uuid` nullable;
- inspect `pts`, `frame_num`, `time_base`, and source metadata for a timestamp
  mapping route;
- evaluate controlled segment recording or external NVR/MediaMTX recording.

Do not write tests that require `frame_uuid` to be always null. Do not document
UUID absence as a permanent conclusion until this runtime probe has been run
and reviewed.

## Boundaries

- No Replay clip implementation.
- No exact snapshot implementation.
- No controlled segment recording.
- No Savant model pipeline change.
- No production exact visual evidence yet.
- No performance test.
