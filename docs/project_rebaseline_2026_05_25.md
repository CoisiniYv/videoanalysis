# Project Rebaseline — 2026-05-25

## 1. Purpose

This document captures the ground truth of what has been built, what is missing, what diverged from the original plan, and what the revised mainline order should be. It reflects the state after completing Phase 3H (Official ZMQ Adapter Ingestion) and its two sub-phases (3H.1 metadata sink, 3H.2 video file sink).

**This document is authoritative for all future Claude Code sessions in this repo.**

---

## 2. Completed Capabilities

### Phase 1: Main Video Pipeline

| Capability | Status | Evidence |
|---|---|---|
| YOLO26-pose ONNX inference (fp16) | DONE | modules/savant_phase1d through 1f |
| nvtracker track_id assignment | DONE | NvDCF tracker, track_id persistence verified |
| PersonPoseObservation extraction | DONE | person_pose_adapter.py |
| Metadata handoff probe (frame metadata) | DONE | modules/savant_phase1e |
| 17 COCO keypoints per person | DONE | YOLO26-pose converter verified |

### Phase 2: Behavior Rules & Backend

| Capability | Status | Evidence |
|---|---|---|
| Intrusion rule | DONE | custom/rules/intrusion.py — verified |
| SecurityEvent schema | DONE | custom/models/events.py |
| Redis security.events export | DONE | RedisStreamEventExporter |
| event-worker (Redis → PostgreSQL) | DONE | services/event-worker |
| PostgreSQL events table | DONE | db/migrations |
| FastAPI events API | DONE | services/api |
| WebSocket alerts | DONE | alert/ws baseline |
| Event status mutations & audit | DONE | Phase 2H |
| Savant → backend end-to-end | DONE | Phase 2I smoke verified |

### Phase 3A-3E: Replay/Media Historical Validation Chain

| Capability | Status | Evidence |
|---|---|---|
| Replay + Video File Sink POC | DONE | Phase 3A compose/smoke |
| clip-worker (record_request → Replay REST) | DONE | services/clip-worker |
| media-worker (snapshot + clip) | DONE | services/media-worker |
| clip_url in API | DONE | Phase 3B smoke |
| snapshot_url in API | DONE | Phase 3C smoke |
| annotated_snapshot_url in API | DONE | Phase 3E smoke |
| SAVANT_BBOX_OVERLAY_ENABLED guard | DONE | Env var controlled |

**IMPORTANT**: Phase 3A-3E uses a **Replay bypass topology** (dual independent file loops). This is NOT the current production mainline. It is a historical validation chain preserved for reference.

### Phase 3F0: Real Savant Bbox Verification

| Capability | Status | Evidence |
|---|---|---|
| bbox_source=savant_detection in DB | DONE | 2936+ events verified |
| Real Savant bbox (not smoke-injected) | DONE | bbox values from GPU inference |
| Postgres persistence hotfix | DONE | phase3b compose volume added |
| Bbox alignment diagnosis | DONE | diagnosis report — root cause found |
| Topology review (4 options) | DONE | topology review doc |

### Phase 3H: Official ZMQ Adapter POC

| Capability | Status | Evidence |
|---|---|---|
| zeromq_source_bin (official source) | DONE | Phase 3H smoke 13/13 |
| source-adapter → ZMQ → Savant | DONE | Redis events + PostgreSQL verified |
| real intrusion events with track_id | DONE | ~6700 events, source_id=phase3h |
| official metadata sink (NDJSON) | DONE | Phase 3H.1 smoke 13/13 |
| real objects with bbox/track_id/keypoints | DONE | 44,000+ frames verified |
| official video file sink (video.mov) | DONE | Phase 3H.2 smoke 13/13 |
| video + metadata same-frame output | DONE | h264 NVENC, 1920x1080 @ 30fps |
| evidence_frame.jpg — manual verified | DONE | Frame 200, 14 person bboxes drawn |
| continuous output warning documented | DONE | stack stopped, sizes recorded |

---

## 3. Incomplete Capabilities

### Behavior Rules (only intrusion done)

| Rule | Status |
|---|---|
| intrusion | DONE |
| loitering | NOT STARTED |
| crowd_gathering | NOT STARTED |
| fall (initial) | NOT STARTED |
| running / chasing | NOT STARTED |
| wall_climb / line_crossing | NOT STARTED |

### Configuration System

| Capability | Status |
|---|---|
| camera_zones / camera_rules API | NOT STARTED |
| config export / Savant config sync | NOT STARTED |
| source_id → camera_id production mapping | PARTIAL (hardcoded in cameras.yml) |
| ROI per camera | NOT STARTED |

### Face Intelligence Pipeline

| Capability | Status |
|---|---|
| SCRFD_2.5G face detection | NOT STARTED |
| Face quality filter | NOT STARTED |
| ArcFace embedding | NOT STARTED |
| pgvector face search | NOT STARTED |
| face_observations table | NOT STARTED |
| persons / gallery API | NOT STARTED |
| watchlist_rules | NOT STARTED |
| watchlist_hit event | NOT STARTED |
| live_search_jobs | NOT STARTED |
| live_search_hit event | NOT STARTED |

