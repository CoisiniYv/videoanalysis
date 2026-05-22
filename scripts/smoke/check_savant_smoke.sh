#!/usr/bin/env bash
#
# Phase 1B — container self-check script (runs INSIDE savant-smoke)
#
# Verifies:
#   1. nvidia-smi is functional
#   2. Required mount points exist and are writable
#   3. Module directory structure is present
#   4. module.yml is parseable (python yaml check)
#
# Intended use: docker compose exec savant-smoke bash scripts/smoke/check_savant_smoke.sh
# Or directly if the working directory is /opt/savant/src/module/../../
#

set -euo pipefail

PASS=0
FAIL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { echo -e "${GREEN}[OK]${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "${RED}[FAIL]${NC} $1"; FAIL=$((FAIL + 1)); }

echo "=========================================="
echo " Savant Smoke — Container Self Check"
echo "=========================================="

# ------------------------------------------------------------------
# 1. GPU access via nvidia-smi
# ------------------------------------------------------------------
echo ""
echo "--- GPU ---"

if nvidia-smi >/dev/null 2>&1; then
    NVIDIA_OUTPUT=$(nvidia-smi --query-gpu=gpu_name,driver_version --format=csv,noheader 2>/dev/null | head -1)
    pass "nvidia-smi — $NVIDIA_OUTPUT"
else
    fail "nvidia-smi failed"
fi

# ------------------------------------------------------------------
# 2. Mount points
# ------------------------------------------------------------------
echo ""
echo "--- Mounts ---"

check_mount() {
    local path="$1"
    local label="$2"
    if [ -d "$path" ]; then
        if touch "$path/.smoke_write_test" 2>/dev/null; then
            rm -f "$path/.smoke_write_test"
            pass "$label ($path) — present and writable"
        else
            fail "$label ($path) — present but NOT writable"
        fi
    else
        fail "$label ($path) — does not exist"
    fi
}

check_mount "/opt/savant/src/module"    "Module directory"
check_mount "/models"                   "Models volume"
check_mount "/downloads"                "Downloads volume"
check_mount "/media"                    "Media volume"

# ------------------------------------------------------------------
# 3. Module file structure
# ------------------------------------------------------------------
echo ""
echo "--- Module structure ---"

MODULE_BASE="/opt/savant/src/module"

check_file() {
    local path="$1"
    local label="$2"
    if [ -f "$path" ]; then
        pass "$label ($path)"
    else
        fail "$label ($path) — missing"
    fi
}

check_file "$MODULE_BASE/module.yml"            "module.yml"
check_file "$MODULE_BASE/config/cameras.yml"    "cameras.yml"
check_dir() {
    local path="$1"
    local label="$2"
    if [ -d "$path" ]; then
        # Check for __init__.py as a sanity signal
        if [ -f "$path/__init__.py" ]; then
            pass "$label ($path) — has __init__.py"
        else
            pass "$label ($path) — directory present (no __init__.py)"
        fi
    else
        fail "$label ($path) — missing"
    fi
}

check_dir "$MODULE_BASE/custom"                     "custom package"
check_dir "$MODULE_BASE/custom/converters"           "converters package"
check_dir "$MODULE_BASE/custom/pyfuncs"              "pyfuncs package"
check_dir "$MODULE_BASE/custom/rules"                "rules package"

# ------------------------------------------------------------------
# 4. module.yml basic syntax check (if python is available)
# ------------------------------------------------------------------
echo ""
echo "--- module.yml syntax ---"

if command -v python3 &>/dev/null; then
    if python3 -c "import yaml; yaml.safe_load(open('$MODULE_BASE/module.yml'))" 2>/dev/null; then
        pass "module.yml — valid YAML"
    else
        fail "module.yml — YAML parse error"
    fi
elif command -v python &>/dev/null; then
    if python -c "import yaml; yaml.safe_load(open('$MODULE_BASE/module.yml'))" 2>/dev/null; then
        pass "module.yml — valid YAML"
    else
        fail "module.yml — YAML parse error"
    fi
else
    pass "module.yml — file exists (skipped YAML parse: no python in container)"
fi

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
echo ""
echo "------------------------------------------"
echo -e "  ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}"
echo "------------------------------------------"

[ "$FAIL" -eq 0 ]
