# Phase 2 Completion Report

## Commit History

| Commit | Phase | Description |
|---|---|---|
| `bb811b7` | 2A | Add track state and intrusion rule harness |
| `a667195` | 2B | Integrate intrusion debug probe into Savant runtime |
| `2e3d0f4` | 2C | Stabilize security event schema and dry-run exporter |
| `47eb7e2` | 2D | Add Redis Streams event exporter with factory selection |
| `2929fe2` | 2E | Implement event-worker with Redis Stream consumer and idempotent PostgreSQL ingestion |
| `7e8536c` | 2E.1 | Fix: make track_id TEXT, add pgcrypto extension, verify keyframe_uuid persistence |
| `3a32341` | 2F | Implement FastAPI event query API with filtering and structured responses |
| `5323ae5` | 2G | Add alert publisher and WebSocket alert push endpoint |
| `013b70b` | 2G.1 | Fix: ensure alert created_at is non-empty ISO 8601 timestamp |
| `433fd1f` | 2H | Add event status mutation API with audit logging |
| `69ccc4b` | 2H | Fix: add explicit image tags to compose files, improve smoke pre-flight checks |
| `05f933f` | 2H | Fix: add redis to API requirements, switch postgres to pgvector/pgvector:pg16 |
| `d926436` | 2H | Fix: rewrite phase2h smoke to use curl+docker exec psql, no host pip deps |
| `be24838` | 2H | Fix: use --noproxy '*' and 127.0.0.1 in phase2h smoke to bypass proxy |
| `7d63381` | 2H | Fix: curl --noproxy quoting, separate events vs audit_logs column refs |
| `b42b664` | 2H | Fix: psql INSERT output parsing, use unique SID, validate UUID |
| `7cd507b` | 2I | Add Savant-to-backend end-to-end deployment smoke |

## Phase Goals

| Phase | Goal |
|---|---|
| 2A | Pure Python track state + intrusion rule engine (unit-testable) |
| 2B | Savant runtime integration: intrusion debug probe |
| 2C | SecurityEvent schema v1.0, dry-run event exporter |
| 2D | Redis Streams event exporter (`EVENT_EXPORTER=redis`) |
| 2E | Event-worker: Redis Stream consumer → PostgreSQL (idempotent) |
| 2F | FastAPI REST API: event query, filtering, structured responses |
| 2G | Alert publisher: security.alerts stream + WebSocket push |
| 2H | Event status mutation API: acknowledge/confirm/resolve + audit_logs |
| 2I | Savant-to-backend full pipeline deployment smoke |

## Three Types of Acceptance Tests

### 1. Pytest Code-Level Tests

- **Scope**: Unit tests for Python modules (rules, models, exporters, repositories, API endpoints)
- **Environment**: Local Python, no Docker, fakeredis, TestClient
- **Count**: 111 tests across 7 suites
- **Command**: `pytest` (suites must run separately due to module name conflicts)

```
Phase 2A: 22 passed   (track state + intrusion rule)
Phase 2C: 17 passed   (SecurityEvent schema + dry-run exporter)
Phase 2D: 12 passed   (Redis Stream exporter)
Phase 2E: 21 passed   (event-worker: consumer, repository, worker)
Phase 2F: 17 passed   (FastAPI event query endpoints)
Phase 2G:  8 passed   (alert publisher)
Phase 2H: 14 passed   (status mutation + audit logs)
Total:  111 passed, 0 failed
```

### 2. Backend-Only Deployment Smoke

- **Scope**: Docker Compose with redis + postgres + event-worker + api
- **No GPU required**: Tests backend pipeline only (no Savant video)
- **Command**: `sudo bash scripts/smoke/check_phase2h_event_status_api.sh`
- **Result**: 23/23 checks passed, 0 failed

Infrastructure verified:
- Container health (redis, postgres, api, event-worker)
- Schema (events table, audit_logs table)
- API endpoints (/health, /ready, /api/v1/events/recent, /api/v1/events/{id})
- Status mutation (acknowledge → confirm → resolve)
- Status transition validation (resolved → acknowledge returns 409)
- Audit logging (event.acknowledge, event.confirm, event.resolve)
- Media fields preserved (snapshot_status, clip_status, recording_strategy)
- Idempotency (source_event_id UNIQUE)

### 3. Savant-to-Backend Full Pipeline Deployment Smoke

- **Scope**: Docker Compose with all backend services + rtsp-server + ffmpeg-source + Savant (GPU)
- **GPU required**: NVIDIA T4 for YOLO26-pose TensorRT inference
- **Command**: `sudo bash scripts/smoke/check_phase2i_savant_to_backend.sh`
- **Result**: 15/15 checks passed, 0 failed

Full pipeline verified:
- Savant produces SecurityEvent → Redis security.events
- Event-worker consumes → PostgreSQL events table
- FastAPI serves event via REST API
- Event-worker publishes alert → Redis security.alerts
- Media fields preserved
- Idempotency verified
- First real intrusion event produced:
  ```
  source_event_id: savant_phase2c:cam_01:530:intrusion:1779563287046
  event_type:      intrusion
  camera_id:       cam_01
  track_id:        530
  ```

## Smoke Script Debug Output

Both smoke scripts (`check_phase2h_event_status_api.sh` and `check_phase2i_savant_to_backend.sh`) contain intentional `[debug]` output lines prefixed with `${BLUE}`. These are **intentional** — they provide SID, UUID, HTTP status codes, and DB state at each step. This output is critical for diagnosing deployment failures. The debug lines do not affect check pass/fail counting.

## Known Technical Debt

### TODO: Savant container runtime `pip install redis`

- **Location**: `infra/docker-compose.phase2i.yml`, savant service entrypoint
- **Current workaround**: `pip install -q redis && exec python -m savant.entrypoint module.yml`
- **Problem**: Runs pip on every container start; adds startup latency; network-dependent
- **Preferred fix**: Build a custom Docker image extending `ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1` with redis-py pre-installed, OR implement a zero-dependency Redis exporter using raw sockets

### TODO: Module name conflicts in pytest

- **Problem**: `custom.models.events` exists in both `modules/savant_phase2a` and `modules/savant_phase2c`; `app` package used by both event-worker and api
- **Workaround**: Run test suites in separate pytest processes
- **Preferred fix**: Namespace packages or unique module names per phase

## Final State

- **SecurityEvent schema v1.0**: Unchanged throughout Phase 2
- **Media fields**: `snapshot_status=not_implemented`, `clip_status=not_implemented`, `recording_strategy=reserved` (MVP policy per CLAUDE.md §8)
- **No snapshot/clip generated**: Reserved for future Replay/clip-worker phase
- **No frontend, Replay, face-worker, clip-worker, SCRFD, ArcFace, pgvector**: Not implemented
- **Git status**: Clean (only pre-existing untracked files)
