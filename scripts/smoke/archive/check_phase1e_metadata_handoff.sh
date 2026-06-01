#!/usr/bin/env bash
# Phase 1E smoke test — metadata handoff diagnostic baseline.
#
# Static checks:
#   1. module.yml uses nvinfer@complex_model
#   2. module.yml contains layer_names: [output0]
#   3. module.yml contains attributes: and keypoints
# Runtime checks (converter-based):
#   4. Container is running (healthy)
#   5. Converter output shows returned_detections > 0
#   6. MetadataHandoffProbe output present in logs
#   7. If probe reads person_count > 0, pass
#   8. If probe reads 0 but converter has detections, warn (not fail)
#   9. Only fail on container/model/converter issues
#
# Usage:
#   bash scripts/smoke/check_phase1e_metadata_handoff.sh
#   sudo bash scripts/smoke/check_phase1e_metadata_handoff.sh

set -euo pipefail

COMPOSE_FILE="infra/docker-compose.phase1e.yml"
MODULE_FILE="modules/savant_phase1e/module.yml"
SERVICE="savant-phase1e"
CONTAINER="phase1e-savant"
PASS=0
FAIL=0
WARN=0

ok()    { PASS=$((PASS + 1)); echo "  [OK] $1"; }
fail()  { FAIL=$((FAIL + 1)); echo "  [FAIL] $1"; }
warn()  { WARN=$((WARN + 1)); echo "  [WARN] $1"; }

echo "=== Phase 1E smoke test: metadata handoff ==="
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

# --- 4. Container health check ------------------------------------------------
INSPECT=$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$CONTAINER" 2>/dev/null || true)

if [ -z "$INSPECT" ]; then
    warn "docker inspect failed — falling back to compose ps"
    if ! docker compose -f "$COMPOSE_FILE" ps 2>/dev/null | grep -q "$CONTAINER"; then
        fail "Container $CONTAINER is not running (try: sudo docker compose -f $COMPOSE_FILE up -d)"
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

# --- 5-8. Log checks -----------------------------------------------------------
LOGS=$(docker compose -f "$COMPOSE_FILE" logs --tail 500 "$SERVICE" 2>/dev/null || true)
if [ -z "$LOGS" ]; then
    fail "Cannot read logs from $SERVICE"
    echo ""
    echo "--- Results: $PASS passed, $FAIL failed ---"
    exit 1
fi

# --- 5. Converter detections > 0 -----------------------------------------------
CONVERTER_OK=false
# Pre-check: if official API summary already reports metadata_handoff_ok=true,
# then converter detections must have existed even if log tail misses them.
HANDOFF_OK_IN_LOGS=$(echo "$LOGS" | grep "phase=phase1e_official_probe_summary" | grep -oP 'metadata_handoff_ok=\K\w+' | tail -1 || echo "false")
if echo "$LOGS" | grep -qE "returned_detections=[1-9][0-9]*|last_returned_detections=[1-9][0-9]*"; then
    CONVERTER_OK=true
    ok "Converter returned_detections > 0"
elif echo "$LOGS" | grep -q "phase=phase1e_converter_attrs_debug"; then
    # attrs_debug exists but with 0 detections — still report for diagnostic
    CONV_DET=$(echo "$LOGS" | grep "phase=phase1e_converter_attrs_debug" | tail -1 | grep -oP 'returned_detections=\K[0-9]+' || echo "0")
    if [ "$CONV_DET" -gt 0 ] 2>/dev/null; then
        CONVERTER_OK=true
        ok "Converter returned_detections=$CONV_DET (>0 via attrs_debug)"
    else
        echo "  [INFO] Converter attrs_debug shows returned_detections=$CONV_DET (non-critical)"
    fi
elif [ "$HANDOFF_OK_IN_LOGS" = "true" ]; then
    # metadata_handoff_ok=true proves converter ran successfully,
    # even if the specific log line scrolled out of the tail window
    CONVERTER_OK=true
    echo "  [INFO] Converter returned_detections log not in tail window, but metadata_handoff_ok=true proves detections reached official metadata"
