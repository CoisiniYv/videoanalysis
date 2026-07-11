#!/usr/bin/env bash
# Midterm deployment one-click startup script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/infra/docker-compose.midterm.yml"
STORAGE_OVERRIDE="$REPO_ROOT/infra/midterm-storage.override.yml"
ENV_FILE="$REPO_ROOT/infra/env/midterm.env"
DATA_ROOT="${VIDEO_ANALYTICS_DATA_ROOT:-/data/video-analytics}"
OPERATOR_URL="${MIDTERM_OPERATOR_URL:-http://127.0.0.1:8090}"

BUILD_IMAGES=true
SKIP_HEALTH=false
WAIT_SECONDS=240
LOCAL_POSTGRES=false
COMPOSE_PROFILES=()
COMPOSE_ARGS=()

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

die() {
    log_error "$*"
    exit 1
}

usage() {
    cat <<USAGE
Usage: bash scripts/midterm_start.sh [OPTIONS]

Start the complete midterm video analytics deployment. After this script
finishes, day-to-day management is done from the 8090 operator portal.

Default operator portal:
  http://127.0.0.1:8090/operator

Options:
  --build              Build images before starting (default)
  --no-build           Start from existing images only
  --local-postgres     Start the local PostgreSQL compose profile and point
                       services at postgres:5432 unless VIDEO_ANALYTICS_DATABASE_URL is set
  --profile NAME       Enable an additional docker compose profile
  --wait-seconds N     Health wait budget, default: 240
  --skip-health        Do not wait for 8090 health/API proxy checks
  -h, --help           Show this help

Environment:
  VIDEO_ANALYTICS_DATA_ROOT      Data root, default: /data/video-analytics
  MIDTERM_OPERATOR_URL           Operator base URL, default: http://127.0.0.1:8090
  MIDTERM_COMPOSE_PROFILES       Comma-separated compose profiles to enable
  VIDEO_ANALYTICS_DATABASE_URL   Database URL override
USAGE
}

add_profile() {
    local profile="$1"
    [[ -n "$profile" ]] || return 0
    local existing
    for existing in "${COMPOSE_PROFILES[@]}"; do
        [[ "$existing" == "$profile" ]] && return 0
    done
    COMPOSE_PROFILES+=("$profile")
    if [[ "$profile" == "local-postgres" ]]; then
        LOCAL_POSTGRES=true
    fi
}

parse_env_profiles() {
    [[ -n "${MIDTERM_COMPOSE_PROFILES:-}" ]] || return 0
    local raw profile
    IFS=',' read -r -a raw <<< "$MIDTERM_COMPOSE_PROFILES"
    for profile in "${raw[@]}"; do
        profile="${profile//[[:space:]]/}"
        add_profile "$profile"
    done
}

