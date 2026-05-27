# Phase F2.3 — Redis Face Observations Producer

## Purpose

Export face observations with embeddings to Redis Stream
`security.face_observations` for downstream consumption by face-worker.

Only `reid_allowed=true` faces (passing quality gate + throttle) are
exported. No image bytes. No pgvector / watchlist / live_search.

## Final Redis Payload Schema

Each stream entry has flat fields for visibility and a full JSON blob
in the `data` field.

### Flat fields (Redis Stream visibility)

| Field | Type | Description |
|---|---|---|
| `type` | str | `"face_observation"` |
| `source_observation_id` | str | Deterministic idempotency key |
| `camera_id` | str | Camera identifier |
| `source_id` | str | Savant source identifier |
| `track_id` | str | Person track ID |
| `timestamp_ms` | str | Timestamp in milliseconds |
| `face_confidence` | str | Face detection confidence |
| `quality` | str | Quality gate score |
| `embedding_model` | str | `"adaface"` |
| `embedding_dim` | str | `"512"` |
| `data` | str | Full JSON blob |

### JSON `data` field (full payload)

| Field | Type | Description |
|---|---|---|
| `schema_version` | str | `"1.0"` |
| `source_observation_id` | str | Idempotency key |
| `producer` | str | `"savant_security"` |
| `message_type` | str | `"face_observation"` |
| `camera_id` | str | Camera identifier |
| `source_id` | str | Source identifier |
| `track_id` | str | Person track ID |
| `timestamp_ms` | int | Timestamp ms |
| `frame_num` | int | Frame number |
| `person_bbox` | list | Person bbox (null if unavailable) |
| `face_bbox` | list | `[xc, yc, w, h]` |
| `landmarks` | list | 10 floats (5 points x 2 coords) |
| `face_confidence` | float | Detection confidence |
| `quality` | float | Gate quality score |
| `detector_model` | str | `"yolov8_face"` |
| `embedding_model` | str | `"adaface"` |
| `embedding_dim` | int | `512` |
| `embedding` | list | 512 floats |
| `embedding_norm` | float | L2 norm (~1.0) |
| `reid_allowed` | bool | `true` |
| `reid_throttle_key` | str | Throttle key |
| `association_score` | float | Face-person association score |
| `association_method` | str | Association method |
| `snapshot_path` | str | null (future) |
| `crop_path` | str | null (future) |
| `payload` | dict | Empty (extensible) |

## Idempotency Key

Format: `face:{source_id}:{track_id}:{timestamp_ms}[:{face_index}]`

- Deterministic per face per frame
- No UUID, no random component
- Face index appended when multiple faces at same timestamp

## Stream Name

`security.face_observations` (configurable via `FACE_OBSERVATION_STREAM`)

## No Image Bytes Guarantee

The schema explicitly excludes:
- `image_bytes`
- `frame_bytes`
- `crop_bytes`
- `base64`
- `jpeg`

Only filesystem path references (`snapshot_path`, `crop_path`) are
included, and both are null in this phase.

## Exporter Behavior

- `FACE_OBSERVATION_EXPORT_ENABLED=true` → `RedisStreamFaceObservationExporter`
- `FACE_OBSERVATION_EXPORT_ENABLED=false` → `DryRunFaceObservationExporter`
- Redis errors are caught, logged, and silently dropped (no crash)
- Follows existing `event_exporter.py` pattern

## Unit Test Results

23/23 pass in `harness/tests/test_face_observation_exporter.py`.

## Static Smoke Result

22/22 pass.

## GPU/Redis Runtime Smoke Result

**PASS** — 2026-05-27

- `[face_obs_export]` logs appear
- `exported=1..5` per log interval
- Redis `XLEN security.face_observations = 9625` (after ~30s)
- Latest entry has all required fields
- No image bytes in entry
- Pipeline stable

## Sample Redis Entry (truncated)

```json
{
  "schema_version": "1.0",
  "source_observation_id": "face:c1_2_test:382:6271560",
  "camera_id": "c1_2_test",
  "track_id": "382",
  "timestamp_ms": 6271560,
  "face_bbox": [750.5, 318.1, 341.8, 362.3],
  "landmarks": [656.7, 282.7, ...],
  "face_confidence": 0.775,
  "quality": 1.0,
  "embedding_model": "adaface",
  "embedding_dim": 512,
  "embedding": [0.00241, ...],
  "embedding_norm": 0.9997,
  "reid_allowed": true,
  "reid_throttle_key": "c1_2_test:382",
  "association_score": 0.937,
  "association_method": "center_inside_upper_body"
}
```

## F2.3b — Throttle Enforcement Fix (2026-05-27)

### Problem

Manual Redis inspection showed same `track_id` entries spaced only 40ms apart,
violating the `FACE_REID_MIN_INTERVAL_MS=1000` throttle policy.

### Root Cause

