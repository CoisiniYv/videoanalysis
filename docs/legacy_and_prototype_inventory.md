# Legacy and Prototype Inventory

Date: 2026-05-29
Status: R2 inventory; do not delete without explicit approval.

## Legacy / Prototype Areas

| Area | Description | Recommendation |
|---|---|---|
| Phase 3H legacy POC | Official adapter ingestion, metadata/video sink POC, and early evidence cache paths. | Keep docs for history; avoid extending old paths for production. |
| F4.1 prototype | `find_person` raw clip fallback, repository, CLI, and contract test. | Archive/prototype until the API/search design is mainlined. |
| F4.3A visual scripts | Single-frame face observation overlay. | Archive/prototype; useful for manual debugging. |
| F4.3B debug scripts | Post-run registered-person evidence package and calibration. | Keep as debug smoke, not production evidence viewer. |
| `scripts/demo` | Demo entrypoints and local scripts. | Review separately; do not treat as production commands. |

## Keep

- Main c1-official-adapter runtime.
- F4.3A local mp4 observation smoke.
- F4.3B debug recognition smoke with clear warnings.
- Calibration report generator for threshold/gallery-quality review.

## Archive or Prototype

- F4.1 raw clip fallback.
- F4.3A one-frame visual evidence.
- Demo scripts that bypass the main runtime.

## Delete Later Only After Manual Approval

- Redundant visual scripts once F4.3B exporter is accepted.
- Obsolete demo scripts after a production operator command replaces them.
- Generated local artifacts under `manual-inspection/` or `tmp/`.

## Never Commit

- `face/` test photos.
- `testVideo/` movie files.
- `/data/video-analytics/media` output packages.
- `manual-inspection/`.

## Common Troubleshooting

- A prototype script works but production does not: check whether it bypasses Savant, Redis, face-worker, or PostgreSQL.
- A demo uses old source ids: rerun with unique source ids and `created_at` filters.
- A visual artifact exists but no event exists: remember F4.3B artifacts are post-run debug exports, not realtime events.

