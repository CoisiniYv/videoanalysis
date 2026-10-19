#!/usr/bin/env bash
# Shared Compose argument builder for the midterm deployment surface.
#
# Every operational caller should source this file and use
# midterm_build_compose_args. Keeping the env file, override order, and
# profiles in one place prevents read-only checks from describing a different
# stack than the startup path. The function exposes both
# MIDTERM_COMPOSE_ARGS and MIDTERM_COMPOSE_PROFILE_ARGS for callers that need
# the complete invocation or only the profile portion.

midterm_build_compose_args() {
    local env_file="$1"
    local compose_file="$2"
    local storage_override="${3:-}"
    local operator_override="${4:-}"
    local profile

    MIDTERM_COMPOSE_ARGS=(--env-file "$env_file" -f "$compose_file")
    MIDTERM_COMPOSE_PROFILE_ARGS=()
    if [[ -n "$storage_override" && -f "$storage_override" ]]; then
        MIDTERM_COMPOSE_ARGS+=(-f "$storage_override")
    fi
    if [[ -n "$operator_override" && -f "$operator_override" ]]; then
        MIDTERM_COMPOSE_ARGS+=(-f "$operator_override")
    fi

    if [[ -n "${MIDTERM_COMPOSE_PROFILES:-}" ]]; then
        local raw_profiles=()
        IFS=',' read -r -a raw_profiles <<< "$MIDTERM_COMPOSE_PROFILES"
        for profile in "${raw_profiles[@]}"; do
            profile="${profile//[[:space:]]/}"
            [[ -n "$profile" ]] || continue
            MIDTERM_COMPOSE_PROFILE_ARGS+=(--profile "$profile")
        done
    fi
    MIDTERM_COMPOSE_ARGS+=("${MIDTERM_COMPOSE_PROFILE_ARGS[@]}")
}

