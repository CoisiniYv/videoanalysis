# 18_database_backed_evidence_viewer.md

## 1. Goal

Make the 8090 evidence viewer use PostgreSQL as the evidence index. Directory
scanning under the evidence root must not be used to build the list page,
pagination, or normal filters.

The filesystem remains the detail/media store:

```text
8090 list/filter/page -> API -> PostgreSQL events/evidence_tasks
8090 selected detail -> existing bundle files (metadata, sidecar, sink metadata, raw_clip)
```

This preserves current playback and overlay behavior while removing the scaling
and consistency risk from listing evidence by scanning bundle directories.

## 2. Problem

The current viewer lists `/api/bundles` from `services/evidence-viewer`, which
iterates evidence directories and reads `metadata.json` / `summary.json` for
filtering. That is not acceptable for the 30/60-stream target:

- list latency grows with retained bundle count;
- pagination order is filesystem `mtime`, not event database time;
- DB status and file status can disagree;
- future shard routing needs a DB-owned source/event index;
- pending/failed evidence has no complete bundle directory, so a directory scan
  cannot represent the operator state correctly.

## 3. Target Design

Add a database-backed API surface:

```text
GET /api/v1/evidence/health
GET /api/v1/evidence/bundles
```

`/api/v1/evidence/bundles` returns the same summary shape currently consumed by
the frontend (`bundles`, `total`, `limit`, `offset`), but derives it from:

- `events` top-level columns;
- `events.payload.media`;
- `evidence_tasks` existence/status where present.

The candidate set is any event with evidence activity:

- `clip_path` or media raw/metadata/bundle paths present;
- `media_status` no longer `not_implemented`;
- at least one `evidence_tasks` row.

`clip_required=true` by itself is not enough for the normal evidence viewer
list. It represents an event that may need evidence later, not a selectable
bundle. Deleted/expired media (`media_deleted`, `media_expired`, or
`payload.maintenance.deleted_at`) is also excluded from the normal list.

The endpoint supports current list filters without reading bundle files:

- `event_type`
- `event_category`
- `source_id`
- `camera_id`
- `event_id` (UUID or `source_event_id` substring)
- `clip_status`
- `person` from DB payload/top-level person fields only
- `limit` / `offset`

## 4. Detail Boundary

Do not rewrite media playback in this phase. After the operator selects an
event, the viewer may still call existing file-backed bundle endpoints:

- `/api/bundles/{event_id}`
- `/api/bundles/{event_id}/annotations`
- `/api/bundles/{event_id}/sink-metadata`
- `/api/bundles/{event_id}/media/raw_clip`

Those detail calls are allowed to fail closed if the database row is pending or
the bundle files are absent. The list page must still show the DB state.

## 5. Implementation Plan

1. Add an API router under `services/api/app/routers/evidence.py`.
2. Add repository query support for evidence bundle summaries.
3. Mount the router in `services/api/app/main.py`.
4. Change the 8090 evidence frontend list/health calls to
   `/api/v1/evidence/*`.
5. Keep selected detail calls on the existing `/api/bundles/*` file-backed
   endpoints.
6. Add focused tests for the DB query contract and frontend static routing.

## 6. Acceptance

Pass:

```bash
pytest -q \
  harness/tests/test_event_repository.py \
  harness/tests/test_evidence_viewer_database_index.py
```

Static acceptance:

- frontend list loading must not call `/api/bundles?`;
- frontend detail loading must still call `/api/bundles/{event_id}`;
- API evidence list query must not reference evidence-root filesystem paths or
  `metadata.json` directory scans.
