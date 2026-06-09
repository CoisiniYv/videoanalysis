#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/infra/docker-compose.midterm.yml}"

echo "check=midterm_deployment"
echo "compose=${COMPOSE_FILE}"

docker compose -f "${COMPOSE_FILE}" config >/tmp/video-analytics-midterm-compose.yml
python -m pytest -q "${ROOT_DIR}/harness/tests/test_midterm_deployment_contract.py"

echo "PASS_MIDTERM_DEPLOYMENT_CONTRACT"