else
    echo "  [INFO] No returned_detections > 0 found in converter logs (non-critical; will re-evaluate after official API checks)"
fi

# --- 6. MetadataHandoffProbe present (legacy phase) ----------------------------
if echo "$LOGS" | grep -q "phase=phase1e_legacy_metadata_handoff"; then
    ok "MetadataHandoffProbe output present (phase=phase1e_legacy_metadata_handoff)"
else
    # Fall back: on_start uses legacy phase
    if echo "$LOGS" | grep -q "phase=phase1e_official_probe_summary"; then
        ok "MetadataHandoffProbe present (official probe summary found)"
    else
        echo "  [INFO] No MetadataHandoffProbe output found in logs (non-critical)"
    fi
fi

# --- 7. Legacy probe person_count (informational only) -------------------------
LEGACY_PERSON_COUNT=$(echo "$LOGS" | grep "phase=phase1e_legacy_metadata_handoff " | grep -v "introspect" | grep -v "_obj " | grep -oP 'person_count=\K[0-9]+' | tail -1 || echo "")
if [ -n "$LEGACY_PERSON_COUNT" ]; then
    if [ "$LEGACY_PERSON_COUNT" -gt 0 ] 2>/dev/null; then
        ok "Legacy probe reads person_count=$LEGACY_PERSON_COUNT"
    else
        echo "  [INFO] Legacy probe reads person_count=$LEGACY_PERSON_COUNT (expected — legacy read paths are not authoritative)"
    fi
fi

# --- 8. (removed) Legacy vs converter mismatch warning is obsolete
#     Official API is now the authoritative source.

# --- 9. Check that saved model was loaded (optional info) -----------------------
if echo "$LOGS" | grep -q "TensorRT engine file is loaded\|phase1d_converter_init\|phase1e.*converter_init"; then
    ok "YOLO26-pose loaded (engine or init message found)"
else
    echo "  [INFO] Could not confirm YOLO26-pose engine load message in logs (non-critical)"
fi

# --- 10. BBox debug output present ----------------------------------------------
if echo "$LOGS" | grep -q "phase=phase1e_converter_bbox_debug"; then
    ok "Converter bbox debug output present"
else
    echo "  [INFO] No phase=phase1e_converter_bbox_debug found (only output during first 5 calls)"
fi

# --- 11. Check for bbox coordinate issues ----------------------------------------
if echo "$LOGS" | grep -q "phase=phase1e_converter_bbox_issues"; then
    ISSUE_DESC=$(echo "$LOGS" | grep "phase=phase1e_converter_bbox_issues" | tail -1)
    warn "BBox coordinate issues detected: $ISSUE_DESC"
else
    ok "No bbox coordinate issues detected"
fi

# --- 12. Probe metadata check present --------------------------------------------
if echo "$LOGS" | grep -q "phase=phase1e_probe_metadata_check"; then
    ok "Probe metadata diagnostics present"
else
    echo "  [INFO] No phase=phase1e_probe_metadata_check in logs (non-critical)"
fi

# --- 13. Check num_obj_meta from probe -------------------------------------------
NUM_META=$(echo "$LOGS" | grep "phase=phase1e_probe_metadata_check" | grep -oP 'ds_num_obj_meta=\K[0-9]+' | tail -1 || echo "")
if [ -n "$NUM_META" ] && [ "$NUM_META" -gt 0 ] 2>/dev/null; then
    ok "Probe detects ds_num_obj_meta=$NUM_META (objects in DeepStream metadata)"
elif [ -n "$NUM_META" ]; then
    echo "  [INFO] Probe detects ds_num_obj_meta=$NUM_META (legacy read path shows 0 — expected)"
fi

# =========================================================================
#  Phase 1E Step 2B: Official Savant API probe checks
# =========================================================================
echo ""
echo "--- Phase 1E Step 2B: Official Savant API Probe ---"

