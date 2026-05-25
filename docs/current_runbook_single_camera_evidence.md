# Current Runbook — Single-Camera Evidence Pipeline (E1 Dev Path)

Date: 2026-05-25
Applies to: HEAD `9f0c87d` (Phase E1.1c).
Audience: engineers and future Claude sessions reproducing the
verified single-camera evidence loop.

This runbook is the **operational** companion to
`docs/phase_e1_alert_evidence_mvp.md`. It records the exact commands to
go from a clean host to a fully verified
snapshot/annotated-snapshot/raw-clip/annotated-clip set on disk and over
HTTP. It is the **dev path**, not production.

---

## 0. Mainline entrypoint at a glance

| Item | Value |
|---|---|
| Compose file | `infra/docker-compose.phase3h-zmq.yml` |
| Compose project name | `phase3h-zmq` |
| Savant module | `modules/savant_phase3h_zmq` (POC; future `savant_security`) |
| Evidence service | `services/evidence-worker` |
| API host port | `8001` (compose: `8001:8000`) |
| Media root (host) | `/data/video-analytics/media` |
| Media root (container) | `/media` |
| Smoke script | `scripts/smoke/check_phase_e1_evidence.sh` |
| Output directory | `/data/video-analytics/media/evidence/events/{event_id}/` |

> **Do not** start `infra/docker-compose.phase3b.yml`. Its continuous
> Replay-bypass sinks must remain stopped (see
> `docs/media_output_directory_policy.md` Section 6 and
> `scripts/smoke/check_media_output_lockdown.sh`).

---

## 1. Stop legacy / conflicting stacks

Stop any historical phase stacks that may still be running. These checks
are idempotent — they succeed even if nothing is up.

```bash
# Stop Phase 3B Replay bypass continuous sinks (must NOT run)
docker compose -f infra/docker-compose.phase3b.yml down 2>/dev/null || true

# Stop any earlier-phase smoke stacks (best effort)
docker compose -f infra/docker-compose.phase2i.yml down 2>/dev/null || true
docker compose -f infra/docker-compose.phase2g.yml down 2>/dev/null || true
docker compose -f infra/docker-compose.phase3a.yml down 2>/dev/null || true

# Confirm no banned containers running
docker ps --format '{{.Names}}' | grep -E \
  '^(phase3b-video-file-sink|phase3b-media-worker|phase3b-clip-worker|phase3b-replay-service|phase1f-savant|phase2c-savant)$' \
  && echo "STOP: banned legacy containers still running" \
  || echo "OK: no banned legacy containers running"
```

If the grep prints any names, stop those containers before continuing.

---

## 2. Check media output lockdown

Verify the host evidence layout is healthy and no banned containers are
writing into legacy directories.

```bash
bash scripts/smoke/check_media_output_lockdown.sh
```

Expected: all checks `OK`, no `FAIL` lines. If any banned-container or
new-file-in-legacy-dir check fails, resolve before starting the pipeline
(see `docs/media_output_directory_policy.md`).

---

## 3. Start the E1 single-camera pipeline

```bash
docker compose -f infra/docker-compose.phase3h-zmq.yml up -d \
  redis postgres source-adapter savant-zmq metadata-sink video-file-sink event-worker api

# Wait for intrusion events to accumulate (~30–60s is enough)
sleep 30
```

Quick health check while it runs:

```bash
# Redis stream length should grow
docker exec phase3h-zmq-redis redis-cli XLEN security.events

# Postgres event count should grow
docker exec phase3h-zmq-postgres psql -U video -d video_analytics -tAc \
  "SELECT COUNT(*) FROM events WHERE event_type = 'intrusion'"
```

> **Lifecycle rule** (from `docs/phase_e1_alert_evidence_mvp.md`
> Section 7): the continuous `video-file-sink` and `metadata-sink` MUST
> be stopped before running `evidence-worker`. The video.mov and
> metadata.json are frozen for the evidence run.

---

## 4. Freeze the aligned video + metadata

Stop only the source / Savant / sinks. Keep `postgres`, `redis`, `api`,
and `event-worker` running so the API can serve the evidence URLs.

```bash
docker compose -f infra/docker-compose.phase3h-zmq.yml stop \
  source-adapter savant-zmq metadata-sink video-file-sink
```

Verify the frozen inputs exist:

```bash
ls -lh /data/video-analytics/media/phase3h-savant-output/
# expect at least: video.mov, metadata.json (non-zero size)
```

---

## 5. Run evidence-worker (one-shot)

`evidence-worker` is a one-shot container: it processes up to
`EVIDENCE_MAX_EVENTS` intrusion events whose `snapshot_path IS NULL`,
writes evidence under
`/data/video-analytics/media/evidence/events/{event_id}/`, updates the
PostgreSQL rows, and exits.

```bash
docker compose -f infra/docker-compose.phase3h-zmq.yml up evidence-worker
```

Re-run to process more events after the next pipeline run (it skips
events that already have a snapshot path).

---

## 6. Run the smoke test

```bash
bash scripts/smoke/check_phase_e1_evidence.sh
```

The script runs 22 checks: compose config, Postgres state, on-disk files
(snapshot, annotated snapshot, raw clip, annotated clip), API URLs,
HTTP 200 responses, annotated bbox red-pixel verification, and ffprobe
validation of the annotated clip (codec `h264`, pix_fmt `yuv420p`,
non-zero duration, non-zero dimensions).

