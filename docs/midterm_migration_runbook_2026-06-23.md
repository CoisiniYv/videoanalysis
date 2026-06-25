# Midterm Migration Runbook

Date: 2026-06-23

如果目标是“新机器干净启动，不迁移任何旧数据”，优先使用中文说明：

```text
docs/midterm_clean_machine_migration_2026-06-25.md
```

对应脚本：

```bash
bash scripts/midterm_package_clean.sh
bash scripts/midterm_deploy_clean.sh <package.tgz>
```

This runbook defines the minimum reproducible package for moving the current
midterm runtime to another machine before the 60-stream performance work starts.
The goal is not to preserve every local runtime artifact. The goal is to prove
that a clean target host can start the same deployable baseline, ingest RTSP,
run Savant, write events/evidence, and pass the readiness checks from the same
code/config state.

## 1. Migration Principle

Do not migrate an ambiguous working tree.

Before packaging, create a release candidate state:

```bash
git status --short
```

Every changed file must be classified as one of:

- included in the migration release commit;
- included as a named patch artifact with a reason;
- intentionally excluded.

The migration package must include a manifest with:

- git commit SHA and branch name;
- `git status --short` captured at packaging time;
- compose profiles expected on the target host;
- env file checksum;
- model directory checksums;
- database dump filename and checksum, if database state is migrated;
- media/evidence archive filename and checksum, if historical evidence is
  migrated.

## 2. What Must Move

Required for a functional clean deployment:

| Category | Source | Target |
| --- | --- | --- |
| Code | repository release commit or git bundle | `/home/user/video-analytics` or chosen checkout path |
| Compose/env | `infra/docker-compose.midterm.yml`, `infra/env/midterm.env` | same relative paths |
| Replay configs | `modules/savant_replay/config.midterm*.json` | same relative paths |
| Savant module/config | `modules/savant_security/module.yml`, `modules/savant_security/config/cameras.midterm.yml`, `modules/savant_security/savant_patches/` | same relative paths |
| Models | `/data/video-analytics/models`, `/data/video-analytics/models-savant-b` if dual-shard profile uses it | same absolute paths unless compose is changed |
| Downloads cache | `/data/video-analytics/downloads` | same absolute path |
| PostgreSQL state | dump from `video_analytics` database | restored before workers start |
| Runtime dirs | `/data/video-analytics/media`, `/data/video-analytics/replay-midterm*`, `/data/video-analytics/artifacts` | created with correct ownership; historical contents optional |

Usually migrate database camera/rule/gallery state. Historical evidence clips and
Replay RocksDB are optional unless the target must show old bundles or resume a
live replay epoch. For a clean performance host, start with empty
`media/evidence`, empty Replay RocksDB directories, and the restored camera/rule
database unless the test explicitly needs old evidence.

Do not treat Redis streams as migration state. Redis is a runtime queue/cache and
should be recreated cleanly on the target.

## 3. Target Host Prerequisites

The target host must have:

- NVIDIA driver compatible with the deployed Savant/DeepStream image;
- NVIDIA Container Toolkit working with Docker;
- Docker Engine and Docker Compose plugin;
- enough disk for `/data/video-analytics`;
- network reachability to the RTSP sources;
- the configured host ports free: `6396`, `8090`, `8098`, `18080`, `18081`, and
  optional `5439`;
- PostgreSQL available either as the expected host database or through the
  `local-postgres` compose profile.

Minimum checks:

```bash
nvidia-smi
docker info
docker compose version
```

## 4. Source Packaging

Create a package directory:

```bash
export MIGRATION_ID=midterm-migration-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p /data/video-analytics/artifacts/migrations/$MIGRATION_ID
```

Capture repo state:

```bash
git rev-parse HEAD > /data/video-analytics/artifacts/migrations/$MIGRATION_ID/git_head.txt
git branch --show-current > /data/video-analytics/artifacts/migrations/$MIGRATION_ID/git_branch.txt
git status --short > /data/video-analytics/artifacts/migrations/$MIGRATION_ID/git_status_short.txt
git diff --stat > /data/video-analytics/artifacts/migrations/$MIGRATION_ID/git_diff_stat.txt
git bundle create /data/video-analytics/artifacts/migrations/$MIGRATION_ID/repo.bundle --all
```

Capture deploy config checksums:

```bash
sha256sum \
  infra/docker-compose.midterm.yml \
  infra/env/midterm.env \
  modules/savant_replay/config.midterm.json \
  modules/savant_replay/config.midterm.replay-a.json \
  modules/savant_replay/config.midterm.replay-b.json \
  modules/savant_security/module.yml \
  modules/savant_security/config/cameras.midterm.yml \
  > /data/video-analytics/artifacts/migrations/$MIGRATION_ID/deploy_config.sha256
```

Archive required model/config data:

```bash
tar -C /data/video-analytics -czf /data/video-analytics/artifacts/migrations/$MIGRATION_ID/models.tgz \
  models models-savant-b downloads
```

Dump PostgreSQL if the target must preserve cameras, rules, gallery, events, or
operator state:

```bash
pg_dump "$VIDEO_ANALYTICS_DATABASE_URL" \
  --format=custom \
  --file=/data/video-analytics/artifacts/migrations/$MIGRATION_ID/video_analytics.pg_dump
```

If historical evidence must move:

```bash
tar -C /data/video-analytics -czf /data/video-analytics/artifacts/migrations/$MIGRATION_ID/media-history.tgz \
  media/evidence media/face_registration media/face-registration media/face_uploads
```

For a clean performance host, do not archive `media/replay-sink-output` or
`replay-midterm*` unless the test requires continuity from the current runtime.

Generate package checksums:

```bash
cd /data/video-analytics/artifacts/migrations/$MIGRATION_ID
sha256sum * > SHA256SUMS
```

## 5. Target Restore

Restore the repository from normal git remote or from the bundle:

```bash
git clone /path/to/repo.bundle /home/user/video-analytics
cd /home/user/video-analytics
```

Restore model data:

```bash
sudo mkdir -p /data/video-analytics
sudo chown -R "$USER":"$USER" /data/video-analytics
tar -C /data/video-analytics -xzf /path/to/models.tgz
```

Create empty runtime directories when historical media/replay state is not being
restored. The startup script also creates these directories, but creating them
explicitly during restore makes ownership problems visible before startup:

```bash
mkdir -p \
  /data/video-analytics/artifacts \
  /data/video-analytics/media/evidence \
  /data/video-analytics/media/replay-sink-output/midterm \
  /data/video-analytics/media/midterm-snapshots \
  /data/video-analytics/media/face_uploads \
  /data/video-analytics/media/face_registration \
  /data/video-analytics/media/face-registration \
  /data/video-analytics/media/debug \
  /data/video-analytics/media/.trash \
  /data/video-analytics/replay-midterm \
  /data/video-analytics/replay-midterm-a \
  /data/video-analytics/replay-midterm-b
```

Restore PostgreSQL before starting workers:

```bash
createdb video_analytics
pg_restore --clean --if-exists --dbname=video_analytics /path/to/video_analytics.pg_dump
```

If using the compose `local-postgres` profile, restore into that service after it
is started and healthy.

## 6. Target Validation

Run static deployment validation first:

```bash
docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml config
python -m pytest -q harness/tests/test_midterm_deployment_contract.py
```

Start the stack through the supported whole-stack script:

```bash
bash scripts/midterm_start.sh
```

If the target host should use the compose-managed PostgreSQL service instead of
an external/host PostgreSQL, start with:

```bash
bash scripts/midterm_start.sh --local-postgres
```

After the script completes, all operator management should happen through
`http://127.0.0.1:8090/operator`. Do not publish the internal API service on
host port `8000`.

Then run:

```bash
bash scripts/midterm_health.sh
bash scripts/smoke/current/check_midterm_deployment.sh
bash scripts/runtime/doctor_midterm.sh
curl --noproxy '*' -fsS http://127.0.0.1:8090/health
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/runtime/overview
```

Migration baseline acceptance token:

```text
PASS_60R_STEP_0_MIGRATION_BASELINE
```

Emit that token only when:

- compose config renders on the target host;
- required images build or load successfully;
- Savant starts and reports current frame annotations for at least one real RTSP
  source;
- Redis, Replay, API, event-worker, clip-worker, media-worker, and evidence-viewer
  are running;
- a real event can produce an evidence bundle, or the run explicitly records that
  event generation was not triggered and all lower-level runtime checks passed;
- no required runtime path depends on files that were present only on the source
  host and missing from the migration manifest.

After this token exists, start Step A in
`specs/22_midterm_60_stream_readiness_risk_closure_plan.md`.