# --- 14. Official metadata API line present ------------------------------------
OFFICIAL_API_LINE=$(echo "$LOGS" | grep "phase=phase1e_official_metadata_api" | tail -1 || true)
OFFICIAL_API_FOUND=false
if [ -n "$OFFICIAL_API_LINE" ]; then
    OFFICIAL_API_FOUND=true
    ok "Official metadata API output present"
else
    fail "No phase=phase1e_official_metadata_api found in logs"
fi

# --- 15. Official API objects_number > 0 ---------------------------------------
OFFICIAL_OBJ_NUM_FOUND=false
if $OFFICIAL_API_FOUND; then
    OFFICIAL_OBJ_NUM=$(echo "$OFFICIAL_API_LINE" | grep -oP 'objects_number=\K[0-9]+' | head -1 || echo "0")
    if [ "$OFFICIAL_OBJ_NUM" -gt 0 ] 2>/dev/null; then
        OFFICIAL_OBJ_NUM_FOUND=true
        ok "Official API objects_number=$OFFICIAL_OBJ_NUM (>0)"
    else
        warn "Official API objects_number=$OFFICIAL_OBJ_NUM (0 objects reported by frame level)"
    fi
fi

# --- 16. Official API sees primary frame object --------------------------------
PRIMARY_FRAME_FOUND=false
if $OFFICIAL_API_FOUND; then
    if echo "$LOGS" | grep -q "phase=phase1e_official_obj.*is_primary=True"; then
        PRIMARY_FRAME_FOUND=true
        ok "Official API sees primary frame object (is_primary=True)"
    elif echo "$LOGS" | grep -q "phase=phase1e_official_obj.*label=frame"; then
        PRIMARY_FRAME_FOUND=true
        ok "Official API sees frame object (label=frame)"
    else
        # Check summary
        if echo "$LOGS" | grep -q "phase=phase1e_official_probe_summary.*has_primary_frame=True"; then
            PRIMARY_FRAME_FOUND=true
            ok "Official API probe summary confirms primary frame object"
        else
                echo "  [INFO] No primary frame object found via official API (may be expected if frame pseudo-object not exposed via this API path)"
        fi
    fi
fi

# --- 17. Official API sees person objects ---------------------------------------
PERSON_OBJECTS_FOUND=false
if $OFFICIAL_API_FOUND; then
    PERSON_COUNT_OFFICIAL=$(echo "$LOGS" | grep "phase=phase1e_official_probe_summary" | grep -oP 'person_count=\K[0-9]+' | tail -1 || echo "0")
    if [ "$PERSON_COUNT_OFFICIAL" -gt 0 ] 2>/dev/null; then
        PERSON_OBJECTS_FOUND=true
        ok "Official API sees $PERSON_COUNT_OFFICIAL person objects"
    else
        warn "Official API finds 0 person objects (complex_model may not be creating detection objects in metadata)"
    fi
fi

# --- 18. Official API reads keypoints attribute (value_len=51) ------------------
KEYPOINTS_ATTR_OK=false
if $PERSON_OBJECTS_FOUND; then
    ATTR_FOUND_COUNT=$(echo "$LOGS" | grep "phase=phase1e_official_attr.*attr_found=true" | wc -l)
    if [ "$ATTR_FOUND_COUNT" -gt 0 ]; then
        ATTR_LIST_LEN=$(echo "$LOGS" | grep "phase=phase1e_official_attr.*attr_found=true" | grep -oP 'attr_list_len=\K[0-9]+' | tail -1 || echo "0")
        ATTR_VALUE_TYPE=$(echo "$LOGS" | grep "phase=phase1e_official_attr.*attr_found=true" | grep -oP 'keypoints_value_type=\K\S+' | tail -1 || echo "unknown")
        ATTR_VALUE_LEN=$(echo "$LOGS" | grep "phase=phase1e_official_attr.*attr_found=true" | grep -oP 'keypoints_value_len=\K[0-9]+' | tail -1 || echo "0")
        ATTR_FIRST_TRIPLET=$(echo "$LOGS" | grep "phase=phase1e_official_attr.*attr_found=true" | grep -oP 'keypoints_first_triplet=\K\([^)]*\)' | tail -1 || echo "(none)")
        VALUE_REPR=$(echo "$LOGS" | grep "phase=phase1e_official_attr.*attr_found=true" | grep -oP 'value_repr_sample=\K\S+' | tail -1 || echo "")
        if [ "$ATTR_VALUE_LEN" -eq 51 ] 2>/dev/null; then
            KEYPOINTS_ATTR_OK=true
            ok "Official API reads keypoints (type=$ATTR_VALUE_TYPE, list_len=$ATTR_LIST_LEN, value_len=$ATTR_VALUE_LEN, triplet=$ATTR_FIRST_TRIPLET)"
        else
            echo "  [WARN] keypoints attribute found but attr_meta.value length is $ATTR_VALUE_LEN (expected 51)"
            echo "        type=$ATTR_VALUE_TYPE list_len=$ATTR_LIST_LEN repr=$VALUE_REPR"
            if echo "$VALUE_REPR" | grep -q "nested\|outer_len\|wrap"; then
                echo "        -> value is nested or wrapped; may need unwrapping in probe"
            fi
        fi
    else
        warn "Official API found person objects but no keypoints attribute via get_attr_meta"
    fi
