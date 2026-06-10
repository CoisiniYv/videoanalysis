# C1E.4 RTSP Transport and Replay Continuity Isolation

Date: 2026-06-01

## Status

C1E.4 changes the acceptance model for the fixed C1E RTSP source:

```text
rtsp://10.37.57.112:8554/live/1080movie
```

This source is not a decode-clean golden source. Independent direct FFmpeg pulls
reported H.264 reference errors from the RTSP stream itself, including:

```text
co located POCs unavailable
reference picture missing during reorder
Missing reference picture
mmco: unref short failure
```

Observed direct-pull samples:

- TCP 30s: 649 frames, about 21.6 fps, H.264 errors=14.
- TCP 60s: 1401 frames, about 23.35 fps versus nominal 23.976 fps, estimated frame loss about 2.6%.
- UDP 15s: 329 frames, about 21.9 fps, H.264 errors=5.
- Stream: H.264 High Profile Level 4.1, 1920x1080, nominal 23.976 fps, B-frames=2.

Therefore C1E.2/C1E.3 must not use `decode_error_count=0` on this fixed RTSP
source as the only PASS condition. Decode-clean validation is still valuable,
but for this source it is a corruption visibility signal, not a hard evidence
chain failure by itself.

## Why C1E.3 Was Not Enough

C1E.3 showed that failed evidence samples had DTS/PTS gaps in
`sink_metadata.json`, and that `raw_clip.mov` gaps aligned with them. That
placed the discontinuity before the final evidence copy and made a pure
media-worker/finalizer problem unlikely.

That evidence did not prove whether the first discontinuity came from:

- RTSP/RTP transport loss.
- Non-clean H.264 emitted by the fixed RTSP source.
- Source Adapter ingest dropping or skipping compressed frames.
- Replay RocksDB storage gaps.
- Replay job output gaps.

The direct FFmpeg evidence now makes the fixed source itself a primary suspect:
the same class of H.264 reference errors can appear without Savant Replay in the
path.

## RTSP Transport

Actual source-adapter transport conclusion:

```text
actual_transport=tcp
```

Evidence:

- C1E compose sets `RTSP_TRANSPORT=${C1E_RTSP_TRANSPORT:-tcp}` on
  `source-adapter`.
- The adapter image entrypoint is `/opt/savant/adapters/gst/sources/rtsp.sh`.
- Inside `ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0`,
  `/opt/savant/adapters/gst/sources/rtsp.sh` reads `RTSP_TRANSPORT`, defaults it
  to `tcp`, and passes it to `ffmpeg_src` as:

```text
params="rtsp_transport=${RTSP_TRANSPORT}"
```

- Runtime diagnosis inspected `c1-official-source-adapter` and saw
  `RTSP_TRANSPORT=tcp`, `RTSP_URI=rtsp://10.37.57.112:8554/live/1080movie`, and
  `ZMQ_ENDPOINT=dealer+connect:tcp://replay-service:5555`.

The smoke script's `ffprobe -rtsp_transport tcp` preflight only proves the URL is
reachable over TCP. It is not, by itself, proof that the Source Adapter uses TCP;
the adapter entrypoint and runtime environment above are the proof.

## TCP Configuration

C1E dev now keeps TCP explicit but configurable:

```yaml
RTSP_TRANSPORT: ${C1E_RTSP_TRANSPORT:-tcp}
```

This is a dev/evidence-trust configuration only. It preserves the single RTSP
input path:

```text
RTSP -> Source Adapter -> Replay -> Savant -> event evidence
```

It does not add a second RTSP path, source extraction, FFmpeg source clipping, or
default `annotated_clip`.

## Smoke Layering

The C1E smoke is now an evidence-chain smoke by default.

It must fail on:

- Missing `raw_clip`.
- Missing `metadata.json`.
- Missing `event_annotation.json`.
- Missing `events.clip_path`.
- `duration_probe_status != ok`.
- Missing `clip_validation`.
- Source extraction fallback.
- Second RTSP pull.
- Production `annotated_clip`.
- Broken Replay job path, missing ROI annotation status, or uncontrolled extra clips.

It does not fail solely because the fixed RTSP source yields decode errors.

Default result states:

```text
PASS
PASS_WITH_SOURCE_CORRUPTION
PASS_WITH_UNVERIFIED_CLIP
```

