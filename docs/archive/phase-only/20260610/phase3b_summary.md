# Phase 3B — Replay Media Reliability & Contract Hardening Summary

## Goal

Harden the Phase 3A media chain for reliability, idempotency, debuggability, and multi-camera source_id readiness. No annotated clips or frontend overlay.

## Changes by Area

### 1. Source ID Formalization

**Removed hardcoded `phase3a` fallback** from `_resolve_source_id()`. The function now receives `default_replay_source_id` as an explicit parameter from `Config`, not via hidden `os.getenv` call.

Priority chain unchanged:
1. `payload.media.source_id` (per-event override)
2. `event.source_id` (from event metadata)
3. `Config.default_replay_source_id` (from `DEFAULT_REPLAY_SOURCE_ID` env var)
4. Returns `""` if nothing configured — record_request is skipped with a clear log

The same logical camera/source must produce the same Replay source_id everywhere. When `payload.media.source_id` is set, it takes priority; otherwise the compose-level env var governs all events consistently.

### 2. Record Request Idempotency

**Before publishing a record_request**, the event-worker now checks the DB for existing `clip_status`. If already set (pending, replay_job_created, ready, failed), the publish is skipped. This prevents duplicate record_requests when:

- The same event is re-delivered after a crash
- An event is re-injected with the same `source_event_id`

**Media status transitions** defined and tracked:

| clip_status | Set by | Meaning |
|---|---|---|
| `not_implemented` | DB default | No recording configured |
| `pending` | event-worker | Record request published |
| `replay_job_created` | clip-worker | Replay job accepted |
| `ready` | media-worker | Sink output available |
| `failed` | clip-worker / event-worker | Keyframe lookup or job creation failed |
| `not_required` | (reserved) | Event doesn't need clip |

`snapshot_status` remains `not_implemented` throughout Phase 3B.

### 3. Clip-Worker DB Write-Back

Clip-worker now connects to PostgreSQL and writes back:
- `clip_status = "replay_job_created"` + `replay_job_id` on success
- `clip_status = "failed"` + `error_message` on keyframe lookup or job creation failure

New dependencies: `psycopg[binary]>=3.0.0`, `DATABASE_URL` env var.

### 4. Media-Worker Idempotency

**`processed_dirs` set is now functional.** Directories are tracked in-memory after processing and skipped on subsequent polls.

Additional DB-level guard: before UPDATE, checks if event already has `clip_status = "ready"`. If so, skips the UPDATE and marks the directory as processed.

Missing `event_id` now produces an `ERROR`-level log with `source_id`, `resulting_stream_id`, and `meta_dir` context.

### 5. API Media Fields

`_safe_media()` now includes all media fields with proper defaults. The `EventResponse.media` dict always contains:
```
clip_status, snapshot_status, recording_strategy,
replay_job_id, sink_output_path, error_message
```
Missing fields default to `null` rather than being absent.

### 6. Queue Inspection Script

`scripts/util/inspect_record_requests.sh` — supports 4 commands:
- `stats` — stream length, events by clip_status, failed jobs
- `pending` — show pending record_requests
- `failed` — show events with clip_status=failed
- `clear-pending` — safely clear pending stream (with confirmation)

### 7. Phase 3B Compose

New `infra/docker-compose.phase3b.yml` with phase3b-* container names, offset ports, and explicit `DEFAULT_REPLAY_SOURCE_ID=phase3b`.

## Test Results

### Unit Tests (102 passed, 0 failed)

| Phase | File | Count |
|---|---|---|
| 2D | test_phase2d_redis_exporter.py | 12 |
| 2F | test_phase2f_api_events.py | 17 |
| 2H | test_phase2h_event_status_api.py | 14 |
| 3A | test_phase3a_record_request.py | 9 |
| 3A | test_phase3a_replay_client.py | 5 |
| 3A | test_phase3a_api_media_urls.py | 5 |
| **3B** | **test_phase3b_source_id_mapping.py** | **10** |
| **3B** | **test_phase3b_record_request_idempotency.py** | **6** |
| **3B** | **test_phase3b_clip_worker_failure.py** | **7** |
| **3B** | **test_phase3b_media_worker_dedup.py** | **12** |
| **3B** | **test_phase3b_api_media_fields.py** | **5** |

