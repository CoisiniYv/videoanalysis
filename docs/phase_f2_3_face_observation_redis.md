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

## Known Limitations

| Limitation | Impact |
|---|---|
| No person_bbox in observation | face-worker needs to look up from track |
| No snapshot/crop paths | Media integration is future phase |
| Throttle in-memory only | Resets on container restart |
| No retry/dead-letter | Dropped on Redis error |
| Embedding ~2KB per entry | Stream grows quickly |

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