Expected: all 22 checks `OK`. The script prints the four URLs at the
end for manual verification.

---

## 7. Manual visual verification

The smoke script verifies pixels and HTTP, but a human must still open
each URL to confirm the visual result. Replace `{event_id}` with the
value from the smoke output (or query the DB; see Section 0 of the
script).

```text
http://localhost:8001/api/v1/events/{event_id}
http://localhost:8001/media/evidence/events/{event_id}/snapshot.jpg
http://localhost:8001/media/evidence/events/{event_id}/annotated_snapshot.jpg
http://localhost:8001/media/evidence/events/{event_id}/clip_raw.mp4
http://localhost:8001/media/evidence/events/{event_id}/clip_annotated.mp4
```

Checklist:

- `snapshot.jpg` — 1920×1080, scene visible, no overlay.
- `annotated_snapshot.jpg` — same scene with red bbox rectangles on
  every person; top-left label block with event id / type / track_id /
  frame_num.
- `clip_raw.mp4` — ~6s, no overlay, no blockiness (stream-copied).
- `clip_annotated.mp4` — ~6s, red bbox overlay on every frame, no
  macroblock artefacts (E1.1c re-encode at CRF 18 / preset `veryfast`).

If a clip looks blocky, see `docs/phase_e1_alert_evidence_mvp.md`
Section 8 — the env vars `ANNOTATED_CLIP_CRF` and
`ANNOTATED_CLIP_PRESET` control the trade-off.

---

## 8. API port

Default: **`8001`** (compose maps `8001:8000` for the `api` service).

If `8001` is already in use on the host, the captured E1 run report used
`8002` instead (`docs/phase_e1_run_report.md`). To switch ports, either
free the conflicting service or override the host port mapping in
compose. The smoke script honours `API_BASE` (default
`http://localhost:8001`), so a one-off run can use:

```bash
API_BASE=http://localhost:8002 bash scripts/smoke/check_phase_e1_evidence.sh
```

---

## 9. Known limitations (dev path)

This loop is **not** the production Replay path. Acceptance criteria
are documented in `docs/phase_e1_alert_evidence_mvp.md` Section 9.
Repeated here so anyone reading this runbook does not misread the
output as production-grade:

1. **Dev evidence path** — `evidence-worker` is a post-hoc one-shot
   service. Production will use real-time event-triggered clips via the
   Replay Service + `clip-worker` + `video-file-sink` (see
   `docs/phase_e1_alert_evidence_mvp.md` Section 10).
2. **Depends on Phase 3H.2 continuous video+metadata** — the pipeline
   must run long enough to populate `video.mov` and `metadata.json`,
   then be stopped before `evidence-worker` can read them. The sinks
   write continuously and unbounded while up.
3. **Event-to-frame matching is approximate** — `evidence-worker` scans
   `metadata.json` for frames containing the event's `track_id` and
   picks the middle match. `frame_uuid` / `keyframe_uuid` / `pts` are
   not yet propagated through the event payload (CLAUDE.md Section 9.6
   known limitation).
4. **Not the production Replay event-triggered path** — bbox/snapshot
   alignment is *frame-stream aligned* (Savant and sinks share a single
   ZMQ frame source) but the event→frame mapping is heuristic.
   Production requires single-ingestion (already satisfied) +
   timestamp-domain mapping + populated `frame_uuid` / `keyframe_uuid`
   (CLAUDE.md Section 9.4).
5. **Continuous sink output grows unbounded** — `video.mov` and
   `metadata.json` keep appending while the pipeline runs. Stop the
   stack promptly after capturing enough events.
6. **`EVIDENCE_MAX_EVENTS` cap (default 5)** — newer events may be
   skipped if their `track_id` was assigned after `metadata.json` was
   frozen. Re-run `evidence-worker` after re-running the pipeline to
   process them.
7. **Single `source_id`** — only events whose `source_id` matches the
   video-file-sink output directory name are processed.
8. **No cooldown / severity policy** — every pending intrusion event up
   to the cap is processed.
9. **`evidence-worker` outputs are root-owned** — the container runs as
   root; host cleanup of `media/evidence/events/` requires sudo.

---

## 10. Teardown

```bash
docker compose -f infra/docker-compose.phase3h-zmq.yml down
```

Evidence on disk under `/data/video-analytics/media/evidence/events/` is
preserved across teardown (volume mount). The Postgres data is
preserved per the compose `postgres` service definition; if you need a
clean DB for the next run, `docker compose ... down -v` removes the
named volumes — confirm this is desired before running it.

---

## 11. Related documents

| Document | Content |
|---|---|
| `docs/phase_r1_mainline_consolidation_plan.md` | R1 consolidation plan (parent of this runbook) |
| `docs/phase_e1_alert_evidence_mvp.md` | Phase E1 evidence generation design |
| `docs/phase_e1_run_report.md` | Captured 2026-05-25 verification run |
| `docs/media_output_directory_policy.md` | Mandatory media output directory policy |
| `docs/production_ingestion_topology_policy.md` | Production single-ingestion topology policy |
| `docs/project_rebaseline_2026_05_25.md` | Authoritative roadmap |
| `CLAUDE.md` §9 | Production ingestion / Replay hard constraints |

---

*Written 2026-05-25. Phase R1 — dev path, not production.*