`FaceReidGatePyFunc` uses raw `frame_meta.pts` as timestamp for the throttle
map. When PTS is in milliseconds (Savant default for some sources), the gate
throttle sees values like `43043960` as a single timestamp — the delta between
consecutive frames (40) always passes the 1000ms check. The exporter converts
`pts / 1_000_000` to get real milliseconds, but had no throttle of its own.

### Fix

- Added `ExportThrottleMap` to `face_observation_exporter.py` service.
- `FaceObservationExporterPyFunc` now has a defensive per-track throttle using
  the exporter's own millisecond timestamp (`pts / 1_000_000`).
- Gate verdict is now required — missing `reid_allowed` metadata skips export
  with reason `missing_gate_verdict`.
- `reid_skip_reason` must be absent or `"ok"` — non-ok values skip export.
- `export_min_interval_ms` parameter defaults to 1000, configurable via
  `FACE_REID_MIN_INTERVAL_MS` env var (shared with gate).

### Clarification

- `landmarks` = 10 floats (5 points x 2 coordinates) — normal.
- `embedding` = 512 floats — the long field in Redis, not landmarks.

### Post-Fix Runtime Verification (2026-05-27)

- Container restarted: `docker compose up -d --force-recreate savant-security`
- Container status: healthy, no startup errors
- Stream cleared: `DEL security.face_observations`
- Run duration: 30 seconds
- XLEN after run: 42
- Entries sampled: 221 (from longer run)
- Unique throttle keys: 28
- Min delta observed: 1000ms
- Violations: 0
- Exporter logs: `[face_obs_export] skip=export_throttled` visible on most
  frames — throttle actively rejecting over-frequent observations.
- Verification script: `scripts/smoke/check_f2_3b_face_observation_throttle_runtime.py`
- Conclusion: face-worker persistence can start next.

## F2.4 — Contract Hardening (2026-05-27)

### Changes

| Problem | Fix |
|---|---|
| camera_id = source_id everywhere | Gate and exporter resolve business camera_id via `CameraConfigBundle.get_by_source_id()` |
| from_draft() missing fields | Now copies all F2.3 fields (face_confidence, detector_model, embedding_model, embedding_dim, embedding, embedding_norm, reid_allowed, etc.) |
| Unattributed faces (no person_track_id) entering stream | Explicit skip: `missing_person_track_id` when track_id <= 0 |
| Face-person association not one-to-one | Greedy one-to-one: each face ≤ 1 person, each person ≤ 1 face per frame |
| PTS unit mismatch between gate and exporter | `normalize_pts_to_ms()` unified helper (heuristic: >= 10^9 → ns, >= 10^7 → µs, < 10^7 → ms) |
| Throttle key inconsistency | 3-part key: `{camera_id}:{source_id}:{person_track_id}` (was 2-part `{source_id}:{track_id}`) |

### Unattributed Face Policy (MVP)

Faces without `person_track_id` (unassociated) MUST NOT be exported to
`security.face_observations` in MVP. This is a final decision. Future
phases may revisit for standalone face detection use cases, but any
change requires explicit spec amendment.

### Timestamp Normalization

`normalize_pts_to_ms()` in `modules/savant_security/custom/services/time_utils.py`
provides a single conversion used by both gate and exporter. Heuristic:

- `pts >= 1_000_000_000` → nanoseconds → `pts // 1_000_000`
- `pts >= 10_000_000` → microseconds → `pts // 1_000`
- `pts < 10_000_000` → already milliseconds → passthrough
- `pts < 0` / `None` / `0` → returns `0`

## Known Limitations

| Limitation | Impact |
|---|---|
| No person_bbox in observation | face-worker needs to look up from track |
| No snapshot/crop paths | Media integration is future phase |
| Throttle in-memory only | Resets on container restart |
| No retry/dead-letter | Dropped on Redis error |
| Embedding ~2KB per entry | Stream grows quickly |
| Runtime `pip install -q redis pyyaml` in pyfunc | Deployment hazard — tracked as tech debt, not fixed in this phase |

## Files Changed

- `modules/savant_security/custom/models/face_events.py` — extended schema
- `modules/savant_security/custom/services/face_observation_exporter.py` — new
- `modules/savant_security/custom/pyfuncs/face_observation_exporter.py` — new
- `modules/savant_security/module.yml` — added FaceObservationExporterPyFunc
- `infra/docker-compose.c1-official-adapter.yml` — added env vars
- `harness/tests/test_face_observation_exporter.py` — new
- `scripts/smoke/check_f2_3_face_observation_redis_runtime.sh` — new
- `docs/phase_f2_3_face_observation_redis.md` — new

## Next Phase Recommendation

**F4** — face-worker persistence:
- Consume `security.face_observations` from Redis Stream
- Store in PostgreSQL `face_observations` table
- pgvector similarity search
- watchlist_hit / live_search_hit events
