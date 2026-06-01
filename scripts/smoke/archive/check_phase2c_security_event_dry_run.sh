#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 2C — SecurityEvent schema stabilization + dry-run export
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase2c.yml"
MODULE_FILE="${SMOKE_DIR}/../../modules/savant_phase2c/module.yml"
CONTAINER_NAME="phase2c-savant"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0

check() {
    local num="$1" desc="$2" result="$3"
    if [[ "$result" == "pass" ]]; then
        echo -e "  ${GREEN}OK${NC}  [$num] $desc"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo -e "  ${RED}FAIL${NC}  [$num] $desc"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

echo "--- Phase 2C Smoke Test ---"
echo ""

# ===========================================================================
# 1. Compose file exists
# ===========================================================================
if [[ -f "$COMPOSE_FILE" ]]; then
    check 1 "phase2c compose file exists" pass
else
    check 1 "phase2c compose file exists" fail
fi

# ===========================================================================
# 2. module.yml exists
# ===========================================================================
if [[ -f "$MODULE_FILE" ]]; then
    check 2 "phase2c module.yml exists" pass
else
    check 2 "phase2c module.yml exists" fail
fi

# ===========================================================================
# 3. Container state — running, not restarting, restart_count=0
# ===========================================================================
INSPECT=$(docker inspect "$CONTAINER_NAME" 2>/dev/null || true)
STATUS=$(echo "$INSPECT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")
RESTARTING=$(echo "$INSPECT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Restarting'])" 2>/dev/null || echo "missing")
RESTART_COUNT=$(echo "$INSPECT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['RestartCount'])" 2>/dev/null || echo "missing")

if [[ "$STATUS" == "running" && "$RESTARTING" == "False" && "$RESTART_COUNT" == "0" ]]; then
    check 3 "Container ${CONTAINER_NAME}: running, not restarting, restart_count=0" pass
elif [[ "$STATUS" != "running" ]]; then
    check 3 "Container ${CONTAINER_NAME}: status=${STATUS} (expected running)" fail
elif [[ "$RESTARTING" != "False" ]]; then
    check 3 "Container ${CONTAINER_NAME}: restarting=${RESTARTING}" fail
else
    check 3 "Container ${CONTAINER_NAME}: restart_count=${RESTART_COUNT}" fail
fi

# ===========================================================================
# Single canonical LOGS for ALL grep-based checks
# ===========================================================================
LOGS="$(docker logs "$CONTAINER_NAME" 2>&1 || true)"

# ===========================================================================
# Extract dry-run event line and JSON early (used as fallback for 4 & 5)
# ===========================================================================
EVENT_LINE="$(printf '%s\n' "$LOGS" | grep 'stage=phase2c_security_event_dry_run' | head -n 1 || true)"
EVENT_JSON="$(printf '%s\n' "$EVENT_LINE" | sed -n 's/^.*security_event_json=//p' || true)"

# ===========================================================================
# 4. YOLO26-pose pipeline loaded
# ===========================================================================
if echo "$LOGS" | grep -qE \
    "stage=phase2c_converter_init|stage=phase2c_converter_entered|yolo26_pose_config_savant\.txt|Load new model:" || \
    [[ -n "$EVENT_JSON" ]]; then
    check 4 "YOLO26-pose pipeline loaded" pass
else
    check 4 "YOLO26-pose pipeline loaded" fail
fi

# ===========================================================================
# 5. Probe initialized
# ===========================================================================
if echo "$LOGS" | grep -qE \
    "stage=phase2c_behavior_event_export_probe_init|stage=phase2c_probe_init|behavior_event_export_probe" || \
    [[ -n "$EVENT_JSON" ]]; then
    check 5 "BehaviorEventExportProbe initialized" pass
else
    check 5 "BehaviorEventExportProbe initialized" fail
fi

# ===========================================================================
# 6. Dry-run event line present
# ===========================================================================
if [[ -n "$EVENT_LINE" ]]; then
    check 6 "stage=phase2c_security_event_dry_run present" pass
else
    check 6 "stage=phase2c_security_event_dry_run present" fail
fi

# ===========================================================================
# 7. security_event_json= present
# ===========================================================================
if [[ -n "$EVENT_JSON" ]]; then
    check 7 "security_event_json= present" pass
else
    check 7 "security_event_json= present" fail
fi

# ===========================================================================
# 8-13. Python JSON validation on the extracted event
# ===========================================================================
if [[ -z "$EVENT_JSON" ]]; then
    for i in 8 9 10 11 12 13; do
        check $i "JSON field validation (no event extracted)" fail
    done
else
    PY_OUTPUT="$(python3 -c "
import json, sys
event = json.loads('''${EVENT_JSON}''')
media = event.get('payload', {}).get('media', {})

checks = [
    (8,  'schema_version == \"1.0\"',            event.get('schema_version') == '1.0'),
    (9,  'source_event_id non-empty',            bool(event.get('source_event_id'))),
    (10, 'event_type == \"intrusion\"',           event.get('event_type') == 'intrusion'),
    (11, 'recording_strategy == \"reserved\"',    media.get('recording_strategy') == 'reserved'),
    (12, 'snapshot_status == \"not_implemented\"', media.get('snapshot_status') == 'not_implemented'),
    (13, 'clip_status == \"not_implemented\"',     media.get('clip_status') == 'not_implemented'),
]

for num, desc, ok in checks:
    print(f'{num}|{\"pass\" if ok else \"fail\"}|{desc}')
" 2>&1)" || true

    while IFS='|' read -r num result desc; do
        if [[ -n "$num" && -n "$result" ]]; then
            check "$num" "$desc" "$result"
        fi
    done <<< "$PY_OUTPUT"
fi

# ===========================================================================
# 14. No Redis XADD or redis connect
# ===========================================================================
if echo "$LOGS" | grep -qiE "xadd|redis.*connect|redis.*error"; then
    check 14 "No Redis XADD/connect in logs" fail
else
    check 14 "No Redis XADD/connect in logs" pass
fi

# ===========================================================================
# 15. Compose services do not include redis/postgres/api/event-worker
# ===========================================================================
COMPOSE_SERVICES="$(docker compose -f "$COMPOSE_FILE" config --services 2>/dev/null || true)"
UNWANTED="$(echo "$COMPOSE_SERVICES" | grep -c -E "redis|postgres|api|event-worker" 2>/dev/null || true)"
if [[ "$UNWANTED" -eq 0 ]]; then
    check 15 "No redis/postgres/api/event-worker in compose services" pass
else
    check 15 "No redis/postgres/api/event-worker in compose services (found ${UNWANTED})" fail
fi

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
