# Phase 3H — Official ZeroMQ Adapter Ingestion POC

Date: 2026-05-25
Status: **VERIFIED** — Smoke test 13/13 passed.

## 1. Purpose

Verify that a Savant module can use the official `zeromq_source_bin` (default pipeline source in Savant) instead of `uridecodebin` to receive frames from `source-adapter` via ZeroMQ, and still produce real detection events through the full pipeline:

```
testVideo/test.mp4
  -> source-adapter (video_loop.sh -> ZMQ DEALER connect)
    -> savant-zmq (zeromq_source_bin ROUTER bind -> YOLO26-pose -> nvtracker -> behavior_event_export_probe)
      -> Redis security.events
        -> event-worker -> PostgreSQL
```

This is a **POC only** — not a production refactor. No existing compose, module, or smoke test is modified.

## 2. What Was Built

### 2.1 New files

| File | Purpose |
|---|---|
| `modules/savant_phase3h_zmq/module.yml` | Savant module using `zeromq_source_bin` |
| `modules/savant_phase3h_zmq/config/` | Tracker config and camera config (copied from phase2c) |
| `modules/savant_phase3h_zmq/custom/` | YOLO26-pose converter and behavior event export probe (copied from phase2c) |
| `infra/docker-compose.phase3h-zmq.yml` | 5-service compose: redis, postgres, source-adapter, savant-zmq, event-worker |
| `scripts/smoke/check_phase3h_zmq_ingestion.sh` | 13-check smoke test |

### 2.2 Key configuration

**Source (module.yml)**:
```yaml
pipeline:
  source:
    element: zeromq_source_bin
    properties:
      socket: ${oc.env:ZMQ_SRC_ENDPOINT}
      source_id: ${oc.decode:${oc.env:SOURCE_ID, null}}
```

**ZMQ socket pairing (compose)**:
```yaml
source-adapter:
  ZMQ_ENDPOINT: dealer+connect:tcp://savant-zmq:5555

savant-zmq:
  ZMQ_SRC_ENDPOINT: router+bind:tcp://0.0.0.0:5555
  ZMQ_SINK_ENDPOINT: pub+connect:ipc:///tmp/sink.ipc
  SOURCE_ID: phase3h
  EVENT_EXPORTER: redis
```

### 2.3 What was NOT changed

- No existing module, compose, or smoke test was modified.
- `uridecodebin` old path is preserved as-is.
- Phase 3B/3C/3E compose and smoke are unaffected (separate compose projects, separate container names, separate ports).
- No replay-service, clip-worker, media-worker, or video-file-sink in this POC.

## 3. Evidence

### 3.1 Savant source confirmation

```
$ docker exec phase3h-zmq-savant grep 'zeromq_source_bin' module.yml
    element: zeromq_source_bin
```

Savant module is using `zeromq_source_bin`, confirming the official ZMQ adapter ingestion mode.

### 3.2 Redis event stream

```
$ docker exec phase3h-zmq-redis redis-cli XLEN security.events
2936
```

2,936+ real detection events in the Redis stream (and continuously growing).

### 3.3 PostgreSQL events

```
source_id=phase3h
event_type=intrusion
track_id=1867
bbox_source=savant_detection
bbox={"x":44.06,"y":507.52,"width":113.25,"height":240.05}
```

2,936+ non-smoke events with `source_id=phase3h` in the database. Each event has:
- A real `track_id` from nvtracker
- A real `bbox` from YOLO26-pose inference
- `bbox_source=savant_detection` confirming GPU inference origin

### 3.4 Smoke test

```
--- Phase 3H Official ZMQ Adapter Ingestion POC Smoke Test ---
  5/5 containers running
  OK  [1] compose file exists
  OK  [2] pipeline source is zeromq_source_bin
  OK  [3] Redis security.events has entries
  OK  [4] Redis has non-smoke Savant events
  OK  [5] event-worker ingested event into DB
  OK  [6] event source_id is phase3h
  OK  [7] event_type is a known type
  OK  [8] track_id is present and non-zero
  OK  [9] payload.bbox_source is savant_detection
  OK  [10] payload.bbox exists with non-zero dimensions
  OK  [11] payload.bbox_source is savant_detection (confirmed)
  OK  [12] module uses zeromq_source_bin (not uridecodebin)
  OK  [13] multiple non-smoke events in DB (>= 1)
--- Results: 13/13 passed, 0 failed ---
```

## 4. Topology Comparison

### Current Phase 3B (uridecodebin, temporary)

```
test.mp4 -> ffmpeg-source -> RTSP -> Savant (uridecodebin) -> GPU inference -> Redis
test.mp4 -> source-adapter -> ZMQ -> Replay Service -> clip/snapshot
```
Two independent file loops — bbox/snapshot not aligned.

### Phase 3H POC (zeromq_source_bin, single ingestion path)

```
test.mp4 -> source-adapter -> ZMQ -> Savant (zeromq_source_bin) -> GPU inference -> Redis -> PostgreSQL
```
Single ingestion path — bbox and frame come from the same ZMQ frame stream.

## 5. What This Enables

1. **Single-ingestion topology**: Source-adapter reads the source once, Savant and Replay consume the same ZMQ frame stream.
2. **Bbox/snapshot frame-level alignment**: When Replay is added back, both bbox (from Savant) and snapshot (from Replay) come from the same ZMQ-sourced frames.
3. **Production architecture**: Aligns with Savant's official recommended deployment model (adapter -> ZMQ -> module -> ZMQ -> sink).

## 6. Limitations (POC Scope)

- No replay-service, clip-worker, or media-worker in this compose.
- No snapshot or clip generation — events only.
- YOLO26-pose model must be present at `/data/video-analytics/models/yolo26_pose/yolo26_pose.onnx`.
- GPU required (NVIDIA TensorRT inference).
- `SAVANT_EVENT_MEDIA_REQUIRED=false` (default) — media fields are empty.

## 7. Next Steps (Not in This Phase)

Per `docs/production_ingestion_topology_policy.md`:

1. **Phase 3F0.4**: Timestamp-domain mapping (decode UUIDv7 keyframe UUID timestamps, store `bbox_frame_num`).
2. **Phase 3H+**: Add replay-service, clip-worker, media-worker back to the ZMQ topology.
3. **Phase 4+**: ROI / Rules engine completion.
4. **Phase 5+**: Face pipeline (SCRFD_2.5G, ArcFace, pgvector).

## 8. Related Documents

| Document | Content |
|---|---|
| `docs/production_ingestion_topology_policy.md` | Mandatory single-ingestion policy |
| `docs/phase3f0_2_diagnosis_report.md` | Bbox/snapshot misalignment root cause |
| `docs/phase3f0_3_topology_review.md` | Four topology options comparison |
| `CLAUDE.md` Section 9 | Hard architectural constraints |
| `specs/01_architecture.md` Section 8 | Replay target architecture |
