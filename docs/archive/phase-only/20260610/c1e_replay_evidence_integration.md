# C1E Replay Evidence Integration

Status: **C1E.4 ACCEPTED** — 2026-06-01

## Final Phase Conclusions

### C1E.2 — Downstream Reliability + Clip Validation

**Accepted under revised evidence-chain semantics.**

- Replay job → Video File Sink upgraded from PUB/SUB to reliable DEALER/ROUTER sockets.
- Per-job sink options: 5s send/receive timeouts, 5 retries, 10000 HWM, 100 inflight ops.
- `clip_validation` added: decode probe produces `decode_error_count`, `duration_ok`, `probe_tool`.
- Status mapping: `generated` / `generated_corrupt` / `generated_unverified`.
- `generated_corrupt` is a valid surfaced evidence state, not a hidden success.
- C1E.1 trust hardening preserved (bounded keyframe fallback, ffprobe duration, ROI annotation).

### C1E.3 — Upstream Continuity Diagnosis

**Diagnosed: fixed RTSP source is non-golden.**

- Clean-runtime smoke was intermittent: 2/3 runs produced `generated_corrupt` with `decode_error_count=2`.
- Failed samples had DTS/PTS gaps in `sink_metadata.json` aligned with `raw_clip.mov` gaps.
- Diagnosis: `upstream_gap` — discontinuity exists before final evidence copy.
- Old RocksDB cache or stale containers are not the sole cause.
- C1E.3 did not prove whether the first loss is in RTSP source, source-adapter ingest, Replay storage, or Replay job output.

### C1E.4 — RTSP Transport + Smoke Layering

**Confirmed: source-adapter uses TCP; layered smoke accepts `generated_corrupt`.**

- Source adapter runtime transport verified: `RTSP_TRANSPORT=tcp`.
- Independent FFmpeg pulls of `rtsp://10.37.57.112:8554/live/1080movie` confirmed H.264 reference errors from the stream itself:
  - `co located POCs unavailable`
  - `reference picture missing during reorder`
  - `Missing reference picture`
  - `mmco: unref short failure`
- Stream profile: H.264 High Profile Level 4.1, 1920×1080, 23.976 fps, B-frames=2.
- TCP 60s test: 1401 frames, ~23.35 fps, ~2.6% frame loss.
- Therefore `decode_error_count=0` is NOT the acceptance gate for this source.
- Smoke layering updated:
  - Default: `PASS` / `PASS_WITH_SOURCE_CORRUPTION` / `PASS_WITH_UNVERIFIED_CLIP`
  - Strict decode-clean: opt-in via `C1E_STRICT_DECODE_CLEAN=1` (golden source only)
- `PASS_WITH_SOURCE_CORRUPTION` means: evidence bundle generated, corruption detected, status explicit (`generated_corrupt`), not pretending clean.

### Summary

```text
C1E.2  downstream reliability + clip validation         PASS (revised evidence-chain semantics)
C1E.3  upstream continuity diagnosis                    PASS (non-golden source identified)
C1E.4  non-golden RTSP handling / smoke layering        PASS
```

What this means: the Replay evidence chain is trustworthy. If the source stream has compressed-frame corruption, the system generates the evidence bundle and marks it `generated_corrupt` — it does not pretend `generated`.

What this does NOT mean: the fixed RTSP source is decode-clean. It is not. Do not chase `decode_error_count=0` on this source.

## Scope

C1E integrates the P1c.2 Replay raw clip evidence path into the C1 official
development stack.

It is a single-event evidence POC. It proves event-triggered Replay raw clip
generation, business metadata generation, event annotation generation, and
`events.clip_path` update from the C1 official dev stack.

It is not continuous recording. It is not incident coalescing. It is not a
production multi-event merge policy. P2 remains responsible for `merge_window`,
`cooldown`, `incident_id`, `first_event_ts`, `last_event_ts`, one clip for many
events, and `event_annotation.json` with `events[]`.

## Runtime Drift Audit

Before C1E:

| Component | Drift Status |
|-----------|--------------|
| `c1-official-savant` | Already bind-mounted `modules/savant_security` at `/opt/savant/src/module`. |
| `c1-official-event-worker` | Build/COPY image mode; service code was not bind-mounted. |
| `c1-official-face-worker` | Build/COPY image mode; service code was not bind-mounted. |
| `c1-official-api` | Build/COPY image mode; API code was not bind-mounted. |
| `c1-official-clip-worker` | Not present in the official adapter compose. |
| `c1-official-media-worker` | Not present in the official adapter compose. |

Fix:

| Component | Bind Mount |
|-----------|------------|
| `c1-official-savant` | `../modules/savant_security:/opt/savant/src/module:rw` |
| `c1-official-event-worker` | `../services/event-worker:/app:rw` |
| `c1-official-face-worker` | `../services/face-worker:/app:rw` |
| `c1-official-api` | `../services/api:/app:rw` |
| `c1-official-clip-worker` | `../services/clip-worker:/app:rw` in the C1E replay dev compose |
| `c1-official-media-worker` | `../services/media-worker:/app:rw` in the C1E replay dev compose |

worker rebuild required: no

restart-only supported: yes

The C1E replay dev compose uses the existing `c1-official-event-worker:latest`
base image and the already-validated P1c clip/media worker base images. Runtime
code is supplied by bind mount, so source changes require container restart,
not rebuild. If a required worker image is missing and `C1E_ALLOW_BUILD=1` is
not set, the smoke reports:

