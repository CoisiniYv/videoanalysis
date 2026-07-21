#!/bin/sh
set -eu

CACHE_ROOT="${ROLLING_CACHE_ROOT:-/media/rolling-cache}"
NAMESPACE="${ROLLING_CACHE_NAMESPACE:-midterm}"
export ROLLING_CACHE_ROOT="${CACHE_ROOT}"
export ROLLING_CACHE_NAMESPACE="${NAMESPACE}"
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1

ROLLING_CACHE_RETENTION_SECONDS="${ROLLING_CACHE_RETENTION_SECONDS:-300}"
ROLLING_CACHE_CLEANUP_INTERVAL_SECONDS="${ROLLING_CACHE_CLEANUP_INTERVAL_SECONDS:-30}"
ROLLING_CACHE_MAX_BYTES="${ROLLING_CACHE_MAX_BYTES:-0}"
ROLLING_CACHE_READ_PIN_TTL_SECONDS="${ROLLING_CACHE_READ_PIN_TTL_SECONDS:-600}"
ROLLING_CACHE_MAINTENANCE_OWNER="${ROLLING_CACHE_MAINTENANCE_OWNER:-true}"

case "$(printf '%s' "${ROLLING_CACHE_MAINTENANCE_OWNER}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    echo "[rolling-cache-maintenance] requested_owner=true root=${CACHE_ROOT} retention_s=${ROLLING_CACHE_RETENTION_SECONDS} max_bytes=${ROLLING_CACHE_MAX_BYTES} interval_s=${ROLLING_CACHE_CLEANUP_INTERVAL_SECONDS} read_pin_ttl_s=${ROLLING_CACHE_READ_PIN_TTL_SECONDS}"
    python /opt/rolling-cache-maintenance.py &
    ;;
  *)
    echo "[rolling-cache-maintenance] requested_owner=false root=${CACHE_ROOT}"
    ;;
esac

echo "[rolling-cache-sink] implementation=long-lived-splitmux root=${CACHE_ROOT} namespace=${NAMESPACE} segment_s=${ROLLING_CACHE_SEGMENT_SECONDS:-4} http_port=${ROLLING_CACHE_SINK_HTTP_PORT:-8080}"

exec python /opt/rolling-cache-sink/app/main.py
