# Phase 3F0.3 — Single Ingestion Topology / Replay-Savant Alignment Review

Date: 2026-05-25

## 1. Current Video Topology (infra/docker-compose.phase3b.yml)

### Service Input/Output Table

| Service | Input | Output | Notes |
|---|---|---|---|
| `ffmpeg-source` | `/testVideo/test.mp4` (file) | `rtsp://rtsp-server:8554/phase3b` | Loops file infinitely, transcodes to h264 RTSP |
| `rtsp-server` | ffmpeg-source RTSP push | RTSP stream available on port 8554 | MediaMTX, passive relay |
| `savant` | `rtsp://rtsp-server:8554/phase3b` (RTSP, via `uridecodebin`) | Redis `security.events` stream; ZMQ sink to IPC | NVIDIA GPU, YOLO26-pose + nvtracker + behavior rules |
| `source-adapter` | `/testVideo/test.mp4` (file) | `dealer+connect:tcp://replay-service:5555` (ZMQ) | `video_loop.sh` adapter; SOURCE_ID=phase3b |
| `replay-service` | ZMQ from source-adapter (router+bind :5555) | REST API (:8080); ZMQ job output to video-file-sink | RocksDB storage; keyframe indexing |
| `clip-worker` | Redis `security.record_requests`; Replay REST API | Replay job creation; DB write (clip_status) | Calls find_keyframe → create_job |
| `video-file-sink` | ZMQ from replay-service (sub+bind :6666) | `/media/replay-sink-output/*/video.mov` + `metadata.json` | NDJSON metadata per clip |
| `media-worker` | Filesystem poll `/media/replay-sink-output/`; DB | DB writes (snapshot_status, annotated_snapshot_status, paths) | Extracts snapshots, draws bbox overlay |

### Current Actual Topology Diagram

```
                    testVideo/test.mp4  (single file on host)
                    /                   \
                   /                     \
        ffmpeg-source               source-adapter
        (loops file → RTSP)        (video_loop.sh → ZMQ)
              |                           |
              v                           v
         rtsp-server              replay-service
         (MediaMTX)               (ZMQ router+bind :5555)
              |                    RocksDB / REST :8080
              v                           |
           savant                         |
     (uridecodebin RTSP)                  |
     YOLO26-pose + tracker                |
     behavior rules                       |
              |                           |
              v                           |
      Redis security.events               |
              |                           |
         event-worker                     |
         (DB insert)                      |
              |                           |
      Redis security.record_requests      |
              |                           |
         clip-worker ---------------------+
   (find_keyframe → create_job → REST)
              |
              v
     replay-service → ZMQ job output
              |
              v
      video-file-sink  (ZMQ sub :6666)
              |
              v
      /media/replay-sink-output/  (.mov + metadata.json)
              |
              v
        media-worker  (poll filesystem)
        (snapshot + annotated_snapshot + bbox overlay)
```

### PROBLEM: Two Independent Ingest Paths

Savant reads from **RTSP** (ffmpeg-source → MediaMTX). source-adapter reads the **same file** directly and feeds replay-service via ZMQ. These two paths run asynchronously — they are at different positions in the video loop at any moment.

Savant's bbox is from one loop position; the replay clip/snapshot is from a different loop position. The bbox drawn on the snapshot is therefore visually misaligned, even though the coordinate math is correct.

## 2. Why Savant Is NOT Behind Replay

### Current Savant Input

- Source element: `uridecodebin` with `uri: ${oc.env:SRC_URL, rtsp://rtsp-server:8554/phase2c}`
- In compose: `SRC_URL=rtsp://rtsp-server:8554/phase3b`
- Savant reads RTSP exclusively via GStreamer `uridecodebin`

### Can Savant Use a ZMQ Source?

**No, not with the current module.yml `uridecodebin` source.** The official Savant adapters-gstreamer image (`ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0`) provides these source adapters:

```
ffmpeg.sh  gige_cam.sh  kvs/  media_files.sh  multi_stream.sh  rtsp.sh  video_loop.sh
```

There is **no ZMQ source adapter** in the standard image. Savant's DeepStream pipeline uses `uridecodebin` (or `nvurisrcbin`), which expects RTSP/file/URI sources — not raw ZMQ Pub/Sub frames.

### Can Savant Receive from Replay's ZMQ Output Directly?

