# Phase 3B.2 Docker Compose Integration Smoke

Date: 2026-05-24

## Commands Run

```bash
# Verify Docker access
docker ps
docker compose version   # v2.29.7

# Clean previous stack
docker compose -f infra/docker-compose.phase3b.yml down --remove-orphans

# Build changed services
docker compose -f infra/docker-compose.phase3b.yml build --no-cache event-worker api clip-worker media-worker

# Start base media stack
docker compose -f infra/docker-compose.phase3b.yml up -d postgres redis rtsp-server replay-service source-adapter video-file-sink

# Wait for healthy, then start workers and API
docker compose -f infra/docker-compose.phase3b.yml up -d event-worker api clip-worker media-worker

# Verify services
docker ps --filter "name=phase3b" --format "table {{.Names}}\t{{.Status}}"

# Run smoke
bash scripts/smoke/check_phase3b.sh
```

## Manual Verification Summary

- Docker access confirmed: `docker ps` and `docker compose version` work without sudo (user added to `docker` group).
- All 10 phase3b containers running.
- `scripts/smoke/check_phase3b.sh`: **18 passed, 0 failed**.
- clip-worker logs show `keyframe_lookup_anchored` with `event_ts_ms`, `from_ns`, `to_ns`.
- `replay_job_created` succeeded with valid job_id.
- DB success event: `clip_status=ready`, `recording_strategy=savant_replay`, `replay_job_id` present, `sink_output_path` present, `clip_path` present.
- DB failure event: `clip_status=failed`, clear `error_message`.
- clip_url HTTP 200: `curl -I http://127.0.0.1:8001/media/replay-sink-output/.../video.mov` returned `HTTP/1.1 200 OK`.

## Services Status

All 10 phase3b containers running:

| Container | Status |
|---|---|
| phase3b-api | Up |
| phase3b-clip-worker | Up |
| phase3b-event-worker | Up |
| phase3b-media-worker | Up |
| phase3b-source-adapter | Up |
| phase3b-replay-service | Up |
| phase3b-postgres | Up (healthy) |
| phase3b-redis | Up (healthy) |
| phase3b-video-file-sink | Up |
| phase3b-rtsp-server | Up |

## Smoke Result

```
--- Results: 18 passed, 0 failed ---
```

All 18 checks pass:

1. compose file exists
2. Replay /api/v1/status (HTTP 200)
3. Injected event into security.events
4. Event persisted in PostgreSQL
5. Idempotent: count=1 after duplicate injection
6. security.record_requests has messages
7. media.clip_status is `ready`
8. replay_job_id stored
9. sink_output_path stored
10. API returns clip_url and curl 200
11. API exposes media.recording_strategy = `savant_replay`
12. API exposes media.clip_status = `ready`
13. API exposes media.replay_job_id
14. media-worker re-scan idempotent (clip_path unchanged)
15. Failed event still inserted in DB
16. Failed event has clip_status=failed
17. Failed event has error_message
18. Failed event still count=1 after duplicate

## Timestamp Anchoring Evidence

clip-worker logs show `keyframe_lookup_anchored` for each event,
deriving `from_ns`/`to_ns` from `event_ts_ms`:

```
keyframe_lookup_anchored source_id=phase3b event_ts_ms=1779624583584 window_s=10 from_ns=1779624573584000000 to_ns=1779624593584000000
replay_job_created job_id=019e59e4-21e9-7861-b894-13e4f44fef9d
```

The `from_ns` and `to_ns` are computed as epoch nanoseconds from `event_ts_ms`.
Because Replay DB timestamps are pipeline-relative (CLOCK_MONOTONIC), the actual
request omits `from`/`to` for an unbounded search until timestamp-domain mapping
is established (tracked as remaining technical debt).

## clip_url HTTP Result

```
clip_url=/media/replay-sink-output/replay-event-d9d7b6c1-3a8b-4f1e-98b0-653f1a172f53-00000000/video.mov
curl http://127.0.0.1:8001/media/... HTTP 200
```

## DB Media Fields

Last successful event:

| Field | Value |
|---|---|
| source_event_id | smoke:phase3b:... |
| clip_path | /media/replay-sink-output/.../video.mov |
| clip_status | ready |
| recording_strategy | savant_replay |
| replay_job_id | 019e59e4-21e9-7861-b894-13e4f44fef9d |
| sink_output_path | /media/replay-sink-output/... |

Failed path event:

| Field | Value |
|---|---|
| clip_status | failed |
| error_message | no keyframe found for source_id=nonexistent_source_xyz event_ts_ms=... |

## Modified Files

1. `infra/docker-compose.phase3b.yml` — set `SYNC_OUTPUT: "true"` for source-adapter
2. `services/clip-worker/app/replay_client.py` — fix `from`/`to` format (u64 integers); parse `new_job` response field; unbounded keyframe search until timestamp-domain mapping established
3. `services/clip-worker/app/worker.py` — log keyframe_provided_directly path; pass event_ts_ms to keyframe lookup
4. `services/media-worker/app/worker.py` — don't overwrite replay_job_id with empty string; try `new_job` metadata field
5. `scripts/smoke/check_phase3b.sh` — fix variable name collision (FAIL_COUNT → FAIL_ROW_COUNT); fix `set -e` interaction with final check

## Remaining Technical Debt

1. **Timestamp-domain mismatch**: `event_ts_ms` is business/epoch-like time; Replay DB uses pipeline-relative / CLOCK_MONOTONIC time. Keyframe lookup currently omits `from`/`to` (unbounded search) until the domain mapping is established. Fix: source adapter must produce epoch-anchored timestamps, or clip-worker must derive the mapping at startup.

2. **Snapshot not implemented**: `snapshot_status` is `not_implemented`. No snapshot extraction from the replay sink output yet. MVP smoke accepts this gap.

3. **Media-worker uses polling**: `media-worker` polls `SINK_OUTPUT_DIR` on a periodic interval (`MEDIA_POLL_INTERVAL_S`). Inotify-based or event-driven notification would reduce latency and IO.

4. **No DLQ for unprocessable record_requests**: Record requests that fail irrecoverably (e.g. invalid source_id, Replay API errors) are marked `clip_status=failed` in the events table but the record_request stream message is ACK'd with no dead-letter queue. A DLQ would allow later inspection and retry.

5. **Source adapter timestamp config**: `SYNC_OUTPUT=true` currently syncs to system monotonic clock, not epoch. A source adapter change or config option to produce epoch-anchored timestamps would close the timestamp-domain gap.

6. **Smoke test idempotency under load**: The smoke test's check 18 verifies idempotency for a single duplicate, but concurrent workers under load have not been tested.

## Git Status

6 files committed in `ff6e3c4`; 7 untracked files (unrelated: phase1d modules, yolo model, redis-cli binary).
