#!/usr/bin/env bash
# Package a clean-machine midterm deployment bundle.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_ROOT="${VIDEO_ANALYTICS_DATA_ROOT:-/data/video-analytics}"
OUT_ROOT="${MIDTERM_MIGRATION_OUT_ROOT:-$DATA_ROOT/artifacts/migrations}"
MIGRATION_ID="${MIGRATION_ID:-midterm-clean-$(date -u +%Y%m%dT%H%M%SZ)}"
PACKAGE_DIR="$OUT_ROOT/$MIGRATION_ID"
PACKAGE_TARBALL="$OUT_ROOT/$MIGRATION_ID.tgz"
DEPLOY_SCRIPT_COPY="$OUT_ROOT/${MIGRATION_ID}_deploy_clean.sh"
INCLUDE_ENGINES=false
INCLUDE_IMAGES=false

usage() {
    cat <<USAGE
Usage: bash scripts/midterm_package_clean.sh [OPTIONS]

Create a clean-machine migration package. The package includes the current
repository snapshot and /data/video-analytics/models. It deliberately excludes
old PostgreSQL data, Redis state, evidence media, Replay RocksDB, artifacts,
downloads, and models-savant-b.

Options:
  --include-engines        Include TensorRT *.engine files from models
  --include-images         Build/pull and include Docker images for offline deploy
  --id VALUE               Migration id, default: $MIGRATION_ID
  --out-root PATH          Output root, default: $OUT_ROOT
  -h, --help               Show this help

Environment:
  VIDEO_ANALYTICS_DATA_ROOT    Data root, default: /data/video-analytics
  MIDTERM_MIGRATION_OUT_ROOT   Output root override
  MIGRATION_ID                 Migration id override
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --include-engines)
            INCLUDE_ENGINES=true
            shift
            ;;
        --include-images)
            INCLUDE_IMAGES=true
            shift
            ;;
        --id)
            [[ $# -ge 2 ]] || {
                echo "--id requires a value" >&2
                exit 1
            }
            MIGRATION_ID="$2"
            PACKAGE_DIR="$OUT_ROOT/$MIGRATION_ID"
            PACKAGE_TARBALL="$OUT_ROOT/$MIGRATION_ID.tgz"
            DEPLOY_SCRIPT_COPY="$OUT_ROOT/${MIGRATION_ID}_deploy_clean.sh"
            shift 2
            ;;
        --out-root)
            [[ $# -ge 2 ]] || {
                echo "--out-root requires a value" >&2
                exit 1
            }
            OUT_ROOT="$2"
            PACKAGE_DIR="$OUT_ROOT/$MIGRATION_ID"
            PACKAGE_TARBALL="$OUT_ROOT/$MIGRATION_ID.tgz"
            DEPLOY_SCRIPT_COPY="$OUT_ROOT/${MIGRATION_ID}_deploy_clean.sh"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
done

require_file() {
    local path="$1"
    [[ -f "$path" ]] || {
        echo "Missing required file: $path" >&2
        exit 1
    }
}

require_dir() {
    local path="$1"
    [[ -d "$path" ]] || {
        echo "Missing required directory: $path" >&2
        exit 1
    }
}

require_file "$REPO_ROOT/infra/docker-compose.midterm.yml"
require_file "$REPO_ROOT/infra/env/midterm.env"
require_file "$REPO_ROOT/scripts/midterm_start.sh"
require_file "$REPO_ROOT/scripts/midterm_deploy_clean.sh"
require_file "$DATA_ROOT/models/yolo26_pose/yolo26_pose.onnx"
require_file "$DATA_ROOT/models/yolov8_face/yolov8n-face.onnx"
require_file "$DATA_ROOT/models/adaface/adaface_ir50_webface4m.onnx"
require_dir "$DATA_ROOT/models"

OFFLINE_IMAGES=(
    "redis:7-alpine"
    "qdrant/qdrant:v1.18.2"
    "pgvector/pgvector:pg16"
    "ghcr.io/insight-platform/savant-replay-x86:v0.6.0"
    "ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1"
    "ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0"
    "video-analytics-midterm-analysis-forwarder:latest"
    "video-analytics-midterm-face-worker:latest"
    "video-analytics-midterm-api:latest"
    "video-analytics-midterm-event-worker:latest"
    "video-analytics-midterm-clip-worker:latest"
    "video-analytics-midterm-media-worker:latest"
    "video-analytics-midterm-evidence-viewer:latest"
    "video-analytics-midterm-rolling-cache-sink:latest"
)

mkdir -p "$PACKAGE_DIR"
rm -f \
    "$PACKAGE_DIR"/repo.tgz \
    "$PACKAGE_DIR"/models.tgz \
    "$PACKAGE_DIR"/images.tar \
    "$PACKAGE_DIR"/image_list.txt \
    "$PACKAGE_DIR"/SHA256SUMS \
    "$PACKAGE_TARBALL"

echo "[package] writing package directory: $PACKAGE_DIR"

(
    cd "$REPO_ROOT"
    git rev-parse HEAD > "$PACKAGE_DIR/git_head.txt" 2>/dev/null || true
    git branch --show-current > "$PACKAGE_DIR/git_branch.txt" 2>/dev/null || true
    git status --short > "$PACKAGE_DIR/git_status_short.txt" 2>/dev/null || true
    git diff --stat > "$PACKAGE_DIR/git_diff_stat.txt" 2>/dev/null || true
)

cat > "$PACKAGE_DIR/README_CLEAN_MIGRATION.txt" <<README
Midterm clean-machine migration package

This package includes:
- repo.tgz: current repository snapshot, including untracked files in the working tree
- models.tgz: /data/video-analytics/models
- images.tar: Docker images for offline deployment, when --include-images was used
- deploy_clean.sh: target-machine restore script

This package intentionally excludes old runtime data:
- PostgreSQL data/dump
- Redis state
- media/evidence
- media/replay-sink-output
- replay-midterm*
- artifacts contents
- downloads contents
- models-savant-b

Target deployment:
  bash deploy_clean.sh $(basename "$PACKAGE_TARBALL")

After startup, use:
  http://127.0.0.1:8090/operator
README

echo "[package] archiving repository snapshot..."
tar -C "$REPO_ROOT" \
    --exclude='.git' \
    --exclude='.pytest_cache' \
    --exclude='.mypy_cache' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='node_modules' \
    --exclude='dist' \
    --exclude='build' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='testVideo' \
    --exclude='yolomodel' \
    -czf "$PACKAGE_DIR/repo.tgz" .

echo "[package] archiving model assets..."
MODEL_TAR_EXCLUDES=()
if [[ "$INCLUDE_ENGINES" != true ]]; then
    MODEL_TAR_EXCLUDES=(--exclude='*.engine')
fi
tar -C "$DATA_ROOT" "${MODEL_TAR_EXCLUDES[@]}" -czf "$PACKAGE_DIR/models.tgz" models

if [[ "$INCLUDE_IMAGES" == true ]]; then
    command -v docker >/dev/null 2>&1 || {
        echo "docker is required for --include-images" >&2
        exit 1
    }
    docker compose version >/dev/null 2>&1 || {
        echo "docker compose plugin is required for --include-images" >&2
        exit 1
    }

    echo "[package] pulling external images for offline deployment..."
    docker pull redis:7-alpine
    docker pull qdrant/qdrant:v1.18.2
    docker pull pgvector/pgvector:pg16
    docker pull ghcr.io/insight-platform/savant-replay-x86:v0.6.0
    docker pull ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1
    docker pull ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0

    echo "[package] building local service images..."
    (
        cd "$REPO_ROOT"
        docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml build face-worker
        docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml build
    )

    printf '%s\n' "${OFFLINE_IMAGES[@]}" > "$PACKAGE_DIR/image_list.txt"
    echo "[package] saving Docker images to images.tar..."
    docker save -o "$PACKAGE_DIR/images.tar" "${OFFLINE_IMAGES[@]}"
else
    : > "$PACKAGE_DIR/image_list.txt"
fi

cp "$REPO_ROOT/scripts/midterm_deploy_clean.sh" "$PACKAGE_DIR/deploy_clean.sh"
chmod +x "$PACKAGE_DIR/deploy_clean.sh"

echo "[package] writing model file checksums..."
if [[ "$INCLUDE_ENGINES" == true ]]; then
    (
        cd "$DATA_ROOT"
        find models -type f -print0 | sort -z | xargs -0 sha256sum
    ) > "$PACKAGE_DIR/model_files.sha256"
else
    (
        cd "$DATA_ROOT"
        find models -type f ! -name '*.engine' -print0 | sort -z | xargs -0 sha256sum
    ) > "$PACKAGE_DIR/model_files.sha256"
fi

cat > "$PACKAGE_DIR/manifest.txt" <<MANIFEST
id=$MIGRATION_ID
created_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
repo_root=$REPO_ROOT
data_root=$DATA_ROOT
include_engines=$INCLUDE_ENGINES
include_images=$INCLUDE_IMAGES
contains_repo_snapshot=true
contains_models=true
contains_docker_images=$INCLUDE_IMAGES
contains_downloads=false
contains_models_savant_b=false
contains_postgres_dump=false
contains_redis_state=false
contains_media_evidence=false
contains_replay_rocksdb=false
contains_artifacts_payload=false
operator_url=http://127.0.0.1:8090/operator
MANIFEST

(
    cd "$PACKAGE_DIR"
    CHECKSUM_FILES=(
        repo.tgz
        models.tgz
        deploy_clean.sh
        manifest.txt
        README_CLEAN_MIGRATION.txt
        model_files.sha256
        image_list.txt
    )
    if [[ "$INCLUDE_IMAGES" == true ]]; then
        CHECKSUM_FILES+=(images.tar)
    fi
    sha256sum "${CHECKSUM_FILES[@]}" > SHA256SUMS
)

echo "[package] creating single tarball..."
tar -C "$PACKAGE_DIR" -czf "$PACKAGE_TARBALL" .
cp "$PACKAGE_DIR/deploy_clean.sh" "$DEPLOY_SCRIPT_COPY"
chmod +x "$DEPLOY_SCRIPT_COPY"

echo
echo "Package created:"
echo "  $PACKAGE_TARBALL"
echo "Deploy script copy:"
echo "  $DEPLOY_SCRIPT_COPY"
echo
echo "Copy both files to the target machine, then run:"
echo "  bash $(basename "$DEPLOY_SCRIPT_COPY") $(basename "$PACKAGE_TARBALL")"