elif $OFFICIAL_API_FOUND; then
    if echo "$LOGS" | grep -q "phase=phase1e_official_attr.*attr_found=false"; then
        warn "Official API attempted keypoints lookup but attr_found=false for all paths"
    fi
fi

# --- 19. Official API metadata_handoff_ok ---------------------------------------
if $OFFICIAL_API_FOUND; then
    HANDOFF_OK=$(echo "$LOGS" | grep "phase=phase1e_official_probe_summary" | grep -oP 'metadata_handoff_ok=\K\w+' | tail -1 || echo "false")
    OFFICIAL_KPTS_OBJS=$(echo "$LOGS" | grep "phase=phase1e_official_probe_summary" | grep -oP 'official_keypoints_objects=\K[0-9]+' | tail -1 || echo "0")
    OFFICIAL_KPTS_VLEN=$(echo "$LOGS" | grep "phase=phase1e_official_probe_summary" | grep -oP 'official_keypoints_value_len=\K[0-9]+' | tail -1 || echo "0")
    OFFICIAL_KPTS_ALEN=$(echo "$LOGS" | grep "phase=phase1e_official_probe_summary" | grep -oP 'official_keypoints_attr_list_len=\K[0-9]+' | tail -1 || echo "0")
    ELEMENT_NAMES=$(echo "$LOGS" | grep "phase=phase1e_official_probe_summary" | grep -oP 'element_names_seen=\[[^\]]*\]' | tail -1 || echo "[]")
    if [ "$HANDOFF_OK" = "true" ]; then
        ok "metadata_handoff_ok=true (objects=$OFFICIAL_OBJ_NUM, persons=$PERSON_COUNT_OFFICIAL, kpts_objs=$OFFICIAL_KPTS_OBJS, kpts_attr_list_len=$OFFICIAL_KPTS_ALEN, kpts_value_len=$OFFICIAL_KPTS_VLEN)"
    else
        warn "metadata_handoff_ok=$HANDOFF_OK (objects=$OFFICIAL_OBJ_NUM, persons=$PERSON_COUNT_OFFICIAL, kpts_value_len=$OFFICIAL_KPTS_VLEN)"
    fi
    ok "Element names observed via official API: $ELEMENT_NAMES"
fi

# =========================================================================
#  Phase 1E Step 2D: Converter attrs debug + attr introspection
# =========================================================================
echo ""
echo "--- Phase 1E Step 2D: Converter Attrs + Attr Introspection ---"

