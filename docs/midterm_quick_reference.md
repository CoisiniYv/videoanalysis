# Midterm Deployment Quick Reference

## One-Click Scripts

Three convenience scripts for managing the midterm deployment:

### 1. Start Deployment

```bash
bash scripts/midterm_start.sh
```

**What it does:**
- Checks prerequisites (Docker, Docker Compose, curl, `ss`, NVIDIA visibility)
- Creates required runtime directories under `/data/video-analytics`
- Validates required model assets and YOLOv8-Face stable symlinks
- Validates compose config with `infra/env/midterm.env`
- Builds `face-worker` first, then builds the remaining service images
- Starts all services
- Waits for the 8090 portal health check and API proxy readiness
- Displays service status

**After success:**
- Operator portal: http://127.0.0.1:8090/operator
- API health: http://127.0.0.1:8090/health
- Runtime overview: http://127.0.0.1:8090/api/v1/runtime/overview

After startup, use the 8090 operator portal for camera, people/face, evidence,
storage maintenance, and controlled runtime management. Do not expose the
internal API service on host port 8000.

### 2. Stop Deployment

```bash
bash scripts/midterm_stop.sh [OPTIONS]
```

**Options:**
- (no options): Stop containers only, preserve volumes/images
- `-v, --volumes`: Remove volumes (WARNING: deletes Redis/Replay data)
- `-i, --images`: Remove built images
- `-a, --all`: Remove both volumes and images
- `-h, --help`: Show help

**Default behavior:** Graceful shutdown, preserves data for next restart.

**Full cleanup example:**
```bash
bash scripts/midterm_stop.sh --all
```

### 3. Health Check

```bash
bash scripts/midterm_health.sh
```

**Checks performed:**
- ✓ Container status (12 default services)
- ✓ Port availability (6396, 8090, 8098, 18080, 18081)
- ✓ Redis connectivity and streams
- ✓ Database connectivity (via API)
- ✓ API endpoints (/health, /runtime/overview, /cameras, /people, /events)
- ✓ Camera configuration status
- ✓ Face gallery registration status
- ✓ Runtime metrics (Savant FPS, Replay status)
- ✓ GPU availability and utilization
- ✓ Disk space usage

**Output:** Color-coded pass/warn/fail summary with actionable info.

## Quick Management Commands

### Check what's running:
```bash
docker compose -f infra/docker-compose.midterm.yml ps
```

### View logs:
```bash
# All services
docker compose -f infra/docker-compose.midterm.yml logs -f

# Specific service
docker compose -f infra/docker-compose.midterm.yml logs -f savant-security
docker compose -f infra/docker-compose.midterm.yml logs -f api
docker compose -f infra/docker-compose.midterm.yml logs -f event-worker
```

### Restart single service (no rebuild):
```bash
docker compose -f infra/docker-compose.midterm.yml up -d --no-build --force-recreate --no-deps <service>
```

### Restart after code change:
```bash
# Only if Dockerfile/requirements/base image changed
docker compose -f infra/docker-compose.midterm.yml up -d --build <service>
```

## API Endpoints for Camera/People Management

### Camera Management

**List cameras:**
```bash
curl --noproxy '*' http://127.0.0.1:8090/api/v1/cameras | jq
```

**Add camera:**
```bash
curl --noproxy '*' -X POST http://127.0.0.1:8090/api/v1/cameras \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Camera 1",
    "rtsp_url": "rtsp://192.168.1.100:554/stream",
    "enabled": true
  }' | jq
```

**Get camera config:**
```bash
curl --noproxy '*' http://127.0.0.1:8090/api/v1/cameras/1/config | jq
```

### People/Face Management

**List registered people:**
```bash
curl --noproxy '*' http://127.0.0.1:8090/api/v1/people | jq
```

**Register face:**
```bash
curl --noproxy '*' -X POST http://127.0.0.1:8090/api/v1/people/register-face \
  -F "image=@/path/to/photo.jpg" \
  -F "full_name=John Doe" \
  -F "external_person_id=employee_001" | jq
```

**Get person details:**
```bash
curl --noproxy '*' http://127.0.0.1:8090/api/v1/people/1 | jq
```

### Events and Evidence

**List recent events:**
```bash
curl --noproxy '*' 'http://127.0.0.1:8090/api/v1/events?limit=10' | jq
```

**Get evidence bundle:**
```bash
curl --noproxy '*' http://127.0.0.1:8090/api/v1/evidence/bundles/<event_id> | jq
```

## Troubleshooting

### Port conflicts
If startup fails with port conflicts:
```bash
# Check what's using the ports
ss -ltnp | grep -E ':(6396|8090|8098|18080|18081)'

# Stop old deployment
bash scripts/midterm_stop.sh
```

### Savant not processing frames
```bash
# Check Savant logs
docker compose -f infra/docker-compose.midterm.yml logs -f savant-security

# Check supervisor status
docker compose -f infra/docker-compose.midterm.yml logs -f api | grep -i savant

# Trigger recovery
curl --noproxy '*' -X POST http://127.0.0.1:8090/api/v1/maintenance/savant/recover
```

### No cameras showing
```bash
# Check if cameras are configured in database
curl --noproxy '*' http://127.0.0.1:8090/api/v1/cameras | jq '.data | length'

# Check if runtime config applied
docker compose -f infra/docker-compose.midterm.yml logs api | grep -i "runtime apply"
```

### Face recognition not working
```bash
# Check gallery status
curl --noproxy '*' http://127.0.0.1:8090/api/v1/people | jq '.data | length'

# Check face worker logs
docker compose -f infra/docker-compose.midterm.yml logs -f face-worker

# Check if face models loaded
docker compose -f infra/docker-compose.midterm.yml logs api | grep -i "adaface\|yolov8"
```

## Typical Workflow

### First-time setup:
```bash
# 1. Start everything
bash scripts/midterm_start.sh

# 2. Check health
bash scripts/midterm_health.sh

# 3. Add camera via operator portal (http://127.0.0.1:8090/operator)
#    or via API

# 4. Register people for face recognition
```

### Daily operation:
```bash
# Morning: Start
bash scripts/midterm_start.sh

# Check if everything healthy
bash scripts/midterm_health.sh

# Evening: Stop (preserve data)
bash scripts/midterm_stop.sh
```

### After code changes:
```bash
# 1. Stop services
bash scripts/midterm_stop.sh

# 2. Commit changes
git add -A && git commit -m "..."

# 3. Restart (will rebuild changed services)
bash scripts/midterm_start.sh

# 4. Verify
bash scripts/midterm_health.sh
```

## Migration Package Prep

Before creating migration package (see `docs/midterm_migration_runbook_2026-06-23.md`):

```bash
# 1. Ensure clean state
git status --short

# 2. Commit all changes or stash
git add -A && git commit -m "Migration candidate: ..."

# 3. Run health check
bash scripts/midterm_health.sh

# 4. Create package
export MIGRATION_ID=midterm-migration-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p /data/video-analytics/artifacts/migrations/$MIGRATION_ID

# ... follow runbook Section 4
```

## Quick Links

- **Operator Portal:** http://127.0.0.1:8090/operator
- **API Docs:** http://127.0.0.1:8090/docs
- **Health:** http://127.0.0.1:8090/health
- **Runtime Overview:** http://127.0.0.1:8090/api/v1/runtime/overview
- **Evidence Viewer:** http://127.0.0.1:8098/
