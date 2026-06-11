#!/bin/sh
# Savant runtime watchdog for the midterm deployment.
#
# Savant can keep the container process alive while the module status is
# STOPPING/STOPPED. Docker restart policies do not restart unhealthy containers,
# so this watchdog performs a bounded controlled recovery when the module dies
# or when frame annotations stop advancing while source adapters are running.

set -u

SAVANT="${WATCHDOG_SAVANT_CONTAINER:-video-analytics-midterm-savant}"
REDIS="${WATCHDOG_REDIS_CONTAINER:-video-analytics-midterm-redis}"
REPLAY="${WATCHDOG_REPLAY_CONTAINER:-video-analytics-midterm-replay-service}"
STATUS_FILE="${WATCHDOG_SAVANT_STATUS_FILE:-/opt/savant/status.txt}"
STREAM="${WATCHDOG_ANNOTATION_STREAM:-security.frame_annotations}"

# Extended regular expression matched against docker container names. The
# default deliberately covers both the compose-managed primary adapter and
# camera-runtime dynamic adapters.
SOURCE_PATTERNS="${WATCHDOG_SOURCE_CONTAINER_PATTERNS:-^video-analytics-midterm-source-adapter$|^video-analytics-source-}"

POLL_S="${WATCHDOG_POLL_INTERVAL_S:-10}"
STOPPED_GRACE_S="${WATCHDOG_STOPPED_GRACE_S:-20}"
STARTUP_GRACE_S="${WATCHDOG_STARTUP_GRACE_S:-1800}"
STALL_S="${WATCHDOG_STALL_SECONDS:-120}"
STALL_ENABLED="${WATCHDOG_STALL_CHECK_ENABLED:-true}"
COOLDOWN_S="${WATCHDOG_RESTART_COOLDOWN_S:-300}"
RESTART_WAIT_S="${WATCHDOG_RESTART_WAIT_S:-1800}"
RESTART_REPLAY="${WATCHDOG_RESTART_REPLAY:-false}"

log() {
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') watchdog: $*"
}

is_true() {
    case "$(echo "${1:-}" | tr 'A-Z' 'a-z')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

container_running() {
    [ "$(docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null)" = "running" ]
}

savant_status() {
    # initializing|starting|running|stopping|stopped|unknown
    _st="$(docker exec "$SAVANT" cat "$STATUS_FILE" 2>/dev/null | tr -d '[:space:]')"
    case "$_st" in
        initializing|starting|running|stopping|stopped) echo "$_st" ;;
        *) echo "unknown" ;;
    esac
}

savant_boot_marker() {
    docker inspect -f '{{.Id}}:{{.State.StartedAt}}' "$SAVANT" 2>/dev/null
}

source_adapter_names() {
    docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E "$SOURCE_PATTERNS" || true
}

running_source_adapter_names() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -E "$SOURCE_PATTERNS" || true
}

any_source_adapter_running() {
    running_source_adapter_names | grep -q .
}

annotation_age_s() {
    # Prints the age in seconds of the last generated entry of $STREAM, or
    # nothing if the stream is missing or Redis is unreachable.
    _last_ms="$(docker exec "$REDIS" redis-cli XINFO STREAM "$STREAM" 2>/dev/null \
        | grep -A1 '^last-generated-id$' | tail -n1 | cut -d- -f1)"
    case "$_last_ms" in
        ''|*[!0-9]*) return 0 ;;
    esac
    _now_s="$(docker exec "$REDIS" redis-cli TIME 2>/dev/null | head -n1)"
    case "$_now_s" in
        ''|*[!0-9]*) return 0 ;;
    esac
    echo $(( _now_s - _last_ms / 1000 ))
}

