# Runtime Test Policy

Status: **mandatory** — all P1/P1b/P1c smoke and runtime verification must follow
this policy. Violations are BLOCKED.

## 1. Bind Mount Verification Before Runtime Smoke

All smoke tests involving Savant or long-running worker containers must verify
bind mounts before proceeding.

### 1.1 Procedure

```bash
# 1. Check compose bind mounts
docker compose -f <compose> config | grep -A80 -E "savant|event-worker|face-worker|clip-worker"

# 2. Compare file hash between host and container
sha256sum modules/savant_security/custom/pyfuncs/behavior_rules.py
docker exec <savant_container> sha256sum /opt/savant/src/module/custom/pyfuncs/behavior_rules.py

# 3. Evaluate
# hash match     → MOUNT_OK: rebuild_required=no, restart_required=yes
# hash mismatch  → MOUNT_NOT_ACTIVE: stop and report before continuing
```

### 1.2 Expected Bind Mounts

| Host Path | Container Path | Service |
|-----------|---------------|---------|
| `modules/savant_security/` | `/opt/savant/src/module` | savant-security |
| `services/event-worker/` | `/app` | event-worker (if bind-mounted) |
| `services/face-worker/` | `/app` | face-worker (if bind-mounted) |
| `services/clip-worker/` | `/app` | clip-worker (if bind-mounted) |
| `services/media-worker/` | `/app` | media-worker (if bind-mounted) |

### 1.3 Smoke Output

```text
bind_mount_status: MOUNT_OK | MOUNT_NOT_ACTIVE
rebuild_used: no
pull_used: no
services_restarted: <service list>
worker_rebuild_required: no
```

If worker hash comparison fails:

```text
Result=BLOCKED
Reason=worker_bind_mount_not_active
```

## 2. Restart-Not-Rebuild Default

### 2.1 Default: No Rebuild

Development assumes code is bind-mounted into containers. Modifying these files
does **not** require `docker compose build` or `docker compose up --build`:

```text
modules/savant_security/custom/**/*.py
modules/savant_security/config/*.yml
modules/savant_security/module.yml
services/api/**/*.py
services/event-worker/**/*.py
services/face-worker/**/*.py
services/clip-worker/**/*.py
services/media-worker/**/*.py
scripts/**/*.py
harness/tests/**/*.py
```

Correct action:

1. Verify bind mount is active (§1).
2. Restart the affected long-running container.
3. Clear old output.
4. Re-run smoke.

### 2.2 Only Rebuild When

`docker compose build` or `up --build` is allowed **only** when:

```text
Dockerfile changed
requirements.txt / pyproject.toml / poetry.lock changed
Code path is COPY'd into image (no bind mount)
New system package / apt dependency added
TensorRT engine / native library / compiled artifact changed
Bind mount verified as NOT active (§1)
```

### 2.3 Forbidden

Smoke and phase prompts must **not** default to:

```bash
docker pull ...
docker compose build
docker compose up --build
```

unless rebuild necessity is already proven.

For P1c development smoke, the default is:

```bash
$COMPOSE -f <compose> up -d --no-build --force-recreate
```

`P1C_ALLOW_BUILD=1` is the only gate that permits:

```bash
$COMPOSE -f <compose> up -d --build --force-recreate
```

If a required worker image is missing and build is not explicitly allowed:

```text
Result=BLOCKED
Reason=worker_image_missing_and_build_not_allowed
Hint=rerun with P1C_ALLOW_BUILD=1 or enable worker bind mounts
```

For C1E official replay dev smoke, the same no-build rule applies:

```bash
$COMPOSE -f infra/docker-compose.c1-official-replay-dev.yml up -d --no-build --force-recreate --pull never
```

`C1E_ALLOW_BUILD=1` is the only C1E gate that permits `--build`. If a required
worker image is missing and build is not explicitly allowed:

```text
Result=BLOCKED
Reason=worker_image_missing_and_build_not_allowed
Hint=rerun with C1E_ALLOW_BUILD=1 or enable worker bind mounts
```

## 2.4 Docker Daemon Access

Every runtime smoke that uses Docker must define `detect_docker()` and set:

```bash
DOCKER="docker"
COMPOSE="docker compose"
```