**Not without a custom GStreamer ZMQ source element.** Replay outputs Savant-protocol ZMQ frames, but Savant module itself has no built-in element to consume ZMQ as a pipeline source. The `uridecodebin` element decodes RTSP/HTTP/file URIs; it cannot ingest ZMQ Pub/Sub or Dealer/Router sockets.

### Can source-adapter Send to Both Replay AND Savant?

The source-adapter (`video_loop.sh`) sends frames via ZMQ to a single endpoint (`ZMQ_ENDPOINT`). It cannot multicast to two destinations. Savant cannot consume that ZMQ stream anyway (see above).

### module.yml Dependency

- `module.yml` uses `element: uridecodebin` with `properties.uri: ${parameters.source_uri}`
- This is deeply tied to GStreamer URI decode pipeline
- Changing to a non-URI source would require a **different module.yml pipeline source element** — which is a significant change to how DeepStream initializes

## 3. Topology Options Comparison

### Option A: source-adapter → Replay → Savant (Ideal, per Spec)

```
testVideo/test.mp4
       |
  source-adapter (video_loop.sh → ZMQ)
       |
  replay-service
       |
       +--- REST API (keyframe find, job create)
       |
       +--- ZMQ → Savant (live inference)
       |
       +--- ZMQ → video-file-sink (clip output)
```

**Assessment:**

| Criterion | Result |
|---|---|
| Compose changes | Remove ffmpeg-source, rtsp-server; add ZMQ source to Savant |
| Savant module.yml change | **MAJOR** — must switch from `uridecodebin` to ZMQ-compatible source element |
| source-adapter change | None |
| replay-service change | None (already outputs ZMQ) |
| Phase 3B/3C/3E smoke impact | All smoke tests use `smoke:*` injected events — need to update event injection flow if RTSP removed |
| bbox/snapshot alignment | **YES** — Savant and replay both see the same frame stream |
| GPU required | Yes (Savant unchanged) |
| Risk level | **HIGH** — Savant doesn't have a standard ZMQ source; requires custom GStreamer pipeline or new adapter |
| Recommendation | Preferred architecture per spec, but requires Savant source-side engineering not currently budgeted |

**Verdict:** Architecturally correct but implementation-heavy. Savant's `uridecodebin` cannot be trivially replaced. Would need to develop a custom GStreamer `zmqsrc` element or a bridge adapter that converts ZMQ to RTSP loopback, both of which are non-trivial.

### Option B: Single Source → Dual Output (ffmpeg → RTSP → Both)

```
testVideo/test.mp4
       |
  ffmpeg-source (loops → RTSP)
       |
  rtsp-server (MediaMTX)
       |
       +--- RTSP → savant (uridecodebin)
       |
       +--- RTSP → source-adapter (rtsp.sh) → replay-service
```

**Assessment:**

| Criterion | Result |
|---|---|
| Compose changes | Change source-adapter from `video_loop.sh` to `rtsp.sh`; point its LOCATION to `rtsp://rtsp-server:8554/phase3b` |
| Savant module.yml change | **None** |
| source-adapter change | **Yes** — use `rtsp.sh` instead of `video_loop.sh`, point to same RTSP URL |
| replay-service change | None |
| Phase 3B/3C/3E smoke impact | **MINIMAL** — source_id stays `phase3b`, event flow unchanged |
| bbox/snapshot alignment | **BETTER but not guaranteed** — same RTSP source, but Savant and source-adapter may still start at different times; RTSP is a live stream, not synchronized frame-level |
| GPU required | Yes |
| Risk level | **LOW** — minimal config changes, both consumers pull from the same RTSP stream |
| Recommendation | **Good incremental improvement.** Eliminates the "two independent file loops" problem. Both Savant and source-adapter/replay see the same RTSP frames. However, RTSP is not frame-synchronized — each consumer connects independently and receives frames at connection time. This reduces the time-domain gap from "different loop iterations" to "connection-time offset" (typically < 1 second). |

**Verdict:** Practical near-term improvement. Reduces the fundamental mismatch but doesn't fully solve it because (a) RTSP consumers connect at slightly different times, (b) keyframe lookup is still unbounded.

### Option C: Both Consume Same Live RTSP (No Local File)

Same topology as Option B, but with a live camera RTSP URL instead of a looped file. The "weak synchronization" concern applies equally — RTSP consumers independently connect and receive frames at connection time.

