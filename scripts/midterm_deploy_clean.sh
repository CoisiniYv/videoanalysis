#!/usr/bin/env bash
# Restore and start a clean-machine midterm deployment package.

set -euo pipefail

TARGET_ROOT="${MIDTERM_TARGET_ROOT:-/home/user/video-analytics}"
DATA_ROOT="${VIDEO_ANALYTICS_DATA_ROOT:-/data/video-analytics}"
START_STACK=true
LOCAL_POSTGRES=false
SKIP_IMAGE_LOAD=false
PACKAGE_PATH=""

usage() {
    cat <<USAGE
Usage: bash scripts/midterm_deploy_clean.sh PACKAGE.tgz [OPTIONS]

Restore a clean-machine migration package created by
scripts/midterm_package_clean.sh. This deploys code and models, and
automatically loads images.tar when the package was created with
--include-images. It creates empty runtime directories for new evidence,
Replay RocksDB, face uploads, and artifacts.

Options:
  --target PATH          Repository target, default: $TARGET_ROOT
  --data-root PATH       Data root, default: $DATA_ROOT
  --local-postgres       Start with the local-postgres compose profile
  --skip-image-load      Do not load package images.tar even if present
  --no-start             Restore files and validate compose, but do not start
  -h, --help             Show this help

Target operator portal after startup:
  http://127.0.0.1:8090/operator
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            [[ $# -ge 2 ]] || {
                echo "--target requires a value" >&2
                exit 1
            }
            TARGET_ROOT="$2"
            shift 2
            ;;
        --data-root)
            [[ $# -ge 2 ]] || {
                echo "--data-root requires a value" >&2
                exit 1
            }
            DATA_ROOT="$2"
            shift 2
            ;;
        --local-postgres)
            LOCAL_POSTGRES=true
            shift
            ;;
        --skip-image-load)
            SKIP_IMAGE_LOAD=true
            shift
            ;;
        --no-start)
            START_STACK=false
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            if [[ -z "$PACKAGE_PATH" ]]; then
                PACKAGE_PATH="$1"
                shift
            else
                echo "Unknown option or duplicate package path: $1" >&2
                usage
                exit 1
            fi
            ;;
    esac
done

[[ -n "$PACKAGE_PATH" ]] || {
    usage
    exit 1
}

[[ -e "$PACKAGE_PATH" ]] || {
    echo "Package path does not exist: $PACKAGE_PATH" >&2
    exit 1
}

command -v tar >/dev/null 2>&1 || {
    echo "tar is required" >&2
    exit 1
}

