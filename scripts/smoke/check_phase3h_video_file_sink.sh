#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 3H.2 — Savant Output Video File Sink POC
# ---------------------------------------------------------------------------
# PREREQ:
#   docker compose -f infra/docker-compose.phase3h-zmq.yml up -d
#
# Verifies video-file-sink consumes Savant ZMQ sink frames+metadata,
# produces video.mov + metadata.json with real objects.
# ---------------------------------------------------------------------------
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase3h-zmq.yml"
OUTPUT_DIR="${SMOKE_DIR}/../../media/phase3h-savant-output"

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

echo "--- Phase 3H.2 Savant Output Video File Sink POC Smoke Test ---"
echo ""

# Pre-flight: check required containers
REQUIRED="phase3h-zmq-source-adapter phase3h-zmq-savant phase3h-zmq-video-file-sink"
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
# [2] Savant OUTPUT_FRAME is configured
# ===========================================================================
OUTPUT_FRAME="$(docker exec phase3h-zmq-savant bash -c 'echo $OUTPUT_FRAME' 2>/dev/null || echo "")"
echo -e "${BLUE}[debug] OUTPUT_FRAME: ${OUTPUT_FRAME}${NC}"
check 2 "Savant OUTPUT_FRAME is configured" "$([[ -n "$OUTPUT_FRAME" && "$OUTPUT_FRAME" != "null" ]] && echo pass || echo fail)"