parse_args() {
    parse_env_profiles
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --build)
                BUILD_IMAGES=true
                shift
                ;;
            --no-build)
                BUILD_IMAGES=false
                shift
                ;;
            --local-postgres)
                add_profile "local-postgres"
                shift
                ;;
            --profile)
                [[ $# -ge 2 ]] || die "--profile requires a value"
                add_profile "$2"
                shift 2
                ;;
            --wait-seconds)
                [[ $# -ge 2 ]] || die "--wait-seconds requires a value"
                [[ "$2" =~ ^[0-9]+$ ]] || die "--wait-seconds must be an integer"
                WAIT_SECONDS="$2"
                shift 2
                ;;
            --skip-health)
                SKIP_HEALTH=true
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "Unknown option: $1"
                ;;
        esac
    done
}

build_compose_args() {
    COMPOSE_ARGS=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")
    [[ -f "$STORAGE_OVERRIDE" ]] && COMPOSE_ARGS+=(-f "$STORAGE_OVERRIDE")
    local profile
    for profile in "${COMPOSE_PROFILES[@]}"; do
        COMPOSE_ARGS+=(--profile "$profile")
    done
}

compose() {
    docker compose "${COMPOSE_ARGS[@]}" "$@"
}

check_command() {
    local command_name="$1"
    command -v "$command_name" >/dev/null 2>&1 || die "$command_name is not installed"
}

check_prerequisites() {
    log_info "Checking host prerequisites..."

    check_command docker
    check_command curl
    check_command ss

    docker compose version >/dev/null 2>&1 || die "docker compose plugin is not available"
    docker info >/dev/null 2>&1 || die "docker daemon is not running or not accessible"

    if ! command -v nvidia-smi >/dev/null 2>&1; then
        log_warning "nvidia-smi is not available; GPU inference will not work until NVIDIA drivers/toolkit are installed"
    elif ! nvidia-smi >/dev/null 2>&1; then
        log_warning "nvidia-smi exists but did not return successfully"
    fi

    log_success "Host prerequisites checked"
}

check_required_files() {
    log_info "Checking deployment files..."

    local files=(
        "$COMPOSE_FILE"
        "$ENV_FILE"
        "$REPO_ROOT/modules/savant_replay/config.midterm.json"
        "$REPO_ROOT/modules/savant_security/module.yml"
        "$REPO_ROOT/modules/savant_security/config/cameras.midterm.yml"
    )

    local file
    for file in "${files[@]}"; do
        [[ -f "$file" ]] || die "Required file is missing: $file"
    done

    log_success "Deployment files are present"
}

ensure_data_directories() {
    log_info "Preparing data directories under $DATA_ROOT..."

    local dirs=(
        "$DATA_ROOT/artifacts"
        "$DATA_ROOT/downloads"
        "$DATA_ROOT/media/evidence"
        "$DATA_ROOT/media/replay-sink-output/midterm"
        "$DATA_ROOT/media/midterm-snapshots"
        "$DATA_ROOT/media/face_uploads"
        "$DATA_ROOT/media/face_registration"
        "$DATA_ROOT/media/face-registration"
        "$DATA_ROOT/media/debug"
        "$DATA_ROOT/media/.trash"
        "$DATA_ROOT/replay-midterm"
        "$DATA_ROOT/replay-midterm-a"
        "$DATA_ROOT/replay-midterm-b"
    )
    if [[ "$LOCAL_POSTGRES" == true ]]; then
        dirs+=("$DATA_ROOT/postgres-midterm")
    fi

    local dir
    for dir in "${dirs[@]}"; do
        if ! mkdir -p "$dir"; then
            die "Cannot create $dir; fix ownership or run: sudo mkdir -p $dir && sudo chown -R \$USER:\$USER $DATA_ROOT"
        fi
    done

    log_success "Runtime directories are ready"
}

ensure_yolov8_face_symlinks() {
    local model_root="$DATA_ROOT/models"
    local face_onnx_target="yolov8_face/yolov8n-face.onnx"
    local face_engine_target="yolov8_face/yolov8n-face.onnx_b1_gpu0_fp16.engine"

    if [[ -e "$model_root/$face_onnx_target" && ! -e "$model_root/yolov8_face.onnx" ]]; then
        ln -sfn "$face_onnx_target" "$model_root/yolov8_face.onnx"
        log_info "Created model symlink: $model_root/yolov8_face.onnx"
    fi

    if [[ -e "$model_root/$face_engine_target" && ! -e "$model_root/yolov8_face.onnx_b1_gpu0_fp16.engine" ]]; then
        ln -sfn "$face_engine_target" "$model_root/yolov8_face.onnx_b1_gpu0_fp16.engine"
        log_info "Created model symlink: $model_root/yolov8_face.onnx_b1_gpu0_fp16.engine"
    fi
}

env_file_value() {
    local key="$1"
    awk -F= -v key="$key" '
        $0 !~ /^[[:space:]]*#/ && $1 == key {
            sub(/^[^=]*=/, "")
            print
            exit
        }
    ' "$ENV_FILE"
}

