#!/usr/bin/env bash
#
# Phase 1B — Savant smoke acceptance test (runs on HOST)
#
# Verifies:
#   1. compose config is valid
#   2. compose up succeeds
#   3. Container stays healthy (nvidia-smi inside container)
#   4. Volume mounts are correct
#   5. Module skeleton is present
#   6. Clean shutdown works
#

set -euo pipefail

COMPOSE_FILE="infra/docker-compose.savant-smoke.yml"
COMPOSE_PROJECT="phase1b-savant-smoke"
SERVICE="savant-smoke"
# Override via env: DC="$DC" bash test_savant_smoke.sh
DC="${DC:-sudo $DC}"

PASS=0
FAIL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { echo -e "${GREEN}[OK]${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "${RED}[FAIL]${NC} $1"; FAIL=$((FAIL + 1)); }

cleanup() {
    echo ""
    echo "--- Cleanup ---"
    $DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" down 2>/dev/null || true
}
trap cleanup EXIT

echo "=========================================="
echo " Phase 1B — Savant Smoke Acceptance Test"
echo "=========================================="

# ------------------------------------------------------------------
# 0. Pre-flight: ensure host data directories exist
# ------------------------------------------------------------------
echo ""
echo "--- Pre-flight ---"

for dir in /data/video-analytics/models /data/video-analytics/downloads /data/video-analytics/media; do
    if [ ! -d "$dir" ]; then
        echo "  Creating $dir ..."
        sudo mkdir -p "$dir"
    fi
done
pass "host data directories ready"

# ------------------------------------------------------------------
# 1. compose config validation
# ------------------------------------------------------------------
echo ""
echo "--- Compose config ---"

if $DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" config >/dev/null 2>&1; then
    pass "compose config is valid"
else
    fail "compose config validation failed"
    exit 1
fi

# ------------------------------------------------------------------
# 2. compose up
# ------------------------------------------------------------------
echo ""
echo "--- Compose up ---"

if $DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" up -d 2>&1; then
    pass "compose up succeeded"
else
    fail "compose up failed"
    exit 1
fi

# ------------------------------------------------------------------
# 3. Wait for healthy
# ------------------------------------------------------------------
echo ""
echo "--- Container health ---"

sleep 3
if $DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" ps --format json 2>/dev/null | grep -q '"Health":"healthy"'; then
    pass "container healthcheck passed"
else
    # Extra wait, GPU init can be slow
    echo "  Waiting 10s for healthcheck..."
    sleep 10
    if $DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" ps --format json 2>/dev/null | grep -q '"Health":"healthy"'; then
        pass "container healthcheck passed (after delay)"
    else
        $DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" ps
        fail "container healthcheck failed"
        exit 1
    fi
fi

# ------------------------------------------------------------------
# 4. nvidia-smi inside container
# ------------------------------------------------------------------
echo ""
echo "--- GPU inside container ---"

if $DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" exec -T "$SERVICE" nvidia-smi >/dev/null 2>&1; then
    GPU_INFO=$($DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" exec -T "$SERVICE" nvidia-smi --query-gpu=gpu_name --format=csv,noheader 2>/dev/null | head -1)
    pass "nvidia-smi inside container — GPU: $GPU_INFO"
else
    fail "nvidia-smi inside container failed"
fi

# ------------------------------------------------------------------
# 5. Mount points inside container
# ------------------------------------------------------------------
echo ""
echo "--- Mounts inside container ---"

check_mount() {
    local path="$1"
    local label="$2"
    if $DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" exec -T "$SERVICE" test -d "$path" 2>/dev/null; then
        if $DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" exec -T "$SERVICE" sh -c "touch '$path/.smoke_test' && rm -f '$path/.smoke_test'" 2>/dev/null; then
            pass "$label ($path) — mounted and writable"
        else
            pass "$label ($path) — mounted (read-only?)"
        fi
    else
        fail "$label ($path) — NOT mounted"
    fi
}

check_mount "/opt/savant/src/module"    "Module directory"
check_mount "/opt/savant/src/scripts"   "Scripts directory"
check_mount "/models"                   "Models volume"
check_mount "/downloads"                "Downloads volume"
check_mount "/media"                    "Media volume"

# ------------------------------------------------------------------
# 6. Module file structure inside container
# ------------------------------------------------------------------
echo ""
echo "--- Module structure inside container ---"

check_file() {
    local path="$1"
    local label="$2"
    if $DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" exec -T "$SERVICE" test -f "$path" 2>/dev/null; then
        pass "$label ($path)"
    else
        fail "$label ($path) — missing"
    fi
}

check_file "/opt/savant/src/module/module.yml"           "module.yml"
check_file "/opt/savant/src/module/config/cameras.yml"  "cameras.yml"
check_file "/opt/savant/src/scripts/smoke/check_savant_smoke.sh" "self-check script"

# ------------------------------------------------------------------
# 7. Run self-check script inside container (if python with yaml is available)
# ------------------------------------------------------------------
echo ""
echo "--- Self-check inside container ---"

if $DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" exec -T "$SERVICE" \
    bash /opt/savant/src/scripts/smoke/check_savant_smoke.sh 2>/dev/null; then
    pass "container self-check script passed"
else
    fail "container self-check script failed (some checks inside container failed)"
fi

# ------------------------------------------------------------------
# 8. Container stability (not restarting)
# ------------------------------------------------------------------
echo ""
echo "--- Stability ---"

STATUS=$($DC -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" ps --format json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('Status','unknown'))" 2>/dev/null || echo "unknown")
if echo "$STATUS" | grep -qi "up"; then
    pass "container status: $STATUS"
else
    fail "container not stable (status: $STATUS)"
fi

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
echo ""
echo "=========================================="
echo -e "  ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}"
echo "=========================================="

[ "$FAIL" -eq 0 ]