WORKDIR="$(mktemp -d)"
cleanup() {
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

if [[ -d "$PACKAGE_PATH" ]]; then
    PACKAGE_DIR="$PACKAGE_PATH"
else
    echo "[deploy] extracting package to temporary directory..."
    tar -C "$WORKDIR" -xzf "$PACKAGE_PATH"
    PACKAGE_DIR="$WORKDIR"
fi

REPO_TGZ="$(find "$PACKAGE_DIR" -maxdepth 2 -name repo.tgz -type f | head -n 1)"
MODELS_TGZ="$(find "$PACKAGE_DIR" -maxdepth 2 -name models.tgz -type f | head -n 1)"
IMAGES_TAR="$(find "$PACKAGE_DIR" -maxdepth 2 -name images.tar -type f | head -n 1 || true)"
SHA_FILE="$(find "$PACKAGE_DIR" -maxdepth 2 -name SHA256SUMS -type f | head -n 1 || true)"

[[ -n "$REPO_TGZ" ]] || {
    echo "repo.tgz not found in package" >&2
    exit 1
}
[[ -n "$MODELS_TGZ" ]] || {
    echo "models.tgz not found in package" >&2
    exit 1
}

if [[ -n "$SHA_FILE" && -f "$SHA_FILE" ]]; then
    echo "[deploy] verifying package checksums..."
    (
        cd "$(dirname "$SHA_FILE")"
        sha256sum -c SHA256SUMS >/dev/null
    )
fi

if [[ -n "$IMAGES_TAR" && "$SKIP_IMAGE_LOAD" != true ]]; then
    command -v docker >/dev/null 2>&1 || {
        echo "docker is required to load images.tar" >&2
        exit 1
    }
    echo "[deploy] loading Docker images from package..."
    docker load -i "$IMAGES_TAR"
fi

if [[ -e "$TARGET_ROOT" && -n "$(find "$TARGET_ROOT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    echo "Target repository directory is not empty: $TARGET_ROOT" >&2
    echo "Use a clean target path or move the existing directory first." >&2
    exit 1
fi

echo "[deploy] restoring repository to $TARGET_ROOT..."
mkdir -p "$TARGET_ROOT"
tar -C "$TARGET_ROOT" -xzf "$REPO_TGZ"

echo "[deploy] preparing data root $DATA_ROOT..."
mkdir -p \
    "$DATA_ROOT/downloads" \
    "$DATA_ROOT/artifacts" \
    "$DATA_ROOT/qdrant-midterm" \
    "$DATA_ROOT/media/evidence" \
    "$DATA_ROOT/media/replay-sink-output/midterm" \
    "$DATA_ROOT/media/midterm-snapshots" \
    "$DATA_ROOT/media/face_uploads" \
    "$DATA_ROOT/media/face_registration" \
    "$DATA_ROOT/media/face-registration" \
    "$DATA_ROOT/media/debug" \
    "$DATA_ROOT/media/.trash" \
    "$DATA_ROOT/replay-midterm" \
    "$DATA_ROOT/replay-midterm-a" \
    "$DATA_ROOT/replay-midterm-b"

if [[ "$LOCAL_POSTGRES" == true ]]; then
    mkdir -p "$DATA_ROOT/postgres-midterm"
fi

echo "[deploy] restoring models to $DATA_ROOT/models..."
tar -C "$DATA_ROOT" -xzf "$MODELS_TGZ"

required_models=(
    "$DATA_ROOT/models/yolo26_pose/yolo26_pose.onnx"
    "$DATA_ROOT/models/yolov8_face/yolov8n-face.onnx"
    "$DATA_ROOT/models/adaface/adaface_ir50_webface4m.onnx"
)

missing=()
for model in "${required_models[@]}"; do
    [[ -f "$model" ]] || missing+=("$model")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Missing required model files after restore:" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    exit 1
fi

if [[ -e "$DATA_ROOT/models/yolov8_face/yolov8n-face.onnx" && ! -e "$DATA_ROOT/models/yolov8_face.onnx" ]]; then
    ln -sfn yolov8_face/yolov8n-face.onnx "$DATA_ROOT/models/yolov8_face.onnx"
fi

echo "[deploy] validating compose config..."
(
    cd "$TARGET_ROOT"
    docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml config >/dev/null
)

if [[ "$START_STACK" != true ]]; then
    echo "[deploy] restore complete; stack start skipped by --no-start"
    echo "Next command:"
    NEXT_START_ARGS=()
    if [[ "$LOCAL_POSTGRES" == true ]]; then
        NEXT_START_ARGS+=(--local-postgres)
    fi
    if [[ -n "$IMAGES_TAR" && "$SKIP_IMAGE_LOAD" != true ]]; then
        NEXT_START_ARGS+=(--no-build)
    fi
    echo "  cd $TARGET_ROOT && bash scripts/midterm_start.sh ${NEXT_START_ARGS[*]}"
    exit 0
fi

echo "[deploy] starting midterm stack..."
START_ARGS=()
if [[ "$LOCAL_POSTGRES" == true ]]; then
    START_ARGS+=(--local-postgres)
fi
if [[ -n "$IMAGES_TAR" && "$SKIP_IMAGE_LOAD" != true ]]; then
    START_ARGS+=(--no-build)
fi

(
    cd "$TARGET_ROOT"
    VIDEO_ANALYTICS_DATA_ROOT="$DATA_ROOT" bash scripts/midterm_start.sh "${START_ARGS[@]}"
)

echo
echo "Clean deployment complete."
echo "Open: http://127.0.0.1:8090/operator"
