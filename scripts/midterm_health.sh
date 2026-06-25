#!/usr/bin/env bash
# Midterm deployment health check.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/infra/docker-compose.midterm.yml"
ENV_FILE="$REPO_ROOT/infra/env/midterm.env"
DATA_ROOT="${VIDEO_ANALYTICS_DATA_ROOT:-/data/video-analytics}"
API_BASE="${MIDTERM_OPERATOR_URL:-http://127.0.0.1:8090}"
COMPOSE_ARGS=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")

if [[ -n "${MIDTERM_COMPOSE_PROFILES:-}" ]]; then
    IFS=',' read -r -a _profiles <<< "$MIDTERM_COMPOSE_PROFILES"
    for _profile in "${_profiles[@]}"; do
        _profile="${_profile//[[:space:]]/}"
        [[ -n "$_profile" ]] && COMPOSE_ARGS+=(--profile "$_profile")
    done
fi

if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    CYAN='\033[0;36m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    CYAN=''
    NC=''
fi

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

compose() {
    docker compose "${COMPOSE_ARGS[@]}" "$@"
}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_pass() {
    echo -e "${GREEN}[PASS]${NC} $*"
    PASS_COUNT=$((PASS_COUNT + 1))
}

log_fail() {
    echo -e "${RED}[FAIL]${NC} $*"
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
    WARN_COUNT=$((WARN_COUNT + 1))
}

