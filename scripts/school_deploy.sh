#!/usr/bin/env bash
# Canonical school deployment entrypoint: secure operator access + baseline capture.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_ROOT="${VIDEO_ANALYTICS_DATA_ROOT:-/data/video-analytics}"
AUTH_FILE="${OPERATOR_AUTH_FILE_HOST:-$DATA_ROOT/media/evidence/.operator-auth}"
OPERATOR_URL="${MIDTERM_OPERATOR_URL:-http://127.0.0.1:8090}"
WAIT_SECONDS="${MIDTERM_DEPLOY_WAIT_SECONDS:-240}"

fail() {
    echo "[ERROR] $*" >&2
    exit 1
}

check_source_state() {
    local line path
    local unexpected=()
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        path="${line:3}"
        case "$path" in
            modules/savant_security/config/cameras.midterm.yml|infra/generated/*)
                ;;
            *)
                unexpected+=("$path")
                ;;
        esac
    done < <(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal)

    if [[ ${#unexpected[@]} -gt 0 ]]; then
        echo "[ERROR] Refusing school deployment with uncommitted source changes:" >&2
        printf '  - %s\n' "${unexpected[@]}" >&2
        echo "Commit or revert source changes before deploying." >&2
        exit 1
    fi
}

wait_for_operator() {
    local elapsed=0
    local interval=5
    while [[ "$elapsed" -lt "$WAIT_SECONDS" ]]; do
        if curl --noproxy '*' -fsS "$OPERATOR_URL/health" >/dev/null 2>&1 && \
           python3 "$SCRIPT_DIR/runtime/check_operator_access.py" \
               "$OPERATOR_URL/api/v1/runtime/overview" "$AUTH_FILE" --timeout 5; then
            echo "[OK] Authenticated 8090 operator portal is ready"
            return 0
        fi
        sleep "$interval"
        elapsed=$((elapsed + interval))
    done
    return 1
}

main() {
    command -v git >/dev/null 2>&1 || fail "git is required"
    command -v python3 >/dev/null 2>&1 || fail "python3 is required"
    command -v curl >/dev/null 2>&1 || fail "curl is required"

    check_source_state

    if ! bash "$SCRIPT_DIR/runtime/set_operator_credentials.sh" --check; then
        echo "Configure the 8090 operator account first:" >&2
        echo "  bash scripts/runtime/set_operator_credentials.sh" >&2
        exit 1
    fi

    echo "[INFO] Starting the school deployment from commit $(git -C "$REPO_ROOT" rev-parse HEAD)"
    bash "$SCRIPT_DIR/midterm_start.sh" --skip-health "$@"

    wait_for_operator || fail "8090 did not pass authenticated readiness checks within ${WAIT_SECONDS}s"

    bash "$SCRIPT_DIR/runtime/capture_deployment_baseline.sh"
    echo "[OK] Deployment baseline: $DATA_ROOT/media/evidence/.deployment-baseline.txt"
    echo "[OK] Authenticated baseline view: $OPERATOR_URL/system/deployment-baseline"
}

main "$@"