# ===========================================================================
# [3-4] Find video.mov and metadata.json
# ===========================================================================
VIDEO_FILE="$(find "${OUTPUT_DIR}" -name "video.mov" -type f 2>/dev/null | head -1 || echo "")"
META_FILE="$(find "${OUTPUT_DIR}" -name "metadata.json" -type f -not -path "*/evidence/*" 2>/dev/null | head -1 || echo "")"

check 3 "video.mov exists" "$([[ -n "$VIDEO_FILE" && -f "$VIDEO_FILE" ]] && echo pass || echo fail)"
check 4 "metadata.json exists" "$([[ -n "$META_FILE" && -f "$META_FILE" ]] && echo pass || echo fail)"

if [[ -n "$VIDEO_FILE" ]]; then
    VIDEO_SIZE="$(stat -c%s "$VIDEO_FILE" 2>/dev/null || echo 0)"
    check 5 "video.mov has content (>100KB)" "$([[ "$VIDEO_SIZE" -gt 102400 ]] && echo pass || echo fail)"
    echo -e "${BLUE}[debug] video.mov: ${VIDEO_SIZE} bytes${NC}"
fi

if [[ -n "$META_FILE" ]]; then
    META_SIZE="$(stat -c%s "$META_FILE" 2>/dev/null || echo 0)"
    check 6 "metadata.json has content (>1KB)" "$([[ "$META_SIZE" -gt 1024 ]] && echo pass || echo fail)"
    echo -e "${BLUE}[debug] metadata.json: ${META_SIZE} bytes${NC}"

    # =========================================================================
    # [7] At least one frame with non-empty objects
    # =========================================================================
    FRAMES_WITH_OBJS="$(python3 -c "
import json
count = 0
with open('${META_FILE}') as f:
    for line in f:
        d = json.loads(line)
        if d.get('metadata',{}).get('objects',[]):
            count += 1
print(count)
" 2>/dev/null || echo 0)"
    check 7 "metadata.json has frames with objects" "$([[ "$FRAMES_WITH_OBJS" -ge 1 ]] && echo pass || echo fail)"
    echo -e "${BLUE}[debug] Frames with objects: ${FRAMES_WITH_OBJS}${NC}"

    # =========================================================================
    # [8] At least one object has label=person
    # =========================================================================
    HAS_PERSON="$(python3 -c "
import json
with open('${META_FILE}') as f:
    for line in f:
        d = json.loads(line)
        for obj in d.get('metadata',{}).get('objects',[]):
            if obj.get('label') == 'person':
                print('yes')
                exit(0)
print('no')
" 2>/dev/null || echo "no")"
    check 8 "object label 'person' found" "$([[ "$HAS_PERSON" == "yes" ]] && echo pass || echo fail)"

    # =========================================================================
    # [9] Object has bbox
    # =========================================================================
    HAS_BBOX="$(python3 -c "
import json
with open('${META_FILE}') as f:
    for line in f:
        d = json.loads(line)
        for obj in d.get('metadata',{}).get('objects',[]):
            if obj.get('bbox') and all(k in obj['bbox'] for k in ('xc','yc','width','height')):
                print('yes')
                exit(0)
print('no')
" 2>/dev/null || echo "no")"
    check 9 "object has bbox (xc/yc/width/height)" "$([[ "$HAS_BBOX" == "yes" ]] && echo pass || echo fail)"

    # =========================================================================
    # [10] Object has object_id (track_id)
    # =========================================================================
    HAS_TRACK="$(python3 -c "
import json
with open('${META_FILE}') as f:
    for line in f:
        d = json.loads(line)
        for obj in d.get('metadata',{}).get('objects',[]):
            oid = obj.get('object_id')
            if oid is not None and oid != 'UNTRACKED_OBJECT_ID' and isinstance(oid, int):
                print('yes')
                exit(0)
print('no')
" 2>/dev/null || echo "no")"
    check 10 "object has integer object_id (track_id)" "$([[ "$HAS_TRACK" == "yes" ]] && echo pass || echo fail)"

    # =========================================================================
    # [11] Video dimensions match metadata
    # =========================================================================
    MATCH="$(python3 -c "
import json, subprocess, os
# Get video dimensions
ffprobe_cmd = ['ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
    '-show_entries', 'stream=width,height', '-of', 'csv=p=0',
    '${VIDEO_FILE}']
try:
    out = subprocess.check_output(ffprobe_cmd).decode().strip()
    vw, vh = map(int, out.split(','))
except:
    vw, vh = 0, 0

with open('${META_FILE}') as f:
    d = json.loads(next(f))
if d.get('width') == vw and d.get('height') == vh and vw > 0:
    print('yes')
else:
    print(f'no (video:{vw}x{vh}, meta:{d.get(\"width\")}x{d.get(\"height\")})')
" 2>/dev/null || echo "no")"
    check 11 "video dimensions match metadata" "$([[ "$MATCH" == "yes" ]] && echo pass || echo fail)"
    echo -e "${BLUE}[debug] Dimension match: ${MATCH}${NC}"

    # =========================================================================
    # [12] Evidence frame exists
    # =========================================================================
    EVIDENCE_FILE="${OUTPUT_DIR}/evidence/evidence_frame.jpg"
    check 12 "evidence_frame.jpg exists" "$([[ -f "$EVIDENCE_FILE" ]] && echo pass || echo fail)"
    if [[ -f "$EVIDENCE_FILE" ]]; then
        EV_SIZE="$(stat -c%s "$EVIDENCE_FILE" 2>/dev/null || echo 0)"
        echo -e "${BLUE}[debug] evidence_frame.jpg: ${EV_SIZE} bytes${NC}"
    fi

    # =========================================================================
    # [13] metadata.json objects come from same Savant pipeline (source_id=phase3h)
    # =========================================================================
    SOURCE_OK="$(python3 -c "
import json
with open('${META_FILE}') as f:
    for i, line in enumerate(f):
        d = json.loads(line)
        if d.get('source_id') == 'phase3h':
            print('yes')
            exit(0)
print('no')
" 2>/dev/null || echo "no")"
    check 13 "metadata source_id is phase3h" "$([[ "$SOURCE_OK" == "yes" ]] && echo pass || echo fail)"
else
    check 6 "metadata.json has content" fail
    check 7 "metadata.json has frames with objects" fail
    check 8 "object label 'person' found" fail
    check 9 "object has bbox" fail
    check 10 "object has integer object_id" fail
    check 11 "video dimensions match metadata" fail
    check 12 "evidence_frame.jpg exists" fail
    check 13 "metadata source_id is phase3h" fail
fi

echo ""
echo "--- Results: ${PASS_COUNT}/13 passed, ${FAIL_COUNT} failed ---"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo ""
    echo -e "${YELLOW}NOTE: GPU-dependent verification.${NC}"
    echo "If the Savant module hasn't started producing output frames yet,"
    echo "wait 1-2 minutes and re-run."
    exit 1
fi