restart_chain() {
    _reason="$1"
    log "TRIGGER reason=${_reason} -> controlled restart of ${SAVANT}"

    docker restart -t 30 "$SAVANT" >/dev/null 2>&1 \
        || log "WARN docker restart ${SAVANT} failed"

    _deadline=$(( $(date +%s) + RESTART_WAIT_S ))
    while [ "$(date +%s)" -lt "$_deadline" ]; do
        if [ "$(savant_status)" = "running" ]; then
            log "savant module is running again"
            break
        fi
        sleep 5
    done
    if [ "$(savant_status)" != "running" ]; then
        log "WARN savant did not reach running within ${RESTART_WAIT_S}s; restarting adapters anyway"
    fi

    if is_true "$RESTART_REPLAY"; then
        log "restarting replay container ${REPLAY}"
        docker restart -t 30 "$REPLAY" >/dev/null 2>&1 \
            || log "WARN docker restart ${REPLAY} failed"
    fi

    for _name in $(source_adapter_names); do
        log "restarting source adapter ${_name}"
        docker restart -t 15 "$_name" >/dev/null 2>&1 \
            || log "WARN docker restart ${_name} failed"
    done

    LAST_RESTART_AT="$(date +%s)"
    STOPPED_SINCE=""
    STARTING_SINCE=""
    BOOT_MARKER="$(savant_boot_marker)"
}

log "starting; savant=${SAVANT} redis=${REDIS} source_patterns=${SOURCE_PATTERNS}"
log "poll=${POLL_S}s stopped_grace=${STOPPED_GRACE_S}s startup_grace=${STARTUP_GRACE_S}s stall=${STALL_S}s cooldown=${COOLDOWN_S}s"

LAST_RESTART_AT=0
STOPPED_SINCE=""
STARTING_SINCE=""
BOOT_MARKER="$(savant_boot_marker)"

while :; do
    sleep "$POLL_S"
    NOW="$(date +%s)"

    if ! container_running "$SAVANT"; then
        # Container exited or is restarting; Docker's restart policy owns this
        # case. Reset our timers and wait.
        STOPPED_SINCE=""
        STARTING_SINCE=""
        continue
    fi

    _marker="$(savant_boot_marker)"
    if [ "$_marker" != "$BOOT_MARKER" ]; then
        BOOT_MARKER="$_marker"
        STOPPED_SINCE=""
        STARTING_SINCE=""
    fi

    IN_COOLDOWN=0
    [ $(( NOW - LAST_RESTART_AT )) -lt "$COOLDOWN_S" ] && IN_COOLDOWN=1

    STATUS="$(savant_status)"
    case "$STATUS" in
        running)
            STOPPED_SINCE=""
            STARTING_SINCE=""
            if is_true "$STALL_ENABLED" && [ "$IN_COOLDOWN" -eq 0 ] \
                && any_source_adapter_running; then
                AGE="$(annotation_age_s)"
                if [ -n "${AGE:-}" ] && [ "$AGE" -gt "$STALL_S" ]; then
                    restart_chain "annotation_stall(age=${AGE}s)"
                fi
            fi
            ;;
        stopping|stopped)
            STARTING_SINCE=""
            if [ -z "$STOPPED_SINCE" ]; then
                STOPPED_SINCE="$NOW"
                log "module status=${STATUS}; grace ${STOPPED_GRACE_S}s before restart"
            elif [ "$IN_COOLDOWN" -eq 0 ] \
                && [ $(( NOW - STOPPED_SINCE )) -ge "$STOPPED_GRACE_S" ]; then
                restart_chain "module_${STATUS}"
            fi
            ;;
        *)
            # initializing | starting | unknown
            STOPPED_SINCE=""
            if [ -z "$STARTING_SINCE" ]; then
                STARTING_SINCE="$NOW"
            elif [ "$IN_COOLDOWN" -eq 0 ] \
                && [ $(( NOW - STARTING_SINCE )) -ge "$STARTUP_GRACE_S" ]; then
                restart_chain "startup_timeout(status=${STATUS})"
            fi
            ;;
    esac
done
