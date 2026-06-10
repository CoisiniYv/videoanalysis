#!/usr/bin/env bash
#
# Phase 1A — Savant GPU smoke test
#
# Tests:
#   1. host nvidia-smi
#   2. CUDA container GPU access  (nvidia/cuda:12.4.1-base-ubuntu22.04)
#   3. Savant DeepStream GPU access
#      (ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1)
#
# The Savant DeepStream image defines its own default entrypoint
# (savant/entrypoint/run.py), so nvidia-smi must be invoked via
# --entrypoint nvidia-smi rather than as a positional argument.
#
# Usage:
#   bash harness/smoke/savant_gpu_smoke.sh          # skip Savant check if image missing
#   PULL_SAVANT=1 bash harness/smoke/savant_gpu_smoke.sh   # auto-pull Savant image
#

set -euo pipefail

PASS=0
FAIL=0
SKIP=0

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

pass() {
    echo -e "${GREEN}[OK]${NC} $1"
    PASS=$((PASS + 1))
}

fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    FAIL=$((FAIL + 1))
}

skip() {
    echo -e "${YELLOW}[SKIP]${NC} $1"
    SKIP=$((SKIP + 1))
}

SAVANT_IMAGE="ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1"
CUDA_IMAGE="nvidia/cuda:12.4.1-base-ubuntu22.04"

echo "=========================================="
echo " Savant GPU Smoke Test"
echo "=========================================="

# ------------------------------------------------------------------
# 1. host nvidia-smi
# ------------------------------------------------------------------
echo ""
echo "--- host nvidia-smi ---"

if command -v nvidia-smi &>/dev/null; then
    if nvidia-smi &>/dev/null; then
        pass "host nvidia-smi"
    else
        fail "host nvidia-smi (command exited non-zero)"
    fi
else
    fail "host nvidia-smi (not found in PATH)"
fi

# ------------------------------------------------------------------
# 2. CUDA container GPU access
# ------------------------------------------------------------------
echo ""
echo "--- CUDA container GPU access ($CUDA_IMAGE) ---"

if docker run --rm --gpus all "$CUDA_IMAGE" nvidia-smi &>/dev/null; then
    pass "CUDA container GPU access"
else
    fail "CUDA container GPU access"
fi

# ------------------------------------------------------------------
# 3. Savant DeepStream GPU access
# ------------------------------------------------------------------
echo ""
echo "--- Savant DeepStream GPU access ($SAVANT_IMAGE) ---"
echo "  Note: Savant image has its own default entrypoint"
echo "  (savant/entrypoint/run.py). Using --entrypoint nvidia-smi"
echo "  to bypass it for this check."
echo ""

if docker image inspect "$SAVANT_IMAGE" &>/dev/null; then
    IMAGE_AVAILABLE=true
else
    IMAGE_AVAILABLE=false
fi

if [ "$IMAGE_AVAILABLE" = true ]; then
    if docker run --rm --gpus all --entrypoint nvidia-smi "$SAVANT_IMAGE" &>/dev/null; then
        pass "Savant DeepStream GPU access"
    else
        fail "Savant DeepStream GPU access"
    fi
elif [ "${PULL_SAVANT:-0}" = "1" ]; then
    echo "  Pulling Savant image (PULL_SAVANT=1) ..."
    if docker pull "$SAVANT_IMAGE" &>/dev/null; then
        if docker run --rm --gpus all --entrypoint nvidia-smi "$SAVANT_IMAGE" &>/dev/null; then
            pass "Savant DeepStream GPU access (pulled)"
        else
            fail "Savant DeepStream GPU access (pulled but run failed)"
        fi
    else
        fail "Savant DeepStream GPU access (pull failed)"
    fi
else
    skip "Savant DeepStream GPU access (image not pulled locally)"
    echo "       Pull manually with: docker pull $SAVANT_IMAGE"
    echo "       Or re-run with:     PULL_SAVANT=1 bash harness/smoke/savant_gpu_smoke.sh"
fi

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
echo ""
echo "------------------------------------------"
echo -e "  ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}, ${YELLOW}$SKIP skipped${NC}"
echo "------------------------------------------"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
