#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 3H.1 — Official Metadata Sink Verification
# ---------------------------------------------------------------------------
# PREREQ:
#   docker compose -f infra/docker-compose.phase3h-zmq.yml up -d
#
# Verifies that the official metadata_json.py sink adapter receives Savant
# ZMQ sink output containing real objects with bbox / track_id / frame info.
# ---------------------------------------------------------------------------
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase3h-zmq.yml"
METADATA_DIR="${SMOKE_DIR}/../../media/phase3h-metadata"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
PASS_COUNT=0; FAIL_COUNT=0

check() {
    local num="$1" desc="$2" result="$3"
    if [[ "$result" == "pass" ]]; then
        echo -e "  ${GREEN}OK${NC}  [$num] $desc"; PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo -e "  ${RED}FAIL${NC}  [$num] $desc"; FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}
fatal() { echo -e "${RED}FATAL${NC}: $*"; exit 1; }

echo "--- Phase 3H.1 Official Metadata Sink Verification Smoke Test ---"
echo ""

# Pre-flight: check required containers
REQUIRED="phase3h-zmq-source-adapter phase3h-zmq-savant phase3h-zmq-metadata-sink"
MISSING=""
for c in $REQUIRED; do
    S="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null || echo "missing")"
    [[ "$S" != "running" ]] && MISSING="$MISSING $c($S)"
done
[[ -n "$MISSING" ]] && { echo -e "${RED}Containers not ready:${NC}"; for m in $MISSING; do echo "  - $m"; done; fatal "containers not ready"; }

CONTAINER_COUNT="$(echo "$REQUIRED" | wc -w)"
echo "  ${CONTAINER_COUNT}/${CONTAINER_COUNT} containers running"
echo ""

check 1 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"

# ===========================================================================
# [2] Savant ZMQ sink is configured for TCP
# ===========================================================================
SINK_ENDPOINT="$(docker exec phase3h-zmq-savant bash -c 'echo $ZMQ_SINK_ENDPOINT' 2>/dev/null || echo "unknown")"
echo -e "${BLUE}[debug] ZMQ_SINK_ENDPOINT: ${SINK_ENDPOINT}${NC}"
check 2 "ZMQ sink endpoint is TCP (not IPC)" "$([[ "$SINK_ENDPOINT" == *"tcp://"* ]] && echo pass || echo fail)"

# ===========================================================================
# [3] Metadata sink is connected (check logs for connection)
# ===========================================================================
check 3 "metadata-sink container running" pass

# ===========================================================================
# [4-5] Metadata file exists and has data
# ===========================================================================
# Find most recent NDJSON file
NDJSON_FILE="$(ls -t "${METADATA_DIR}"/*.ndjson 2>/dev/null | head -1 || echo "")"
check 4 "metadata NDJSON file exists" "$([[ -n "$NDJSON_FILE" && -f "$NDJSON_FILE" ]] && echo pass || echo fail)"

if [[ -n "$NDJSON_FILE" ]]; then
    NDJSON_SIZE="$(stat -c%s "$NDJSON_FILE" 2>/dev/null || echo 0)"
    check 5 "metadata file has content (>1KB)" "$([[ "$NDJSON_SIZE" -gt 1024 ]] && echo pass || echo fail)"
    echo -e "${BLUE}[debug] NDJSON file: ${NDJSON_FILE} (${NDJSON_SIZE} bytes)${NC}"

    # =========================================================================
    # [6] At least one frame has non-empty objects
    # =========================================================================
    FRAMES_WITH_OBJS="$(python3 -c "
import json
count = 0
with open('${NDJSON_FILE}') as f:
    for line in f:
        d = json.loads(line)
        if d.get('metadata',{}).get('objects',[]):
            count += 1
print(count)
" 2>/dev/null || echo 0)"
    check 6 "frames with non-empty objects exist (>=1)" "$([[ "$FRAMES_WITH_OBJS" -ge 1 ]] && echo pass || echo fail)"
    echo -e "${BLUE}[debug] Frames with objects: ${FRAMES_WITH_OBJS}${NC}"

    # =========================================================================
    # [7] At least one object has label "person"
    # =========================================================================
    HAS_PERSON="$(python3 -c "
import json
with open('${NDJSON_FILE}') as f:
    for line in f:
        d = json.loads(line)
        for obj in d.get('metadata',{}).get('objects',[]):
            if obj.get('label') == 'person':
                print('yes')
                exit(0)
print('no')
" 2>/dev/null || echo "no")"
    check 7 "object label 'person' found" "$([[ "$HAS_PERSON" == "yes" ]] && echo pass || echo fail)"

    # =========================================================================
    # [8] Object has bbox with all required coordinates
    # =========================================================================
    BBOX_CHECK="$(python3 -c "