---

## 4. Plan Deviation Analysis

### 4.1 Behavior rules incomplete

The original plan (CLAUDE.md Section 5) had Phase 2 covering intrusion, loitering, crowd_gathering, and fall. Only intrusion was completed. The other rules were deferred while Phase 3A-3H (Replay, media, alignment, ZMQ POC) consumed all development bandwidth.

### 4.2 Face pipeline deferred beyond plan

The original plan had Phase 3 as the face pipeline (SCRFD → ArcFace → pgvector), Phase 4 as watchlist, and Phase 5 as live search. The actual Phase 3 work was dominated by Replay/media/snapshot/annotated-snapshot/bbox-alignment/official-ZMQ-POC — none of which was face-related.

Phase 3H.2 produced valuable architecture validation (single-ingestion ZMQ frame+metadata stream), but the face pipeline must not be pushed further into the indefinite future.

### 4.3 Media over-investment

Replay, clip-worker, media-worker, snapshot, and annotated snapshot were all validated and are worth keeping. But media POC should be frozen at Phase 3H.2 rather than extended indefinitely. The continuous sink warning in the Phase 3H.2 docs explicitly marks this boundary.

---

## 5. Technical Debt

### Repository hygiene

| Debt | Detail |
|---|---|
| Module proliferation | 11 modules/ directories, many historical phase snapshots |
| Compose proliferation | 17 docker-compose files, many for phased smoke tests only |
| No single main module | savant_security exists but is not the sole active module |
| Untracked artifacts | debug/, manual-inspection/, media/, temp/, model files, binaries |
| Model files in tree | yolo26n-pose.pt, yolomodel/ — should not enter Git |

### Architecture debt

| Debt | Detail |
|---|---|
| Phase 3B Replay bypass topology | Dual independent file loops — documented as temporary, NOT production |
| uridecodebin deviation | Phase 3B Savant uses uridecodebin for RTSP — official path is zeromq_source_bin |
| Phase 3H ZMQ topology | Proved in POC, needs convergence into main module |
| Replay config sync | Replay config and compose env must be manually kept in sync |
| frame_uuid / keyframe_uuid | Still None — not yet populated for production alignment |
| Runtime pip install | Savant container runs `pip install redis` on startup |
| source_id / camera_id mapping | Hardcoded in cameras.yml, not production-grade |

---

## 6. Revised Mainline Order

Phase 3H.2 is the **last media POC phase**. Media expansion is frozen after this point. The Phase 3H ZMQ topology is preserved as the future production reference.

### Recommended sequence

```
R0: Project Rebaseline (THIS PHASE)
  - docs, .gitignore, roadmap corrections
  - No functional changes

R1: Module / Compose Consolidation
  - Plan documented; execution deferred to dedicated phase
  - Target: savant_security as sole main module
  - Target: fewer compose files with profiles

B2.1: Loitering Rule
  - Pure Python rule module
  - Unit tests
  - Smoke test

B2.2: Crowd Gathering Rule
  - Pure Python rule module
  - Unit tests
  - Smoke test

B2.3: Fall Rule (Initial)
  - Pure Python rule module
  - Unit tests
  - Smoke test

C1: Camera Zones / Rules API
  - REST API for camera ROI configuration
  - Config sync to Savant module

F1: SCRFD Face Detection
  - ONNX model integration
  - Face bounding box + confidence
  - face_observations table

F2: ArcFace + pgvector
  - ArcFace embedding model
  - pgvector index + similarity search
  - Face quality filter

F3: Watchlist / Live Search
  - persons table + gallery API
  - watchlist_rules + watchlist_hit
  - live_search_jobs + live_search_hit

Phase 4+: Production Hardening
  - 60-camera performance
  - GPU memory stability
  - Monitoring / Grafana
```

### Frozen (not expanding further at this time)

- Replay Service media pipeline
- clip-worker / media-worker
- annotated snapshot / bbox overlay
- video-file-sink continuous output
- Official ZMQ metadata/video adapters (POC complete)

Phase 3H ZMQ topology is preserved as architecture reference for future production single-ingestion design.

---

## 7. Preservation Policy

The following are preserved but NOT the mainline:

| Artifact | Policy |
|---|---|
| Historical phase modules (phase1d through phase2c) | Read-only, archive candidates |
| Historical phase composes (phase1c through phase2i) | Read-only, archive candidates |
| Phase 3A-3E compose/smoke | Preserved for media validation reference |
| Phase 3B Replay topology | Documented as temporary, NOT production |
| savant_phase3h_zmq module | Preserved for official ZMQ topology reference |
| savant_replay, savant_video_file_sink, savant_smoke | Preserved for reference |

---

*Written 2026-05-25. Reflects state after Phase 3H.2 completion and stack teardown.*
