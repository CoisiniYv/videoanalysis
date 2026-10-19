#!/usr/bin/env bash
# Pre-create the stopped containers that the 8090 topology controller manages.
# This is a deployment-time preparation step; it never starts the dual runtime.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="${MIDTERM_COMPOSE_FILE:-$REPO_ROOT/infra/docker-compose.midterm.yml}"
STORAGE_OVERRIDE="${MIDTERM_STORAGE_OVERRIDE:-$REPO_ROOT/infra/midterm-storage.override.yml}"
OPERATOR_OVERRIDE="${MIDTERM_OPERATOR_OVERRIDE:-$REPO_ROOT/infra/operator-dual-runtime.override.yml}"
ENV_FILE="${MIDTERM_ENV_FILE:-$REPO_ROOT/infra/env/midterm.env}"

# shellcheck source=midterm_compose_context.sh
source "$SCRIPT_DIR/midterm_compose_context.sh"
midterm_build_compose_args "$ENV_FILE" "$COMPOSE_FILE" "$STORAGE_OVERRIDE" "$OPERATOR_OVERRIDE"
MIDTERM_COMPOSE_ARGS+=(--profile operator-dual-runtime)
compose_args=("${MIDTERM_COMPOSE_ARGS[@]}")

services=(
  cuda-mps-operator
  savant-a
  savant-b
  replay-raw-fanout-a
  replay-raw-fanout-b
  analysis-forwarder-a
  analysis-forwarder-b
  replay-a
  replay-b
  video-file-sink-a
  video-file-sink-b
  rolling-cache-sink-a
  rolling-cache-sink-b
  adaface-roi-worker
)

mkdir -p /tmp/video-analytics-mps/pipe /tmp/video-analytics-mps/log

# Branch B keeps a separate writable engine cache to avoid same-GPU TensorRT
# engine races. Seed the immutable ONNX/config inputs before the operator can
# start the dual runtime; preflight will reject an incomplete cache.
bash "$SCRIPT_DIR/prepare_dual_4090_savant_b_model_cache.sh" >/dev/null

cd "$REPO_ROOT"
for service in "${services[@]}"; do
  container="video-analytics-midterm-${service}"
  recreate_flag="--force-recreate"
  if docker inspect "$container" --format '{{.State.Running}}' 2>/dev/null | grep -q '^true$'; then
    # Never interrupt a currently active runtime during a normal startup check.
    recreate_flag="--no-recreate"
  fi
  docker compose "${compose_args[@]}" up \
    --no-start \
    --no-deps \
    --no-build \
    "$recreate_flag" \
    "$service"
done

missing=()
for service in "${services[@]}"; do
  container="video-analytics-midterm-${service}"
  if ! docker inspect "$container" >/dev/null 2>&1; then
    missing+=("$container")
  fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
  printf 'operator runtime pre-create failed; missing containers:\n' >&2
  printf '  - %s\n' "${missing[@]}" >&2
  exit 1
fi

printf '8090 dual-runtime containers are pre-created; the operator portal owns start/stop.\n'
