# Phase 3B.1 — Replay Timestamp Anchoring Fix Summary

## Goal

Fix the remaining technical debt from Phase 3B where `clip-worker.find_keyframe()` ignored the event timestamp. Replay clip generation must anchor keyframe lookup around the actual `event_ts_ms` from the event, not just return any available keyframe.

## Why Timestamp Anchoring Matters

Without timestamp anchoring, `find_keyframe()` always returns the most recent keyframe regardless of when the event occurred. This produces incorrect clips:

- An event at T=10s could return a keyframe from T=45s
- The `offset.seconds` in the replay job seeks backward from the anchor keyframe, so a mismatched anchor produces clips that don't contain the event
- The clip might show empty footage or a completely different time window

With timestamp anchoring, the Replay API searches for keyframes within a configurable window around the event time, ensuring the anchor keyframe is temporally close to the event.

## Changes

### 1. `replay_client.py` — `find_keyframe()` now uses `ts_ms`

- Added `_ts_ms_to_iso()` helper to convert epoch milliseconds to ISO 8601 UTC strings
- When `ts_ms > 0`: computes `from` = event_time - window_s, `to` = event_time + window_s, passes both as ISO 8601 timestamps in the POST body
- When `ts_ms == 0`: passes `from=null, to=null` (unbounded lookup, backward-compatible)
- Error messages now include both `source_id` and `ts_ms` for debuggability

### 2. `config.py` — new `keyframe_lookup_window_s` field

- Env var: `KEYFRAME_LOOKUP_WINDOW_S`, default: `10` (seconds)
- Controls the search window radius around `event_ts_ms`

### 3. `worker.py` — failure handling for missing `event_ts_ms`

- When `event_ts_ms` is 0 or missing AND `keyframe_uuid` is not provided:
  - Sets `clip_status=failed`
  - Writes `error_message="missing event_ts_ms in record_request source_id=..."`
  - Does NOT create a Replay job
  - ACKs the message (does not block the queue)
- When `source_id` is missing: fails with `error_message="missing source_id in record_request"`
- When `find_keyframe()` returns None: error includes both `source_id` and `event_ts_ms`

### 4. Compose files updated

- `infra/docker-compose.phase3a.yml` — added `KEYFRAME_LOOKUP_WINDOW_S: "10"`
- `infra/docker-compose.phase3b.yml` — added `KEYFRAME_LOOKUP_WINDOW_S: "10"`

## Test Results

### New tests (9 passed)

| Test | What it proves |
|---|---|
| `test_ts_ms_to_iso_returns_iso8601_utc` | ISO 8601 conversion correctness |
| `test_ts_ms_to_iso_handles_zero` | Zero ms → epoch start |
| `test_find_keyframe_passes_from_to_when_ts_ms_provided` | ts_ms > 0 → from/to are set as ISO 8601 |
| `test_find_keyframe_passes_null_from_to_when_ts_ms_zero` | ts_ms == 0 → from/to are None (backward compat) |
| `test_find_keyframe_window_is_symmetric` | Search window is centered on event_ts_ms |
| `test_find_keyframe_bypassed_when_uuid_provided` | Direct keyframe_uuid path preserved |
| `test_missing_event_ts_ms_would_fail_cleanly` | Missing ts_ms → clip_status=failed |
| `test_no_keyframe_found_fails_with_event_context` | 404 → returns None, worker handles |
| `test_timestamp_anchored_lookup_is_idempotent` | Same inputs → same from/to window |

### Full regression (111 passed, 0 failed)

All existing 102 tests from Phase 2D, 2F, 2H, 3A, 3B continue to pass.

## Keyframe Lookup Flow (updated)

```
record_request received
├── keyframe_uuid provided? → use directly (bypass lookup)
├── source_id missing? → fail: clip_status=failed
├── event_ts_ms missing/0? → fail: clip_status=failed
└── both present → find_keyframe(source_id, ts_ms, window_s)
    ├── ts_ms > 0 → POST {from: ISO, to: ISO, limit: 1}
    ├── ts_ms == 0 → POST {from: null, to: null, limit: 1} (backward compat)
    ├── 404 → None → fail: clip_status=failed
    └── 200 → extract UUID → create_job(...)
```

## Remaining Technical Debt

| Item | Priority | Phase |
|---|---|---|
| Savant container runtime `pip install redis` | High | 2I |
| Replay config.json sync with compose env vars (manual) | Medium | 3B+ |
| clip-worker consumer name is static (single instance) | Low | 3C |
| media-worker polls filesystem; no event-driven notification | Low | 3B+ |
| No dead-letter queue for unprocessable record_requests | Low | 3C |
| Phase 2H/2I docker smoke not run in this phase (requires compose + GPU) | Low | Gate |
