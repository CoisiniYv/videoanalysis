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
git_source_dirty="false"
git_runtime_config_dirty="false"
while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    path="${line:3}"
    case "$path" in
        modules/savant_security/config/cameras.midterm.yml|infra/generated/*)
            git_runtime_config_dirty="true"
            ;;
        *)
            git_source_dirty="true"
            ;;
    esac
done < <(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal || true)

compose_args=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")
[[ ! -f "$STORAGE_OVERRIDE" ]] || compose_args+=(-f "$STORAGE_OVERRIDE")
active_profiles=()
if [[ -n "${MIDTERM_COMPOSE_PROFILES:-}" ]]; then
    IFS=',' read -r -a raw_profiles <<< "$MIDTERM_COMPOSE_PROFILES"
    for profile in "${raw_profiles[@]}"; do
        profile="${profile//[[:space:]]/}"
        [[ -n "$profile" ]] || continue
        active_profiles+=("$profile")
        compose_args+=(--profile "$profile")
    done
fi
active_profiles_csv="$(IFS=,; echo "${active_profiles[*]-}")"

compose_sha256="$(docker compose "${compose_args[@]}" config | sha256sum | awk '{print $1}')"
compose_images="$(docker compose "${compose_args[@]}" config --images | sort -u | paste -sd ',' -)"

running_image_ids="$(
    docker ps \
        --filter 'label=com.docker.compose.project=video-analytics-midterm' \
        --format '{{.ID}}' 2>/dev/null \
    | while IFS= read -r container_id; do
        [[ -n "$container_id" ]] || continue
        docker inspect --format '{{.Name}}={{.Image}}' "$container_id" 2>/dev/null || true
      done \
    | sed 's#^/##' \
    | sort \
    | paste -sd ',' -
)"

docker_version="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
compose_version="$(docker compose version --short 2>/dev/null || true)"
gpu_summary="$(
    nvidia-smi \
        --query-gpu=index,name,driver_version,memory.total \
        --format=csv,noheader,nounits 2>/dev/null \
    | paste -sd ';' - || true
)"

camera_config="$REPO_ROOT/modules/savant_security/config/cameras.midterm.yml"
camera_config_sha256="missing"
[[ ! -f "$camera_config" ]] || camera_config_sha256="$(sha256sum "$camera_config" | awk '{print $1}')"

generated_config_sha256="$(
    find "$REPO_ROOT/infra/generated" -type f -print0 2>/dev/null \
    | sort -z \
    | xargs -0 -r sha256sum \
    | sha256sum \
    | awk '{print $1}'
)"

migration_set_sha256="$(
    find "$REPO_ROOT/db/migrations" -maxdepth 1 -type f -name '*.sql' -print0 \
    | sort -z \
    | xargs -0 -r sha256sum \
    | sha256sum \
    | awk '{print $1}'
)"

env_file_value() {
    local key="$1"
    awk -F= -v key="$key" '
        $0 !~ /^[[:space:]]*#/ && $1 == key {
            sub(/^[^=]*=/, "")
            gsub(/^"|"$/, "")
            print
            exit
        }
    ' "$ENV_FILE"
}

model_host_path() {
    local path="$1"
    if [[ "$path" == /models/* ]]; then
        printf '%s/models/%s' "$DATA_ROOT" "${path#/models/}"
    elif [[ "$path" == /* ]]; then
        printf '%s' "$path"
    fi
}

model_digest() {
    local path="$1"
    if [[ -n "$path" && -f "$path" ]]; then
        printf '%s' "$(sha256sum "$path" | awk '{print $1}')"
    else
        printf '%s' "missing"
    fi
}

pose_model="${POSE_MODEL_FILE:-$(env_file_value POSE_MODEL_FILE)}"
face_model="${FACE_DETECTOR_MODEL_FILE:-$(env_file_value FACE_DETECTOR_MODEL_FILE)}"
adaface_model="${ADAFACE_ENGINE_PATH:-$(env_file_value ADAFACE_ENGINE_PATH)}"
[[ -n "$adaface_model" ]] || adaface_model="/models/adaface/adaface_ir50_webface4m.onnx_b16_gpu0_fp16.engine"

pose_model_host="$(model_host_path "$pose_model")"
face_model_host="$(model_host_path "$face_model")"
adaface_model_host="$(model_host_path "$adaface_model")"

pose_sha256="$(model_digest "$pose_model_host")"
face_sha256="$(model_digest "$face_model_host")"
adaface_sha256="$(model_digest "$adaface_model_host")"

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
    printf 'git_source_dirty=%s\n' "$git_source_dirty"
    printf 'git_runtime_config_dirty=%s\n' "$git_runtime_config_dirty"
    printf 'compose_profiles=%s\n' "$active_profiles_csv"
    printf 'compose_config_sha256=%s\n' "$compose_sha256"
    printf 'compose_images=%s\n' "$compose_images"
    printf 'running_image_ids=%s\n' "$running_image_ids"
    printf 'docker_server_version=%s\n' "$docker_version"
    printf 'docker_compose_version=%s\n' "$compose_version"
    printf 'gpu_summary=%s\n' "$gpu_summary"
    printf 'camera_config_sha256=%s\n' "$camera_config_sha256"
    printf 'generated_config_sha256=%s\n' "$generated_config_sha256"
    printf 'migration_set_sha256=%s\n' "$migration_set_sha256"
    printf 'model_pose_path=%s\n' "$pose_model_host"
    printf 'model_pose_sha256=%s\n' "$pose_sha256"
    printf 'model_face_path=%s\n' "$face_model_host"
    printf 'model_face_sha256=%s\n' "$face_sha256"
    printf 'model_adaface_path=%s\n' "$adaface_model_host"
    printf 'model_adaface_sha256=%s\n' "$adaface_sha256"
} > "$tmp"
chmod 0644 "$tmp"
mv -f "$tmp" "$OUTPUT"
trap - EXIT

printf 'Deployment baseline captured: %s\n' "$OUTPUT"
printf 'Commit: %s (source_dirty=%s runtime_config_dirty=%s)\n' \
    "$git_commit" "$git_source_dirty" "$git_runtime_config_dirty"
