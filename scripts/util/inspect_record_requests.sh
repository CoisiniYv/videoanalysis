#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# inspect_record_requests.sh — inspect and manage security.record_requests
# ---------------------------------------------------------------------------
# Usage:
#   bash scripts/util/inspect_record_requests.sh [phase3a|phase3b] [pending|failed|clear-pending|clear-failed|stats]
#
# Examples:
#   bash scripts/util/inspect_record_requests.sh phase3a stats
#   bash scripts/util/inspect_record_requests.sh phase3b pending
#   bash scripts/util/inspect_record_requests.sh phase3b failed
#   bash scripts/util/inspect_record_requests.sh phase3a clear-pending
# ---------------------------------------------------------------------------
set -euo pipefail

PHASE="${1:-phase3a}"
COMMAND="${2:-stats}"

case "$PHASE" in
    phase3a) REDIS_CONTAINER="phase3a-redis" ; PG_CONTAINER="phase3a-postgres" ;;
    phase3b) REDIS_CONTAINER="phase3b-redis" ; PG_CONTAINER="phase3b-postgres" ;;
    *) echo "Unknown phase: $PHASE (use phase3a or phase3b)"; exit 1 ;;
esac

_redis() { docker exec "$REDIS_CONTAINER" redis-cli "$@" 2>/dev/null; }
_pg()    { docker exec "$PG_CONTAINER" psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null; }
_pg_json() { docker exec "$PG_CONTAINER" psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null; }

STREAM="security.record_requests"

echo "=== Record Request Inspector (${PHASE}) ==="
echo ""

case "$COMMAND" in
    stats)
        LEN="$(_redis XLEN "$STREAM" 2>/dev/null || echo "0")"
        echo "Stream: $STREAM"
        echo "  Length: $LEN"
        echo ""
        echo "Events by clip_status:"
        _pg "SELECT COALESCE(payload->'media'->>'clip_status', 'not_implemented') AS status, COUNT(*) FROM events GROUP BY status ORDER BY status;" 2>/dev/null || echo "  (no DB or no rows)"
        echo ""
        echo "Failed media jobs (last 10):"
        _pg_json "SELECT id, source_event_id, payload->'media'->>'error_message' AS error FROM events WHERE payload->'media'->>'clip_status' = 'failed' ORDER BY updated_at DESC LIMIT 10;" 2>/dev/null || echo "  (none)"
        ;;
    pending)
        echo "Pending record_requests (last 20):"
        _redis XRANGE "$STREAM" - + COUNT 20 2>/dev/null | while IFS= read -r line; do
            echo "  $line"
        done
        ;;
    failed)
        echo "Failed media events:"
        _pg_json "SELECT id, source_event_id, payload->'media'->>'clip_status' AS clip_status, payload->'media'->>'error_message' AS error, updated_at FROM events WHERE payload->'media'->>'clip_status' = 'failed' ORDER BY updated_at DESC;" 2>/dev/null || echo "  (none)"
        ;;
    clear-pending)
        LEN="$(_redis XLEN "$STREAM" 2>/dev/null || echo "0")"
        if [[ "$LEN" -eq 0 ]]; then
            echo "No pending messages to clear."
            exit 0
        fi
        echo "WARNING: About to delete ${LEN} pending record_requests from ${STREAM}."
        echo "This is safe for Phase 3A test leftovers but will also remove any real pending requests."
        read -rp "Type 'yes' to confirm: " CONFIRM
        if [[ "$CONFIRM" == "yes" ]]; then
            _redis DEL "$STREAM"
            echo "Cleared $STREAM (recreated as empty stream)."
        else
            echo "Aborted."
        fi
        ;;
    clear-failed)
        echo "Clearing failed status for events that have no active record_requests..."
        _pg "UPDATE events SET payload = jsonb_set(COALESCE(payload, '{}'::jsonb), '{media,clip_status}', '\"not_implemented\"') WHERE payload->'media'->>'clip_status' = 'failed' AND payload->'media'->>'replay_job_id' IS NULL;" 2>/dev/null && echo "  Done." || echo "  No rows updated."
        ;;
    *)
        echo "Unknown command: $COMMAND"
        echo "Usage: $0 [phase3a|phase3b] [stats|pending|failed|clear-pending|clear-failed]"
        exit 1
        ;;
esac