`PASS_WITH_SOURCE_CORRUPTION` means the evidence bundle was generated, the
corruption was detected, and the status remained explicit:

```text
clip_validation.ok=false
clip_validation.decode_error_count>0
clip_status=generated_corrupt
```

This is a valid evidence status for a non-golden source. It is not the same as
pretending the clip is clean. API and UI surfaces should be able to display:

```text
clip generated, source decode corruption detected
```

Strict decode-clean smoke remains available only for stable/golden RTSP sources:

```bash
C1E_STRICT_DECODE_CLEAN=1 scripts/smoke/check_c1e_official_replay_evidence_integration.sh
```

Do not use strict decode-clean as the acceptance gate for
`rtsp://10.37.57.112:8554/live/1080movie`.

## C1E.2 Acceptance Definition

C1E.2 can be accepted as the Replay evidence hardening stage when:

- Downstream Replay job -> Video File Sink uses reliable DEALER/ROUTER sockets.
- Reliable sink options are present in the Replay job payload.
- Corruption is detected and surfaced through `clip_validation`.
- Evidence bundle still contains `raw_clip`, `metadata.json`,
  `sink_metadata.json`, and `event_annotation.json`.
- `events.clip_path` is updated.
- `generated_corrupt` is used when decode corruption is detected.
- Source corruption is not hidden as `generated`.
- No second RTSP path is used.
- No source extraction fallback is used.
- No production `annotated_clip` is generated.

This is different from requiring `decode_error_count=0` for every smoke against
the current fixed RTSP source.

## Continuity Findings

Clean C1E.3 smoke was intermittent:

- Fail: `generated_corrupt`, `decode_error_count=2`.
- Pass: `generated`, `decode_error_count=0`.
- Fail: `generated_corrupt`, `decode_error_count=2`.

Representative failed evidence:

- `raw_clip_duration=10.05`
- `duration_probe_status=ok`
- `clip_validation.ok=false`
- `clip_status=generated_corrupt`
- raw clip DTS gaps aligned with sink metadata DTS gaps.
- `sink_metadata` frame numbers were continuous, but DTS/PTS had gaps.

C1E.4 TCP run evidence after smoke layering update:

- Source Adapter runtime transport: `tcp`.
- Evidence chain completed.
- Event: `5e634257-2d74-4a70-a1eb-f7e19dc9802e`.
- Result: `PASS_WITH_SOURCE_CORRUPTION`.
- `clip_status=generated_corrupt`.
- `clip_validation.ok=false`.
- `clip_validation.decode_error_count=2`.
- Decode sample: `Missing reference picture`.
- `raw_clip_duration=10.05`.
- `duration_probe_status=ok`.
- raw clip DTS gap count: `1`.
- sink metadata DTS gap count: `1`.
- raw clip gap aligned with sink metadata gap: `yes`.

## Failure Stage

Current suspected root cause:

```text
fixed_rtsp_source_non_golden / unknown_source_stream_corruption
```

The evidence no longer supports treating every `generated_corrupt` result as a
Savant Replay failure. Direct FFmpeg pulls show the fixed source can itself emit
or deliver H.264 streams with missing references.

Remaining instrumentation gaps:

- Source Adapter does not expose per-stream packet loss or compressed-frame
  continuity counters in the C1E smoke.
- Replay Service logs show received/added/forwarded activity but not a concise
  per-job continuity report by DTS/frame UUID.
- Replay RocksDB storage continuity cannot yet be compared independently from
  Replay job output continuity.

## Next Steps

Recommended next phase:

```text
C1E.5 golden-source evidence validation and API status surfacing
```

Options:

- Add a decode-clean golden RTSP source for strict C1E smoke.
- Keep the current fixed source as a non-golden robustness source.
- Add source/replay compressed-frame continuity counters when available.
- Ensure API/UI clearly represent `generated_corrupt` as evidence generated with
  source decode corruption detected.

## Boundaries Preserved

- No second RTSP path.
- No source extraction.
- No FFmpeg source clip fallback.
- No production `annotated_clip`.
- No random tuning of `BUFFER_LEN`, `REPLAY_FPS`, or stop condition.
- C1E.1 protections remain preserved.
- C1E.2 reliable sink and validation protections remain preserved.
