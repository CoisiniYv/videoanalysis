# P1c.2 Runtime Discipline and Evidence Contract Hardening

Status: implemented.

This document records the P1c.2 entry audit and the hardening contract for the
P1c-RTSP single-event Replay evidence POC.

## Entry Audit

1. Worker bind mounts: `event-worker`, `clip-worker`, and `media-worker` were
   built from their service Dockerfiles and did not bind mount service source
   into `/app`.
2. Worker build requirement: worker code changes still required image rebuilds
   before P1c.2 hardening because service source was copied into the image.
3. Bare Docker commands in smoke: the smoke had `detect_docker()`, but later
   still used bare `docker`, `docker compose`, `docker exec`, `docker logs`,
   `docker stop`, and `docker run`.
4. Default smoke build mode: the smoke defaulted to
   `docker compose up -d --build --force-recreate`.
5. `event_annotation.json` bbox format: person bbox values from event payloads
   were copied through while declaring `bbox_format = xyxy`, including object
   shapes such as `{x,y,width,height}` that are actually `xywh`.
6. `metadata.json` source: the P1 finalizer copied Video File Sink
   `metadata.json` into the evidence bundle as `metadata.json`; it did not
   generate business-level evidence metadata.
7. Replay stop condition: clip-worker could build `ts_delta_sec` payloads when
   configured, but the smoke did not report `stop_condition_mode` or
   `fallback_reason` explicitly.
8. P1c scope: current P1c is a single-event evidence POC. It is not incident
   coalescing, not continuous recording, and not a production multi-event merge
   policy.
9. Replay TTL: P1c Replay config contains
   `storage.rocksdb.data_expiration_ttl` with `secs = 60`; the smoke parsed the
   TTL seconds, but did not report the RocksDB path and did not distinguish
   missing TTL from too-short TTL.
10. Fixed RTSP: the smoke asserted the fixed RTSP URI and RTSP reachability, and
    the compose used `rtsp://10.37.57.112:8554/live/1080movie`. The smoke still
    needed stronger final reporting for no local file, no test video, no source
    extraction fallback, and no second RTSP pull.

## Hardening Scope

P1c-RTSP remains a single-event evidence POC:

```text
RTSP -> source-adapter -> replay-service -> savant-security
     -> security.events -> event-worker -> security.record_requests
     -> clip-worker -> Replay job -> video-file-sink
     -> media-worker -> evidence bundle
```

This phase does not implement continuous recording, incident coalescing,
multi-camera behavior, gallery recognition, annotated clip rendering, frontend
work, production compose changes, or performance testing.

## Runtime Contract

- Docker access must be detected once with one of:
  `DOCKER_ACCESS_OK`, `SUDO_DOCKER_REQUIRED`, or `DOCKER_ACCESS_BLOCKED`.
- After detection, Docker runtime commands must use `$DOCKER` and `$COMPOSE`.
- Default smoke runs must use no-build startup. `P1C_ALLOW_BUILD=1` is required
  before `--build` is allowed.
- Smoke runs must not use `docker pull`.
- Worker source must be bind-mounted into `/app` for development. Hashes for
  representative worker files must match between host and container before the
  smoke continues.
- If worker images are missing and build is not explicitly allowed, the smoke
  must report `Result=BLOCKED` and
  `Reason=worker_image_missing_and_build_not_allowed`.

## Replay Contract

- Replay TTL field: `storage.rocksdb.data_expiration_ttl`.
- Required TTL seconds: `pre_seconds + post_seconds + scheduling_margin`.
- P1c uses `pre_seconds = 5`, `post_seconds = 5`,
  `scheduling_margin = 10`; TTL must be at least 20 seconds.
- Replay job payloads prefer:

```json
{
  "offset": {"seconds": 5},
  "stop_condition": {"ts_delta_sec": {"max_delta_sec": 10}}
}
```

- A `frame_count` fallback is valid only when an exact `fallback_reason` is
  recorded.

## Evidence Contract

- `event_annotation.json` stores parsed person bbox overlays. Object bboxes
  shaped as `{x,y,width,height}` are treated as `xywh` source data and converted
  to `xyxy`.
- `metadata.json` is business-level evidence metadata. Raw Video File Sink
  metadata is preserved separately as `sink_metadata.json`.
- P1c does not generate `annotated_clip`.
- Evidence limitations must explicitly include:
  `single-event evidence POC`, `not incident coalescing`,
  `not continuous recording`, and `no annotated_clip generated`.

## Next Phase

P2 - Incident Window and Recording Coalescing:

- `merge_window`
- `cooldown`
- `incident_id`
- `first_event_ts`
- `last_event_ts`
- one clip for many events
- `event_annotation.json` with `events[]`