**Assessment:** Same as Option B functionally. For production with real cameras, this is the natural topology. The keyframe lookup problem is independent of whether the source is live or file-based.

### Option D: Disable Real Bbox Overlay (Conservative Fallback)

```
Keep current topology. media-worker draws only labels (event_id, event_type,
camera_id, confidence, timestamp), skips bbox rectangle for bbox_source=savant_detection.
```

**Assessment:**

| Criterion | Result |
|---|---|
| Compose changes | None |
| Savant module.yml change | None |
| source-adapter change | None |
| replay-service change | None |
| Phase 3B/3C/3E smoke impact | None |
| bbox/snapshot alignment | N/A — bbox not drawn |
| GPU required | Yes |
| Risk level | **MINIMAL** — one-line code change in media-worker |
| Recommendation | Conservative stopgap until topology or timestamp mapping is fixed |

**Verdict:** Safe temporary measure. Prevents misleading visual output. Should be paired with a clear plan for re-enabling.

## 4. Timestamp / Alignment Fields — Availability Audit

### Savant Side (person_pose_adapter.py + behavior_event_export_probe.py)

| Field | Available? | Source | Currently Stored? |
|---|---|---|---|
| `frame_meta.frame_num` | **YES** | `person_pose_adapter.py:47` — `frame_id = int(getattr(frame_meta, "frame_num", 0))` | Partially — stored in `event.frame_id` but **not** in `payload.media` |
| `frame_meta.ntp_timestamp` | **YES** (when available) | `person_pose_adapter.py:19-23` — `_get_timestamp_ms()` tries NTP first | Converted to `timestamp_ms` per observation |
| `frame_meta.buf_pts` | **YES** | `person_pose_adapter.py:25-29` — fallback after NTP | Used as `timestamp_ms` when NTP unavailable |
| `frame_meta.source_id` | **YES** | `person_pose_adapter.py:45` — `source_id = str(getattr(frame_meta, "source_id", ""))` | Yes, stored in payload.media.source_id |
| `frame_meta.objects[].track_id` | **YES** | `person_pose_adapter.py:59` | Yes, stored in event.track_id |
| `frame_meta.objects[].object_id` | **YES** (fallback for track_id) | `person_pose_adapter.py:61` | Only used if track_id is None |
| `frame_uuid` (Savant frame UUID) | **UNKNOWN** — not accessed in current code | Not queried from frame_meta | Always set to `None` (`behavior_event_export_probe.py:242`) |
| `keyframe_uuid` (Savant keyframe UUID) | **UNKNOWN** — not accessed in current code | Not queried from frame_meta | Always set to `None` (`behavior_event_export_probe.py:243`) |
| `obj_meta.bbox` | **YES** | `person_pose_adapter.py:68-83` (xc, yc, w, h → x, y, w, h) | Yes, in payload.bbox |
| `obj_meta.confidence` | **YES** | `person_pose_adapter.py:87` | Yes, in event.confidence |

### Replay Side (replay_client.py + metadata.json)

| Field | Available? | Source | Currently Used? |
|---|---|---|---|
| Keyframe UUID (Replay) | **YES** | `POST /api/v1/keyframes/find` response | Used to create job (`anchor_keyframe`) |
| Keyframe PTS | **NO** — API doesn't expose it | Keyframe endpoint returns only UUID | Not available |
| Keyframe source_id | **YES** | Keyframe response: `["source_id", ["uuid"]]` | Used implicitly (filtered by source_id in request) |
| VideoFrame PTS (in clip) | **YES** | metadata.json NDJSON: `"pts": 33333333` (ns) | Not used for alignment |
| VideoFrame DTS | **YES** | metadata.json NDJSON: `"dts": 33333333` (ns) | Not used |
| VideoFrame frame_num | **YES** | metadata.json NDJSON: `"frame_num": 0` | Not used |
| VideoFrame keyframe flag | **YES** | metadata.json NDJSON: `"keyframe": true/false` | Not used |
| VideoFrame metadata.objects | **YES** but always `[]` | metadata.json: `"metadata": {"objects": []}` | Expected empty (Replay bypasses Savant) |
| UUIDv7 timestamp (embedded in keyframe UUID) | **YES** (embedded) | UUIDv7 contains ms-precision timestamp | **NOT decoded or used** |

### Can These Fields Be Written to Event Payload?

