#!/usr/bin/env bash
# Create or validate the fail-closed 8090 operator credential file.

set -euo pipefail

DATA_ROOT="${VIDEO_ANALYTICS_DATA_ROOT:-/data/video-analytics}"
AUTH_FILE="${OPERATOR_AUTH_FILE_HOST:-$DATA_ROOT/media/evidence/.operator-auth}"
MODE="set"

usage() {
    cat <<'USAGE'
Usage: bash scripts/runtime/set_operator_credentials.sh [--check]

Creates the two-line Basic Auth credential file used by the 8090 operator
portal. The password is never passed on the command line.

Environment overrides:
  VIDEO_ANALYTICS_DATA_ROOT   default: /data/video-analytics
  OPERATOR_AUTH_FILE_HOST    default: <data-root>/media/evidence/.operator-auth
  OPERATOR_AUTH_USERNAME     optional non-interactive username
  OPERATOR_AUTH_PASSWORD     optional non-interactive password
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)
            MODE="check"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

validate_file() {
    [[ -f "$AUTH_FILE" ]] || {
        echo "Operator credential file is missing: $AUTH_FILE" >&2
        return 1
    }
    local line_count username password
    line_count="$(wc -l < "$AUTH_FILE" | tr -d '[:space:]')"
    [[ "$line_count" == "2" ]] || {
        echo "Operator credential file must contain exactly two lines" >&2
        return 1
    }
    username="$(sed -n '1p' "$AUTH_FILE")"
    password="$(sed -n '2p' "$AUTH_FILE")"
    [[ -n "$username" && "$username" != *:* ]] || {
        echo "Operator username must be non-empty and must not contain ':'" >&2
        return 1
    }
    [[ ${#password} -ge 12 ]] || {
        echo "Operator password must be at least 12 characters" >&2
        return 1
    }
}

if [[ "$MODE" == "check" ]]; then
    validate_file
    echo "Operator credentials are configured: $AUTH_FILE"
    exit 0
fi

username="${OPERATOR_AUTH_USERNAME:-}"
password="${OPERATOR_AUTH_PASSWORD:-}"
password_confirm=""

if [[ -z "$username" ]]; then
    [[ -t 0 ]] || {
        echo "Set OPERATOR_AUTH_USERNAME for non-interactive use" >&2
        exit 2
    }
    read -r -p "Operator username: " username
fi

[[ -n "$username" && "$username" != *:* ]] || {
    echo "Operator username must be non-empty and must not contain ':'" >&2
    exit 2
}

if [[ -z "$password" ]]; then
    [[ -t 0 ]] || {
        echo "Set OPERATOR_AUTH_PASSWORD for non-interactive use" >&2
        exit 2
    }
    read -r -s -p "Operator password (minimum 12 characters): " password
    echo
    read -r -s -p "Confirm operator password: " password_confirm
    echo
    [[ "$password" == "$password_confirm" ]] || {
        echo "Passwords do not match" >&2
        exit 2
    }
fi

[[ ${#password} -ge 12 ]] || {
    echo "Operator password must be at least 12 characters" >&2
    exit 2
}

mkdir -p "$(dirname "$AUTH_FILE")"
umask 077
tmp="${AUTH_FILE}.tmp.$$"
trap 'rm -f "$tmp"' EXIT
printf '%s\n%s\n' "$username" "$password" > "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$AUTH_FILE"
trap - EXIT

validate_file
echo "Operator credentials updated: $AUTH_FILE"
