#!/usr/bin/env bash
# Midterm deployment one-click shutdown script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/infra/docker-compose.midterm.yml"
ENV_FILE="$REPO_ROOT/infra/env/midterm.env"
COMPOSE_ARGS=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")
ALL_PROFILES=(local-postgres dual-replay-shards dual-4090-two-source)

for profile in "${ALL_PROFILES[@]}"; do
    COMPOSE_ARGS+=(--profile "$profile")
done

if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    NC=''
fi

log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

compose() {
    docker compose "${COMPOSE_ARGS[@]}" "$@"
}

REMOVE_VOLUMES=false
REMOVE_IMAGES=false

usage() {
    cat <<USAGE
Usage: bash scripts/midterm_stop.sh [OPTIONS]

Stop midterm deployment containers. By default this preserves images and
bind-mounted data under /data/video-analytics.

Options:
  -v, --volumes    Remove compose-managed named volumes, if any
  -i, --images     Remove locally built midterm images
  -a, --all        Remove compose-managed volumes and locally built images
  -h, --help       Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--volumes)
            REMOVE_VOLUMES=true
            shift
            ;;
        -i|--images)
            REMOVE_IMAGES=true
            shift
            ;;
        -a|--all)
            REMOVE_VOLUMES=true
            REMOVE_IMAGES=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

check_docker() {
    command -v docker >/dev/null 2>&1 || {
        log_error "docker is not installed"
        exit 1
    }
    docker compose version >/dev/null 2>&1 || {
        log_error "docker compose plugin is not available"
        exit 1
    }
}

has_midterm_containers() {
    docker ps -a --filter "name=video-analytics-midterm-" --format '{{.Names}}' 2>/dev/null | grep -q .
}

show_status() {
    log_info "Current service status:"
    if has_midterm_containers; then
        compose ps -a
    else
        log_info "No midterm containers found"
    fi
}

stop_services() {
    log_info "Stopping services..."

    cd "$REPO_ROOT"
    if has_midterm_containers; then
        compose stop
        log_success "Services stopped"
    else
        log_info "No services to stop"
    fi
}

remove_containers() {
    log_info "Removing stopped containers..."

    if has_midterm_containers; then
        compose rm -f
        log_success "Stopped containers removed"
    else
        log_info "No containers to remove"
    fi
}

remove_volumes() {
    if [[ "$REMOVE_VOLUMES" != true ]]; then
        return 0
    fi

    log_warning "Removing compose-managed named volumes, if any."
    log_warning "Bind-mounted /data/video-analytics directories are preserved by Docker Compose."
    read -r -p "Continue? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        compose down -v
        log_success "Compose-managed volumes removed"
    else
        log_info "Volume removal cancelled"
    fi
}

remove_images() {
    if [[ "$REMOVE_IMAGES" != true ]]; then
        return 0
    fi

    log_info "Removing locally built midterm images..."

    local images=(
        "video-analytics-midterm-api"
        "video-analytics-midterm-event-worker"
        "video-analytics-midterm-clip-worker"
        "video-analytics-midterm-media-worker"
        "video-analytics-midterm-face-worker"
        "video-analytics-midterm-evidence-viewer"
        "video-analytics-midterm-analysis-forwarder"
    )

    local image
    for image in "${images[@]}"; do
        if docker image inspect "$image:latest" >/dev/null 2>&1; then
            docker rmi "$image:latest" >/dev/null 2>&1 || log_warning "Could not remove $image:latest"
        fi
    done

    log_success "Image removal step completed"
}

check_orphaned() {
    log_info "Checking for remaining midterm containers..."

    local remaining
    remaining="$(docker ps -a --filter "name=video-analytics-midterm-" --format '{{.Names}}' 2>/dev/null || true)"

    if [[ -n "$remaining" ]]; then
        log_warning "Containers still present:"
        echo "$remaining"
    else
        log_success "No midterm containers remain"
    fi
}

main() {
    echo ""
    log_info "===== Midterm Deployment Shutdown ====="
    echo ""

    check_docker
    show_status

    echo ""
    stop_services
    remove_containers
    remove_volumes
    remove_images

    echo ""
    check_orphaned

    echo ""
    log_success "===== Shutdown Complete ====="
    log_info "Runtime data under /data/video-analytics is preserved unless you delete it manually."
    echo ""
}

main "$@"