**Yes — the schema supports it.** `SecurityEvent` dataclass (`events.py:60-61`) already has:

```python
frame_uuid: Optional[str] = None
keyframe_uuid: Optional[str] = None
```

But they are never populated. `behavior_event_export_probe.py:242-243` hardcodes `None`.

To populate them, we would need to:
1. Query `frame_meta` for a `frame_uuid` attribute (existence unconfirmed — not in current code)
2. If Savant DeepStream doesn't provide a frame_uuid, generate a deterministic one from `source_id + frame_num + ntp_timestamp`
3. Write it to both `event.frame_uuid` and `event.payload.media.frame_uuid`
4. The Replay keyframe UUID is only available AFTER clip-worker processes the event — it cannot be populated at event export time

### Official Savant Documentation Reference

This section summarizes relevant facts from official Savant documentation, to explain why our current topology deviates from the recommended pattern and what needs to change.

**Adapter architecture:**

- Savant adapters are **independent containers**, separate from the module container.
- Adapters communicate with modules via **ZeroMQ + Savant protocol** (not RTSP, not shared filesystem).
- Adapters can transmit: video frames, frame-level metadata, detected objects, and object attributes.
- The module receives adapter input via the pipeline source element.

**Default pipeline source:**

- Savant's default pipeline source is `zeromq_source_bin`, configured via `ZMQ_SRC_ENDPOINT` environment variable.
- When using `zeromq_source_bin`, the module does **not** open its own RTSP connection — it receives frames pushed by the adapter.
- `uridecodebin` (what we currently use) is the **alternative** source for direct RTSP/file consumption without an adapter.
- `pass-through codec=copy` optimization only applies when using `zeromq_source_bin`, not `uridecodebin`.

**Replay Service role:**

- Replay Service sits **between** upstream (source adapter) and downstream (module, sinks).
- It buffers the last N seconds of video frames + metadata into RocksDB.
- It exposes a REST API for keyframe lookup and re-streaming job creation.
- A Replay job can output to a sink adapter (e.g., video-file-sink) or back to a module.

**Module sink behavior:**

- If `output_frame` is omitted from the pipeline, the module does **not** send video frames to downstream sinks.
- This is the normal mode when only metadata/events are needed from the module.

**Why we deviate from the official topology:**

| Official Recommendation | Our Current State | Reason for Deviation |
|---|---|---|
| Adapter sends frames via ZMQ to module | Savant uses `uridecodebin` to pull RTSP directly | Simpler setup for initial GPU pipeline bringup (Phase 1-2); no adapter ZMQ source available in standard DeepStream image |
| Replay sits between adapter and module | Replay is fed by a separate `source-adapter` (video_loop.sh → ZMQ) | We needed Replay for clip generation (Phase 3A+) but had no way to insert it between Savant's `uridecodebin` and the RTSP source |
| Module receives frames from adapter/Replay | Module reads RTSP independently of Replay | DeepStream `uridecodebin` cannot consume ZMQ; no standard `zmqsrc` GStreamer element in the image |
| Event bbox and Replay frames are from the same stream | Bbox is from Savant's RTSP frames; Replay frames are from a separate file loop | Consequence of the dual-ingestion topology — bbox and snapshot are from different loop iterations |

**The consequence:** Real bbox and Replay snapshot are not naturally from the same frame. The bbox coordinates are mathematically correct, but the underlying frame content is from a different point in the video loop.

**Path back to the official topology:** The long-term fix requires either:
1. A custom GStreamer ZMQ source element so Savant can consume directly from Replay/adapter, OR
2. A bridge adapter that converts ZMQ to a URI-decodable stream, OR
3. Using a newer Savant version that supports `zeromq_source_bin` with the DeepStream image

Until then, we document the gap explicitly and do not claim bbox overlay accuracy.

## 5. Recommended Next Step

### Priority: Option B (Single RTSP Source) + Option D (Disable Real Bbox Overlay) Combined

**Recommendation:** Implement **Option B** (unified RTSP ingestion) first, then evaluate if bbox alignment is sufficient. Keep **Option D** as a fallback — if the bbox still doesn't align after Option B, disable real bbox overlay until timestamp-domain mapping is implemented.

### Sequence