log_section() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  $*${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

check_prerequisites() {
    log_section "Health Tool Prerequisites"

    local commands=(docker curl jq ss)
    local command_name
    for command_name in "${commands[@]}"; do
        if command -v "$command_name" >/dev/null 2>&1; then
            log_pass "$command_name: available"
        else
            log_fail "$command_name: missing"
        fi
    done

    if docker compose version >/dev/null 2>&1; then
        log_pass "docker compose: available"
    else
        log_fail "docker compose: unavailable"
    fi
}

service_state() {
    local service="$1"
    compose ps "$service" --format json 2>/dev/null \
        | jq -r 'if type == "array" then (.[0].State // "missing") else (.State // "missing") end' 2>/dev/null \
        || echo "missing"
}

check_containers() {
    log_section "Container Status"

    local expected_services=(
        "redis"
        "api"
        "evidence-viewer"
        "replay-service"
        "analysis-forwarder"
        "savant-security"
        "source-adapter"
        "event-worker"
        "face-worker"
        "clip-worker"
        "video-file-sink"
        "media-worker"
    )

    local service status
    for service in "${expected_services[@]}"; do
        status="$(service_state "$service")"
        if [[ "$status" == "running" ]]; then
            log_pass "$service: running"
        elif [[ "$status" == "missing" || "$status" == "null" ]]; then
            log_fail "$service: not found"
        else
            log_fail "$service: $status"
        fi
    done
}

check_ports() {
    log_section "Port Availability"

    local ports=(
        "6396:Redis"
        "8090:8090 operator portal"
        "8098:Replay API"
        "18080:Savant metrics"
        "18081:analysis-forwarder metrics"
    )

    local port_info port service
    for port_info in "${ports[@]}"; do
        IFS=':' read -r port service <<< "$port_info"
        if ss -ltnH | awk '{print $4}' | grep -Eq "[:.]${port}$"; then
            log_pass "$service (port $port): listening"
        else
            log_fail "$service (port $port): not listening"
        fi
    done
}

check_api_endpoints() {
    log_section "8090 Portal And API Proxy"

    if curl --noproxy '*' -fsS "$API_BASE/health" >/dev/null 2>&1; then
        log_pass "8090 /health: OK"
    else
        log_fail "8090 /health: unreachable"
        return
    fi

    local endpoints=(
        "/api/v1/runtime/overview:Runtime overview proxy"
        "/api/v1/cameras:Cameras API"
        "/api/v1/people:People API"
        "/api/v1/events:Events API"
        "/api/v1/evidence/health:Evidence database index health"
    )

    local item path label
    for item in "${endpoints[@]}"; do
        IFS=':' read -r path label <<< "$item"
        if curl --noproxy '*' -fsS "$API_BASE$path" >/dev/null 2>&1; then
            log_pass "$label: OK"
        else
            log_fail "$label: failed"
        fi
    done
}

json_count() {
    local response="$1"
    echo "$response" | jq -r 'if (.data | type) == "array" then (.data | length) else 0 end' 2>/dev/null || echo "0"
}

check_camera_config() {
    log_section "Camera Configuration"

    local response camera_count
    response="$(curl --noproxy '*' -fsS "$API_BASE/api/v1/cameras" 2>/dev/null || echo '{"data":null}')"
    camera_count="$(json_count "$response")"

    if [[ "$camera_count" =~ ^[0-9]+$ && "$camera_count" -gt 0 ]]; then
        log_pass "Camera count: $camera_count configured"
        echo "$response" | jq -r '.data[] | "  - Camera \(.camera_id): \(.name // "unnamed") (\(.rtsp_url // "no URL"))"' 2>/dev/null || true
    elif [[ "$camera_count" == "0" ]]; then
        log_warn "Camera count: 0 (no cameras configured)"
        log_info "  Add cameras from: $API_BASE/operator"
    else
        log_fail "Camera configuration: could not retrieve"
    fi
}

check_face_gallery() {
    log_section "Face Gallery Status"

    local response people_count
    response="$(curl --noproxy '*' -fsS "$API_BASE/api/v1/people" 2>/dev/null || echo '{"data":null}')"
    people_count="$(json_count "$response")"

    if [[ "$people_count" =~ ^[0-9]+$ && "$people_count" -gt 0 ]]; then
        log_pass "Gallery people count: $people_count registered"
        echo "$response" | jq -r '.data[] | "  - Person \(.person_id): \(.full_name // "unnamed") (\(.gallery_embedding_count // 0) embeddings)"' 2>/dev/null || true
    elif [[ "$people_count" == "0" ]]; then
        log_warn "Gallery people count: 0 (no people registered)"
        log_info "  Register faces from: $API_BASE/operator"
    else
        log_fail "Face gallery: could not retrieve"
    fi
}

check_runtime_metrics() {
    log_section "Runtime Metrics"

    local response
    response="$(curl --noproxy '*' -fsS "$API_BASE/api/v1/runtime/overview" 2>/dev/null || echo '{"data":null}')"

    if echo "$response" | jq -e '.data' >/dev/null 2>&1; then
        log_pass "Runtime overview: available"

        local savant_status savant_fps replay_status
        savant_status="$(echo "$response" | jq -r '.data.savant.status // .data.savant.module_status // "unknown"' 2>/dev/null)"
        savant_fps="$(echo "$response" | jq -r '.data.savant.current_fps // .data.savant.fps // "N/A"' 2>/dev/null)"
        replay_status="$(echo "$response" | jq -r '.data.replay.status // "unknown"' 2>/dev/null)"

        echo "  - Savant status: $savant_status (FPS: $savant_fps)"
        echo "  - Replay status: $replay_status"

        if [[ "$savant_status" == "running" || "$savant_status" == "healthy" ]]; then
            log_pass "Savant inference: running"
        elif [[ "$savant_status" == "starting" || "$savant_status" == "unknown" ]]; then
            log_warn "Savant inference: $savant_status"
        else
            log_fail "Savant inference: $savant_status"
        fi

        if [[ "$replay_status" == "running" || "$replay_status" == "healthy" || "$replay_status" == "unknown" ]]; then
            log_pass "Replay service: $replay_status"
        else
            log_warn "Replay service: $replay_status"
        fi
    else
        log_fail "Runtime overview: unavailable"
    fi
}

check_redis() {
    log_section "Redis Connectivity"

    if docker exec video-analytics-midterm-redis redis-cli ping >/dev/null 2>&1; then
        log_pass "Redis: responding"

        local stream_count
        stream_count="$(docker exec video-analytics-midterm-redis redis-cli --raw KEYS 'security.*' 2>/dev/null | wc -l | tr -d ' ')"
        log_info "Redis security streams: $stream_count keys"
    else
        log_fail "Redis: not responding"
    fi
}

check_database() {
    log_section "Database Connectivity"

    if curl --noproxy '*' -fsS "$API_BASE/api/v1/cameras" >/dev/null 2>&1; then
        log_pass "Database: API can query camera table"
    else
        log_warn "Database: could not verify through API"
    fi
}

check_disk_space() {
    log_section "Disk Space"

    if [[ ! -d "$DATA_ROOT" ]]; then
        log_fail "$DATA_ROOT: missing"
        return
    fi

    local data_usage
    data_usage="$(df -P "$DATA_ROOT" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}' || echo "0")"

    if [[ "$data_usage" =~ ^[0-9]+$ && "$data_usage" -lt 80 ]]; then
        log_pass "$DATA_ROOT: ${data_usage}% used"
    elif [[ "$data_usage" =~ ^[0-9]+$ && "$data_usage" -lt 90 ]]; then
        log_warn "$DATA_ROOT: ${data_usage}% used"
    else
        log_fail "$DATA_ROOT: ${data_usage}% used"
    fi

    echo "  Breakdown:"
    du -sh "$DATA_ROOT"/{media,models,replay-midterm*} 2>/dev/null | awk '{print "    " $2 ": " $1}' || true
}

check_gpu() {
    log_section "GPU Availability"

    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
        log_pass "NVIDIA GPU: available"
        nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total --format=csv,noheader 2>/dev/null \
            | awk -F', ' '{printf "  - GPU %s (%s): %s util, %s / %s memory\n", $1, $2, $3, $4, $5}' || true
    else
        log_warn "NVIDIA GPU: not detected or unavailable"
    fi
}

print_summary() {
    log_section "Health Check Summary"

    local total=$((PASS_COUNT + FAIL_COUNT + WARN_COUNT))

    echo ""
    echo "  Total checks: $total"
    echo -e "  ${GREEN}Passed: $PASS_COUNT${NC}"
    echo -e "  ${YELLOW}Warnings: $WARN_COUNT${NC}"
    echo -e "  ${RED}Failed: $FAIL_COUNT${NC}"
    echo ""

    if [[ "$FAIL_COUNT" -eq 0 && "$WARN_COUNT" -eq 0 ]]; then
        echo -e "${GREEN}All checks passed - system is healthy${NC}"
        return 0
    elif [[ "$FAIL_COUNT" -eq 0 ]]; then
        echo -e "${YELLOW}System is operational with warnings${NC}"
        return 0
    else
        echo -e "${RED}System has failures - review above${NC}"
        return 1
    fi
}

main() {
    echo ""
    log_info "===== Midterm Deployment Health Check ====="
    log_info "Timestamp: $(date -Iseconds)"
    echo ""

    check_prerequisites
    check_containers
    check_ports
    check_redis
    check_database
    check_api_endpoints
    check_camera_config
    check_face_gallery
    check_runtime_metrics
    check_gpu
    check_disk_space

    echo ""
    print_summary

    echo ""
    log_info "Operator portal: $API_BASE/operator"
    log_info "API documentation: $API_BASE/docs"
    echo ""
}

main "$@"
