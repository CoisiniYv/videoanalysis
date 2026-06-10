# R3.3A2c Replay/Cache UUID Domain Report

Status: BLOCKED.

Conclusion: `blocked`

Same-domain result: `blocked`

Reason: current mainline topology has no same-stream Replay/cache path, and the
local Replay configuration is an ingress cache with `out_stream: null`. There
is no verified in-line path of `source adapter -> Replay/cache -> Savant module`
that would let Replay/cache metadata and Savant events observe the same
savant-rs `VideoFrame` UUID domain.

No Replay clip was requested or generated. No visual alignment was performed.

## Scope

R3.3A2c answers one question only: whether Savant module event/trace
`frame_uuid` / `previous_keyframe_uuid` and Replay/cache archived metadata use
the same UUID domain for the same frame.

UUID domain verification requires a UUID-independent correlation key first,
such as PTS/timestamp or an A2a-style visual event frame. Only after identifying
the same frame without using UUID may the UUID values be compared. This report
did not reach that comparison step because the same-stream Replay/cache POC is
not available.

## Topology Inspection

Current c1 mainline:

```text
external source adapter
  -> ZMQ dealer+connect:tcp://savant-security:5555
  -> savant-security zeromq_source_bin router+bind:tcp://0.0.0.0:5555
  -> YOLO26-pose / behavior events and YOLOv8-Face / face observations
  -> Redis / PostgreSQL
  -> c1 metadata-sink and video-file-sink subscribe to Savant module output
```

Current c1 mainline does not include Replay/cache between the source adapter and
Savant module.

Historical phase3a/phase3b topology:

```text
source-adapter -> Replay Service
Savant inference -> separate RTSP/file path
```

That topology is not acceptable A2c evidence because it does not put Replay and
Savant on the same stream. It can reintroduce the original drift failure mode.

Current Replay config:

```text
modules/savant_replay/config.json
  in_stream.url = router+bind:tcp://0.0.0.0:5555
  out_stream = null
```

This config can receive and cache a stream, but it does not expose a configured
pass-through output stream to feed the Savant module in line.

## UUID Generation Point

R3.3A0/R3.3A1 proved that `BehaviorRulesPyFunc` receives an outer
`savant.deepstream.meta.frame.NvDsFrameMeta` containing a nested
`builtins.VideoFrame`, and `VideoFrame.uuid` is readable at the event decision
point.

The first locally observable point is therefore the Savant module runtime frame
object, before behavior event export and face observation export. The repository
does not contain the official source-adapter or `zeromq_source_bin` implementation
needed to prove whether the UUID is first generated in the adapter or at module
ZMQ ingestion. The UUID is present no later than module ingestion and before the
pyfuncs run.

For A2c, the unresolved generation point means the only acceptable proof is a
same-stream Replay/cache POC that compares Replay/cache metadata and Savant
event/trace metadata for the same frame using a UUID-independent key.

## Correlation Key Method

Planned method:

```text
1. Choose the same frame by PTS/timestamp or the A2a visual alarm-frame method.
2. Read Savant event/trace frame_uuid and previous_keyframe_uuid.
3. Read Replay/cache frame uuid and previous_keyframe_uuid/keyframe uuid.
4. Compare UUID values only after the same frame was identified without UUID.
```

Executed method: not executed. Step 1 topology inspection blocked the POC before
Replay/cache metadata existed to compare.

The A2a reference event remains valid for source-frame identity only:

```text
event_id = 373d94e7-199b-4ed3-be01-0eb9d68957e1
source_id = r3_3a2a_rtsp_identity_1780141642
frame_uuid = 019e78b6-4689-7d82-b6b6-ad39f7a37a77
previous_keyframe_uuid = 019e78b6-4129-7912-bacc-96215b6c2b5d
frame_pts = 13721388888
frame_num = 189
ntp_timestamp = 1780141672200209000
```

Those values were not compared against Replay/cache metadata in this phase.

## Result

```text
conclusion = blocked
same_domain = unknown
different_domain = unknown
correlation_key_used = none
replay_cache_metadata_compared = false
clip_generated = false
visual_alignment_performed = false
```

PASS was not claimed. `different_domain` was not claimed either, because no
same-frame Replay/cache metadata was available for a valid UUID-independent
comparison.

## GAP Candidates

- Replay is not in the current c1 mainline path.
- The available Replay config has `out_stream: null`, so it is not configured as
  an in-line relay to Savant.
- Legacy phase3a/phase3b POCs are sidecar/separate-stream evidence, not
  same-stream UUID-domain evidence.
- The exact UUID generation point is not fully proven from local source because
  the official adapter and `zeromq_source_bin` implementation are external.
- A same-stream bridge or Replay pass-through configuration may be required.
- If Replay cannot preserve the same savant-rs `VideoFrame` UUID through an
  in-line path, the next fallback is source/timestamp/NVR mapping rather than
  Replay UUID anchoring.

## Boundaries

- No Replay clip.
- No visual alignment.
- No production clip-worker.
- No production media-worker.
- No production Video File Sink deployment.
- No DB migration.
- No performance test.
- No API or dashboard integration.
- No mainline compose change.
- No debug media artifact committed.

## Next Step

Continue gap analysis for an explicitly throwaway same-stream Replay/cache POC:

```text
source adapter -> Replay/cache with configured pass-through output -> Savant module
```

The POC must expose Replay/cache metadata for correlation by PTS/timestamp or
A2a-style visual frame identity before any UUID comparison. Clip generation
remains out of scope until UUID same-domain is proven.