or, when ordinary Docker access fails but sudo Docker works:

```bash
DOCKER="sudo docker"
COMPOSE="sudo docker compose"
```

`detect_docker()` must emit exactly one of:

```text
DOCKER_ACCESS_OK
SUDO_DOCKER_REQUIRED
DOCKER_ACCESS_BLOCKED
```

If both ordinary and sudo Docker are unavailable:

```text
Result=BLOCKED
Reason=docker_daemon_unavailable
```

After `detect_docker()` runs, all Docker runtime commands must use `$DOCKER` or
`$COMPOSE`. Bare `docker ps`, `docker compose`, `docker logs`, `docker exec`,
`docker stop`, `docker rm`, and `docker run` are forbidden outside
`detect_docker()`.

Smoke reports must include:

```text
docker_access:
docker_command_prefix:
compose_command_prefix:
sudo_used:
actual_containers_started:
```

## 3. Restart Policy by Component

### 3.1 Savant PyFunc / Module Changes

Files requiring Savant container restart:

```text
modules/savant_security/custom/pyfuncs/*
modules/savant_security/custom/services/*
modules/savant_security/custom/rules/*
modules/savant_security/custom/adapters/*
modules/savant_security/custom/converters/*
modules/savant_security/config/*
modules/savant_security/module.yml
```

Command:

```bash
docker compose -f <compose> restart savant-security
# or for POC stacks:
docker compose -p <project> -f <compose> restart savant-security
```

### 3.2 Worker Changes

| Modified Service | Restart Container |
|-----------------|-------------------|
| `services/event-worker/` | `event-worker` |
| `services/face-worker/` | `face-worker` |
| `services/clip-worker/` | `clip-worker` |
| `services/media-worker/` | `media-worker` / `finalizer` |

### 3.3 Offline Script Changes (No Restart)

These do **not** require container restart:

```text
scripts/debug/*
scripts/smoke/*
harness/tests/*
```

unless they depend on running container code.

## 4. Fixed RTSP Source for P1/P1b/P1c

### 4.1 Required RTSP Source

All P1b-RTSP and P1c-RTSP tests must use:

```text
rtsp://10.37.57.112:8554/live/1080movie
```

Environment variables:

```env
P1_RTSP_URI=rtsp://10.37.57.112:8554/live/1080movie
P1_INPUT_TYPE=rtsp
```

C1E official replay dev uses the same fixed RTSP input and must not fall back
to a local file:

```text
C1E_INPUT_TYPE=rtsp
C1E_INPUT_URI=rtsp://10.37.57.112:8554/live/1080movie
C1E_SOURCE_ID=c1e_rtsp_replay
C1E_CAMERA_ID=cam_c1e_rtsp_replay
```

### 4.2 Smoke Assertions

All P1 RTSP smoke must assert:

```text
input_type == rtsp
input_uri == rtsp://10.37.57.112:8554/live/1080movie
local_file_used == false
test_video_used == false
file_uri_used == false
source_extraction_fallback == false
```

### 4.3 RTSP Unreachable

If RTSP is not reachable:

```text
Result = BLOCKED
Reason = rtsp_unreachable
```

### 4.4 Forbidden Fallbacks

P1 tests must **never** fall back to:

- Local MP4 files
- `file://` URIs
- Test video (`testVideo/`)
- Looping file source
- Previous A2a source clip
- ffmpeg/source extraction

## 5. Replay Cache TTL / Disk Policy

### 5.1 Required Configuration

All Replay POC and production-like compose must configure:

```env
REPLAY_CACHE_TTL_SECONDS=60
REPLAY_MAX_RECORD_SECONDS=15
DEFAULT_PRE_SECONDS=5
DEFAULT_POST_SECONDS=5
REPLAY_ROCKSDB_PATH=/data/video-analytics/replay/rocksdb
```

### 5.2 Replay Config Field Mapping

| Environment Variable | Replay Config Field | Default |
|---------------------|--------------------|---------| 
| `REPLAY_CACHE_TTL_SECONDS` | `storage.rocksdb.data_expiration_ttl.secs` | 60 |
| `REPLAY_ROCKSDB_PATH` | `storage.rocksdb.path` | `/opt/rocksdb` |
| `DEFAULT_PRE_SECONDS` | clip-worker config | 5 |
| `DEFAULT_POST_SECONDS` | clip-worker config | 5 |

