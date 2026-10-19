#!/usr/bin/env bash
# Capture a machine-readable, secret-free deployment identity.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DATA_ROOT="${VIDEO_ANALYTICS_DATA_ROOT:-/data/video-analytics}"
ENV_FILE="${MIDTERM_ENV_FILE:-$REPO_ROOT/infra/env/midterm.env}"
COMPOSE_FILE="${MIDTERM_COMPOSE_FILE:-$REPO_ROOT/infra/docker-compose.midterm.yml}"
STORAGE_OVERRIDE="${MIDTERM_STORAGE_OVERRIDE:-$REPO_ROOT/infra/midterm-storage.override.yml}"
OPERATOR_OVERRIDE="${MIDTERM_OPERATOR_OVERRIDE:-}"
OUTPUT="${DEPLOYMENT_MANIFEST_FILE_HOST:-$DATA_ROOT/media/evidence/.deployment-manifest.json}"

# shellcheck source=midterm_compose_context.sh
source "$SCRIPT_DIR/midterm_compose_context.sh"
midterm_build_compose_args "$ENV_FILE" "$COMPOSE_FILE" "$STORAGE_OVERRIDE" "$OPERATOR_OVERRIDE"

mkdir -p "$(dirname "$OUTPUT")"

git_commit="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
git_branch="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
git_dirty="false"
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal 2>/dev/null || true)" ]]; then
    git_dirty="true"
fi

compose_config="$(docker compose "${MIDTERM_COMPOSE_ARGS[@]}" config)"
compose_config_sha256="$(printf '%s' "$compose_config" | sha256sum | awk '{print $1}')"
compose_images="$(docker compose "${MIDTERM_COMPOSE_ARGS[@]}" config --images | sort -u | paste -sd ',' -)"

image_records="$(
    docker compose "${MIDTERM_COMPOSE_ARGS[@]}" config --images \
        | sort -u \
        | while IFS= read -r image; do
            [[ -n "$image" ]] || continue
            image_id="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)"
            repo_digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$image" 2>/dev/null || true)"
            printf '%s\t%s\t%s\n' "$image" "$image_id" "$repo_digest"
          done
)"

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

migration_set_sha256="$(
    find "$REPO_ROOT/db/migrations" -maxdepth 1 -type f -name '*.sql' -print0 2>/dev/null \
        | sort -z \
        | xargs -0 -r sha256sum \
        | sha256sum \
        | awk '{print $1}'
)"

model_set_sha256="$(
    find "$DATA_ROOT/models" -type f -print0 2>/dev/null \
        | sort -z \
        | xargs -0 -r sha256sum \
        | sha256sum \
        | awk '{print $1}'
)"

docker_server_version="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
compose_version="$(docker compose version --short 2>/dev/null || true)"
gpu_summary="$(
    nvidia-smi --query-gpu=index,name,driver_version,memory.total \
        --format=csv,noheader,nounits 2>/dev/null | paste -sd ';' - || true
)"
captured_at="$(TZ=Asia/Seoul date -Iseconds)"
profiles="${MIDTERM_COMPOSE_PROFILES:-}"

OUTPUT="$OUTPUT" \
GIT_COMMIT="$git_commit" \
GIT_BRANCH="$git_branch" \
GIT_DIRTY="$git_dirty" \
COMPOSE_CONFIG_SHA256="$compose_config_sha256" \
COMPOSE_IMAGES="$compose_images" \
IMAGE_RECORDS="$image_records" \
RUNNING_IMAGE_IDS="$running_image_ids" \
MIGRATION_SET_SHA256="$migration_set_sha256" \
MODEL_SET_SHA256="$model_set_sha256" \
DOCKER_SERVER_VERSION="$docker_server_version" \
COMPOSE_VERSION="$compose_version" \
GPU_SUMMARY="$gpu_summary" \
CAPTURED_AT="$captured_at" \
COMPOSE_PROFILES="$profiles" \
VIDEO_ANALYTICS_DATA_ROOT="$DATA_ROOT" \
python3 - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path


def split_records(raw: str) -> list[dict[str, str]]:
    rows = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if not parts or not parts[0]:
            continue
        rows.append({
            "image": parts[0],
            "image_id": parts[1] if len(parts) > 1 else "",
            "repo_digest": parts[2] if len(parts) > 2 else "",
        })
    return rows


output = Path(os.environ["OUTPUT"])
manifest = {
    "schema_version": "video-analytics.deployment.v1",
    "captured_at": os.environ.get("CAPTURED_AT", ""),
    "release_id": os.environ.get("GIT_COMMIT", "")[:12],
    "git": {
        "commit": os.environ.get("GIT_COMMIT", ""),
        "branch": os.environ.get("GIT_BRANCH", ""),
        "dirty": os.environ.get("GIT_DIRTY") == "true",
    },
    "compose": {
        "config_sha256": os.environ.get("COMPOSE_CONFIG_SHA256", ""),
        "profiles": [item for item in os.environ.get("COMPOSE_PROFILES", "").split(",") if item],
        "images": os.environ.get("COMPOSE_IMAGES", "").split(",") if os.environ.get("COMPOSE_IMAGES") else [],
        "image_records": split_records(os.environ.get("IMAGE_RECORDS", "")),
    },
    "runtime": {
        "running_image_ids": os.environ.get("RUNNING_IMAGE_IDS", ""),
        "data_root": os.environ.get("VIDEO_ANALYTICS_DATA_ROOT", ""),
        "docker_server_version": os.environ.get("DOCKER_SERVER_VERSION", ""),
        "compose_version": os.environ.get("COMPOSE_VERSION", ""),
        "gpu_summary": os.environ.get("GPU_SUMMARY", ""),
    },
    "artifacts": {
        "migration_set_sha256": os.environ.get("MIGRATION_SET_SHA256", ""),
        "model_set_sha256": os.environ.get("MODEL_SET_SHA256", ""),
    },
    "update": {
        "channel": os.environ.get("DEPLOYMENT_UPDATE_CHANNEL", ""),
        "manifest_path": os.environ.get("DEPLOYMENT_UPDATE_MANIFEST_PATH", ""),
    },
}

temporary = output.with_name(f"{output.name}.tmp.{os.getpid()}")
temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
temporary.replace(output)
print(f"Deployment manifest captured: {output}")
PY