```
Phase 3F0.3a: Implement Option B (unified RTSP topology)
  - source-adapter switches from video_loop.sh → rtsp.sh
  - source-adapter reads from rtsp://rtsp-server:8554/phase3b
  - Savant continues reading from same RTSP URL
  - Run full regression (Phase 3B/3C/3E smoke)
  - Evaluate bbox/snapshot alignment with diagnostic frames

Phase 3F0.3b: If alignment is still insufficient
  - Implement Option D: disable real bbox overlay
  - Keep label overlay (event_id, type, camera, confidence)
  - Defer bbox overlay until timestamp-domain mapping is complete

Phase 3F0.4 (future): Timestamp-domain mapping
  - Decode UUIDv7 timestamps from Replay keyframe UUIDs
  - Store bbox_frame_num in event payload.media
  - Establish Savant frame_num ↔ Replay PTS correspondence
  - Anchor keyframe lookup to event_ts_ms (uncomment from_ns/to_ns in replay_client.py)
```

### Why Not Option A (Replay → Savant) Right Now?

Option A is architecturally ideal and matches the spec (`specs/01_architecture.md:30-33`), but requires:
1. A custom GStreamer ZMQ source element for Savant (not in standard image)
2. Or a bridge adapter (ZMQ→RTSP loopback) which adds complexity and latency
3. Significant DeepStream pipeline reconfiguration
4. Risk of GPU pipeline instability during migration

This should be planned as a separate engineering phase, not a hotfix.

### Why Not Timestamp-Domain Mapping First?

Timestamp-domain mapping (making `from_ns`/`to_ns` work correctly) is necessary for production but is a research task:
1. Replay's pipeline-relative timestamps (ns since recording start) don't map to Savant's NTP/PTS timestamps
2. UUIDv7 keyframe UUIDs embed timestamps but the Replay API doesn't expose PTS
3. Even with correct keyframe lookup, the bbox coordinates from Savant frame N might not apply perfectly to replay frame N+M (due to independent RTSP connections)

Option B (unified RTSP source) is the **lowest-risk, highest-impact** change we can make immediately. It eliminates the most fundamental problem: two completely independent loops through the test file.

## 6. Files to Modify (Option B)

| File | Change |
|---|---|
| `infra/docker-compose.phase3b.yml` | Change source-adapter entrypoint from `video_loop.sh` to `rtsp.sh`; change `LOCATION` from `/testVideo/test.mp4` to `rtsp://rtsp-server:8554/phase3b`; remove `DOWNLOAD_PATH` env var (not used by rtsp.sh) |
| `docs/phase3f0_2_diagnosis_report.md` | Already exists — add to git (not debug images) |
| `docs/phase3f0_3_topology_review.md` | This file — add to git |

**No changes to:** Savant module.yml, replay-service config, clip-worker, media-worker, event-worker, API.

### Files to Modify (Option D, if needed)

| File | Change |
|---|---|
| `services/media-worker/app/annotated_snapshot.py` | Skip `_draw_bbox()` when `bbox_source == "savant_detection"` and `SAVANT_BBOX_OVERLAY_ENABLED != "true"` |

## 7. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| rtsp.sh adapter fails to connect to MediaMTX | Low | source-adapter crash, replay has no input | Test connectivity; rtsp.sh is a standard Savant adapter |
| Savant and source-adapter connect at different RTSP times → still small offset | Medium | Bbox still slightly misaligned (sub-second) | Acceptable for MVP; full fix requires timestamp mapping |
| RTSP server becomes single point of failure | Low | Both Savant and replay lose input | Already the case for Savant alone; no regression |
| Phase 3B/3C/3E smoke breaks | Low | source_id or timing changes | Smoke tests inject events directly to Redis — don't depend on video topology |
| source-adapter SOURCE_ID changes | None | source_id stays "phase3b" — same as before | Verify SOURCE_ID env unchanged |

## 8. Git Status (Before Any Changes)

```
Untracked files:
  debug/                    ← Diagnostic images (DO NOT COMMIT)
  manual-inspection/        ← Auto-copied images (DO NOT COMMIT)
  docs/phase3f0_2_diagnosis_report.md   ← NEW — add to git
  docs/phase3f0_3_topology_review.md    ← NEW — add to git (this file)
  modules/savant_phase1d/   ← Unrelated in-progress module
  redis-cli, yolo26n-pose.pt, yolomodel/, temp/  ← Not for commit

No modifications to committed files.
```
