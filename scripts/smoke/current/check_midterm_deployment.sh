#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/infra/docker-compose.midterm.yml}"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/infra/env/midterm.env}"
STORAGE_OVERRIDE="${STORAGE_OVERRIDE:-${ROOT_DIR}/infra/midterm-storage.override.yml}"

# shellcheck source=../../runtime/midterm_compose_context.sh
source "${ROOT_DIR}/scripts/runtime/midterm_compose_context.sh"
midterm_build_compose_args "$ENV_FILE" "$COMPOSE_FILE" "$STORAGE_OVERRIDE"

echo "check=midterm_deployment"
echo "compose=${COMPOSE_FILE}"

docker compose "${MIDTERM_COMPOSE_ARGS[@]}" config >/tmp/video-analytics-midterm-compose.yml
python -m pytest -q "${ROOT_DIR}/harness/tests/test_midterm_deployment_contract.py"

echo "PASS_MIDTERM_DEPLOYMENT_CONTRACT"