### 5.3 Smoke Output

```text
replay_ttl_configured: yes/no
replay_ttl_field: storage.rocksdb.data_expiration_ttl
replay_ttl_seconds: 60
ttl_requirement_seconds: <pre + post + scheduling_margin>
replay_ttl_ok: yes/no
replay_rocksdb_path: /opt/rocksdb (or configured path)
```

If TTL is not configured or cannot be proven:

```text
Result = BLOCKED
Reason = replay_ttl_not_configured
```

If TTL is shorter than the event window plus scheduling margin:

```text
Result = BLOCKED
Reason = replay_ttl_too_short
```

### 5.4 Policy Statement

- Replay is a **short-term cache** of the last N seconds, not permanent storage.
- Replay directories must have TTL / retention policies.
- Replay must **not** replace NVR for long-term recording.
- Replay must **not** default to saving all 60 streams indefinitely.

## 6. POC Container Isolation

### 6.1 Independent Compose Projects

P1/P1b/P1c tests must use independent compose project names:

```bash
docker compose -p p1a-inline -f infra/docker-compose.p1a-replay-inline-poc.yml up -d
docker compose -p p1b-rtsp -f infra/docker-compose.p1b-rtsp-replay-manual-sink.yml up -d
docker compose -p p1c-rtsp -f infra/docker-compose.p1c-rtsp-replay-event-evidence.yml up -d
```

### 6.2 Pre-Smoke Container Listing

P1 smoke must print running containers:

```bash
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
```

and verify only expected P1 containers are active, or unrelated C1 containers
are explicitly noted as ignored.

## 7. P1c Single-Event Evidence POC Scope

P1c-RTSP is a single-event evidence POC. It proves event-triggered Replay raw
clip generation and DB `clip_path` update.

It is not:

- continuous recording
- incident coalescing
- production multi-event merge policy

P1c smoke must report and enforce:

```text
events_created:
record_requests_created:
replay_jobs_created:
evidence_bundles_created:
extra_clips_detected:
```

For P1c.2:

```text
record_requests_created == 1
replay_jobs_created == 1
evidence_bundles_created == 1
extra_clips_detected == 0
```

If more than one evidence bundle is produced:

```text
Result = FAIL
Reason = uncontrolled_clip_generation
```

P2 - Incident Window and Recording Coalescing is the phase for `merge_window`,
`cooldown`, `incident_id`, `first_event_ts`, `last_event_ts`, one clip for many
events, and `event_annotation.json` with `events[]`.

### 6.3 Explicit Container Names

When C1 and P1 stacks run simultaneously, smoke must use explicit P1 container
names:

```text
p1*-redis
p1*-replay-service
p1*-savant-security
p1*-video-file-sink
```

Bare names are **forbidden**:

```text
redis          ← forbidden
postgres       ← forbidden
savant         ← forbidden
replay-service ← forbidden
```

### 6.4 Smoke Output

```text
compose_project: <project-name>
redis_container: <explicit-name>
replay_container: <explicit-name>
savant_container: <explicit-name>
video_file_sink_container: <explicit-name>
```

## 7. Docker Daemon Access / Sudo Policy

### 7.1 Pre-Smoke Docker Access Check

All Docker runtime smoke must check Docker daemon access before proceeding:

```bash
docker ps >/tmp/docker_ps_check.out 2>/tmp/docker_ps_check.err
```

If that fails:

```bash
sudo docker ps >/tmp/sudo_docker_ps_check.out 2>/tmp/sudo_docker_ps_check.err
```

Output must be one of:

```text
DOCKER_ACCESS_OK
SUDO_DOCKER_REQUIRED
DOCKER_ACCESS_BLOCKED
```

### 7.2 Sudo Fallback

If `docker ps` fails but `sudo docker ps` succeeds, all subsequent Docker
commands must use sudo consistently:

```bash
DOCKER="sudo docker"
COMPOSE="sudo docker compose"
```