New Phase 3B tests: 40. Total suite: 102.

### Phase 2 Regression Gate

| Phase | Tests | Result |
|---|---|---|
| 2D (Redis exporter) | 12 | PASS |
| 2F (API events) | 17 | PASS |
| 2H (Event status API) | 14 | PASS |

Phase 2H/2I docker smoke requires compose stack — documented as known dependency.

### Phase 3B Smoke (18 checks)

Script: `scripts/smoke/check_phase3b.sh`

1. Compose file exists
2. Replay /api/v1/status = 200
3. Event injected into security.events
4. Event in PostgreSQL
5. Idempotent: count=1 after duplicate
6. security.record_requests has messages
7. media.clip_status = ready
8. replay_job_id stored
9. sink_output_path stored
10. clip_url HTTP 200
11. API exposes media.recording_strategy
12. API exposes media.clip_status
13. API exposes media.replay_job_id
14. media-worker re-scan idempotent (clip_path unchanged)
15. Failed event still inserted in DB
16. Failed event has clip_status=failed
17. Failed event has error_message
18. Failed event count=1 after duplicate

## Modified Files

| File | Change |
|---|---|
| `services/event-worker/app/config.py` | Added `default_replay_source_id` field |
| `services/event-worker/app/record_request.py` | `_resolve_source_id()` accepts explicit default; removed `os.getenv` |
| `services/event-worker/app/worker.py` | Passes `default_replay_source_id`; idempotent record_request with DB check |
| `services/event-worker/app/repository.py` | Added `get_media_clip_status()`, `set_clip_status()` |
| `services/clip-worker/app/config.py` | Added `database_url` field |
| `services/clip-worker/app/worker.py` | Writes back clip_status/replay_job_id/error_message to DB |
| `services/clip-worker/app/repository.py` | **New** — `update_clip_status()` function |
| `services/clip-worker/main.py` | Added PostgreSQL connection |
| `services/clip-worker/requirements.txt` | Added `psycopg[binary]>=3.0.0` |
| `services/media-worker/app/worker.py` | `processed_dirs` functional; `_is_already_ready()` guard; error logging for missing event_id |
| `services/api/app/schemas/events.py` | `_safe_media()` includes `replay_job_id`, `sink_output_path`, `error_message` with null defaults |
| `infra/docker-compose.phase3a.yml` | Added `DATABASE_URL` + postgres dependency to clip-worker |
| `infra/docker-compose.phase3b.yml` | **New** — Phase 3B compose stack |
| `harness/tests/test_phase3a_record_request.py` | Updated to new `_resolve_source_id(event, default)` signature |
| `harness/tests/test_phase3b_source_id_mapping.py` | **New** — 10 tests |
| `harness/tests/test_phase3b_record_request_idempotency.py` | **New** — 6 tests |
| `harness/tests/test_phase3b_clip_worker_failure.py` | **New** — 7 tests |
| `harness/tests/test_phase3b_media_worker_dedup.py` | **New** — 12 tests |
| `harness/tests/test_phase3b_api_media_fields.py` | **New** — 5 tests |
| `scripts/smoke/check_phase3b.sh` | **New** — 18-check smoke script |
| `scripts/util/inspect_record_requests.sh` | **New** — queue inspection/cleanup helper |

## Remaining Technical Debt

| Item | Priority | Phase |
|---|---|---|
| Savant container runtime `pip install redis` | High | 2I |
| Replay config.json must be manually kept in sync with compose env vars | Medium | 3B+ |
| source_id mapping from Savant events to Replay sources still env-based, not dynamic | Medium | 3B+ |
| media-worker polls filesystem; no event-driven notification | Low | 3B+ |
| clip-worker `find_keyframe` ignores event timestamp (always returns most recent keyframe) | Medium | 3C |
| No dead-letter queue for unprocessable record_requests after failed status | Low | 3C |
| clip-worker consumer name is static (single instance only) | Low | 3C |
| Phase 2H/2I docker smoke not run in this phase (requires compose stack + GPU) | Low | Gate |