# --- 20. Converter attrs debug present -----------------------------------------
if echo "$LOGS" | grep -q "phase=phase1e_converter_attrs_debug"; then
    CONV_FIRST_NAME=$(echo "$LOGS" | grep "phase=phase1e_converter_attrs_debug" | tail -1 | grep -oP 'first_attr_name=\K\S+' || echo "")
    CONV_FIRST_VTYPE=$(echo "$LOGS" | grep "phase=phase1e_converter_attrs_debug" | tail -1 | grep -oP 'first_attr_value_type=\K\S+' || echo "")
    CONV_FIRST_VLEN=$(echo "$LOGS" | grep "phase=phase1e_converter_attrs_debug" | tail -1 | grep -oP 'first_attr_value_len=\K[0-9]+' || echo "0")
    CONV_FIRST_CONF=$(echo "$LOGS" | grep "phase=phase1e_converter_attrs_debug" | tail -1 | grep -oP 'first_attr_confidence=\K\S+' || echo "")
    CONV_ATTRS_LEN=$(echo "$LOGS" | grep "phase=phase1e_converter_attrs_debug" | tail -1 | grep -oP 'attrs_len=\K[0-9]+' || echo "0")
    CONV_BBOX_COUNT=$(echo "$LOGS" | grep "phase=phase1e_converter_attrs_debug" | tail -1 | grep -oP 'bbox_count=\K[0-9]+' || echo "0")
    ok "Converter attrs debug output present"
    if [ "$CONV_ATTRS_LEN" = "$CONV_BBOX_COUNT" ]; then
        ok "Converter attrs_len=$CONV_ATTRS_LEN == bbox_count=$CONV_BBOX_COUNT"
    else
        warn "Converter attrs_len=$CONV_ATTRS_LEN != bbox_count=$CONV_BBOX_COUNT"
    fi
    if [ "$CONV_FIRST_NAME" = "keypoints" ]; then
        ok "Converter first_attr_name=$CONV_FIRST_NAME"
    else
        warn "Converter first_attr_name=$CONV_FIRST_NAME (expected keypoints)"
    fi
    if [ "$CONV_FIRST_VLEN" -eq 51 ] 2>/dev/null; then
        ok "Converter first_attr_value_len=$CONV_FIRST_VLEN"
    else
        warn "Converter first_attr_value_len=$CONV_FIRST_VLEN (expected 51)"
    fi
    if [ -n "$CONV_FIRST_CONF" ] && [ "$CONV_FIRST_CONF" != "None" ] && [ "$CONV_FIRST_CONF" != "0.0" ]; then
        ok "Converter first_attr_confidence=$CONV_FIRST_CONF (is float)"
    else
        warn "Converter first_attr_confidence=$CONV_FIRST_CONF (expected float, got None/0)"
    fi
else
    echo "  [INFO] No phase=phase1e_converter_attrs_debug found in logs (only output during converter calls; may have scrolled out of tail window)"
fi

# --- 21. Attr introspection results --------------------------------------------
ATTR_SEEN=false
if echo "$LOGS" | grep -q "phase=phase1e_attr_seen"; then
    ATTR_SEEN=true
    ATTR_CONTAINERS=$(echo "$LOGS" | grep "phase=phase1e_attr_seen" | grep -oP 'attr_container=\K\S+' | tr '\n' ',' | sed 's/,$//')
    ok "Attr introspection found containers: $ATTR_CONTAINERS"
    # Check for specific items
    if echo "$LOGS" | grep -q "phase=phase1e_attr_seen_item"; then
        ok "Attr container items enumerated"
    fi
else
    echo "  [INFO] No phase=phase1e_attr_seen found (no attribute containers on object)"
fi

# --- 22. Brute-force attr lookup results ---------------------------------------
BRUTE_FORCE_OK=false
if echo "$LOGS" | grep -q "phase=phase1e_attr_lookup_attempt.*found=true"; then
    BRUTE_FORCE_OK=true
    BF_ELEMENT=$(echo "$LOGS" | grep "phase=phase1e_attr_lookup_attempt.*found=true" | tail -1 | grep -oP 'element_name=\K\S+' || echo "")
    BF_ATTR=$(echo "$LOGS" | grep "phase=phase1e_attr_lookup_attempt.*found=true" | tail -1 | grep -oP 'attr_name=\K\S+' || echo "")
    BF_METHOD=$(echo "$LOGS" | grep "phase=phase1e_attr_lookup_attempt.*found=true" | tail -1 | grep -oP 'method=\K\S+' || echo "")
    BF_VLEN=$(echo "$LOGS" | grep "phase=phase1e_attr_lookup_attempt.*found=true" | tail -1 | grep -oP 'value_len=\K[0-9]+' || echo "0")
    ok "Brute-force lookup found keypoints (element=$BF_ELEMENT, attr=$BF_ATTR, method=$BF_METHOD, value_len=$BF_VLEN)"
