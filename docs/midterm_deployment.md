# Midterm Deployment

Date: 2026-06-09

This is the active project-machine deployment entrypoint.

## Files

| Purpose | File |
|---|---|
| Compose | `infra/docker-compose.midterm.yml` |
| Env | `infra/env/midterm.env` |
| Replay config | `modules/savant_replay/config.midterm.json` |
| Camera config | `modules/savant_security/config/cameras.midterm.yml` |
| Savant module | `modules/savant_security/module.yml` |

Do not deploy from archived C1/C2/phase compose files.

## Start

```bash
docker compose -f infra/docker-compose.midterm.yml config
docker compose -f infra/docker-compose.midterm.yml up -d --build
```

Lightweight pre-deploy check:

```bash
bash scripts/smoke/current/check_midterm_deployment.sh
```

Read-only deployment doctor:

```bash
bash scripts/runtime/doctor_midterm.sh
```

The stack uses these default host ports:

- Redis: `6396`
- Replay API: `8098`
- Evidence viewer: `8090`

Workers use the existing host PostgreSQL by default:

```text
postgresql://video:video@host.docker.internal:5432/video_analytics
```

Use the `local-postgres` compose profile only when you explicitly want the
stack-local PostgreSQL on host port `5439`.

## Evidence Policy

The evidence path is replay-first:

```text
RTSP -> Replay -> Savant -> Redis/PostgreSQL
  -> clip-worker Replay job -> video-file-sink -> media-worker sidecar
```

Evidence bundles contain:

- `raw_clip.mov`
- `sink_metadata.json`
- `annotations.frame_cache.identity.jsonl`
- `summary.frame_cache.identity.json`
- `metadata.json`

The media-worker writes `project_version=midterm` and `schema_version=2.0-midterm`
for this deployment. Legacy metadata fields are disabled in the midterm compose.

## Runtime Calibration

Current defaults are set in `infra/env/midterm.env`:

- Savant ingress FPS gate: enabled, `8/1`
- Pose infer interval: `1`
- Pose detector/selector thresholds: `0.50`
- Pose keypoint threshold: `0.35`
- Pose minimum box: `60x100`
- Face detector threshold: `0.50`
- Watchlist similarity threshold: `0.60`

These values are chosen to reduce excessive pose boxes, slow the inference path
to a reasonable rate, and keep low-quality face detections out of comparison.

## Archive Locations

Historical files are preserved here:

- `infra/archive/phase-only/20260609/`
- `modules/savant_replay/archive/phase-only/20260609/`
- `modules/savant_security/config/archive/phase-only/20260609/`

They are not deployment entrypoints.