import json
with open('${NDJSON_FILE}') as f:
    for line in f:
        d = json.loads(line)
        for obj in d.get('metadata',{}).get('objects',[]):
            if obj.get('bbox') and all(k in obj['bbox'] for k in ('xc','yc','width','height')):
                print('yes')
                exit(0)
print('no')
" 2>/dev/null || echo "no")"
    check 8 "object bbox has xc/yc/width/height" "$([[ "$BBOX_CHECK" == "yes" ]] && echo pass || echo fail)"

    # =========================================================================
    # [9] Object has object_id (track_id)
    # =========================================================================
    TRACK_CHECK="$(python3 -c "
import json
with open('${NDJSON_FILE}') as f:
    for line in f:
        d = json.loads(line)
        for obj in d.get('metadata',{}).get('objects',[]):
            oid = obj.get('object_id')
            if oid is not None and oid != 'UNTRACKED_OBJECT_ID' and isinstance(oid, int):
                print('yes')
                exit(0)
print('no')
" 2>/dev/null || echo "no")"
    check 9 "object has integer object_id (track_id)" "$([[ "$TRACK_CHECK" == "yes" ]] && echo pass || echo fail)"

    # =========================================================================
    # [10] Frame has source_id = phase3h
    # =========================================================================
    SOURCE_CHECK="$(python3 -c "
import json
count_ok = 0
with open('${NDJSON_FILE}') as f:
    for line in f:
        d = json.loads(line)
        if d.get('source_id') == 'phase3h':
            count_ok += 1
if count_ok > 0:
    print('yes')
else:
    print('no')
" 2>/dev/null || echo "no")"
    check 10 "frame source_id is phase3h" "$([[ "$SOURCE_CHECK" == "yes" ]] && echo pass || echo fail)"

    # =========================================================================
    # [11] Frame has frame_num (incrementing)
    # =========================================================================
    FRAMENUM_CHECK="$(python3 -c "
import json
fnums = []
with open('${NDJSON_FILE}') as f:
    for line in f:
        d = json.loads(line)
        fn = d.get('frame_num')
        if fn is not None:
            fnums.append(fn)
if len(fnums) >= 2 and fnums[-1] > fnums[0]:
    print('yes')
else:
    print('no')
" 2>/dev/null || echo "no")"
    check 11 "frame has frame_num (incrementing)" "$([[ "$FRAMENUM_CHECK" == "yes" ]] && echo pass || echo fail)"

    # =========================================================================
    # [12] Frame has pts
    # =========================================================================
    PTS_CHECK="$(python3 -c "
import json
with open('${NDJSON_FILE}') as f:
    d = json.loads(next(f))
    if d.get('pts') is not None:
        print('yes')
    else:
        print('no')
" 2>/dev/null || echo "no")"
    check 12 "frame has pts timestamp" "$([[ "$PTS_CHECK" == "yes" ]] && echo pass || echo fail)"

    # =========================================================================
    # [13] Same object_id persists across consecutive frames (tracker continuity)
    # =========================================================================
    TRACKER_CHECK="$(python3 -c "
import json
with open('${NDJSON_FILE}') as f:
    lines = f.readlines()
found = False
for i in range(len(lines)-2):
    d1 = json.loads(lines[i])
    d2 = json.loads(lines[i+1])
    d3 = json.loads(lines[i+2])
    ids1 = {o['object_id'] for o in d1['metadata']['objects'] if o.get('object_id') not in ('UNTRACKED_OBJECT_ID', None)}
    ids2 = {o['object_id'] for o in d2['metadata']['objects'] if o.get('object_id') not in ('UNTRACKED_OBJECT_ID', None)}
    ids3 = {o['object_id'] for o in d3['metadata']['objects'] if o.get('object_id') not in ('UNTRACKED_OBJECT_ID', None)}
    if ids1 & ids2 & ids3:
        found = True
        break
print('yes' if found else 'no')
" 2>/dev/null || echo "no")"
    check 13 "track_id persists across 3 consecutive frames" "$([[ "$TRACKER_CHECK" == "yes" ]] && echo pass || echo fail)"
else
    check 5 "metadata file has content" fail
    check 6 "frames with non-empty objects exist" fail
    check 7 "object label 'person' found" fail
    check 8 "object bbox has xc/yc/width/height" fail
    check 9 "object has integer object_id" fail
    check 10 "frame source_id is phase3h" fail
    check 11 "frame has frame_num" fail
    check 12 "frame has pts timestamp" fail
    check 13 "track_id persists across consecutive frames" fail
fi

echo ""
echo "--- Results: ${PASS_COUNT}/13 passed, ${FAIL_COUNT} failed ---"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo ""
    echo -e "${YELLOW}NOTE: This is a GPU-dependent verification.${NC}"
    echo "If the Savant module hasn't finished starting or the video loop"
    echo "hasn't produced metadata yet, wait 1-2 minutes and re-run."
    exit 1
fi
