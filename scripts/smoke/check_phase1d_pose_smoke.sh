#!/usr/bin/env bash
# Phase 1D smoke test — YOLO26-pose (complex_model) verification.
#
# Static checks:
#   1. module.yml uses nvinfer@complex_model
#   2. module.yml contains layer_names: [output0]
#   3. module.yml contains attributes: and keypoints
#   4. module.yml does NOT have output0: as a YAML key
# Runtime checks (converter-based):
#   5. Container is running (healthy)
#   6. Logs contain stage=phase1d_converter_return or stage=phase1d_converter_summary
#   7. Logs show returned_detections (or last_returned_detections) > 0
#   8. Logs show bbox_tensor_shape (or last_bbox_tensor_shape) = [N, 6]
#   9. Logs show attrs_len (or last_attrs_len) > 0
#  10. Logs show first_keypoints_len=51 or last_first_keypoints_len=51
#  11. WARN if PoseMetadataProbe sees objects=0 (known ordering limitation)
#
# Usage:
#   bash scripts/smoke/check_phase1d_pose_smoke.sh
#   sudo bash scripts/smoke/check_phase1d_pose_smoke.sh

set -euo pipefail

COMPOSE_FILE="infra/docker-compose.phase1d.yml"
MODULE_FILE="modules/savant_phase1d/module.yml"
SERVICE="savant-phase1d"
CONTAINER="phase1d-savant"
PASS=0
FAIL=0
WARN=0

ok()    { PASS=$((PASS + 1)); echo "  [OK] $1"; }
fail()  { FAIL=$((FAIL + 1)); echo "  [FAIL] $1"; }
warn()  { WARN=$((WARN + 1)); echo "  [WARN] $1"; }

echo "=== Phase 1D smoke test ==="
echo ""

# --- 1. Static: nvinfer@complex_model ----------------------------------------
if [ -f "$MODULE_FILE" ] && grep -Eq "element:\s*nvinfer@complex_model" "$MODULE_FILE"; then
    ok "module.yml uses nvinfer@complex_model"
else
    fail "module.yml does not use nvinfer@complex_model"
fi

# --- 2. Static: layer_names: [output0] ---------------------------------------
if [ -f "$MODULE_FILE" ] && grep -Eq "layer_names:\s*\[output0\]" "$MODULE_FILE"; then
    ok "module.yml references layer_names: [output0]"
else
    fail "module.yml missing layer_names: [output0]"
fi

# --- 3. Static: attributes: and keypoints ------------------------------------
if [ -f "$MODULE_FILE" ] && grep -Eq "^\s+attributes:" "$MODULE_FILE"; then
    ok "module.yml contains attributes: section"
else
    fail "module.yml missing attributes: section"
fi
if [ -f "$MODULE_FILE" ] && grep -Eq "name:\s*keypoints" "$MODULE_FILE"; then
    ok "module.yml defines keypoints attribute"
else
    fail "module.yml missing keypoints attribute"
fi

# --- 4. Static: output0: must NOT be a YAML key ------------------------------
if [ -f "$MODULE_FILE" ] && grep -Eq "^\s+output0:" "$MODULE_FILE"; then
    fail "module.yml has output0: as a YAML key (must be inside layer_names only)"
else
    ok "module.yml does not have output0: as a YAML key"
fi

# --- 5. Container health check ------------------------------------------------
INSPECT=$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$CONTAINER" 2>/dev/null || true)

if [ -z "$INSPECT" ]; then
    warn "docker inspect failed — falling back to compose ps"
    if ! docker compose -f "$COMPOSE_FILE" ps 2>/dev/null | grep -q "$CONTAINER"; then
        fail "Container $CONTAINER is not running (try: sudo bash $0)"
        echo ""
        echo "--- Results: $PASS passed, $FAIL failed ---"
        exit 1
    fi
    warn "Container $CONTAINER appears running but health unknown"
else
    STATUS=$(echo "$INSPECT" | awk '{print $1}')
    HEALTH=$(echo "$INSPECT" | awk '{print $2}')

    if [ "$STATUS" != "running" ]; then
        fail "Container $CONTAINER status=$STATUS (expected running)"
        echo ""
        echo "--- Results: $PASS passed, $FAIL failed ---"
        exit 1
    fi

    if [ "$HEALTH" = "healthy" ]; then
        ok "Container $CONTAINER is running + healthy"
    elif [ -n "$HEALTH" ] && [ "$HEALTH" != "healthy" ]; then
        fail "Container $CONTAINER health=$HEALTH (expected healthy)"
    else
        warn "Container $CONTAINER is running (no health check defined)"
    fi
fi

# --- 6-10. Converter-based log checks ----------------------------------------
LOGS=$(docker compose -f "$COMPOSE_FILE" logs --tail 500 "$SERVICE" 2>/dev/null || true)
if [ -z "$LOGS" ]; then
    fail "Cannot read logs from $SERVICE"
    echo ""
    echo "--- Results: $PASS passed, $FAIL failed ---"
    exit 1
fi

# --- 6. Converter return/summary present -------------------------------------
if echo "$LOGS" | grep -Eq "stage=phase1d_converter_return|stage=phase1d_converter_summary"; then
    ok "Converter output present (phase1d_converter_return or _summary)"
else
    fail "No converter output found (phase1d_converter_return or _summary)"
fi

# --- 7. returned_detections > 0 -----------------------------------------------
if echo "$LOGS" | grep -Eq "returned_detections=[1-9][0-9]*|last_returned_detections=[1-9][0-9]*"; then
    ok "Converter returned_detections > 0"
else
    fail "No returned_detections > 0 found in converter logs"
fi

# --- 8. bbox_tensor_shape = [N, 6] --------------------------------------------
if echo "$LOGS" | grep -Eq "bbox_tensor_shape=\[[0-9]+,[[:space:]]*6\]|last_bbox_tensor_shape=\[[0-9]+,[[:space:]]*6\]"; then
    ok "Converter bbox_tensor_shape has 6 columns (class_id, conf, xc, yc, w, h)"
else
    fail "No bbox_tensor_shape=[N,6] found in converter logs"
fi

# --- 9. attrs_len > 0 ---------------------------------------------------------
if echo "$LOGS" | grep -Eq "attrs_len=[1-9][0-9]*|last_attrs_len=[1-9][0-9]*"; then
    ok "Converter attrs_len > 0 (keypoint attributes present)"
else
    fail "No attrs_len > 0 found in converter logs"
fi

# --- 10. first_keypoints_len=51 -----------------------------------------------
if echo "$LOGS" | grep -Eq "first_keypoints_len=51|last_first_keypoints_len=51"; then
    ok "Converter keypoints length=51 (17 COCO keypoints x 3)"
else
    fail "No keypoints length=51 found in converter logs"
fi

# --- 11. Warn if probe sees objects=0 (known limitation) --------------------
if echo "$LOGS" | grep "stage=phase1d_pose_probe" | grep -q "objects=0"; then
    warn "PoseMetadataProbe sees objects=0 (pyfunc may run before update-frame-meta)"
fi

# --- Summary -----------------------------------------------------------------
echo ""
echo "--- Results: $PASS passed, $FAIL failed, $WARN warnings ---"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
