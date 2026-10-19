# Deployment lifecycle change record

## Scope

Branch: `codex/deployment-lifecycle-20261020`

Implemented the requested deployment improvements 1, 2, 3, and 5. The in-place
update and automatic rollback item 4 remains deferred.

The existing working tree was first preserved in checkpoint commit `10bd6f8`
(`chore: checkpoint existing pressure runtime guards`), created with
`Asia/Seoul` timestamps and pushed to the matching GitHub branch before this
implementation continued.

## Key files

- `scripts/runtime/midterm_compose_context.sh`: shared env, Compose files,
  overrides, and profile arguments.
- `scripts/runtime/capture_deployment_manifest.sh`: secret-free release,
  Compose, image, model, migration, GPU, and Docker identity.
- `scripts/runtime/midterm_db_migrate.py`: ordered migrations, checksums,
  advisory lock, PostgreSQL custom backup, and Seoul timestamps.
- `scripts/midterm_package_clean.sh` and `scripts/midterm_deploy_clean.sh`:
  clean-machine package and restore flow.
- `scripts/midterm_start.sh`, `scripts/midterm_stop.sh`,
  `scripts/midterm_health.sh`, `scripts/school_deploy.sh`, and runtime doctor/
  smoke entrypoints: common Compose argument usage and deployment records.
- `services/api/app/services/deployment_status.py`,
  `services/api/app/routers/runtime.py`, and the evidence-viewer runtime page:
  read-only deployment status on 8090.
- `docs/midterm_deployment.md`, `docs/frontend_interface/02_api_inventory.md`,
  and `README.md`: operator and API documentation.

## Operations performed in this checkout

- Read-only Git status, history, remote, and diff inspection.
- Static inspection of the affected scripts, Compose file, API, UI, docs, and
  deployment contract expectations.
- Edited source and documentation files only.
- No shell script, Python script, Docker command, database command, browser
  build, or test was executed on this machine for this change.

The implementation commit and GitHub push completion are recorded in the final
Git log and the accompanying task report; commit creation and push timestamps
use `Asia/Seoul`.