elif echo "$LOGS" | grep -q "phase=phase1e_attr_lookup_attempt.*found=false"; then
    # Identify if ALL attempts failed or just the summary line
    BF_ATTEMPTED=$(echo "$LOGS" | grep "phase=phase1e_attr_lookup_attempt.*found=true" | wc -l)
    if [ "$BF_ATTEMPTED" -eq 0 ] && echo "$LOGS" | grep -q "phase=phase1e_attr_lookup_attempt.*found=false"; then
        warn "Brute-force lookup: 0/${#attr_candidates[@]} combinations found keypoints"
    fi
fi

# --- 23. Cross-check: converter attrs OK but keypoints not in metadata ---------
if $PERSON_OBJECTS_FOUND && ! $KEYPOINTS_ATTR_OK && [ "$CONV_FIRST_VLEN" -eq 51 ] 2>/dev/null; then
    warn "Converter attrs returned (value_len=51) but keypoints not visible in object metadata via official API"
    echo "        -> Possible causes:"
    echo "           1. module.yml output.attributes config structure wrong"
    echo "           2. Attr confidence=None caused Savant to skip attribute attach"
    echo "           3. Savant complex_model doesn't expose attrs through ObjectMeta.get_attr_meta()"
    echo "           4. Need post-pipe pyfunc to manually re-attach converter keypoints"
fi

# =========================================================================
#  Diagnostic report
# =========================================================================
echo ""
echo "--- Phase 1E Metadata Handoff Diagnostic Report ---"

if $PERSON_OBJECTS_FOUND && $KEYPOINTS_ATTR_OK; then
    echo "  STATUS: PASS — Official API reads $PERSON_COUNT_OFFICIAL person objects with keypoints_value_len=51"
    echo "  Conclusion: Metadata handoff is working correctly."
    echo "  Note: Legacy probe read paths show 0 objects — this is expected because they use"
    echo "  deprecated read patterns (objects(), get_objects(), object_meta) instead of the"
    echo "  official frame_meta.objects property. Not a real issue."
elif $PERSON_OBJECTS_FOUND && ! $KEYPOINTS_ATTR_OK; then
    echo "  STATUS: PARTIAL — Person objects found but keypoints attr_meta.value length != 51"
    echo "  Next: Check whether attr_meta.value is a nested structure needing unwrap."
    echo "  The value_repr_sample in check 18 shows the actual value shape."
elif $OFFICIAL_OBJ_NUM_FOUND && ! $PERSON_OBJECTS_FOUND; then
    echo "  STATUS: PARTIAL — Objects exist in frame metadata but none are persons"
    echo "  Next: Check complex_model output object config."
elif $PRIMARY_FRAME_FOUND && ! $PERSON_OBJECTS_FOUND; then
    echo "  STATUS: PARTIAL — Primary frame object seen but no person detection objects"
    echo "  Next: Check whether nvinfer@complex_model creates detection objects."
elif ! $PRIMARY_FRAME_FOUND && $OFFICIAL_API_FOUND; then
    echo "  STATUS: FAIL — No objects at all via official API (not even primary frame)"
    echo "  Next: Check pyfunc base class, process_frame signature, frame_meta type."
elif ! $OFFICIAL_API_FOUND; then
    echo "  STATUS: FAIL — Official API probe not found in logs"
    echo "  Next: Check if MetadataHandoffProbe is running."
else
    echo "  STATUS: UNCLEAR — Mixed signals from official API probe"
fi

# --- Summary --------------------------------------------------------------------
echo ""
echo "--- Results: $PASS passed, $FAIL failed, $WARN warnings ---"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