model_container_path_to_host() {
    local path="$1"
    if [[ "$path" == /models/* ]]; then
        printf '%s/models/%s\n' "$DATA_ROOT" "${path#/models/}"
    elif [[ "$path" == /* ]]; then
        printf '%s\n' "$path"
    fi
}

check_model_assets() {
    log_info "Checking required model assets..."

    ensure_yolov8_face_symlinks

    local pose_model_file="${POSE_MODEL_FILE:-$(env_file_value POSE_MODEL_FILE)}"
    local face_detector_model_file="${FACE_DETECTOR_MODEL_FILE:-$(env_file_value FACE_DETECTOR_MODEL_FILE)}"
    local pose_model_host
    local face_detector_model_host
    pose_model_host="$(model_container_path_to_host "$pose_model_file")"
    face_detector_model_host="$(model_container_path_to_host "$face_detector_model_file")"

    local required_models=(
        "$DATA_ROOT/models/yolo26_pose/yolo26_pose.onnx"
        "$DATA_ROOT/models/yolov8_face/yolov8n-face.onnx"
        "$DATA_ROOT/models/yolov8_face.onnx"
        "$DATA_ROOT/models/adaface/adaface_ir50_webface4m.onnx"
    )
    [[ -z "$pose_model_host" ]] || required_models+=("$pose_model_host")
    [[ -z "$face_detector_model_host" ]] || required_models+=("$face_detector_model_host")

    local missing=()
    local model
    for model in "${required_models[@]}"; do
        [[ -f "$model" ]] || missing+=("$model")
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Missing required model assets:"
        printf '  - %s\n' "${missing[@]}" >&2
        log_error "Restore the migration models.tgz into $DATA_ROOT before starting this stack"
        log_error "For batch>1 YOLO, generate dynamic assets with: bash scripts/tools/build_yolo_dynamic_batch_engines.sh"
        return 1
    fi

    log_success "Required model assets are present"
}

port_is_listening() {
    local port="$1"
    ss -ltnH | awk '{print $4}' | grep -Eq "[:.]${port}$"
}

port_is_owned_by_midterm_container() {
    local port="$1"
    local owners owner
    owners="$(docker ps --filter "publish=$port" --format '{{.Names}}' 2>/dev/null || true)"
    [[ -n "$owners" ]] || return 1
    while IFS= read -r owner; do
        [[ -z "$owner" ]] && continue
        [[ "$owner" == video-analytics-midterm-* ]] || return 1
    done <<< "$owners"
    return 0
}

profile_enabled() {
    local target="$1"
    local profile
    for profile in "${COMPOSE_PROFILES[@]}"; do
        [[ "$profile" == "$target" ]] && return 0
    done
    return 1
}

check_ports() {
    log_info "Checking host port availability..."

    local ports=(
        "6396:Redis"
        "8090:8090 operator portal"
        "8098:Replay API"
        "18080:Savant metrics"
        "18081:analysis-forwarder metrics"
    )
    if [[ "$LOCAL_POSTGRES" == true ]]; then
        ports+=("5439:local PostgreSQL")
    fi
    if profile_enabled "dual-replay-shards" || profile_enabled "dual-4090-two-source"; then
        ports+=("8198:Replay shard A" "8298:Replay shard B")
    fi
    if profile_enabled "dual-4090-two-source"; then
        ports+=(
            "18180:Savant A metrics"
            "18181:Savant B metrics"
            "18182:forwarder A metrics"
            "18183:forwarder B metrics"
        )
    fi

    local conflict_count=0
    local port_info port label
    for port_info in "${ports[@]}"; do
        IFS=':' read -r port label <<< "$port_info"
        if port_is_listening "$port"; then
            if port_is_owned_by_midterm_container "$port"; then
                log_info "$label port $port is already owned by this midterm stack"
            else
                log_error "$label port $port is already in use by another process/container"
                conflict_count=$((conflict_count + 1))
            fi
        fi
    done

    [[ "$conflict_count" -eq 0 ]] || return 1
    log_success "Required host ports are available"
}

apply_local_postgres_default() {
    if [[ "$LOCAL_POSTGRES" != true ]]; then
        return 0
    fi
    if [[ -z "${VIDEO_ANALYTICS_DATABASE_URL:-}" ]]; then
        export VIDEO_ANALYTICS_DATABASE_URL="postgresql://video:video@postgres:5432/video_analytics"
        log_info "Using local-postgres profile database: $VIDEO_ANALYTICS_DATABASE_URL"
    else
        log_info "Using provided VIDEO_ANALYTICS_DATABASE_URL=$VIDEO_ANALYTICS_DATABASE_URL"
    fi
}

validate_config() {
    log_info "Validating docker compose configuration..."
    compose config >/dev/null
    log_success "Compose configuration is valid"
}

build_images() {
    if [[ "$BUILD_IMAGES" != true ]]; then
        log_info "Skipping image build because --no-build was requested"
        return 0
    fi

    log_info "Building face-worker image first for API face-runtime base..."
    docker compose "${COMPOSE_ARGS[@]}" build face-worker

    log_info "Building remaining service images..."
    compose build

    log_success "Images are built"
}

start_services() {
    log_info "Starting midterm deployment..."
    cd "$REPO_ROOT"

    if [[ "$BUILD_IMAGES" == true ]]; then
        compose up -d
    else
        compose up -d --no-build
    fi

    log_success "Services started"
}

wait_for_url() {
    local label="$1"
    local url="$2"
    local elapsed=0
    local interval=5

    while [[ "$elapsed" -lt "$WAIT_SECONDS" ]]; do
        if curl --noproxy '*' -fsS "$url" >/dev/null 2>&1; then
            log_success "$label is ready after ${elapsed}s"
            return 0
        fi
        sleep "$interval"
        elapsed=$((elapsed + interval))
        printf '.'
    done
    echo ""
    return 1
}

wait_for_health() {
    if [[ "$SKIP_HEALTH" == true ]]; then
        log_info "Skipping health wait"
        return 0
    fi

    log_info "Waiting for 8090 operator portal (max ${WAIT_SECONDS}s)..."
    wait_for_url "8090 /health" "$OPERATOR_URL/health" || {
        log_warning "8090 /health did not become ready within ${WAIT_SECONDS}s"
        return 1
    }

    log_info "Checking 8090 API proxy..."
    wait_for_url "8090 runtime overview proxy" "$OPERATOR_URL/api/v1/runtime/overview" || {
        log_warning "8090 is up, but the internal API proxy/runtime overview is not ready"
        return 1
    }
}

show_status() {
    log_info "Service status:"
    compose ps
}

print_next_steps() {
    echo ""
    log_success "===== Startup Complete ====="
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  Web 管理入口：${NC}${BLUE}${OPERATOR_URL}/operator${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    log_info "启动完成后，摄像头、人员/人脸、告警证据、存储维护和受控运行时重启都从 8090 页面管理。"
    log_info "内部 API 只在 compose 网络内监听 8000；对外不要单独发布 8000。"
    echo ""
    log_info "常用命令："
    log_info "  健康检查: bash $SCRIPT_DIR/midterm_health.sh"
    log_info "  停止服务: bash $SCRIPT_DIR/midterm_stop.sh"
    log_info "  查看日志: docker compose --env-file $ENV_FILE -f $COMPOSE_FILE logs -f"
    echo ""
}

main() {
    parse_args "$@"
    build_compose_args
    apply_local_postgres_default

    echo ""
    log_info "===== Midterm Deployment Startup ====="
    echo ""

    check_prerequisites
    check_required_files
    ensure_data_directories
    check_model_assets
    check_ports
    validate_config

    echo ""
    build_images
    start_services

    echo ""
    wait_for_health || true

    echo ""
    show_status
    print_next_steps
}

main "$@"
