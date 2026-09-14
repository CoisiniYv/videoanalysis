#!/usr/bin/env bash
# Capture a compact, secret-free deployment identity for the school runtime.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DATA_ROOT="${VIDEO_ANALYTICS_DATA_ROOT:-/data/video-analytics}"
OUTPUT="${DEPLOYMENT_BASELINE_FILE_HOST:-$DATA_ROOT/media/evidence/.deployment-baseline.txt}"
ENV_FILE="$REPO_ROOT/infra/env/midterm.env"
COMPOSE_FILE="$REPO_ROOT/infra/docker-compose.midterm.yml"
STORAGE_OVERRIDE="$REPO_ROOT/infra/midterm-storage.override.yml"

mkdir -p "$(dirname "$OUTPUT")"

git_commit="$(git -C "$REPO_ROOT" rev-parse HEAD)"
git_branch="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
git_status="$(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal || true)"
git_dirty="false"
[[ -z "$git_status" ]] || git_dirty="true"

compose_args=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")
[[ ! -f "$STORAGE_OVERRIDE" ]] || compose_args+=(-f "$STORAGE_OVERRIDE")
compose_sha256="$(docker compose "${compose_args[@]}" config | sha256sum | awk '{print $1}')"
compose_images="$(docker compose "${compose_args[@]}" config --images | sort -u | paste -sd ',' -)"

docker_version="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
compose_version="$(docker compose version --short 2>/dev/null || true)"

camera_config="$REPO_ROOT/modules/savant_security/config/cameras.midterm.yml"
camera_config_sha256="missing"
[[ ! -f "$camera_config" ]] || camera_config_sha256="$(sha256sum "$camera_config" | awk '{print $1}')"

migration_set_sha256="$(find "$REPO_ROOT/db/migrations" -maxdepth 1 -type f -name '*.sql' -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')"

model_digest() {
    local path="$1"
    if [[ -f "$path" ]]; then
        printf '%s' "$(sha256sum "$path" | awk '{print $1}')"
    else
        printf '%s' "missing"
    fi
}

pose_sha256="$(model_digest "$DATA_ROOT/models/yolo26_pose/yolo26_pose.onnx")"
face_sha256="$(model_digest "$DATA_ROOT/models/yolov8_face/yolov8n-face.onnx")"
adaface_sha256="$(model_digest "$DATA_ROOT/models/adaface/adaface_ir50_webface4m.onnx")"

captured_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
hostname_value="$(hostname)"

tmp="${OUTPUT}.tmp.$$"
trap 'rm -f "$tmp"' EXIT
{
    printf 'schema_version=1\n'
    printf 'captured_at=%s\n' "$captured_at"
    printf 'hostname=%s\n' "$hostname_value"
    printf 'git_commit=%s\n' "$git_commit"
    printf 'git_branch=%s\n' "$git_branch"
    printf 'git_dirty=%s\n' "$git_dirty"
    printf 'compose_config_sha256=%s\n' "$compose_sha256"
    printf 'compose_images=%s\n' "$compose_images"
    printf 'docker_server_version=%s\n' "$docker_version"
    printf 'docker_compose_version=%s\n' "$compose_version"
    printf 'camera_config_sha256=%s\n' "$camera_config_sha256"
    printf 'migration_set_sha256=%s\n' "$migration_set_sha256"
    printf 'model_yolo26_pose_sha256=%s\n' "$pose_sha256"
    printf 'model_yolov8_face_sha256=%s\n' "$face_sha256"
    printf 'model_adaface_sha256=%s\n' "$adaface_sha256"
} > "$tmp"
chmod 0644 "$tmp"
mv -f "$tmp" "$OUTPUT"
trap - EXIT

printf 'Deployment baseline captured: %s\n' "$OUTPUT"
printf 'Commit: %s (dirty=%s)\n' "$git_commit" "$git_dirty"
