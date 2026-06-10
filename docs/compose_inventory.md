# Compose Inventory

Date: 2026-06-09
Status: current deployment entrypoint is the midterm project version.

This file answers "which compose do I run?" for the deployable project tree.

## Current Deployment

Run the midterm project version:

```bash
docker compose -f infra/docker-compose.midterm.yml up -d --build
```

This starts the `video-analytics-midterm` compose project and
`video-analytics-midterm-*` containers.

Current deployment files:

| Purpose | File |
|---|---|
| Compose | `infra/docker-compose.midterm.yml` |
| Env | `infra/env/midterm.env` |
| Replay config | `modules/savant_replay/config.midterm.json` |
| Camera config | `modules/savant_security/config/cameras.midterm.yml` |
| Savant module | `modules/savant_security/module.yml` |

The deployment source id is `primary_rtsp`. The evidence viewer is exposed on
host port `8090`; Replay API is exposed on host port `8098`.

## Runtime Shape

```text
RTSP source
  -> replay-service storage
  -> savant-security inference
  -> Redis/PostgreSQL workers
  -> clip-worker Replay job
  -> video-file-sink raw clip
  -> media-worker JSONL sidecar evidence
  -> evidence-viewer
```

The deployable version uses neutral project naming. It should not require
historical codename filenames at runtime.

## Archived Stage Entrypoints

Historical codename entrypoints were moved out of the active deploy surface:

| Archive | Contents |
|---|---|
| `infra/archive/phase-only/20260609/` | Former root compose files and env files |
| `modules/savant_replay/archive/phase-only/20260609/` | Former Replay configs |
| `modules/savant_security/config/archive/phase-only/20260609/` | Former camera config |
| `docs/archive/phase-only/20260610/` | Former design and runbook docs |
| `harness/tests/archive/phase-only/20260610/` | Former contract tests |
| `scripts/*/archive/phase-only/20260610/` | Former smoke/debug/tool scripts |
| `services/archive/phase-only/20260610/` | Former service prototypes |
| `infra/archive/phase-only/20260602/` | Older historical compose files |

Archived files are for traceability and historical regression only. Do not use
them as project-machine deployment entrypoints.