Mixed usage is forbidden — the script must not use `sudo docker` in the first
half and bare `docker` in the second half.

### 7.3 Blocked State

If both `docker` and `sudo docker` fail:

```text
Result = BLOCKED
Reason = docker_daemon_unavailable
```

**No further execution allowed.** Specifically forbidden:

- Mocking container states
- Running only contract tests and claiming smoke PASS
- Using local source extraction instead of Replay
- Reusing old output directories as if from a new run

### 7.4 Smoke Script Pattern

All P1 smoke scripts must detect Docker access at startup:

```bash
detect_docker() {
  if docker ps >/dev/null 2>&1; then
    DOCKER="docker"
    COMPOSE="docker compose"
    DOCKER_ACCESS="DOCKER_ACCESS_OK"
    SUDO_USED="no"
  elif sudo docker ps >/dev/null 2>&1; then
    DOCKER="sudo docker"
    COMPOSE="sudo docker compose"
    DOCKER_ACCESS="SUDO_DOCKER_REQUIRED"
    SUDO_USED="yes"
  else
    echo "[BLOCKED] Docker daemon unavailable"
    exit 2
  fi
}
```

All subsequent commands must use `$DOCKER` and `$COMPOSE`, not bare `docker`.

### 7.5 Smoke Output

```text
docker_access: DOCKER_ACCESS_OK | SUDO_DOCKER_REQUIRED | DOCKER_ACCESS_BLOCKED
docker_command_prefix: docker | sudo docker
compose_command_prefix: docker compose | sudo docker compose
sudo_used: yes/no
actual_containers_started: yes/no
```

### 7.6 Explicit Acknowledgment

When Codex / Claude Code needs to run P1 smoke, it must state:

```text
This smoke requires Docker daemon access.
If direct docker access fails, I will use sudo docker / sudo docker compose.
This will start POC containers, connect to RTSP, and write media outputs
under /data/video-analytics/media.
```

Silent failure is forbidden.

## 8. Smoke Output Requirements

### 8.1 All P1 Smokes

```text
input_type:
input_uri:
local_file_used:
test_video_used:
source_extraction_fallback:
second_rtsp_pull:
bind_mount_status:
rebuild_used:
pull_used:
services_restarted:
replay_ttl_configured:
replay_ttl_seconds:
replay_rocksdb_path:
compose_project:
redis_container:
replay_container:
savant_container:
video_file_sink_container:
docker_access:
docker_command_prefix:
compose_command_prefix:
sudo_used:
actual_containers_started:
```

### 8.2 P1b/P1c Video Output

```text
video_file:
video_size:
video_duration:
ffprobe_status:
metadata_json:
```

### 8.3 P1c DB Output

```text
event_id:
record_request_id:
events.clip_path:
payload.media.clip_status:
payload.media.recording_strategy:
payload.media.evidence_dir:
```

## 9. P1 Phase Distinctions

| Phase | Input | Proves | Does Not Prove |
|-------|-------|--------|----------------|
| P1a | Local test video | Replay inline pass-through topology | RTSP evidence |
| P1b-local | Local test video | Replay manual job → video file sink | RTSP evidence |
| P1b-RTSP | RTSP | Replay manual job from real RTSP source | Event-triggered clip |
| P1c-RTSP | RTSP | Full event-triggered replay evidence bundle | Production deployment |

- P1b-local only proves local/test source capability.
- P1b-RTSP is the RTSP Replay manual sink proof.
- P1c-RTSP must inherit the fixed RTSP source.
- P1c must not use source extraction fallback.
- P1c must not use a second RTSP pull.
- Replay TTL must be explicitly configured in all P1 compose.
- Code changes require restart, not default rebuild.

## 10. Commit Rules

### 9.1 Forbidden

```bash
git add .
```

### 9.2 Allowed

```text
docs/runtime_test_policy.md
docs/p1*.md
CLAUDE.md
specs/07_deployment.md
specs/09_harness.md
specs/11_docker_communication_and_clip_design.md
harness/tests/test_runtime_test_policy_contract.py
```

### 9.3 Never Commit

```text
/data/**
media/**
*.mp4
*.mov
*.webm
*.jpg
replay rocksdb data
replay sink output
evidence output
```