```text
Result=BLOCKED
Reason=worker_image_missing_and_build_not_allowed
```

## Dev Compose

Dev-only compose:

```text
infra/docker-compose.c1-official-replay-dev.yml
```

Topology:

```text
RTSP
  -> source-adapter
  -> replay-service
  -> savant-security
  -> Redis security.events
  -> event-worker
  -> PostgreSQL events
  -> security.record_requests
  -> clip-worker
  -> Replay job
  -> video-file-sink
  -> media-worker/finalizer
  -> /data/video-analytics/media/evidence/<event_id>/
```

The source adapter sends only to Replay:

```text
dealer+connect:tcp://replay-service:5555
```

Replay forwards to Savant:

```text
dealer+connect:tcp://savant-security:5557
```

There is no direct parallel `source-adapter -> savant-security` path and no
second source adapter pulling the same RTSP stream.

## Input Policy

Fixed source:

```text
input_type = rtsp
input_uri = rtsp://10.37.57.112:8554/live/1080movie
source_id = c1e_rtsp_replay
camera_id = cam_c1e_rtsp_replay
local_file_used = false
test_video_used = false
source_extraction_fallback = false
second_rtsp_pull = false
```

If RTSP is unreachable:

```text
Result=BLOCKED
Reason=rtsp_unreachable
```

No local file fallback is allowed.

## Replay Policy

C1E reuses the P1c.2 Replay inline config:

```text
modules/savant_replay/config.p1c_rtsp_inline.json
```

Required TTL field:

```text
storage.rocksdb.data_expiration_ttl
```

Expected C1E.2 dev/evidence trust value:

```json
{"secs": 300, "nanos": 0}
```

`compaction_period` is `{"secs": 120, "nanos": 0}`. This is not a 60-stream
production capacity conclusion; production sizing still needs a Replay TTL,
bitrate, NVMe capacity, and write-amplification review.

The smoke prints and validates:

```text
replay_ttl_field
replay_ttl_seconds
ttl_requirement_seconds
replay_ttl_ok
replay_rocksdb_path
```

The Replay job uses:

```json
{
  "offset": {"seconds": 5},
  "stop_condition": {
    "ts_delta_sec": {
      "max_delta_sec": 10
    }
  }
}
```

If the Replay API rejects `ts_delta_sec`, `frame_count` fallback is allowed
only when `fallback_reason` is recorded.

## Recording Gate

The C1 official event-worker runs with:

```text
RECORDING_ENABLED=true
RECORDING_EVENT_TYPES=intrusion
RECORDING_SOURCE_ID=c1e_rtsp_replay
RECORDING_MAX_REQUESTS_PER_RUN=1
RECORDING_COOLDOWN_SECONDS=30
RECORDING_PRE_SECONDS=5
RECORDING_POST_SECONDS=5
```

Event insertion is independent from clip success. Record request publication is
gated and idempotent by `source_event_id` plus `savant_replay`.

## Evidence Contract

The C1E media-worker writes only raw evidence bundles:

```text
/data/video-analytics/media/evidence/<event_id>/
  raw_clip.mov | raw_clip.webm | raw_clip.mp4
  metadata.json
  sink_metadata.json
  event_annotation.json
```

`metadata.json` is business-level metadata and references the preserved sink
metadata path. It records:

```text
input_type
input_uri
local_file_used=false
source_extraction_fallback=false
second_rtsp_pull=false
event_id
source_event_id
camera_id
source_id
event_ts_ms
frame_uuid
keyframe_uuid
previous_keyframe_uuid
replay_job_id
offset_seconds
stop_condition
stop_condition_mode
raw_clip_path
event_annotation_path
raw_clip_duration
expected_duration_seconds
duration_probe_status
clip_validation
clip_status=generated | generated_corrupt | generated_unverified
limitations
```

`event_annotation.json` converts object bbox input:

```json
{"x": 1653, "y": 715, "width": 121, "height": 142}
```

to:

```json
{
  "bbox_format": "xyxy",
  "bbox": [1653, 715, 1774, 857],
  "bbox_source_format": "xywh",
  "bbox_raw": {"x": 1653, "y": 715, "width": 121, "height": 142}
}
```

No `annotated_clip` is generated by default.

## Smoke

Smoke:

```text
scripts/smoke/check_c1e_official_replay_evidence_integration.sh
```

Default runtime discipline:

```text
detect_docker()
build_used=no
pull_used=no
bind_mount_status=MOUNT_OK
services_restarted=<service list>
```

The smoke uses `$DOCKER` and `$COMPOSE` after Docker access detection. It
defaults to:

```bash
$COMPOSE -f infra/docker-compose.c1-official-replay-dev.yml up -d --no-build --force-recreate --pull never
```

Build is allowed only with:

```text
C1E_ALLOW_BUILD=1
```

Expected single-event counts:

```text
record_requests_created=1
replay_jobs_created=1
evidence_bundles_created=1
extra_clips_detected=0
```

If extra evidence bundles are produced:

```text
Result=FAIL
Reason=uncontrolled_clip_generation
```

The source adapter is stopped after the first evidence bundle and the compose
stack is brought down by the smoke cleanup trap.

## Artifact Policy

C1E runtime artifacts are written under:

```text
/data/video-analytics/media
```

No media artifact, report artifact, runtime CSV/JSON, temporary directory, or
Replay RocksDB data is written under the repository.
