# Phase 3F0.2a — Postgres Persistence Hotfix

Date: 2026-05-25

## Problem

Phase 3B postgres service had no persistent volume. `docker compose down` removed the container and all event data. Media files on `/data/video-analytics/media/` survived but lost DB context (event_id, bbox, event_ts_ms, replay_job_id, snapshot_offset_seconds), making bbox/snapshot alignment diagnosis impossible.

## Fix

Added bind mount to `infra/docker-compose.phase3b.yml`:

```yaml
volumes:
  - ../db/migrations:/docker-entrypoint-initdb.d:ro
  - /data/video-analytics/postgres-phase3b:/var/lib/postgresql/data  # NEW
```

Host directory created with UID 999 (postgres) ownership:

```bash
mkdir -p /data/video-analytics/postgres-phase3b
chown 999:999 /data/video-analytics/postgres-phase3b
```

## Verification

1. Inserted test marker event
2. `docker compose down` (without `-v`)
3. `docker compose up -d postgres`
4. Test marker still present — data survives full down/up cycle

## Important

- Never use `docker compose down -v` — it would delete the bind-mounted data
- The bind mount is at `/data/video-analytics/postgres-phase3b`, separate from `/data/video-analytics/media`

## Regression

| Test | Result |
|---|---|
| Phase 3B smoke (18/18) | Pass |
| Phase 3C smoke (14/14) | Pass |
| Phase 3E smoke (15/15) | Pass |
| Real Savant events with full pipeline | 461+ events at ready/ready/ready/ready |

## Next

Continue Phase 3F0.2 — bbox/snapshot alignment diagnosis.
