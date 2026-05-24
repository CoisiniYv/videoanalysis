# Phase 3C — Event Snapshot MVP

Date: 2026-05-25

## Design Goal

Generate an event snapshot from an already-ready Replay clip, store `events.snapshot_path`, expose `snapshot_url` from FastAPI, and verify `curl snapshot_url` returns HTTP 200.

## Snapshot Extraction Policy

- Clip offset = `pre_seconds` from payload.media (default `DEFAULT_PRE_SECONDS` = 5).
- NOT random, NOT always first frame, NOT always last frame.
- Extraction uses `imageio-ffmpeg` (static ffmpeg binary, pip-installable).
- Duration detected via ffmpeg stderr parsing (no ffprobe dependency).
- Fallback when clip duration <= pre_seconds: offset = max(0, duration / 2).
- Fallback reason recorded in `payload.media.snapshot_fallback_reason`.
- `payload.media.snapshot_offset_seconds` always recorded.

## Modified Files

| File | Change |
|---|---|
| `services/media-worker/Dockerfile` | Removed apt-get; ffmpeg provided by imageio-ffmpeg pip package |
| `services/media-worker/requirements.txt` | Added `imageio-ffmpeg>=0.5.0` |
| `services/media-worker/app/config.py` | Added `snapshot_output_dir`, `default_pre_seconds` |
| `services/media-worker/app/snapshot.py` | New: `generate_snapshot()`, `_ffmpeg_duration()`, `_ffmpeg_extract()` |
| `services/media-worker/app/worker.py` | Added `_snapshot_needed()`, `_update_snapshot_status()`, `_mark_not_required()`, `_process_pending_snapshots()` |
| `infra/docker-compose.phase3b.yml` | Added `SNAPSHOT_OUTPUT_DIR`, `DEFAULT_PRE_SECONDS` env vars |
| `harness/tests/test_phase3c_snapshot.py` | New: 15 unit tests |
| `scripts/smoke/check_phase3c.sh` | New: 14 smoke checks |

## Unit Test Results

```
harness/tests/test_phase3c_snapshot.py — 15 passed

Coverage:
- snapshot offset uses pre_seconds, not random/first/last frame
- fallback when duration < pre_seconds records fallback reason
- snapshot_required=true + clip ready -> snapshot_status=ready + snapshot_path
- missing clip_path -> snapshot_status=failed, clip_status unchanged
- ffmpeg missing -> snapshot_status=failed with clear error
- ffmpeg extraction failure -> snapshot_status=failed
- _snapshot_needed correctly filters ready/not_required/snapshot_required
- _update_snapshot_status writes correct fields
- _process_pending_snapshots generates snapshot via mocked ffmpeg
- media-worker idempotent: existing file promoted to ready without re-extraction
- snapshot failure does not change clip_status in SET clause
- _ffmpeg_duration returns None on FileNotFoundError
```

## Phase 3B Smoke Result

```
18 passed, 0 failed
```

Phase 3B clip pipeline unchanged. Existing media fields preserved.

## Phase 3C Compose Smoke Result

```
14 passed, 0 failed
```

Checks:
1. compose file exists
2. event injected
3. event in PostgreSQL
4. clip_status=ready
5. snapshot_status=ready
6. snapshot_path stored
7. snapshot_offset_seconds equals pre_seconds (5)
8. API returns snapshot_url → curl HTTP 200
9. snapshot file exists on media volume
10. API exposes media.snapshot_status=ready
11. media-worker re-scan idempotent (snapshot_path unchanged)
12. media-worker re-scan preserves snapshot_status=ready
13. clip_status remains ready after snapshot
14. snapshot_required=false → snapshot_status=not_required

## snapshot_url HTTP 200 Evidence

```
snapshot_url=/media/snapshots/1cd83c18-a1e9-41b1-b07c-b16ed9bf2a51.jpg
curl http://127.0.0.1:8001/media/snapshots/1cd83c18-a1e9-41b1-b07c-b16ed9bf2a51.jpg HTTP 200
```

## DB Media Fields Evidence

Success event:
| Field | Value |
|---|---|
| clip_status | ready |
| snapshot_status | ready |
| snapshot_path | /media/snapshots/{event_id}.jpg |
| snapshot_offset_seconds | 5.0 |
| clip_path | /media/replay-sink-output/.../video.mov |
| replay_job_id | present |
| sink_output_path | present |
| recording_strategy | savant_replay |

snapshot_required=false event:
| Field | Value |
|---|---|
| snapshot_status | not_required |

## Known Technical Debt

1. **Replay timestamp-domain mismatch**: `event_ts_ms` is epoch; Replay DB uses pipeline-relative (CLOCK_MONOTONIC) time. Keyframe lookup uses unbounded search.
2. **Source adapter SYNC_OUTPUT** syncs to monotonic clock, not epoch.
3. **Snapshot extracted from generated clip at pre_seconds offset**: production-grade exact event timestamp mapping still needs timestamp-domain mapping.
4. **Media-worker still polls** on a periodic interval.
5. **No DLQ for unprocessable record_requests**.
6. **Concurrent idempotency under heavy worker load** remains untested.
7. **Current clip/snapshot are raw media**, no detection boxes / annotated media.

Phase 3C generates raw event snapshots only. Annotated overlays, Savant draw_func integration, ROI polygon rendering, and detection-box rendering are intentionally deferred to a later phase.

8. **imageio-ffmpeg** provides static ffmpeg binary (no ffprobe). Duration detection uses ffmpeg stderr parsing.

## Commit Hash

`edb8cd2` — `feat(phase3c): generate event snapshots from replay clips`

## Git Status

8 files committed (4 modified, 4 new). 7 untracked files (unrelated: phase1d modules, yolo model, redis-cli binary).
